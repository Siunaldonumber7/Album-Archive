from flask import Flask, render_template, request, redirect, url_for, session, abort
import os
import secrets
import hmac
from datetime import timedelta

from album_database import DB_NAME, DATA_DIR, get_db, init_db
from pagination import (
    ALBUMS_PER_PAGE,
    parse_page_number,
    build_pagination_pages,
)
from catalog import (
    split_genres,
    artist_credit_parts,
    split_artists,
)

from cache_utils import BoundedCache
from track_service import (
    has_manual_track_override,
    get_manual_album_tracks,
    get_album_tracks,
)
from media_service import configure_media, cover_thumbnail_url
from admin_routes import register_admin_routes
from album_routes import register_album_routes
from track_routes import register_track_routes
from search_engine import get_artist_profile_image
from library_view import (
    INDEX_SORT_SQL,
    resolve_index_state,
    build_index_where,
    sort_index_rows,
    build_artist_directory,
    collect_artist_albums,
    build_format_summary,
    build_album_type_summary,
    sort_artist_albums,
    resolve_artist_sort,
)

app = Flask(__name__)


def load_or_create_private_value(env_name, filename, generator):
    env_value = str(os.environ.get(env_name, '') or '').strip()
    if env_value:
        return env_value

    path = os.path.join(DATA_DIR, filename)

    try:
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as handle:
                value = handle.read().strip()
                if value:
                    return value

        value = generator()
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write(value + '\n')
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
        return value
    except Exception as exc:
        print('PRIVATE VALUE FILE ERROR:', filename, exc)
        return generator()


APP_SECRET = load_or_create_private_value(
    'APP_SECRET_KEY',
    'flask_secret.key',
    lambda: secrets.token_urlsafe(48)
)
ADMIN_PIN = load_or_create_private_value(
    'ADMIN_PIN',
    'admin_pin.txt',
    lambda: f'{secrets.randbelow(100_000_000):08d}'
)

app.secret_key = APP_SECRET
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=True,
    PERMANENT_SESSION_LIFETIME=timedelta(days=30)
)

LOGIN_ATTEMPTS = BoundedCache(500)

configure_media(app)

ADMIN_ENDPOINTS = {
    'admin_dashboard',
    'manage_albums',
    'set_main_album',
    'add_album_page',
    'add_album_from_link',
    'add_album_manual',
    'register',
    'register_bulk',
    'save_album',
    'save_bulk_albums',
    'edit_album',
    'update_album',
    'delete_album',
    'edit_album_tracks',
    'save_album_tracks',
    'reset_album_tracks',
    'admin_logout',
}


def get_csrf_token():
    token = session.get('_csrf_token')
    if not token:
        token = secrets.token_urlsafe(32)
        session['_csrf_token'] = token
    return token


app.jinja_env.globals['csrf_token'] = get_csrf_token


@app.before_request
def protect_admin_routes():
    endpoint = request.endpoint or ''

    if endpoint == 'admin_login':
        if request.method == 'POST':
            expected = session.get('_csrf_token', '')
            received = request.form.get('_csrf_token', '')
            if not expected or not received or not hmac.compare_digest(expected, received):
                abort(400)
        return None

    if endpoint not in ADMIN_ENDPOINTS:
        return None

    if not session.get('admin_authenticated'):
        return redirect(url_for('admin_login'))

    if request.method == 'POST':
        expected = session.get('_csrf_token', '')
        received = request.form.get('_csrf_token', '')
        if not expected or not received or not hmac.compare_digest(expected, received):
            abort(400)

    return None


@app.after_request
def add_security_headers(response):
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
    response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    response.headers.setdefault(
        'Content-Security-Policy',
        "default-src 'self'; img-src 'self' https: data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'self'; frame-ancestors 'self'"
    )

    forwarded_proto = request.headers.get('X-Forwarded-Proto', '').lower()
    if request.is_secure or forwarded_proto == 'https':
        response.headers.setdefault(
            'Strict-Transport-Security',
            'max-age=31536000; includeSubDomains'
        )

    return response

app.jinja_env.globals['split_genres'] = split_genres
app.jinja_env.globals['artist_credit_parts'] = artist_credit_parts
app.jinja_env.globals['split_artists'] = split_artists
app.jinja_env.globals['cover_thumbnail_url'] = cover_thumbnail_url

register_admin_routes(
    app,
    ADMIN_PIN,
    LOGIN_ATTEMPTS
)
register_album_routes(app)
register_track_routes(app)


@app.route('/')
def index():
    state = resolve_index_state(
        request.args,
        session
    )

    library_q = state['library_q']
    classification_filter = state['classification']
    format_filter = state['media_format']
    album_type_filter = state['album_type']
    genre_filter = state['genre']
    sort_order = state['sort']

    page = parse_page_number(
        request.args.get('page', '1')
    )
    saved = request.args.get('saved', '')
    bulk_saved = request.args.get(
        'bulk_saved',
        ''
    )

    where_sql, params = build_index_where(
        state
    )
    conn = get_db()

    try:
        filtered_album_count = conn.execute(
            'SELECT COUNT(*) AS count '
            'FROM albums'
            + where_sql,
            params
        ).fetchone()['count']

        total_pages = max(
            1,
            (
                filtered_album_count
                + ALBUMS_PER_PAGE
                - 1
            ) // ALBUMS_PER_PAGE
        )
        page = min(
            page,
            total_pages
        )
        offset = (
            page - 1
        ) * ALBUMS_PER_PAGE

        if sort_order in {
            'artist',
            'title',
        }:
            rows = conn.execute(
                'SELECT * FROM albums'
                + where_sql,
                params
            ).fetchall()
            rows = sort_index_rows(
                rows,
                sort_order
            )
            albums = rows[
                offset:
                offset + ALBUMS_PER_PAGE
            ]
        else:
            query = (
                'SELECT * FROM albums'
                + where_sql
                + ' ORDER BY '
                + INDEX_SORT_SQL[
                    sort_order
                ]
                + ' LIMIT ? OFFSET ?'
            )
            albums = conn.execute(
                query,
                params + [
                    ALBUMS_PER_PAGE,
                    offset,
                ]
            ).fetchall()

        total_album_count = conn.execute(
            'SELECT COUNT(*) AS count '
            'FROM albums '
            'WHERE is_primary_display = 1'
        ).fetchone()['count']

        genre_rows = conn.execute(
            "SELECT genre FROM albums "
            "WHERE is_primary_display = 1 "
            "AND genre IS NOT NULL "
            "AND TRIM(genre) != ''"
        ).fetchall()

        genre_options = sorted(
            {
                genre
                for row in genre_rows
                for genre in split_genres(
                    row['genre']
                )
            },
            key=str.casefold
        )
    finally:
        conn.close()

    return render_template(
        'index.html',
        albums=albums,
        library_q=library_q,
        classification_filter=(
            classification_filter
        ),
        format_filter=format_filter,
        album_type_filter=(
            album_type_filter
        ),
        genre_filter=genre_filter,
        genre_options=genre_options,
        sort_order=sort_order,
        total_album_count=(
            total_album_count
        ),
        filtered_album_count=(
            filtered_album_count
        ),
        page=page,
        total_pages=total_pages,
        pagination_pages=(
            build_pagination_pages(
                page,
                total_pages
            )
        ),
        saved=saved,
        bulk_saved=bulk_saved
    )


@app.route('/artist-profile-image/<path:artist_name>')
def artist_profile_image(artist_name):
    artist_name = str(artist_name or '').strip()

    if not artist_name:
        return ('', 404)

    profile_url = get_artist_profile_image(
        artist_name
    )

    if not profile_url:
        response = app.response_class(
            status=404
        )
        response.headers['Cache-Control'] = 'public, max-age=3600'
        return response

    response = redirect(
        profile_url,
        code=302
    )
    response.headers['Cache-Control'] = 'public, max-age=86400'
    return response


@app.route('/artists')
def artists_page():
    query = request.args.get(
        'q',
        ''
    ).strip()

    conn = get_db()

    try:
        rows = conn.execute(
            '''
            SELECT artist, cover_url
            FROM albums
            WHERE is_primary_display = 1
              AND artist IS NOT NULL
              AND TRIM(artist) != ''
            ORDER BY id DESC
            '''
        ).fetchall()
    finally:
        conn.close()

    directory = build_artist_directory(
        rows,
        query
    )

    return render_template(
        'artists.html',
        query=query,
        **directory
    )


@app.route('/artist/<path:artist_name>')
def artist_page(artist_name):
    artist_name = str(
        artist_name
        or ''
    ).strip()

    if not artist_name:
        return redirect(
            url_for('index')
        )

    sort_order = resolve_artist_sort(
        request.args,
        session
    )
    page = parse_page_number(
        request.args.get('page', '1')
    )

    conn = get_db()

    try:
        all_albums = conn.execute(
            '''
            SELECT *
            FROM albums
            WHERE is_primary_display = 1
            ORDER BY
                CASE
                    WHEN release_year
                        GLOB '[0-9][0-9][0-9][0-9]'
                    THEN CAST(
                        release_year
                        AS INTEGER
                    )
                    ELSE 0
                END DESC,
                album_title COLLATE NOCASE ASC,
                id DESC
            '''
        ).fetchall()

        all_owned_copies = conn.execute(
            '''
            SELECT artist, media_format
            FROM albums
            WHERE artist IS NOT NULL
              AND TRIM(artist) != ''
            '''
        ).fetchall()
    finally:
        conn.close()

    (
        matched_albums,
        canonical_artist,
        target_key,
    ) = collect_artist_albums(
        all_albums,
        artist_name
    )

    if not matched_albums:
        abort(404)

    artist_profile_url = (
        get_artist_profile_image(
            canonical_artist
        )
    )

    fallback_cover_url = next(
        (
            str(
                album['cover_url']
                or ''
            ).strip()
            for album in matched_albums
            if str(
                album['cover_url']
                or ''
            ).strip()
        ),
        ''
    )

    hero_background_url = (
        artist_profile_url
        or fallback_cover_url
    )
    hero_background_source = (
        'profile'
        if artist_profile_url
        else (
            'cover'
            if fallback_cover_url
            else ''
        )
    )

    owned_count = len(
        matched_albums
    )
    (
        format_counts,
        format_summary,
    ) = build_format_summary(
        all_owned_copies,
        target_key
    )
    album_type_summary = (
        build_album_type_summary(
            matched_albums
        )
    )
    matched_albums = sort_artist_albums(
        matched_albums,
        sort_order
    )

    total_pages = max(
        1,
        (
            owned_count
            + ALBUMS_PER_PAGE
            - 1
        ) // ALBUMS_PER_PAGE
    )
    page = min(
        page,
        total_pages
    )
    offset = (
        page - 1
    ) * ALBUMS_PER_PAGE
    albums = matched_albums[
        offset:
        offset + ALBUMS_PER_PAGE
    ]

    return render_template(
        'artist.html',
        artist_name=canonical_artist,
        albums=albums,
        artist_profile_url=(
            artist_profile_url
        ),
        hero_background_url=(
            hero_background_url
        ),
        hero_background_source=(
            hero_background_source
        ),
        owned_count=owned_count,
        format_counts=format_counts,
        format_summary=format_summary,
        album_type_summary=(
            album_type_summary
        ),
        sort_order=sort_order,
        page=page,
        total_pages=total_pages,
        pagination_pages=(
            build_pagination_pages(
                page,
                total_pages
            )
        )
    )


@app.route('/album/<int:album_row_id>')
def album_detail(album_row_id):
    conn = get_db()

    album = conn.execute(
        'SELECT * FROM albums WHERE id = ?',
        (album_row_id,)
    ).fetchone()

    if album is None:
        conn.close()
        return redirect(url_for('index'))

    owned_copies = conn.execute(
        '''
        SELECT *
        FROM albums
        WHERE album_id = ?
        ORDER BY id ASC
        ''',
        (album['album_id'],)
    ).fetchall()

    manual_track_mode = has_manual_track_override(
        conn,
        album['album_id']
    )

    if manual_track_mode:
        tracks = get_manual_album_tracks(
            conn,
            album['album_id']
        )
        track_data = {
            'tracks': tracks,
            'store': 'MANUAL',
            'resolved_by': 'manual_db'
        }
    else:
        tracks = []
        track_data = None

    conn.close()

    if not manual_track_mode:
        track_data = get_album_tracks(
            collection_id=album['apple_collection_id'] or '',
            artist=album['artist'] or '',
            album_title=album['album_title'] or '',
            release_year=album['release_year'] or ''
        )
        tracks = track_data['tracks']

    total_discs = 0
    if tracks:
        total_discs = max(
            int(track.get('disc_number', 1) or 1)
            for track in tracks
        )

    print(
        'DETAIL TRACK SOURCE:',
        track_data.get('resolved_by', '') if track_data else '',
        'COUNT:',
        len(tracks),
        'OWNED COPIES:',
        len(owned_copies),
        'MANUAL:',
        manual_track_mode
    )

    return render_template(
        'detail.html',
        album=album,
        tracks=tracks,
        track_store='MANUAL' if manual_track_mode else 'KR',
        total_discs=total_discs,
        owned_copies=owned_copies,
        owned_count=len(owned_copies),
        manual_track_mode=manual_track_mode
    )


if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=5001, debug=True)

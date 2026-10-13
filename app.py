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
    ALBUM_TYPES,
    split_genres,
    artist_credit_parts,
    split_artists,
    artist_group_key,
    artist_group_sort_key,
    name_sort_key,
)

from cache_utils import BoundedCache
from track_service import (
    has_manual_track_override,
    get_manual_album_tracks,
    normalize_manual_duration,
    get_album_tracks,
    clear_track_caches,
)
from media_service import configure_media, cover_thumbnail_url
from admin_routes import register_admin_routes
from album_routes import register_album_routes
from search_engine import get_artist_profile_image

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


@app.route('/')
def index():
    state_keys = (
        'library_q',
        'classification',
        'media_format',
        'album_type',
        'genre',
        'sort',
    )
    reset_filters = (
        request.args.get('reset_filters', '').strip() == '1'
    )
    has_explicit_state = any(
        key in request.args
        for key in state_keys
    )

    if reset_filters:
        session.pop('album_index_state', None)
        saved_state = {}
    else:
        saved_state = session.get('album_index_state', {})
        if not isinstance(saved_state, dict):
            saved_state = {}

    def state_value(key, default=''):
        if has_explicit_state:
            return request.args.get(key, default)
        return saved_state.get(key, default)

    library_q = str(
        state_value('library_q', '')
        or ''
    ).strip()
    classification_filter = str(
        state_value('classification', '')
        or ''
    ).strip()
    format_filter = str(
        state_value('media_format', '')
        or ''
    ).strip()
    album_type_filter = str(
        state_value('album_type', '')
        or ''
    ).strip()
    genre_filter = str(
        state_value('genre', '')
        or ''
    ).strip()
    sort_order = str(
        state_value('sort', 'release_desc')
        or 'release_desc'
    ).strip()
    page = parse_page_number(request.args.get('page', '1'))

    sort_sql_map = {
        'release_desc': (
            "CASE WHEN release_year GLOB '[0-9][0-9][0-9][0-9]' "
            "THEN 0 ELSE 1 END ASC, "
            "CAST(release_year AS INTEGER) DESC, "
            "album_title COLLATE NOCASE ASC, id DESC"
        ),
        'release_asc': (
            "CASE WHEN release_year GLOB '[0-9][0-9][0-9][0-9]' "
            "THEN 0 ELSE 1 END ASC, "
            "CAST(release_year AS INTEGER) ASC, "
            "album_title COLLATE NOCASE ASC, id ASC"
        ),
        'price_desc': (
            "CASE WHEN purchase_price IS NULL THEN 1 ELSE 0 END ASC, "
            "purchase_price DESC, album_title COLLATE NOCASE ASC, id DESC"
        ),
        'price_asc': (
            "CASE WHEN purchase_price IS NULL THEN 1 ELSE 0 END ASC, "
            "purchase_price ASC, album_title COLLATE NOCASE ASC, id DESC"
        ),
    }

    allowed_sorts = set(sort_sql_map) | {
        'artist',
        'title',
    }

    if sort_order not in allowed_sorts:
        sort_order = 'release_desc'

    if not reset_filters:
        session['album_index_state'] = {
            key: value
            for key, value in {
                'library_q': library_q,
                'classification': classification_filter,
                'media_format': format_filter,
                'album_type': album_type_filter,
                'genre': genre_filter,
                'sort': sort_order,
            }.items()
            if value
        }

    saved = request.args.get('saved', '')
    bulk_saved = request.args.get('bulk_saved', '')

    conn = get_db()
    conditions = ['is_primary_display = 1']
    params = []

    if library_q:
        value = f'%{library_q}%'
        conditions.append(
            '( artist LIKE ? OR album_title LIKE ? OR release_year LIKE ? )'
        )
        params.extend([value, value, value])

    if classification_filter:
        conditions.append('classification = ?')
        params.append(classification_filter)

    if format_filter == 'CD':
        conditions.append("media_format = 'CD'")
    elif format_filter == 'LP':
        conditions.append("media_format = 'LP'")
    elif format_filter == 'ETC':
        conditions.append(
            "media_format IN ( 'Casette', 'USB', '기타' )"
        )

    if album_type_filter in ALBUM_TYPES:
        conditions.append('album_type = ?')
        params.append(album_type_filter)

    if genre_filter:
        conditions.append(
            "(',' || REPLACE(COALESCE(genre, ''), ', ', ',') || ',') LIKE ?"
        )
        params.append(f'%,{genre_filter},%')

    where_sql = (
        ' WHERE ' + ' AND '.join(conditions)
        if conditions
        else ''
    )

    filtered_album_count = conn.execute(
        'SELECT COUNT(*) AS count FROM albums' + where_sql,
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
    page = min(page, total_pages)
    offset = (page - 1) * ALBUMS_PER_PAGE

    if sort_order in {'artist', 'title'}:
        sorted_albums = conn.execute(
            'SELECT * FROM albums' + where_sql,
            params
        ).fetchall()

        if sort_order == 'artist':
            sorted_albums = sorted(
                sorted_albums,
                key=lambda album: (
                    name_sort_key(album['artist']),
                    name_sort_key(album['album_title']),
                    -(int(album['id'] or 0)),
                )
            )
        else:
            sorted_albums = sorted(
                sorted_albums,
                key=lambda album: (
                    name_sort_key(album['album_title']),
                    name_sort_key(album['artist']),
                    -(int(album['id'] or 0)),
                )
            )

        albums = sorted_albums[
            offset:offset + ALBUMS_PER_PAGE
        ]
    else:
        query = (
            'SELECT * FROM albums'
            + where_sql
            + ' ORDER BY '
            + sort_sql_map[sort_order]
            + ' LIMIT ? OFFSET ?'
        )
        albums = conn.execute(
            query,
            params + [ALBUMS_PER_PAGE, offset]
        ).fetchall()

    total_album_count = conn.execute(
        'SELECT COUNT(*) AS count FROM albums '
        'WHERE is_primary_display = 1'
    ).fetchone()['count']

    genre_rows = conn.execute(
        "SELECT genre FROM albums "
        "WHERE is_primary_display = 1 "
        "AND genre IS NOT NULL AND TRIM(genre) != ''"
    ).fetchall()
    genre_options = sorted(
        {
            genre
            for row in genre_rows
            for genre in split_genres(row['genre'])
        },
        key=str.casefold
    )

    conn.close()

    return render_template(
        'index.html',
        albums=albums,
        library_q=library_q,
        classification_filter=classification_filter,
        format_filter=format_filter,
        album_type_filter=album_type_filter,
        genre_filter=genre_filter,
        genre_options=genre_options,
        sort_order=sort_order,
        total_album_count=total_album_count,
        filtered_album_count=filtered_album_count,
        page=page,
        total_pages=total_pages,
        pagination_pages=build_pagination_pages(
            page,
            total_pages
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
    query = request.args.get('q', '').strip()

    conn = get_db()
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
    conn.close()

    artist_map = {}

    for row in rows:
        for artist_name in split_artists(row['artist']):
            name = str(artist_name or '').strip()
            if not name:
                continue

            key = name.casefold()
            item = artist_map.get(key)

            if item is None:
                item = {
                    'name': name,
                    'count': 0,
                    'cover_url': '',
                }
                artist_map[key] = item

            item['count'] += 1

            if not item['cover_url'] and row['cover_url']:
                item['cover_url'] = row['cover_url']

    total_artist_count = len(artist_map)
    artists = list(artist_map.values())

    if query:
        query_key = query.casefold()
        artists = [
            item
            for item in artists
            if query_key in item['name'].casefold()
        ]

    artists.sort(
        key=lambda item: (
            artist_group_sort_key(
                artist_group_key(item['name'])
            ),
            name_sort_key(item['name'])
        )
    )

    grouped = {}

    for item in artists:
        group = artist_group_key(item['name'])
        grouped.setdefault(group, []).append(item)

    grouped_artists = [
        {
            'key': group,
            'anchor': (
                'other'
                if group == '#'
                else 'group-' + str(ord(group[0]))
            ),
            'artists': grouped[group],
        }
        for group in sorted(
            grouped,
            key=artist_group_sort_key
        )
    ]

    return render_template(
        'artists.html',
        grouped_artists=grouped_artists,
        total_artist_count=total_artist_count,
        filtered_artist_count=len(artists),
        query=query
    )


@app.route('/artist/<path:artist_name>')
def artist_page(artist_name):
    artist_name = str(artist_name or '').strip()

    allowed_sorts = {
        'release_desc',
        'release_asc',
        'price_desc',
        'price_asc',
        'title',
    }

    requested_sort = request.args.get('sort')
    if requested_sort is None:
        sort_order = str(
            session.get(
                'album_artist_sort',
                'release_desc'
            )
            or 'release_desc'
        ).strip()
    else:
        sort_order = str(
            requested_sort
            or 'release_desc'
        ).strip()

    if sort_order not in allowed_sorts:
        sort_order = 'release_desc'

    session['album_artist_sort'] = sort_order

    page = parse_page_number(request.args.get('page', '1'))

    if not artist_name:
        return redirect(url_for('index'))

    target_key = artist_name.casefold()
    conn = get_db()

    all_albums = conn.execute(
        '''
        SELECT *
        FROM albums
        WHERE is_primary_display = 1
        ORDER BY
            CASE
                WHEN release_year GLOB '[0-9][0-9][0-9][0-9]'
                THEN CAST(release_year AS INTEGER)
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

    conn.close()

    matched_albums = []
    canonical_artist = ''

    for album in all_albums:
        for credit_name in split_artists(album['artist']):
            if credit_name.casefold() == target_key:
                matched_albums.append(album)
                if not canonical_artist:
                    canonical_artist = credit_name
                break

    if not matched_albums:
        abort(404)

    artist_profile_url = get_artist_profile_image(
        canonical_artist
    )

    fallback_cover_url = next(
        (
            str(album['cover_url'] or '').strip()
            for album in matched_albums
            if str(album['cover_url'] or '').strip()
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

    owned_count = len(matched_albums)

    format_counts = {
        'CD': 0,
        'LP': 0,
        'CASETTE': 0,
        'ETC': 0,
    }

    for album in all_owned_copies:
        artist_match = any(
            credit_name.casefold() == target_key
            for credit_name in split_artists(
                album['artist']
            )
        )

        if not artist_match:
            continue

        media_format = str(
            album['media_format'] or ''
        ).strip()
        media_key = media_format.casefold()

        if media_key == 'cd':
            format_counts['CD'] += 1
        elif media_key == 'lp':
            format_counts['LP'] += 1
        elif media_key in {
            'casette',
            'cassette',
            '카세트',
        }:
            format_counts['CASETTE'] += 1
        else:
            format_counts['ETC'] += 1

    format_summary = [
        {
            'name': name,
            'count': format_counts[name]
        }
        for name in (
            'CD',
            'LP',
            'CASETTE',
            'ETC',
        )
        if format_counts[name] > 0
    ]

    type_order = [
        '앨범',
        'EP(미니)',
        '싱글',
        '믹스테잎',
    ]
    album_type_counts = {
        type_name: 0
        for type_name in type_order
    }

    for album in matched_albums:
        album_type = (
            str(album['album_type'] or '앨범').strip()
            or '앨범'
        )
        if album_type in album_type_counts:
            album_type_counts[album_type] += 1

    album_type_summary = [
        {
            'name': type_name,
            'count': album_type_counts[type_name]
        }
        for type_name in type_order
        if album_type_counts[type_name] > 0
    ]

    def release_year_number(album):
        value = str(album['release_year'] or '').strip()
        return int(value) if value.isdigit() else None

    def purchase_price_number(album):
        value = album['purchase_price']
        if value is None:
            return None
        try:
            return int(value)
        except Exception:
            return None

    if sort_order == 'release_asc':
        matched_albums.sort(
            key=lambda album: (
                release_year_number(album) is None,
                release_year_number(album) or 0,
                name_sort_key(album['album_title']),
                int(album['id'] or 0),
            )
        )
    elif sort_order == 'price_desc':
        matched_albums.sort(
            key=lambda album: (
                purchase_price_number(album) is None,
                -(purchase_price_number(album) or 0),
                name_sort_key(album['album_title']),
                -(int(album['id'] or 0)),
            )
        )
    elif sort_order == 'price_asc':
        matched_albums.sort(
            key=lambda album: (
                purchase_price_number(album) is None,
                purchase_price_number(album) or 0,
                name_sort_key(album['album_title']),
                -(int(album['id'] or 0)),
            )
        )
    elif sort_order == 'title':
        matched_albums.sort(
            key=lambda album: (
                name_sort_key(album['album_title']),
                -(int(album['id'] or 0)),
            )
        )
    else:
        matched_albums.sort(
            key=lambda album: (
                release_year_number(album) is None,
                -(release_year_number(album) or 0),
                name_sort_key(album['album_title']),
                -(int(album['id'] or 0)),
            )
        )

    total_pages = max(
        1,
        (
            owned_count
            + ALBUMS_PER_PAGE
            - 1
        ) // ALBUMS_PER_PAGE
    )
    page = min(page, total_pages)
    offset = (page - 1) * ALBUMS_PER_PAGE
    albums = matched_albums[
        offset:offset + ALBUMS_PER_PAGE
    ]

    return render_template(
        'artist.html',
        artist_name=canonical_artist,
        albums=albums,
        artist_profile_url=artist_profile_url,
        hero_background_url=hero_background_url,
        hero_background_source=hero_background_source,
        owned_count=owned_count,
        format_counts=format_counts,
        format_summary=format_summary,
        album_type_summary=album_type_summary,
        sort_order=sort_order,
        page=page,
        total_pages=total_pages,
        pagination_pages=build_pagination_pages(
            page,
            total_pages
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


@app.route('/album/<int:album_row_id>/tracks')
def edit_album_tracks(album_row_id):
    track_saved = request.args.get('track_saved', '').strip()
    track_reset = request.args.get('track_reset', '').strip()
    conn = get_db()
    album = conn.execute(
        'SELECT * FROM albums WHERE id = ?',
        (album_row_id,)
    ).fetchone()

    if album is None:
        conn.close()
        return redirect(url_for('index'))

    manual_track_mode = has_manual_track_override(
        conn,
        album['album_id']
    )

    if manual_track_mode:
        tracks = get_manual_album_tracks(
            conn,
            album['album_id']
        )
    else:
        tracks = []

    conn.close()

    # 아직 수동 저장 전이면 Apple에서 불러온 현재 수록곡을 편집 초안으로 사용한다.
    if not manual_track_mode:
        track_data = get_album_tracks(
            collection_id=album['apple_collection_id'] or '',
            artist=album['artist'] or '',
            album_title=album['album_title'] or '',
            release_year=album['release_year'] or ''
        )
        tracks = track_data.get('tracks', [])

    editor_tracks = []
    for track in tracks:
        editor_tracks.append({
            'disc_number': int(track.get('disc_number', 1) or 1),
            'track_number': int(track.get('track_number', 1) or 1),
            'title': track.get('title', '') or '',
            'artist': track.get('artist', '') or '',
            'duration': track.get('duration', '') or ''
        })

    if not editor_tracks:
        editor_tracks = [{
            'disc_number': 1,
            'track_number': 1,
            'title': '',
            'artist': '',
            'duration': ''
        }]

    return render_template(
        'track_edit.html',
        album=album,
        tracks=editor_tracks,
        manual_track_mode=manual_track_mode,
        track_saved=track_saved,
        track_reset=track_reset
    )


@app.route('/album/<int:album_row_id>/tracks/save', methods=['POST'])
def save_album_tracks(album_row_id):
    conn = get_db()
    album = conn.execute(
        'SELECT * FROM albums WHERE id = ?',
        (album_row_id,)
    ).fetchone()

    if album is None:
        conn.close()
        return redirect(url_for('index'))

    disc_values = request.form.getlist('disc_number')
    track_values = request.form.getlist('track_number')
    title_values = request.form.getlist('title')
    artist_values = request.form.getlist('track_artist')
    duration_values = request.form.getlist('duration')

    rows = []

    for index, raw_title in enumerate(title_values):
        title = str(raw_title or '').strip()
        if not title:
            continue

        try:
            disc_number = int(disc_values[index])
        except Exception:
            disc_number = 1
        disc_number = max(1, disc_number)

        try:
            track_number = int(track_values[index])
        except Exception:
            track_number = index + 1
        track_number = max(1, track_number)

        artist = ''
        if index < len(artist_values):
            artist = str(artist_values[index] or '').strip()

        duration = ''
        if index < len(duration_values):
            duration = normalize_manual_duration(
                duration_values[index]
            )

        rows.append({
            'disc_number': disc_number,
            'track_number': track_number,
            'title': title,
            'artist': artist,
            'duration': duration
        })

    rows.sort(
        key=lambda item: (
            item['disc_number'],
            item['track_number'],
            item['title'].lower()
        )
    )

    try:
        conn.execute(
            'DELETE FROM album_tracks WHERE album_id = ?',
            (album['album_id'],)
        )

        for row in rows:
            conn.execute(
                '''
                INSERT INTO album_tracks (
                    album_id,
                    disc_number,
                    track_number,
                    title,
                    artist,
                    duration
                ) VALUES (?, ?, ?, ?, ?, ?)
                ''',
                (
                    album['album_id'],
                    row['disc_number'],
                    row['track_number'],
                    row['title'],
                    row['artist'],
                    row['duration']
                )
            )

        conn.execute(
            '''
            INSERT INTO album_track_settings (
                album_id,
                manual_override,
                updated_at
            ) VALUES (?, 1, CURRENT_TIMESTAMP)
            ON CONFLICT(album_id) DO UPDATE SET
                manual_override = 1,
                updated_at = CURRENT_TIMESTAMP
            ''',
            (album['album_id'],)
        )

        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()

    return redirect(
        url_for(
            'edit_album_tracks',
            album_row_id=album_row_id,
            track_saved='1'
        )
    )


@app.route('/album/<int:album_row_id>/tracks/reset', methods=['POST'])
def reset_album_tracks(album_row_id):
    conn = get_db()
    album = conn.execute(
        'SELECT * FROM albums WHERE id = ?',
        (album_row_id,)
    ).fetchone()

    if album is None:
        conn.close()
        return redirect(url_for('index'))

    conn.execute(
        'DELETE FROM album_tracks WHERE album_id = ?',
        (album['album_id'],)
    )
    conn.execute(
        'DELETE FROM album_track_settings WHERE album_id = ?',
        (album['album_id'],)
    )
    conn.commit()
    conn.close()

    clear_track_caches()

    return redirect(
        url_for(
            'edit_album_tracks',
            album_row_id=album_row_id,
            track_reset='1'
        )
    )


if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=5001, debug=True)

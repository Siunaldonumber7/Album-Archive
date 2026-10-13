from flask import (
    Response,
    abort,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from album_database import get_db
from catalog import split_genres
from library_view import (
    INDEX_SORT_SQL,
    build_album_type_summary,
    build_artist_directory,
    build_format_summary,
    build_index_where,
    collect_artist_albums,
    resolve_artist_sort,
    resolve_index_state,
    sort_artist_albums,
    sort_index_rows,
)
from pagination import (
    ALBUMS_PER_PAGE,
    build_pagination_pages,
    parse_page_number,
)
from search_engine import (
    get_artist_profile_image,
)
from track_service import (
    get_album_tracks,
    get_manual_album_tracks,
    has_manual_track_override,
)


def register_library_routes(app):
    app.add_url_rule(
        '/',
        endpoint='index',
        view_func=index
    )
    app.add_url_rule(
        '/artist-profile-image/<path:artist_name>',
        endpoint='artist_profile_image',
        view_func=artist_profile_image
    )
    app.add_url_rule(
        '/artists',
        endpoint='artists_page',
        view_func=artists_page
    )
    app.add_url_rule(
        '/artist/<path:artist_name>',
        endpoint='artist_page',
        view_func=artist_page
    )
    app.add_url_rule(
        '/album/<int:album_row_id>',
        endpoint='album_detail',
        view_func=album_detail
    )


def index():
    state = resolve_index_state(
        request.args,
        session
    )

    library_q = state['library_q']
    classification_filter = (
        state['classification']
    )
    format_filter = state['media_format']
    album_type_filter = (
        state['album_type']
    )
    genre_filter = state['genre']
    sort_order = state['sort']

    page = parse_page_number(
        request.args.get('page', '1')
    )
    saved = request.args.get(
        'saved',
        ''
    )
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


def artist_profile_image(artist_name):
    artist_name = str(
        artist_name
        or ''
    ).strip()

    if not artist_name:
        return Response(
            status=404
        )

    profile_url = get_artist_profile_image(
        artist_name
    )

    if not profile_url:
        response = Response(
            status=404
        )
        response.headers[
            'Cache-Control'
        ] = 'public, max-age=3600'
        return response

    response = redirect(
        profile_url,
        code=302
    )
    response.headers[
        'Cache-Control'
    ] = 'public, max-age=86400'
    return response


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
                album_title
                    COLLATE NOCASE ASC,
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


def album_detail(album_row_id):
    conn = get_db()

    try:
        album = conn.execute(
            'SELECT * FROM albums '
            'WHERE id = ?',
            (album_row_id,)
        ).fetchone()

        if album is None:
            return redirect(
                url_for('index')
            )

        owned_copies = conn.execute(
            '''
            SELECT *
            FROM albums
            WHERE album_id = ?
            ORDER BY id ASC
            ''',
            (album['album_id'],)
        ).fetchall()

        manual_track_mode = (
            has_manual_track_override(
                conn,
                album['album_id']
            )
        )

        if manual_track_mode:
            tracks = get_manual_album_tracks(
                conn,
                album['album_id']
            )
            track_data = {
                'tracks': tracks,
                'store': 'MANUAL',
                'resolved_by': 'manual_db',
            }
        else:
            tracks = []
            track_data = None
    finally:
        conn.close()

    if not manual_track_mode:
        track_data = get_album_tracks(
            collection_id=(
                album['apple_collection_id']
                or ''
            ),
            artist=(
                album['artist']
                or ''
            ),
            album_title=(
                album['album_title']
                or ''
            ),
            release_year=(
                album['release_year']
                or ''
            )
        )
        tracks = track_data.get(
            'tracks',
            []
        )

    total_discs = 0

    if tracks:
        total_discs = max(
            int(
                track.get(
                    'disc_number',
                    1
                )
                or 1
            )
            for track in tracks
        )

    print(
        'DETAIL TRACK SOURCE:',
        (
            track_data.get(
                'resolved_by',
                ''
            )
            if track_data
            else ''
        ),
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
        track_store=(
            'MANUAL'
            if manual_track_mode
            else 'KR'
        ),
        total_discs=total_discs,
        owned_copies=owned_copies,
        owned_count=len(
            owned_copies
        ),
        manual_track_mode=(
            manual_track_mode
        )
    )

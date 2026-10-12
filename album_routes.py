from concurrent.futures import ThreadPoolExecutor

from flask import render_template, request, redirect, url_for

from album_database import get_db
from album_service import (
    make_album_key,
    get_next_management_no,
    get_or_create_album_id,
)
from catalog import ALBUM_TYPES, normalize_genres
from media_service import (
    allowed_image_file,
    save_uploaded_cover,
    delete_local_cover,
)
from search_engine import (
    search_apple,
    resolve_apple_music_album_link,
    lookup_album_metadata_store,
    learn_artist_alias,
    clear_search_cache,
)
from track_service import clear_track_caches


def register_album_routes(app):
    app.add_url_rule(
        '/add',
        endpoint='add_album_page',
        view_func=add_album_page
    )
    app.add_url_rule(
        '/add-link',
        endpoint='add_album_from_link',
        view_func=add_album_from_link,
        methods=['POST']
    )
    app.add_url_rule(
        '/add-manual',
        endpoint='add_album_manual',
        view_func=add_album_manual,
        methods=['POST']
    )
    app.add_url_rule(
        '/register-bulk',
        endpoint='register_bulk',
        view_func=register_bulk,
        methods=['POST']
    )
    app.add_url_rule(
        '/save-bulk',
        endpoint='save_bulk_albums',
        view_func=save_bulk_albums,
        methods=['POST']
    )
    app.add_url_rule(
        '/register',
        endpoint='register',
        view_func=register,
        methods=['POST']
    )
    app.add_url_rule(
        '/save',
        endpoint='save_album',
        view_func=save_album,
        methods=['POST']
    )
    app.add_url_rule(
        '/edit/<int:album_row_id>',
        endpoint='edit_album',
        view_func=edit_album
    )
    app.add_url_rule(
        '/update/<int:album_row_id>',
        endpoint='update_album',
        view_func=update_album,
        methods=['POST']
    )
    app.add_url_rule(
        '/delete/<int:album_row_id>',
        endpoint='delete_album',
        view_func=delete_album,
        methods=['POST']
    )


def add_album_page():
    artist = request.args.get('artist', '').strip()
    album = request.args.get('album', '').strip()
    link_error = request.args.get('link_error', '').strip()
    manual_error = request.args.get('manual_error', '').strip()
    saved = request.args.get('saved', '').strip()
    manual_saved = request.args.get('manual_saved', '').strip()
    bulk_saved = request.args.get('bulk_saved', '').strip()
    searched = False
    apple_results = []

    if artist or album:
        searched = True
        apple_results = search_apple(artist, album)

    conn = get_db()
    total_album_count = conn.execute('SELECT COUNT(*) AS count FROM albums').fetchone()['count']
    conn.close()

    return render_template(
        'add.html',
        artist=artist,
        album=album,
        searched=searched,
        apple_results=apple_results,
        total_album_count=total_album_count,
        link_error=link_error,
        manual_error=manual_error,
        saved=saved,
        manual_saved=manual_saved,
        bulk_saved=bulk_saved
    )

def add_album_from_link():
    apple_url = request.form.get('apple_url', '').strip()

    metadata = resolve_apple_music_album_link(apple_url)

    if not metadata:
        return redirect(
            url_for(
                'add_album_page',
                link_error='Apple Music 앨범 링크를 확인하지 못했습니다. 앨범 페이지 링크인지 확인해 주세요.'
            )
        )

    apple_id = metadata.get('apple_id', '')
    artist = metadata.get('artist', '')
    album_title = metadata.get('title', '')
    release_year = metadata.get('release_year', '')
    cover_url = metadata.get('cover', '')
    apple_artist_id = metadata.get('artist_id', '')
    album_type = metadata.get('album_type', '') or '앨범'
    genre = metadata.get('genre', '')

    album_key = make_album_key(
        artist,
        album_title,
        release_year
    )

    conn = get_db()

    existing = conn.execute(
        "SELECT album_id FROM album_master WHERE album_key = ? OR ( ? != '' AND apple_collection_id = ? ) LIMIT 1",
        (album_key, apple_id, apple_id)
    ).fetchone()

    duplicate_items = []
    duplicate_count = 0

    if existing:
        preview_album_id = existing['album_id']
        duplicate_items = conn.execute(
            'SELECT id, management_no, album_id, media_format, artist, album_title, release_year, open_status, signed, purchase_price FROM albums WHERE album_id = ? ORDER BY id ASC',
            (preview_album_id,)
        ).fetchall()
        duplicate_count = len(duplicate_items)
    else:
        preview_album_id = '신규 자동 생성'

    conn.close()

    selected = {
        'apple_id': apple_id,
        'apple_artist_id': apple_artist_id,
        'search_artist': artist,
        'search_album': album_title,
        'artist': artist,
        'album_title': album_title,
        'release_year': release_year,
        'cover_url': cover_url,
        'album_type': album_type,
        'genre': genre,
        'preview_album_id': preview_album_id
    }

    return render_template(
        'register.html',
        album=selected,
        duplicate_count=duplicate_count,
        duplicate_items=duplicate_items
    )

def add_album_manual():
    """Apple Music에 없는 실물 음반을 직접 등록한다."""

    artist = request.form.get('artist', '').strip()
    album_title = request.form.get('album_title', '').strip()
    release_year = request.form.get('release_year', '').strip()
    classification = request.form.get('classification', '').strip()
    media_format = request.form.get('media_format', '').strip()
    album_type = request.form.get('album_type', '').strip()
    genre = normalize_genres(request.form.getlist('genre'))
    open_status = request.form.get('open_status', '').strip()
    signed = request.form.get('signed', '').strip()
    purchase_price_text = request.form.get('purchase_price', '').replace(',', '').strip()
    memo = request.form.get('memo', '').strip()
    cover_file = request.files.get('cover_file')

    if not artist or not album_title:
        return redirect(
            url_for(
                'add_album_page',
                manual_error='아티스트와 앨범명은 반드시 입력해 주세요.'
            )
        )

    if classification not in {'한국', '해외'}:
        classification = '한국'

    if media_format not in {'CD', 'LP', 'Casette', 'USB', '기타'}:
        media_format = 'CD'
    if album_type not in ALBUM_TYPES:
        album_type = '앨범'

    if open_status not in {'개봉', '미개봉'}:
        open_status = '개봉'

    if signed not in {'O', 'X'}:
        signed = 'X'

    if cover_file and cover_file.filename and not allowed_image_file(cover_file.filename):
        return redirect(
            url_for(
                'add_album_page',
                manual_error='커버 이미지는 JPG, JPEG, PNG, WEBP, GIF 파일만 등록할 수 있습니다.'
            )
        )

    try:
        purchase_price = int(purchase_price_text) if purchase_price_text else None
    except Exception:
        purchase_price = None

    cover_url = ''

    if cover_file and cover_file.filename:
        cover_url = save_uploaded_cover(cover_file) or ''

    conn = get_db()

    try:
        # Apple ID가 없어도 같은 아티스트 + 앨범명 + 발매년도면
        # 기존 album_id를 공유하므로 중복 보유본이 자동으로 묶인다.
        album_id = get_or_create_album_id(
            conn,
            '',
            artist,
            album_title,
            release_year,
            cover_url
        )

        management_no = get_next_management_no(
            conn,
            media_format
        )

        conn.execute(
            """INSERT INTO albums (
                management_no,
                album_id,
                classification,
                media_format,
                album_type,
                genre,
                artist,
                album_title,
                release_year,
                open_status,
                signed,
                purchase_price,
                memo,
                apple_collection_id,
                cover_url
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                management_no,
                album_id,
                classification,
                media_format,
                album_type,
                genre,
                artist,
                album_title,
                release_year,
                open_status,
                signed,
                purchase_price,
                memo,
                '',
                cover_url
            )
        )

        conn.commit()

    except Exception:
        conn.rollback()

        if cover_url:
            delete_local_cover(cover_url)

        raise

    finally:
        conn.close()

    clear_search_cache()
    clear_track_caches()

    return redirect(
        url_for(
            'add_album_page',
            manual_saved=management_no
        )
    )

def register_bulk():
    raw_selections = request.form.getlist('bulk_selection')
    search_artist = request.form.get('search_artist', '').strip()
    search_album = request.form.get('search_album', '').strip()

    selections = []
    seen = set()

    for raw in raw_selections[:30]:
        raw = str(raw or '').strip()

        if '|' not in raw:
            continue

        store, apple_id = raw.split('|', 1)
        store = store.strip().upper()
        apple_id = apple_id.strip()

        if store not in {'KR', 'US', 'JP', 'GB'}:
            continue
        if not apple_id.isdigit():
            continue

        key = f'{store}|{apple_id}'

        if key in seen:
            continue

        seen.add(key)
        selections.append((store, apple_id))

    if not selections:
        return redirect(url_for('add_album_page'))

    # Apple/iTunes 메타데이터 조회만 병렬 처리한다.
    # SQLite 작업은 아래에서 메인 스레드로 순차 처리한다.
    worker_count = min(6, len(selections))

    print(
        'BULK PREVIEW PARALLEL:',
        len(selections),
        'albums /',
        worker_count,
        'workers'
    )

    def fetch_selection(selection):
        store, apple_id = selection
        return lookup_album_metadata_store(
            apple_id,
            store
        )

    try:
        with ThreadPoolExecutor(
            max_workers=worker_count
        ) as executor:
            metadata_results = list(
                executor.map(
                    fetch_selection,
                    selections
                )
            )
    except Exception as e:
        print('BULK PREVIEW PARALLEL ERROR:', e)
        metadata_results = [
            lookup_album_metadata_store(
                apple_id,
                store
            )
            for store, apple_id in selections
        ]

    # 병렬 요청에서 순간적으로 실패한 항목만 한 번 순차 재시도한다.
    # 성공한 항목은 그대로 재사용해서 불필요한 Apple 호출을 늘리지 않는다.
    failed_indexes = [
        index
        for index, item in enumerate(metadata_results)
        if not item
    ]

    if failed_indexes:
        print(
            'BULK PREVIEW RETRY:',
            len(failed_indexes),
            'albums'
        )

        for index in failed_indexes:
            store, apple_id = selections[index]
            metadata_results[index] = lookup_album_metadata_store(
                apple_id,
                store
            )

    albums = []
    conn = get_db()

    try:
        for (store, apple_id), item in zip(
            selections,
            metadata_results
        ):
            if not item:
                continue

            album_key = make_album_key(
                item.get('artist', ''),
                item.get('title', ''),
                item.get('release_year', '')
            )

            existing = conn.execute(
                "SELECT album_id FROM album_master WHERE album_key = ? OR ( ? != '' AND apple_collection_id = ? ) LIMIT 1",
                (album_key, apple_id, apple_id)
            ).fetchone()

            duplicate_count = 0

            if existing:
                duplicate_count = conn.execute(
                    'SELECT COUNT(*) AS count FROM albums WHERE album_id = ?',
                    (existing['album_id'],)
                ).fetchone()['count']

            item['classification'] = (
                '한국'
                if store == 'KR'
                else '해외'
            )
            item['duplicate_count'] = duplicate_count
            albums.append(item)

    finally:
        conn.close()

    if not albums:
        return redirect(url_for('add_album_page'))

    return render_template(
        'bulk_register.html',
        albums=albums,
        search_artist=search_artist,
        search_album=search_album
    )

def save_bulk_albums():
    selected_indexes = request.form.getlist('selected_index')
    search_artist = request.form.get('search_artist', '').strip()
    search_album = request.form.get('search_album', '').strip()

    if not selected_indexes:
        return redirect(url_for('add_album_page'))

    indexes = []

    for raw_index in selected_indexes[:30]:
        try:
            index = int(raw_index)
        except Exception:
            continue

        if index < 0 or index > 29 or index in indexes:
            continue

        indexes.append(index)

    if not indexes:
        return redirect(url_for('add_album_page'))

    conn = get_db()
    saved_management_numbers = []

    try:
        for index in indexes:
            prefix = f'album_{index}_'

            apple_id = request.form.get(prefix + 'apple_id', '').strip()
            apple_artist_id = request.form.get(prefix + 'artist_id', '').strip()
            artist = request.form.get(prefix + 'artist', '').strip()
            album_title = request.form.get(prefix + 'title', '').strip()
            release_year = request.form.get(prefix + 'release_year', '').strip()
            cover_url = request.form.get(prefix + 'cover', '').strip()
            classification = request.form.get(prefix + 'classification', '').strip()
            media_format = request.form.get(prefix + 'media_format', '').strip()
            album_type = request.form.get(prefix + 'album_type', '').strip()
            genre = normalize_genres(
                request.form.getlist(prefix + 'genre')
            )
            open_status = request.form.get(prefix + 'open_status', '').strip()
            signed = request.form.get(prefix + 'signed', '').strip()
            purchase_price_text = (
                request.form.get(prefix + 'purchase_price', '')
                .replace(',', '')
                .strip()
            )
            memo = request.form.get(prefix + 'memo', '').strip()

            if not artist or not album_title:
                raise ValueError(
                    f'일괄 등록 데이터가 올바르지 않습니다: {index}'
                )

            if classification not in {'한국', '해외'}:
                classification = '한국'
            if media_format not in {'CD', 'LP', 'Casette', 'USB', '기타'}:
                media_format = 'CD'
            if album_type not in ALBUM_TYPES:
                album_type = '앨범'
            if open_status not in {'개봉', '미개봉'}:
                open_status = '개봉'
            if signed not in {'O', 'X'}:
                signed = 'X'

            try:
                purchase_price = (
                    int(purchase_price_text)
                    if purchase_price_text != ''
                    else None
                )
                if purchase_price is not None and purchase_price < 0:
                    purchase_price = None
            except Exception:
                purchase_price = None

            learn_artist_alias(
                conn,
                artist,
                artist,
                apple_artist_id
            )

            album_id = get_or_create_album_id(
                conn,
                apple_id,
                artist,
                album_title,
                release_year,
                cover_url
            )

            management_no = get_next_management_no(
                conn,
                media_format
            )

            conn.execute(
                '''
                INSERT INTO albums (
                    management_no,
                    album_id,
                    classification,
                    media_format,
                    album_type,
                    genre,
                    artist,
                    album_title,
                    release_year,
                    open_status,
                    signed,
                    purchase_price,
                    memo,
                    apple_collection_id,
                    cover_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    management_no,
                    album_id,
                    classification,
                    media_format,
                    album_type,
                    genre,
                    artist,
                    album_title,
                    release_year,
                    open_status,
                    signed,
                    purchase_price,
                    memo,
                    apple_id,
                    cover_url
                )
            )

            saved_management_numbers.append(management_no)

        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()

    clear_search_cache()
    clear_track_caches()

    return redirect(
        url_for(
            'add_album_page',
            bulk_saved=len(saved_management_numbers)
        )
    )

def register():
    apple_id = request.form.get('apple_id', '').strip()
    apple_artist_id = request.form.get('apple_artist_id', '').strip()
    search_artist = request.form.get('search_artist', '').strip()
    search_album = request.form.get('search_album', '').strip()
    artist = request.form.get('artist', '').strip()
    album_title = request.form.get('album_title', '').strip()
    release_year = request.form.get('release_year', '').strip()
    cover_url = request.form.get('cover_url', '').strip()
    album_type = request.form.get('album_type', '').strip() or '앨범'
    genre = request.form.get('genre', '').strip()
    album_key = make_album_key(artist, album_title, release_year)
    conn = get_db()
    existing = conn.execute("SELECT album_id FROM album_master WHERE album_key = ? OR ( ? != '' AND apple_collection_id = ? ) LIMIT 1", (album_key, apple_id, apple_id)).fetchone()
    duplicate_items = []
    duplicate_count = 0
    if existing:
        preview_album_id = existing['album_id']
        duplicate_items = conn.execute('SELECT id, management_no, album_id, media_format, artist, album_title, release_year, open_status, signed, purchase_price FROM albums WHERE album_id = ? ORDER BY id ASC', (preview_album_id,)).fetchall()
        duplicate_count = len(duplicate_items)
    else:
        preview_album_id = '신규 자동 생성'
    conn.close()
    selected = {'apple_id': apple_id, 'apple_artist_id': apple_artist_id, 'search_artist': search_artist, 'search_album': search_album, 'artist': artist, 'album_title': album_title, 'release_year': release_year, 'cover_url': cover_url, 'album_type': album_type, 'genre': genre, 'preview_album_id': preview_album_id}
    return render_template('register.html', album=selected, duplicate_count=duplicate_count, duplicate_items=duplicate_items)

def save_album():
    apple_id = request.form.get('apple_id', '').strip()
    apple_artist_id = request.form.get('apple_artist_id', '').strip()
    search_artist = request.form.get('search_artist', '').strip()
    search_album = request.form.get('search_album', '').strip()
    cover_url = request.form.get('cover_url', '').strip()
    classification = request.form.get('classification', '').strip()
    media_format = request.form.get('media_format', '').strip()
    album_type = request.form.get('album_type', '').strip()
    genre = normalize_genres(request.form.getlist('genre'))
    artist = request.form.get('artist', '').strip()
    album_title = request.form.get('album_title', '').strip()
    release_year = request.form.get('release_year', '').strip()
    open_status = request.form.get('open_status', '').strip()
    signed = request.form.get('signed', '').strip()
    purchase_price_text = request.form.get('purchase_price', '').replace(',', '').strip()
    memo = request.form.get('memo', '').strip()
    if not artist or not album_title:
        return redirect(url_for('add_album_page'))

    if classification not in {'한국', '해외'}:
        classification = '한국'
    if media_format not in {'CD', 'LP', 'Casette', 'USB', '기타'}:
        media_format = 'CD'
    if open_status not in {'개봉', '미개봉'}:
        open_status = '개봉'
    if signed not in {'O', 'X'}:
        signed = 'X'

    try:
        purchase_price = int(purchase_price_text)
        if purchase_price < 0:
            purchase_price = None
    except Exception:
        purchase_price = None
    conn = get_db()
    learn_artist_alias(conn, search_artist, artist, apple_artist_id)
    album_id = get_or_create_album_id(conn, apple_id, artist, album_title, release_year, cover_url)
    management_no = get_next_management_no(conn, media_format)
    if album_type not in ALBUM_TYPES:
        album_type = '앨범'
    conn.execute('INSERT INTO albums ( management_no, album_id, classification, media_format, album_type, genre, artist, album_title, release_year, open_status, signed, purchase_price, memo, apple_collection_id, cover_url ) VALUES ( ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ? )', (management_no, album_id, classification, media_format, album_type, genre, artist, album_title, release_year, open_status, signed, purchase_price, memo, apple_id, cover_url))
    conn.commit()
    conn.close()
    clear_search_cache()
    clear_track_caches()
    return redirect(
        url_for(
            'add_album_page',
            saved=management_no
        )
    )

def edit_album(album_row_id):
    updated = request.args.get('updated', '').strip()
    conn = get_db()
    album = conn.execute('SELECT * FROM albums WHERE id = ?', (album_row_id,)).fetchone()
    conn.close()
    if album is None:
        return redirect(url_for('manage_albums'))
    return render_template('edit.html', album=album, updated=updated)

def update_album(album_row_id):
    classification = request.form.get('classification', '').strip()
    media_format = request.form.get('media_format', '').strip()
    album_type = request.form.get('album_type', '').strip()
    genre = normalize_genres(request.form.getlist('genre'))
    artist = request.form.get('artist', '').strip()
    album_title = request.form.get('album_title', '').strip()
    release_year = request.form.get('release_year', '').strip()
    open_status = request.form.get('open_status', '').strip()
    signed = request.form.get('signed', '').strip()
    purchase_price_text = request.form.get('purchase_price', '').replace(',', '').strip()
    memo = request.form.get('memo', '').strip()
    cover_file = request.files.get('cover_file')
    remove_cover = request.form.get('remove_cover', '0')
    try:
        purchase_price = int(purchase_price_text)
    except Exception:
        purchase_price = None
    conn = get_db()
    current = conn.execute('SELECT * FROM albums WHERE id = ?', (album_row_id,)).fetchone()
    if not current:
        conn.close()
        return redirect(url_for('manage_albums'))
    old_cover_url = current['cover_url'] or ''
    new_cover_url = old_cover_url
    if remove_cover == '1':
        new_cover_url = ''
    if cover_file and cover_file.filename:
        uploaded_cover_url = save_uploaded_cover(cover_file)
        if uploaded_cover_url:
            new_cover_url = uploaded_cover_url
    if album_type not in ALBUM_TYPES:
        album_type = '앨범'
    conn.execute('UPDATE albums SET classification = ?, media_format = ?, album_type = ?, genre = ?, artist = ?, album_title = ?, release_year = ?, open_status = ?, signed = ?, purchase_price = ?, memo = ?, cover_url = ? WHERE id = ?', (classification, media_format, album_type, genre, artist, album_title, release_year, open_status, signed, purchase_price, memo, new_cover_url, album_row_id))
    conn.commit()
    management_no = current['management_no']

    old_cover_still_used = 0
    if old_cover_url and old_cover_url != new_cover_url:
        old_cover_still_used = conn.execute(
            'SELECT COUNT(*) AS count FROM albums WHERE cover_url = ?',
            (old_cover_url,)
        ).fetchone()['count']

    conn.close()

    if old_cover_url and old_cover_url != new_cover_url and old_cover_still_used == 0:
        delete_local_cover(old_cover_url)
    clear_track_caches()
    return redirect(
        url_for(
            'edit_album',
            album_row_id=album_row_id,
            updated=management_no
        )
    )

def delete_album(album_row_id):
    conn = get_db()
    album = conn.execute('SELECT * FROM albums WHERE id = ?', (album_row_id,)).fetchone()
    if album is None:
        conn.close()
        return redirect(url_for('manage_albums'))
    management_no = album['management_no']
    cover_url = album['cover_url'] or ''
    album_id = album['album_id']
    conn.execute('DELETE FROM albums WHERE id = ?', (album_row_id,))
    remaining = conn.execute(
        'SELECT COUNT(*) AS count FROM albums WHERE album_id = ?',
        (album_id,)
    ).fetchone()['count']
    if remaining == 0:
        conn.execute('DELETE FROM album_tracks WHERE album_id = ?', (album_id,))
        conn.execute('DELETE FROM album_track_settings WHERE album_id = ?', (album_id,))
    cover_still_used = 0
    if cover_url:
        cover_still_used = conn.execute(
            'SELECT COUNT(*) AS count FROM albums WHERE cover_url = ?',
            (cover_url,)
        ).fetchone()['count']

    conn.commit()
    conn.close()

    if cover_url and cover_still_used == 0:
        delete_local_cover(cover_url)
    clear_track_caches()
    return redirect(url_for('manage_albums', deleted=management_no))
if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=5001, debug=True)


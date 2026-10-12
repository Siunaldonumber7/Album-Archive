from flask import Flask, render_template, request, redirect, url_for, session, abort
import os
import secrets
import hmac
import time
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from PIL import Image, UnidentifiedImageError

from album_database import DB_NAME, DATA_DIR, get_db, init_db
from pagination import (
    ALBUMS_PER_PAGE,
    parse_page_number,
    build_pagination_pages,
)
from catalog import (
    ALBUM_TYPES,
    GENRE_OPTIONS,
    normalize_genre_name,
    split_genres,
    normalize_genres,
    genre_choices,
    extract_apple_genre,
    normalize_apple_album_type,
)

from cache_utils import BoundedCache
from search_engine import (
    normalize_text,
    apple_artwork_size,
    search_apple,
    resolve_apple_music_album_link,
    lookup_album_metadata_store,
    learn_artist_alias,
    clear_search_cache,
)
from track_service import (
    has_manual_track_override,
    get_manual_album_tracks,
    normalize_manual_duration,
    get_album_tracks,
    clear_track_caches,
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

UPLOAD_FOLDER = os.path.join(app.root_path, 'static', 'uploads')
THUMBNAIL_FOLDER = os.path.join(UPLOAD_FOLDER, '.thumbs')
ALLOWED_IMAGE_EXTENSIONS = {'jpg', 'jpeg', 'png', 'webp', 'gif'}
app.config['MAX_CONTENT_LENGTH'] = 15 * 1024 * 1024
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
Image.MAX_IMAGE_PIXELS = 40_000_000
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(THUMBNAIL_FOLDER, exist_ok=True)


ADMIN_ENDPOINTS = {
    'admin_dashboard',
    'manage_albums',
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

app.jinja_env.globals['genre_options_all'] = GENRE_OPTIONS
app.jinja_env.globals['split_genres'] = split_genres
app.jinja_env.globals['genre_choices'] = genre_choices


def allowed_image_file(filename):
    if not filename or '.' not in filename:
        return False
    extension = filename.rsplit('.', 1)[1].lower()
    return extension in ALLOWED_IMAGE_EXTENSIONS

def save_uploaded_cover(file):
    if not file or not file.filename:
        return None
    if not allowed_image_file(file.filename):
        return None

    try:
        file.stream.seek(0)
        with Image.open(file.stream) as image:
            width, height = image.size
            if width < 1 or height < 1 or width * height > 40_000_000:
                return None
            image.verify()
        file.stream.seek(0)
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        return None

    # 원본 파일명(한글/공백 포함)을 최대한 유지하되 경로 문자는 제거한다.
    original_name = os.path.basename(file.filename.replace('\\', '/')).strip()
    original_name = ''.join(
        ch for ch in original_name
        if ch not in {'/', '\\'} and ord(ch) >= 32
    ).strip(' .')

    if not original_name or '.' not in original_name:
        return None

    stem, extension = os.path.splitext(original_name)
    extension = extension.lower()

    # 파일명이 "." 등으로만 구성된 경우를 방지한다.
    stem = stem.strip(' .') or 'cover'
    filename = f'{stem}{extension}'
    save_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)

    # 같은 파일명이 이미 있으면 _2, _3 ... 을 붙여 기존 파일을 덮어쓰지 않는다.
    counter = 2
    while os.path.exists(save_path):
        filename = f'{stem}_{counter}{extension}'
        save_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        counter += 1

    file.save(save_path)
    return url_for('static', filename=f'uploads/{filename}')

def delete_local_cover(cover_url):
    if not cover_url:
        return
    prefix = '/static/uploads/'
    if not cover_url.startswith(prefix):
        return
    filename = cover_url[len(prefix):]
    upload_folder_abs = os.path.abspath(app.config['UPLOAD_FOLDER'])
    file_path = os.path.abspath(os.path.join(upload_folder_abs, filename))
    if not file_path.startswith(upload_folder_abs + os.sep):
        return
    try:
        if os.path.isfile(file_path):
            os.remove(file_path)

        stem = os.path.splitext(os.path.basename(filename))[0].strip(' .') or 'cover'
        if os.path.isdir(THUMBNAIL_FOLDER):
            prefix_name = f'{stem}__thumb_'
            for thumb_name in os.listdir(THUMBNAIL_FOLDER):
                if thumb_name.startswith(prefix_name):
                    thumb_path = os.path.join(THUMBNAIL_FOLDER, thumb_name)
                    if os.path.isfile(thumb_path):
                        os.remove(thumb_path)

    except Exception as e:
        print('LOCAL COVER DELETE ERROR:', e)

def make_album_key(artist, album_title, release_year):
    return normalize_text(artist) + '|' + normalize_text(album_title) + '|' + str(release_year or '').strip()

def local_cover_thumbnail_path(filename, size=400):
    safe_name = os.path.basename(str(filename or '').replace('\\', '/'))
    stem = os.path.splitext(safe_name)[0].strip(' .') or 'cover'
    return os.path.join(
        THUMBNAIL_FOLDER,
        f'{stem}__thumb_{int(size)}.webp'
    )


def cover_thumbnail_url(cover_url, size=400):
    """
    목록 화면용 경량 커버 URL을 반환한다.
    Apple 이미지는 CDN 크기만 줄이고, 직접 업로드한 이미지는
    최초 1회만 WEBP 썸네일을 생성해 재사용한다.
    """
    cover_url = str(cover_url or '').strip()
    if not cover_url:
        return ''

    size = max(120, min(int(size or 400), 800))

    if cover_url.startswith('http://') or cover_url.startswith('https://'):
        return apple_artwork_size(cover_url, size)

    prefix = '/static/uploads/'
    if not cover_url.startswith(prefix):
        return cover_url

    filename = os.path.basename(cover_url[len(prefix):].replace('\\', '/'))
    if not filename:
        return cover_url

    source_path = os.path.abspath(
        os.path.join(app.config['UPLOAD_FOLDER'], filename)
    )
    upload_root = os.path.abspath(app.config['UPLOAD_FOLDER'])

    if not source_path.startswith(upload_root + os.sep):
        return cover_url
    if not os.path.isfile(source_path):
        return cover_url

    thumb_path = local_cover_thumbnail_path(filename, size)

    try:
        if not os.path.isfile(thumb_path):
            os.makedirs(THUMBNAIL_FOLDER, exist_ok=True)

            with Image.open(source_path) as image:
                try:
                    image.seek(0)
                except Exception:
                    pass

                image.thumbnail(
                    (size, size),
                    Image.Resampling.LANCZOS
                )

                if image.mode not in {'RGB', 'RGBA'}:
                    image = image.convert('RGBA' if 'transparency' in image.info else 'RGB')

                image.save(
                    thumb_path,
                    'WEBP',
                    quality=82,
                    method=4
                )

        thumb_name = os.path.basename(thumb_path)
        return url_for(
            'static',
            filename=f'uploads/.thumbs/{thumb_name}'
        )

    except Exception as e:
        print('COVER THUMBNAIL ERROR:', e)
        return cover_url


app.jinja_env.globals['cover_thumbnail_url'] = cover_thumbnail_url


def get_management_prefix(media_format):
    if media_format == 'CD':
        return 'CD'
    if media_format == 'LP':
        return 'LP'
    return 'ETC'

def get_next_management_no(conn, media_format):
    prefix = get_management_prefix(media_format)
    counter_type = f'MANAGEMENT_{prefix}'
    row = conn.execute('SELECT last_number FROM number_counters WHERE counter_type = ?', (counter_type,)).fetchone()
    if row:
        next_number = row['last_number'] + 1
        conn.execute('UPDATE number_counters SET last_number = ? WHERE counter_type = ?', (next_number, counter_type))
    else:
        next_number = 1
        conn.execute('INSERT INTO number_counters ( counter_type, last_number ) VALUES (?, ?)', (counter_type, next_number))
    return f'{prefix}-{next_number:04d}'

def get_or_create_album_id(conn, apple_id, artist, album_title, release_year, cover_url):
    album_key = make_album_key(artist, album_title, release_year)
    existing = conn.execute("SELECT * FROM album_master WHERE album_key = ? OR ( ? != '' AND apple_collection_id = ? ) LIMIT 1", (album_key, apple_id, apple_id)).fetchone()
    if existing:
        return existing['album_id']
    cursor = conn.execute('INSERT INTO album_master ( album_key, apple_collection_id, artist, album_title, release_year, cover_url ) VALUES (?, ?, ?, ?, ?, ?)', (album_key, apple_id, artist, album_title, release_year, cover_url))
    master_id = cursor.lastrowid
    album_id = f'ALB-{master_id:04d}'
    conn.execute('UPDATE album_master SET album_id = ? WHERE id = ?', (album_id, master_id))
    return album_id

@app.route('/')
def index():
    library_q = request.args.get('library_q', '').strip()
    classification_filter = request.args.get('classification', '').strip()
    format_filter = request.args.get('media_format', '').strip()
    album_type_filter = request.args.get('album_type', '').strip()
    genre_filter = request.args.get('genre', '').strip()
    page = parse_page_number(request.args.get('page', '1'))
    saved = request.args.get('saved', '')
    bulk_saved = request.args.get('bulk_saved', '')

    conn = get_db()
    conditions = []
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

    query = (
        'SELECT * FROM albums'
        + where_sql
        + ' ORDER BY id DESC LIMIT ? OFFSET ?'
    )
    albums = conn.execute(
        query,
        params + [ALBUMS_PER_PAGE, offset]
    ).fetchall()

    total_album_count = conn.execute(
        'SELECT COUNT(*) AS count FROM albums'
    ).fetchone()['count']

    genre_rows = conn.execute(
        "SELECT genre FROM albums WHERE genre IS NOT NULL AND TRIM(genre) != ''"
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


@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if session.get('admin_authenticated'):
        return redirect(url_for('admin_dashboard'))

    error = ''

    forwarded_for = request.headers.get('X-Forwarded-For', '')
    client_key = (
        forwarded_for.split(',')[0].strip()
        if forwarded_for
        else (request.remote_addr or 'unknown')
    )

    attempt = LOGIN_ATTEMPTS.get(
        client_key,
        {'count': 0, 'started_at': time.time()}
    )

    if time.time() - attempt.get('started_at', 0) > 900:
        attempt = {'count': 0, 'started_at': time.time()}
        LOGIN_ATTEMPTS[client_key] = attempt

    if request.method == 'POST':
        if attempt.get('count', 0) >= 5:
            error = 'PIN 입력을 여러 번 실패했습니다. 15분 뒤 다시 시도해 주세요.'
            return render_template('admin_login.html', error=error), 429

        submitted_pin = str(request.form.get('pin', '') or '').strip()

        if submitted_pin and hmac.compare_digest(submitted_pin, ADMIN_PIN):
            LOGIN_ATTEMPTS.pop(client_key, None)
            session.clear()
            session['admin_authenticated'] = True
            session.permanent = True
            session['_csrf_token'] = secrets.token_urlsafe(32)
            return redirect(url_for('admin_dashboard'))

        attempt['count'] = attempt.get('count', 0) + 1
        LOGIN_ATTEMPTS[client_key] = attempt
        remaining = max(0, 5 - attempt['count'])
        error = f'PIN이 올바르지 않습니다. 남은 시도 {remaining}회'

    return render_template('admin_login.html', error=error)


@app.route('/admin/logout', methods=['POST'])
def admin_logout():
    session.clear()
    return redirect(url_for('index'))


@app.route('/admin')
def admin_dashboard():
    conn = get_db()
    stats = {
        'total': conn.execute("SELECT COUNT(*) AS count FROM albums").fetchone()['count'],
        'korean': conn.execute("SELECT COUNT(*) AS count FROM albums WHERE classification = '한국'").fetchone()['count'],
        'overseas': conn.execute("SELECT COUNT(*) AS count FROM albums WHERE classification = '해외'").fetchone()['count'],
        'cd': conn.execute("SELECT COUNT(*) AS count FROM albums WHERE media_format = 'CD'").fetchone()['count'],
        'lp': conn.execute("SELECT COUNT(*) AS count FROM albums WHERE media_format = 'LP'").fetchone()['count'],
    }
    recent_albums = conn.execute(
        "SELECT id, management_no, artist, album_title, cover_url FROM albums ORDER BY id DESC LIMIT 5"
    ).fetchall()
    conn.close()
    return render_template('admin.html', stats=stats, recent_albums=recent_albums)


@app.route('/manage')
def manage_albums():
    library_q = request.args.get('library_q', '').strip()
    classification_filter = request.args.get('classification', '').strip()
    format_filter = request.args.get('media_format', '').strip()
    status_filter = request.args.get('open_status', '').strip()
    signed_filter = request.args.get('signed', '').strip()
    sort_by = request.args.get('sort_by', 'id').strip()
    sort_order = request.args.get(
        'sort_order',
        'desc'
    ).strip().lower()
    page = parse_page_number(request.args.get('page', '1'))
    updated = request.args.get('updated', '')
    deleted = request.args.get('deleted', '')

    conn = get_db()
    conditions = []
    params = []

    if library_q:
        value = f'%{library_q}%'
        conditions.append(
            '( management_no LIKE ? OR album_id LIKE ? OR artist LIKE ? OR album_title LIKE ? OR release_year LIKE ? OR memo LIKE ? )'
        )
        params.extend(
            [value, value, value, value, value, value]
        )

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

    if status_filter:
        conditions.append('open_status = ?')
        params.append(status_filter)

    if signed_filter:
        conditions.append('signed = ?')
        params.append(signed_filter)

    sort_columns = {
        'id': 'id',
        'management_no': 'management_no COLLATE NOCASE',
        'artist': 'artist COLLATE NOCASE',
        'album_title': 'album_title COLLATE NOCASE',
        'release_year': 'release_year',
        'purchase_price': 'purchase_price'
    }

    if sort_by not in sort_columns:
        sort_by = 'id'

    if sort_order not in {'asc', 'desc'}:
        sort_order = 'desc'

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

    query = (
        'SELECT * FROM albums'
        + where_sql
        + f" ORDER BY {sort_columns[sort_by]} {sort_order.upper()}, id DESC"
        + ' LIMIT ? OFFSET ?'
    )
    albums = conn.execute(
        query,
        params + [ALBUMS_PER_PAGE, offset]
    ).fetchall()

    total_album_count = conn.execute(
        'SELECT COUNT(*) AS count FROM albums'
    ).fetchone()['count']

    conn.close()

    return render_template(
        'manage.html',
        albums=albums,
        total_album_count=total_album_count,
        filtered_album_count=filtered_album_count,
        library_q=library_q,
        classification_filter=classification_filter,
        format_filter=format_filter,
        status_filter=status_filter,
        signed_filter=signed_filter,
        sort_by=sort_by,
        sort_order=sort_order,
        page=page,
        total_pages=total_pages,
        pagination_pages=build_pagination_pages(
            page,
            total_pages
        ),
        updated=updated,
        deleted=deleted
    )


@app.route('/add')
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


@app.route('/add-link', methods=['POST'])
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


@app.route('/add-manual', methods=['POST'])
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


@app.route('/register-bulk', methods=['POST'])
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


@app.route('/save-bulk', methods=['POST'])
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


@app.route('/register', methods=['POST'])
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

@app.route('/save', methods=['POST'])
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

@app.route('/edit/<int:album_row_id>')
def edit_album(album_row_id):
    updated = request.args.get('updated', '').strip()
    conn = get_db()
    album = conn.execute('SELECT * FROM albums WHERE id = ?', (album_row_id,)).fetchone()
    conn.close()
    if album is None:
        return redirect(url_for('manage_albums'))
    return render_template('edit.html', album=album, updated=updated)

@app.route('/update/<int:album_row_id>', methods=['POST'])
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

@app.route('/delete/<int:album_row_id>', methods=['POST'])
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

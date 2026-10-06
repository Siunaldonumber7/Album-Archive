from flask import Flask, render_template, request, redirect, url_for, session, abort
import base64
import html as html_lib
import json
import os
import re
import sqlite3
import secrets
import hmac
import time
import threading
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from html.parser import HTMLParser
from urllib.parse import unquote, urlparse
import requests
from PIL import Image, UnidentifiedImageError
app = Flask(__name__)
DB_NAME = os.environ.get('ALBUM_DB_PATH', 'albums.db')
DATA_DIR = os.path.dirname(os.path.abspath(DB_NAME))
os.makedirs(DATA_DIR, exist_ok=True)


class BoundedCache(dict):
    """작은 개인 서버에서 캐시가 무한정 커지지 않도록 개수를 제한한다."""

    def __init__(self, max_items):
        super().__init__()
        self.max_items = max_items
        self._lock = threading.RLock()

    def __setitem__(self, key, value):
        with self._lock:
            if key in self:
                super().__delitem__(key)
            super().__setitem__(key, value)
            while len(self) > self.max_items:
                oldest_key = next(iter(self))
                super().__delitem__(oldest_key)

    def get(self, key, default=None):
        with self._lock:
            return super().get(key, default)

    def clear(self):
        with self._lock:
            return super().clear()


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

APPLE_SESSION = requests.Session()
APPLE_WEB_HEADERS = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36', 'Accept-Language': 'ko-KR,ko;q=0.9,en;q=0.6', 'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'}

SEARCH_CACHE = BoundedCache(300)
CACHE_SECONDS = 1800
TRACK_CACHE = BoundedCache(500)
TRACK_CACHE_SECONDS = 3600
APPLE_PAGE_CACHE = BoundedCache(300)
APPLE_PAGE_CACHE_SECONDS = 3600
KR_TITLE_CACHE = BoundedCache(1000)
LOGIN_ATTEMPTS = BoundedCache(500)

# =========================================================
# MusicBrainz - 인디/한글 아티스트 공식명·별칭 보조 검색
# =========================================================

MUSICBRAINZ_SESSION = requests.Session()
MUSICBRAINZ_SESSION.headers.update({
    'User-Agent': 'MyAlbumArchive/1.0 (personal music archive)',
    'Accept': 'application/json'
})
MUSICBRAINZ_CACHE = BoundedCache(300)
MUSICBRAINZ_CACHE_SECONDS = 86400
MUSICBRAINZ_RATE_LOCK = threading.Lock()
MUSICBRAINZ_LAST_REQUEST = 0.0
UPLOAD_FOLDER = os.path.join(app.root_path, 'static', 'uploads')
ALLOWED_IMAGE_EXTENSIONS = {'jpg', 'jpeg', 'png', 'webp', 'gif'}
app.config['MAX_CONTENT_LENGTH'] = 15 * 1024 * 1024
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
Image.MAX_IMAGE_PIXELS = 40_000_000
os.makedirs(UPLOAD_FOLDER, exist_ok=True)


ADMIN_ENDPOINTS = {
    'admin_dashboard',
    'manage_albums',
    'add_album_page',
    'add_album_from_link',
    'add_album_manual',
    'register',
    'save_album',
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

def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.execute('CREATE TABLE IF NOT EXISTS albums ( id INTEGER PRIMARY KEY AUTOINCREMENT, management_no TEXT UNIQUE NOT NULL, album_id TEXT NOT NULL, classification TEXT, media_format TEXT, album_type TEXT, genre TEXT, artist TEXT NOT NULL, album_title TEXT NOT NULL, release_year TEXT, open_status TEXT, signed TEXT, purchase_price INTEGER, memo TEXT, apple_collection_id TEXT, cover_url TEXT )')
    existing_columns = {row['name'] for row in conn.execute('PRAGMA table_info(albums)').fetchall()}
    if 'album_type' not in existing_columns:
        conn.execute('ALTER TABLE albums ADD COLUMN album_type TEXT')
    if 'genre' not in existing_columns:
        conn.execute('ALTER TABLE albums ADD COLUMN genre TEXT')
    conn.execute('CREATE TABLE IF NOT EXISTS album_master ( id INTEGER PRIMARY KEY AUTOINCREMENT, album_id TEXT UNIQUE, album_key TEXT UNIQUE, apple_collection_id TEXT, artist TEXT, album_title TEXT, release_year TEXT, cover_url TEXT )')
    conn.execute('CREATE TABLE IF NOT EXISTS number_counters ( counter_type TEXT PRIMARY KEY, last_number INTEGER NOT NULL DEFAULT 0 )')
    conn.execute('CREATE TABLE IF NOT EXISTS artist_aliases ( id INTEGER PRIMARY KEY AUTOINCREMENT, alias_key TEXT UNIQUE NOT NULL, alias_text TEXT, canonical_name TEXT, apple_artist_id TEXT )')
    conn.execute('CREATE TABLE IF NOT EXISTS album_tracks ( id INTEGER PRIMARY KEY AUTOINCREMENT, album_id TEXT NOT NULL, disc_number INTEGER NOT NULL DEFAULT 1, track_number INTEGER NOT NULL DEFAULT 1, title TEXT NOT NULL, artist TEXT, duration TEXT )')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_album_tracks_album_id ON album_tracks (album_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_albums_album_id ON albums (album_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_albums_classification ON albums (classification)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_albums_media_format ON albums (media_format)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_albums_album_type ON albums (album_type)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_albums_genre ON albums (genre)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_albums_artist ON albums (artist)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_albums_release_year ON albums (release_year)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_albums_apple_collection_id ON albums (apple_collection_id)')
    conn.execute('CREATE TABLE IF NOT EXISTS album_track_settings ( album_id TEXT PRIMARY KEY, manual_override INTEGER NOT NULL DEFAULT 0, updated_at TEXT DEFAULT CURRENT_TIMESTAMP )')
    conn.commit()
    conn.close()


ALBUM_TYPES = ('앨범', 'EP(미니)', '싱글', '믹스테잎')


def normalize_genre_name(value):
    value = str(value or '').strip()
    if not value:
        return ''

    # Apple Music / iTunes의 영문 장르명을 컬렉션에서 보기 편한
    # 한글 표기로 최대한 통일한다. 널리 쓰이는 약어(R&B 등)는 유지한다.
    genre_map = {
        'hip-hop/rap': '힙합',
        'hip hop/rap': '힙합',
        'hip-hop': '힙합',
        'hip hop': '힙합',
        'rap': '힙합',

        'r&b/soul': 'R&B',
        'r&b': 'R&B',
        'soul': '소울',

        'pop': 'Pop',
        'k-pop': 'K-Pop',
        'kpop': 'K-Pop',
        'j-pop': 'J-Pop',
        'jpop': 'J-Pop',
        'mandopop': '만도팝',
        'cantopop': '칸토팝',
        'french pop': '프렌치 팝',
        'german pop': '저먼 팝',

        'rock': '록',
        'indie rock': '인디 록',
        'alternative': '얼터너티브',
        'alternative & indie': '얼터너티브/인디',
        'alternative/indie': '얼터너티브/인디',
        'indie': '인디',
        'punk': '펑크',
        'punk rock': '펑크 록',
        'metal': '메탈',
        'hard rock': '하드 록',
        'progressive rock': '프로그레시브 록',
        'psychedelic': '사이키델릭',

        'electronic': '일렉트로닉',
        'electronica': '일렉트로니카',
        'dance': '댄스',
        'house': '하우스',
        'techno': '테크노',
        'trance': '트랜스',
        'ambient': '앰비언트',
        'downtempo': '다운템포',

        'jazz': '재즈',
        'blues': '블루스',
        'folk': '포크',
        'country': '컨트리',
        'reggae': '레게',
        'ska': '스카',
        'funk': '펑크(Funk)',
        'disco': '디스코',

        'singer/songwriter': '싱어송라이터',
        'singer-songwriter': '싱어송라이터',
        'vocal': '보컬',
        'easy listening': '이지 리스닝',
        'adult contemporary': '어덜트 컨템포러리',

        'classical': '클래식',
        'opera': '오페라',
        'new age': '뉴에이지',
        'soundtrack': '사운드트랙',
        'original score': '영화음악',
        'musicals': '뮤지컬',

        'latin': '라틴',
        'brazilian': '브라질',
        'african': '아프리카',
        'world': '월드뮤직',
        'worldwide': '월드뮤직',
        'international': '월드뮤직',

        'christian & gospel': 'CCM/가스펠',
        'christian': 'CCM',
        'gospel': '가스펠',

        "children's music": '어린이 음악',
        'children': '어린이 음악',
        'holiday': '홀리데이',
        'christmas': '크리스마스',
        'comedy': '코미디',
        'spoken word': '스포큰 워드',
        'fitness & workout': '피트니스',
        'disney': '디즈니',

        'indian': '인도 음악',
        'bollywood': '볼리우드',
        'korean': '한국 음악',
        'japanese': '일본 음악',
    }

    return genre_map.get(value.lower(), value)


def extract_apple_genre(item):
    if not isinstance(item, dict):
        return ''

    attrs = item.get('attributes') if isinstance(item.get('attributes'), dict) else {}
    genre_names = attrs.get('genreNames') or item.get('genreNames')

    if isinstance(genre_names, list):
        cleaned = [str(v or '').strip() for v in genre_names if str(v or '').strip()]
        for value in cleaned:
            if value.lower() not in {'music', '음악'}:
                return normalize_genre_name(value)
        if cleaned:
            return normalize_genre_name(cleaned[0])

    raw = (
        item.get('primaryGenreName')
        or attrs.get('primaryGenreName')
        or item.get('genre')
        or attrs.get('genre')
        or ''
    )

    if isinstance(raw, list):
        raw = next((str(v or '').strip() for v in raw if str(v or '').strip()), '')

    return normalize_genre_name(raw)


def normalize_apple_album_type(item, title=''):
    item = item if isinstance(item, dict) else {}
    attrs = item.get('attributes') if isinstance(item.get('attributes'), dict) else {}
    title = str(title or item.get('collectionName') or item.get('name') or attrs.get('name') or '').strip()
    raw = ' '.join(str(v or '') for v in [
        item.get('collectionType'), item.get('kind'), item.get('albumType'),
        attrs.get('collectionType'), attrs.get('kind'), attrs.get('albumType')
    ]).lower()
    title_lower = title.lower()
    if 'mixtape' in raw or 'mix tape' in raw or re.search(r'\bmixtape\b', title_lower):
        return '믹스테잎'
    if raw.strip() == 'ep' or 'extended play' in raw or re.search(r'[-–—]\s*ep\s*$', title_lower) or re.search(r'\(ep\)\s*$', title_lower):
        return 'EP(미니)'
    is_single = item.get('isSingle')
    if is_single is None:
        is_single = attrs.get('isSingle')
    if is_single is True or 'single' in raw or re.search(r'[-–—]\s*single\s*$', title_lower):
        return '싱글'
    return '앨범'

def has_manual_track_override(conn, album_id):
    if not album_id:
        return False
    row = conn.execute(
        'SELECT manual_override FROM album_track_settings WHERE album_id = ? LIMIT 1',
        (album_id,)
    ).fetchone()
    return bool(row and row['manual_override'])


def get_manual_album_tracks(conn, album_id):
    if not album_id:
        return []
    rows = conn.execute(
        '''
        SELECT disc_number, track_number, title, artist, duration
        FROM album_tracks
        WHERE album_id = ?
        ORDER BY disc_number ASC, track_number ASC, id ASC
        ''',
        (album_id,)
    ).fetchall()
    return [
        {
            'disc_number': int(row['disc_number'] or 1),
            'track_number': int(row['track_number'] or 1),
            'title': row['title'] or '',
            'artist': row['artist'] or '',
            'duration': row['duration'] or ''
        }
        for row in rows
    ]


def normalize_manual_duration(value):
    value = str(value or '').strip()
    if not value:
        return ''

    # 3:45 / 1:03:22 형태만 저장한다.
    parts = value.split(':')
    if len(parts) not in {2, 3} or not all(part.isdigit() for part in parts):
        return ''

    numbers = [int(part) for part in parts]

    if len(numbers) == 2:
        minutes, seconds = numbers
        if seconds >= 60:
            return ''
        return f'{minutes}:{seconds:02d}'

    hours, minutes, seconds = numbers
    if minutes >= 60 or seconds >= 60:
        return ''
    return f'{hours}:{minutes:02d}:{seconds:02d}'


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
    except Exception as e:
        print('LOCAL COVER DELETE ERROR:', e)

def normalize_text(text):
    if not text:
        return ''
    text = str(text).lower().strip()
    return re.sub('[^a-z0-9가-힣ぁ-んァ-ン一-龥]+', '', text)

def contains_hangul(text):
    if not text:
        return False
    return bool(re.search('[가-힣]', str(text)))

def make_album_key(artist, album_title, release_year):
    return normalize_text(artist) + '|' + normalize_text(album_title) + '|' + str(release_year or '').strip()

def similarity(a, b):
    a = normalize_text(a)
    b = normalize_text(b)
    if not a or not b:
        return 0
    return SequenceMatcher(None, a, b).ratio()

def musicbrainz_wait_for_rate_limit():
    global MUSICBRAINZ_LAST_REQUEST

    with MUSICBRAINZ_RATE_LOCK:
        now = time.monotonic()
        elapsed = now - MUSICBRAINZ_LAST_REQUEST
        wait_seconds = 1.05 - elapsed

        if wait_seconds > 0:
            time.sleep(wait_seconds)

        MUSICBRAINZ_LAST_REQUEST = time.monotonic()


def search_musicbrainz_artist_names(artist_name):
    """
    Apple KR에서 한글 아티스트를 못 찾았을 때만 사용하는 보조 resolver.

    MusicBrainz 검색은 기본적으로 artist / alias / sortname을 함께 검색한다.
    반환값은 Apple에 다시 넣어볼 공식명/별칭 후보 리스트이다.
    """

    artist_name = (artist_name or '').strip()

    if not artist_name:
        return []

    cache_key = normalize_text(artist_name)
    cached = MUSICBRAINZ_CACHE.get(cache_key)

    if (
        cached
        and time.time() - cached['time'] < MUSICBRAINZ_CACHE_SECONDS
    ):
        return cached['names']

    try:
        musicbrainz_wait_for_rate_limit()

        response = MUSICBRAINZ_SESSION.get(
            'https://musicbrainz.org/ws/2/artist/',
            params={
                'query': artist_name,
                'fmt': 'json',
                'limit': 10
            },
            timeout=10
        )

        response.raise_for_status()
        data = response.json()
        artists = data.get('artists', [])

    except Exception as e:
        print('MUSICBRAINZ ARTIST SEARCH ERROR:', e)
        MUSICBRAINZ_CACHE[cache_key] = {
            'time': time.time(),
            'names': []
        }
        return []

    ranked = []
    target = normalize_text(artist_name)

    for item in artists:
        official_name = (item.get('name') or '').strip()
        sort_name = (item.get('sort-name') or '').strip()
        country = (item.get('country') or '').upper()
        mb_score = int(item.get('score') or 0)
        aliases = item.get('aliases') or []

        score = mb_score

        if country == 'KR':
            score += 30

        if official_name and normalize_text(official_name) == target:
            score += 80

        alias_names = []
        exact_alias_match = False

        for alias in aliases:
            if isinstance(alias, dict):
                alias_name = (alias.get('name') or '').strip()
                locale = (alias.get('locale') or '').lower()
                primary = bool(alias.get('primary'))
            else:
                alias_name = str(alias).strip()
                locale = ''
                primary = False

            if not alias_name:
                continue

            alias_names.append((alias_name, locale, primary))

            if normalize_text(alias_name) == target:
                exact_alias_match = True
                score += 70

                if locale.startswith('ko'):
                    score += 20

        if exact_alias_match:
            score += 30

        ranked.append({
            'score': score,
            'official_name': official_name,
            'sort_name': sort_name,
            'aliases': alias_names,
            'country': country,
            'mbid': item.get('id', '')
        })

    ranked.sort(
        key=lambda item: item['score'],
        reverse=True
    )

    candidate_names = []
    seen_names = set()

    def add_candidate(name):
        name = (name or '').strip()

        if not name:
            return

        key = normalize_text(name)

        if not key or key == target or key in seen_names:
            return

        seen_names.add(key)
        candidate_names.append(name)

    # 상위 후보 몇 명만 사용해 엉뚱한 동명이인 검색을 줄인다.
    for item in ranked[:4]:
        # 공식 활동명을 우선한다.
        add_candidate(item['official_name'])

        # 영문/로마자 alias를 그 다음으로 시도한다.
        non_hangul_aliases = []
        hangul_aliases = []

        for alias_name, locale, primary in item['aliases']:
            if contains_hangul(alias_name):
                hangul_aliases.append((alias_name, locale, primary))
            else:
                non_hangul_aliases.append((alias_name, locale, primary))

        non_hangul_aliases.sort(
            key=lambda value: (
                not value[2],
                0 if value[1].startswith('en') else 1,
                len(value[0])
            )
        )

        for alias_name, _, _ in non_hangul_aliases[:4]:
            add_candidate(alias_name)

        # 공식명이 한글인데 별도의 영문명이 sort-name에 있을 경우 보조한다.
        if item['sort_name'] and not contains_hangul(item['sort_name']):
            add_candidate(item['sort_name'])

        # 다른 한글 표기도 마지막 후보로 둔다.
        for alias_name, _, _ in hangul_aliases[:2]:
            add_candidate(alias_name)

        if len(candidate_names) >= 10:
            break

    candidate_names = candidate_names[:10]

    MUSICBRAINZ_CACHE[cache_key] = {
        'time': time.time(),
        'names': candidate_names
    }

    print(
        'MUSICBRAINZ ARTIST CANDIDATES:',
        artist_name,
        '→',
        candidate_names
    )

    return candidate_names


def search_musicbrainz_artist_on_apple(artist_name, album=''):
    """
    MusicBrainz는 이름/별칭 해결만 담당한다.
    이 단계에서는 Apple KR만 검색하고, 해외 스토어 fallback은
    search_apple()의 마지막 단계에서 별도로 처리한다.
    """

    candidate_names = search_musicbrainz_artist_names(artist_name)

    if not candidate_names:
        return [], ''

    for candidate_name in candidate_names:
        print(
            'MUSICBRAINZ → APPLE KR:',
            artist_name,
            '→',
            candidate_name
        )

        results = search_artist_albums_in_stores(
            candidate_name,
            album,
            ['KR'],
            prefer_korean=True
        )

        if results:
            return results, candidate_name

    return [], ''

def find_learned_artist(artist_name):
    if not artist_name:
        return None
    alias_key = normalize_text(artist_name)
    conn = get_db()
    result = conn.execute('SELECT canonical_name, apple_artist_id FROM artist_aliases WHERE alias_key = ? LIMIT 1', (alias_key,)).fetchone()
    conn.close()
    if not result:
        return None
    return {'artist_name': result['canonical_name'], 'artist_id': result['apple_artist_id']}

def learn_artist_alias(conn, search_artist, canonical_name, apple_artist_id):
    if not canonical_name or not apple_artist_id:
        return
    aliases = []
    if search_artist:
        aliases.append(search_artist.strip())
    aliases.append(canonical_name.strip())
    for alias in aliases:
        alias_key = normalize_text(alias)
        if not alias_key:
            continue
        conn.execute('INSERT INTO artist_aliases ( alias_key, alias_text, canonical_name, apple_artist_id ) VALUES (?, ?, ?, ?) ON CONFLICT(alias_key) DO UPDATE SET canonical_name = excluded.canonical_name, apple_artist_id = excluded.apple_artist_id', (alias_key, alias, canonical_name, apple_artist_id))

def apple_high_res(url):
    if not url:
        return ''
    return url.replace('100x100bb', '1000x1000bb').replace('100x100-75', '1000x1000-75')


def apple_artwork_size(url, size=400):
    url = str(url or '').strip()
    if not url:
        return ''
    url = re.sub(r'\d+x\d+bb', f'{size}x{size}bb', url)
    url = re.sub(r'\d+x\d+-75', f'{size}x{size}-75', url)
    return url

def get_explicit_label(item):
    value = item.get('collectionExplicitness', '')
    if value == 'explicit':
        return 'Explicit'
    if value == 'cleaned':
        return 'Clean'
    return ''

def format_track_duration(milliseconds):
    try:
        milliseconds = int(milliseconds)
    except Exception:
        return ''
    if milliseconds <= 0:
        return ''
    total_seconds = milliseconds // 1000
    minutes = total_seconds // 60
    seconds = total_seconds % 60
    return f'{minutes}:{seconds:02d}'

class AppleMusicHTMLParser(HTMLParser):

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title_parts = []
        self.in_title = False
        self.meta_titles = {}
        self.meta_values = {}
        self.current_script = None
        self.current_script_buffer = []
        self.serialized_scripts = []
        self.ld_json_scripts = []

    def handle_starttag(self, tag, attrs):
        attrs_dict = {key: value or '' for key, value in attrs}
        tag = tag.lower()
        if tag == 'title':
            self.in_title = True
            return
        if tag == 'meta':
            key = (attrs_dict.get('property') or attrs_dict.get('name') or '').lower()
            content = attrs_dict.get('content', '')
            if key and content:
                self.meta_values[key] = content
            if key in {'og:title', 'twitter:title', 'apple:title'} and content:
                self.meta_titles[key] = content
            return
        if tag == 'script':
            script_id = attrs_dict.get('id', '')
            script_type = attrs_dict.get('type', '').lower()
            if script_id == 'serialized-server-data':
                self.current_script = 'serialized'
                self.current_script_buffer = []
            elif script_type == 'application/ld+json':
                self.current_script = 'ldjson'
                self.current_script_buffer = []

    def handle_data(self, data):
        if self.in_title:
            self.title_parts.append(data)
        if self.current_script:
            self.current_script_buffer.append(data)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == 'title':
            self.in_title = False
            return
        if tag == 'script' and self.current_script:
            content = ''.join(self.current_script_buffer).strip()
            if content:
                if self.current_script == 'serialized':
                    self.serialized_scripts.append(content)
                elif self.current_script == 'ldjson':
                    self.ld_json_scripts.append(content)
            self.current_script = None
            self.current_script_buffer = []

    @property
    def page_title(self):
        return ''.join(self.title_parts).strip()

def parse_apple_music_html(html_text):
    parser = AppleMusicHTMLParser()
    try:
        parser.feed(html_text)
    except Exception as e:
        print('APPLE HTML PARSER ERROR:', e)
    return parser

def normalize_apple_music_kr_urls(collection_id, view_url=''):
    collection_id = str(collection_id or '').strip()
    view_url = str(view_url or '').strip()
    urls = []
    if view_url:
        try:
            parsed = urlparse(view_url)
            path_parts = [part for part in parsed.path.split('/') if part]
            if len(path_parts) >= 3:
                if path_parts[0].lower() in {'kr', 'us', 'jp', 'gb', 'ca', 'au'}:
                    remainder = '/'.join(path_parts[1:])
                else:
                    remainder = '/'.join(path_parts)
                if remainder.startswith('album/'):
                    urls.append('https://music.apple.com/kr/' + remainder)
        except Exception:
            pass
    if collection_id:
        urls.append(f'https://music.apple.com/kr/album/x/{collection_id}')
    seen = set()
    result = []
    for url in urls:
        if url and url not in seen:
            seen.add(url)
            result.append(url)
    return result

def fetch_apple_music_kr_page(collection_id, view_url=''):
    collection_id = str(collection_id or '').strip()
    cache_key = collection_id or str(view_url or '').strip()
    if cache_key:
        cached = APPLE_PAGE_CACHE.get(cache_key)
        if cached and time.time() - cached['time'] < APPLE_PAGE_CACHE_SECONDS:
            return cached['data']
    urls = normalize_apple_music_kr_urls(collection_id, view_url)
    for url in urls:
        try:
            response = requests.get(url, headers=APPLE_WEB_HEADERS, timeout=10, allow_redirects=True)
            if response.status_code != 200:
                continue
            html_text = response.content.decode('utf-8', errors='replace')
            if not html_text:
                continue
            result = {'url': response.url, 'html': html_text, 'parser': parse_apple_music_html(html_text)}
            if cache_key:
                APPLE_PAGE_CACHE[cache_key] = {'time': time.time(), 'data': result}
            return result
        except Exception as e:
            print('APPLE KR PAGE ERROR:', e)
    return None

def clean_apple_music_page_title(raw_title, fallback_title=''):
    """Apple Music KR 페이지의 여러 제목 포맷에서 '앨범명'만 추출한다.

    예:
      '하나에게. - EP - 김뜻돌의 앨범 - Apple Music'
        -> '하나에게. - EP'
      'Apple Music에서 감상하는 김뜻돌의 하나에게. - EP'
        -> '하나에게. - EP'
    """
    if not raw_title:
        return fallback_title

    title = html_lib.unescape(str(raw_title))
    title = (
        title
        .replace('\u200e', '')
        .replace('\u200f', '')
        .replace('\ufeff', '')
        .replace('\xa0', ' ')
        .strip()
    )
    title = re.sub(r'\s+', ' ', title).strip()

    # 1) 현재 KR Apple Music og:title 형태
    #    'Apple Music에서 감상하는 김뜻돌의 하나에게. - EP'
    marketing_prefix = 'Apple Music에서 감상하는 '
    if title.startswith(marketing_prefix):
        remainder = title[len(marketing_prefix):].strip()

        # '아티스트의 앨범명'에서 첫 번째 소유격 뒤를 앨범명으로 사용.
        # 일반적인 국내 아티스트명에서는 이 패턴이 가장 안정적이다.
        match = re.match(r'^.+?의\s+(.+)$', remainder)
        if match:
            candidate = match.group(1).strip()
            if candidate:
                return candidate

        # 패턴 분리가 안 되면 마케팅 문구만이라도 제거
        if remainder:
            return remainder

    # 2) <title> 형태
    #    '하나에게. - EP - 김뜻돌의 앨범 - Apple Music'
    title = re.sub(
        r'\s*-\s*Apple\s*Music\s*$',
        '',
        title,
        flags=re.I
    ).strip()

    # '<앨범명> - <아티스트>의 앨범'에서 마지막 설명 부분만 제거한다.
    # rsplit을 써야 '하나에게. - EP'의 '- EP'가 보존된다.
    title_parts = title.rsplit(' - ', 1)
    if (
        len(title_parts) == 2
        and re.search(r'의\s*앨범\s*$', title_parts[1])
    ):
        title = title_parts[0].strip()

    english_parts = title.rsplit(' - ', 1)
    if (
        len(english_parts) == 2
        and re.match(r'^Album\s+by\s+.+$', english_parts[1], flags=re.I)
    ):
        title = english_parts[0].strip()

    # 혹시 위 문구가 뒤늦게 남아 있는 경우 한 번 더 제거
    if title.startswith(marketing_prefix):
        remainder = title[len(marketing_prefix):].strip()
        match = re.match(r'^.+?의\s+(.+)$', remainder)
        if match:
            candidate = match.group(1).strip()
            if candidate:
                return candidate
        title = remainder

    return title or fallback_title

def get_kr_localized_album_title(collection_id, view_url='', fallback_title=''):
    collection_id = str(collection_id or '').strip()
    cache_key = collection_id or view_url or fallback_title

    if cache_key in KR_TITLE_CACHE:
        return KR_TITLE_CACHE[cache_key]

    page = fetch_apple_music_kr_page(collection_id, view_url)

    if not page:
        KR_TITLE_CACHE[cache_key] = fallback_title
        return fallback_title

    parser = page['parser']

    # <title>이 대체로 가장 깔끔하다.
    # og:title은 'Apple Music에서 감상하는 ...' 마케팅 문구가 붙는 경우가 있다.
    title_candidates = [
        parser.page_title,
        parser.meta_titles.get('apple:title', ''),
        parser.meta_titles.get('og:title', ''),
        parser.meta_titles.get('twitter:title', ''),
    ]

    for raw_title in title_candidates:
        if not raw_title:
            continue

        localized = clean_apple_music_page_title(
            raw_title,
            fallback_title
        )

        # 마케팅 문구가 남아 있으면 다음 후보를 시도한다.
        if localized.startswith('Apple Music에서 감상하는 '):
            continue

        if localized:
            KR_TITLE_CACHE[cache_key] = localized
            print(
                'KR LOCALIZED TITLE:',
                fallback_title,
                '→',
                localized
            )
            return localized

    KR_TITLE_CACHE[cache_key] = fallback_title
    return fallback_title

def localize_kr_results(results, max_items=30):
    targets = []
    for index, item in enumerate(results):
        if len(targets) >= max_items:
            break
        if item.get('store') != 'KR':
            continue
        title = item.get('title', '')
        if contains_hangul(title):
            continue
        if not item.get('apple_id'):
            continue
        targets.append((index, item.get('apple_id', ''), item.get('collection_url', ''), title))
    if not targets:
        return results

    def worker(data):
        index, collection_id, view_url, fallback = data
        localized = get_kr_localized_album_title(collection_id, view_url, fallback)
        return (index, localized)
    try:
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = [executor.submit(worker, target) for target in targets]
            for future in as_completed(futures):
                index, localized = future.result()
                if localized:
                    results[index]['title'] = localized
    except Exception as e:
        print('KR TITLE LOCALIZE ERROR:', e)
    return results

def safe_json_decode(raw):
    if raw is None:
        return None
    raw = str(raw).strip()
    if not raw:
        return None
    attempts = [raw, html_lib.unescape(raw)]
    try:
        decoded_url = unquote(raw)
        if decoded_url != raw:
            attempts.append(decoded_url)
    except Exception:
        pass
    for text in attempts:
        try:
            return json.loads(text)
        except Exception:
            pass
    try:
        padding = '=' * (-len(raw) % 4)
        decoded = base64.b64decode(raw + padding, validate=False).decode('utf-8')
        return json.loads(decoded)
    except Exception:
        pass
    return None

def get_artist_from_track_object(value):
    artist = str(value.get('artistName') or value.get('artist_name') or value.get('byline') or '').strip()
    if artist:
        return artist
    subtitle_links = value.get('subtitleLinks')
    if isinstance(subtitle_links, list):
        names = []
        for link in subtitle_links:
            if not isinstance(link, dict):
                continue
            name = str(link.get('title') or '').strip()
            if name:
                names.append(name)
        if names:
            return ', '.join(names)
    subtitle = value.get('subtitle')
    if isinstance(subtitle, str):
        return subtitle.strip()
    return ''

def parse_apple_music_track_object(value):
    if not isinstance(value, dict):
        return None
    object_type = str(value.get('type') or '').lower()
    if object_type in {'music-videos', 'musicvideo', 'musicvideos'}:
        return None
    title = ''
    artist = ''
    album_name = ''
    track_id = ''
    duration_ms = 0
    disc_number = 1
    track_number = 0
    attributes = value.get('attributes')
    if object_type == 'songs' and isinstance(attributes, dict):
        title = str(attributes.get('name') or '').strip()
        artist = str(attributes.get('artistName') or '').strip()
        album_name = str(attributes.get('albumName') or '').strip()
        track_id = str(value.get('id') or '').strip()
        duration_ms = attributes.get('durationInMillis') or 0
        disc_number = attributes.get('discNumber') or 1
        track_number = attributes.get('trackNumber') or 0
    if not title:
        title = str(value.get('songName') or value.get('trackName') or '').strip()
        if not title:
            possible_title = value.get('title')
            if isinstance(possible_title, str):
                title = possible_title.strip()
        if not title and isinstance(value.get('name'), str) and (value.get('artistName') or value.get('subtitleLinks')):
            title = str(value.get('name') or '').strip()
        artist = get_artist_from_track_object(value)
        album_name = str(value.get('albumName') or value.get('collectionName') or '').strip()
        track_id = str(value.get('trackId') or value.get('songId') or value.get('adamId') or '').strip()
        content_descriptor = value.get('contentDescriptor')
        if not track_id and isinstance(content_descriptor, dict):
            identifiers = content_descriptor.get('identifiers')
            if isinstance(identifiers, dict):
                track_id = str(identifiers.get('storeAdamID') or '').strip()
        if not track_id and isinstance(value.get('id'), str):
            match = re.search('(\\d{6,})$', value['id'])
            if match:
                track_id = match.group(1)
        duration_ms = value.get('durationInMillis') or value.get('trackTimeMillis') or value.get('duration') or 0
        disc_number = value.get('discNumber') or 1
        track_number = value.get('trackNumber') or 0
    if not title or len(title) < 1:
        return None
    reject_titles = {'apple music', 'music에서 열기', '미리 듣기', '재생', '전체 보기'}
    if title.lower() in reject_titles:
        return None
    try:
        duration_ms = int(float(duration_ms or 0))
    except Exception:
        duration_ms = 0
    try:
        disc_number = int(disc_number or 1)
    except Exception:
        disc_number = 1
    try:
        track_number = int(track_number or 0)
    except Exception:
        track_number = 0
    has_track_hint = bool(object_type == 'songs' or track_id or track_number or duration_ms or value.get('contentDescriptor') or str(value.get('id') or '').startswith('track-lockup'))
    if not has_track_hint:
        return None
    return {'id': track_id, 'title': title, 'artist': artist, 'album_name': album_name, 'duration_ms': duration_ms, 'duration': format_track_duration(duration_ms), 'disc_number': disc_number, 'track_number': track_number}

def traverse_for_track_objects(root):
    if not isinstance(root, (dict, list)):
        return []
    stack = [root]
    tracks = []
    seen_keys = set()
    safety = 0
    while stack and safety < 200000:
        value = stack.pop()
        safety += 1
        if isinstance(value, dict):
            track = parse_apple_music_track_object(value)
            if track:
                dedupe_key = track['id'] or normalize_text(track['title']) + '|' + normalize_text(track['artist'])
                if dedupe_key and dedupe_key not in seen_keys:
                    seen_keys.add(dedupe_key)
                    tracks.append(track)
            for child in value.values():
                if isinstance(child, (dict, list)):
                    stack.append(child)
        elif isinstance(value, list):
            for child in value:
                if isinstance(child, (dict, list)):
                    stack.append(child)
    return tracks

def parse_iso_duration_to_ms(value):
    if not value:
        return 0
    match = re.match('PT(?:(\\d+)H)?(?:(\\d+)M)?(?:(\\d+)S)?', str(value), flags=re.I)
    if not match:
        return 0
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    return (hours * 3600 + minutes * 60 + seconds) * 1000

def extract_jsonld_tracks(parser):
    tracks = []
    seen = set()
    for raw in parser.ld_json_scripts:
        data = safe_json_decode(raw)
        if not data:
            continue
        stack = [data]
        while stack:
            value = stack.pop()
            if isinstance(value, dict):
                type_value = value.get('@type')
                if type_value in {'MusicAlbum', 'MusicPlaylist'}:
                    track_list = value.get('track')
                    if isinstance(track_list, list):
                        for index, item in enumerate(track_list, start=1):
                            if not isinstance(item, dict):
                                continue
                            title = str(item.get('name') or '').strip()
                            if not title:
                                continue
                            artist = ''
                            by_artist = item.get('byArtist')
                            if isinstance(by_artist, dict):
                                artist = str(by_artist.get('name') or '').strip()
                            elif isinstance(by_artist, str):
                                artist = by_artist.strip()
                            duration_ms = parse_iso_duration_to_ms(item.get('duration'))
                            track_url = str(item.get('url') or '')
                            track_id = ''
                            id_match = re.search('/(\\d{6,})(?:\\?|$)', track_url)
                            if id_match:
                                track_id = id_match.group(1)
                            key = track_id or normalize_text(title) + '|' + normalize_text(artist)
                            if key in seen:
                                continue
                            seen.add(key)
                            tracks.append({'id': track_id, 'title': title, 'artist': artist, 'album_name': '', 'duration_ms': duration_ms, 'duration': format_track_duration(duration_ms), 'disc_number': 1, 'track_number': index})
                for child in value.values():
                    if isinstance(child, (dict, list)):
                        stack.append(child)
            elif isinstance(value, list):
                for child in value:
                    if isinstance(child, (dict, list)):
                        stack.append(child)
    return tracks

def select_tracks_for_album(tracks, artist, album_title):
    if not tracks:
        return []
    target_album = normalize_text(album_title)
    target_artist = normalize_text(artist)
    album_groups = {}
    for track in tracks:
        album_name = track.get('album_name', '')
        if not album_name:
            continue
        key = normalize_text(album_name)
        if not key:
            continue
        album_groups.setdefault(key, {'name': album_name, 'tracks': []})['tracks'].append(track)
    if album_groups:
        best_group = None
        best_score = -1
        for group in album_groups.values():
            group_name = group['name']
            title_score = similarity(album_title, group_name)
            score = int(title_score * 2000)
            if normalize_text(group_name) == target_album:
                score += 3000
            elif target_album and (target_album in normalize_text(group_name) or normalize_text(group_name) in target_album):
                score += 1000
            group_artist_scores = [similarity(artist, track.get('artist', '')) for track in group['tracks'] if track.get('artist')]
            if group_artist_scores:
                score += int(max(group_artist_scores) * 500)
            if score > best_score:
                best_score = score
                best_group = group['tracks']
        if best_group and best_score >= 700:
            tracks = best_group
    numbered = [track for track in tracks if track.get('track_number', 0) > 0]
    if len(numbered) >= 2:
        tracks = numbered
    filtered = []
    for track in tracks:
        track_artist = track.get('artist', '')
        if target_artist and track_artist and (similarity(artist, track_artist) < 0.25):
            continue
        filtered.append(track)
    if filtered:
        tracks = filtered
    tracks = sorted(tracks, key=lambda item: (item.get('disc_number', 1), item.get('track_number', 0) or 9999, item.get('title', '')))
    next_number_by_disc = {}
    for track in tracks:
        disc = track.get('disc_number', 1) or 1
        if track.get('track_number', 0) <= 0:
            current = next_number_by_disc.get(disc, 0) + 1
            track['track_number'] = current
            next_number_by_disc[disc] = current
        else:
            next_number_by_disc[disc] = max(next_number_by_disc.get(disc, 0), track['track_number'])
    return tracks

def extract_tracks_from_apple_music_page(page, artist, album_title):
    if not page:
        return []
    parser = page['parser']
    candidates = []
    for raw in parser.serialized_scripts:
        data = safe_json_decode(raw)
        if not data:
            continue
        candidates.extend(traverse_for_track_objects(data))
    selected = select_tracks_for_album(candidates, artist, album_title)
    if selected:
        print('APPLE WEB SERIALIZED TRACK COUNT:', len(selected))
        return selected
    ld_tracks = extract_jsonld_tracks(parser)
    selected = select_tracks_for_album(ld_tracks, artist, album_title)
    if selected:
        print('APPLE WEB JSON-LD TRACK COUNT:', len(selected))
    return selected

def track_from_itunes_item(item):
    track_name = item.get('trackName', '')
    if not track_name:
        return None
    try:
        disc_number = int(item.get('discNumber', 1) or 1)
    except Exception:
        disc_number = 1
    try:
        track_number = int(item.get('trackNumber', 0) or 0)
    except Exception:
        track_number = 0
    duration_ms = item.get('trackTimeMillis') or 0
    try:
        duration_ms = int(duration_ms)
    except Exception:
        duration_ms = 0
    return {'id': str(item.get('trackId') or ''), 'title': track_name, 'artist': item.get('artistName', ''), 'album_name': item.get('collectionName', ''), 'duration_ms': duration_ms, 'duration': format_track_duration(duration_ms), 'disc_number': disc_number, 'track_number': track_number}

def lookup_kr_tracks_by_id(collection_id):
    collection_id = str(collection_id or '').strip()
    if not collection_id:
        return []
    try:
        response = APPLE_SESSION.get('https://itunes.apple.com/lookup', params={'id': collection_id, 'entity': 'song', 'country': 'KR', 'limit': 300, 'explicit': 'Yes'}, timeout=12)
        response.raise_for_status()
        data = response.json()
        tracks = []
        for item in data.get('results', []):
            if item.get('wrapperType') != 'track':
                continue
            if item.get('kind') != 'song':
                continue
            track = track_from_itunes_item(item)
            if track:
                tracks.append(track)
        tracks.sort(key=lambda item: (item['disc_number'], item['track_number']))
        print('ITUNES KR LOOKUP TRACK COUNT:', len(tracks))
        return tracks
    except Exception as e:
        print('ITUNES KR TRACK ERROR:', e)
        return []

def search_kr_album_candidates(search_term, use_album_attribute=False):
    if not search_term:
        return []
    try:
        params = {'term': search_term, 'country': 'KR', 'media': 'music', 'entity': 'album', 'limit': 100, 'explicit': 'Yes'}
        if use_album_attribute:
            params['attribute'] = 'albumTerm'
        response = APPLE_SESSION.get('https://itunes.apple.com/search', params=params, timeout=12)
        response.raise_for_status()
        data = response.json()
        results = []
        for item in data.get('results', []):
            collection_id = item.get('collectionId')
            if not collection_id:
                continue
            title = item.get('collectionName', '')
            if not title:
                continue
            release_date = item.get('releaseDate', '')[:10]
            results.append({'collection_id': str(collection_id), 'artist': item.get('artistName', ''), 'title': title, 'release_year': release_date[:4] if release_date else '', 'collection_url': item.get('collectionViewUrl', ''), 'track_count': item.get('trackCount', 0) or 0})
        return results
    except Exception as e:
        print('KR ALBUM SEARCH ERROR:', e)
        return []

def find_kr_equivalent_album(artist, album_title, release_year=''):
    artist = str(artist or '').strip()
    album_title = str(album_title or '').strip()
    release_year = str(release_year or '').strip()
    if not album_title:
        return None
    searches = [(album_title, True), (f'{artist} {album_title}'.strip(), False)]
    candidates = []
    seen = set()
    for term, use_attribute in searches:
        if not term:
            continue
        found = search_kr_album_candidates(term, use_attribute)
        for candidate in found:
            collection_id = candidate['collection_id']
            if collection_id in seen:
                continue
            seen.add(collection_id)
            candidates.append(candidate)
    if not candidates:
        return None
    target_has_korean = contains_hangul(album_title)
    best = None
    best_score = -1
    for candidate in candidates:
        compare_title = candidate['title']
        if target_has_korean and (not contains_hangul(compare_title)):
            compare_title = get_kr_localized_album_title(candidate['collection_id'], candidate['collection_url'], compare_title)
        title_score = similarity(album_title, compare_title)
        artist_score = similarity(artist, candidate['artist']) if artist else 0
        score = int(title_score * 1800)
        score += int(artist_score * 700)
        if normalize_text(compare_title) == normalize_text(album_title):
            score += 2500
        if release_year and candidate['release_year'] == release_year:
            score += 500
        candidate['localized_title'] = compare_title
        print('KR ALBUM CANDIDATE:', candidate['collection_id'], compare_title, candidate['artist'], candidate['release_year'], 'SCORE=', score)
        if score > best_score:
            best_score = score
            best = candidate
    if not best or best_score < 550:
        return None
    print('KR ALBUM MATCH:', best['collection_id'], best.get('localized_title', best['title']))
    return best

def search_kr_tracks_by_term(artist, album_title, release_year=''):
    terms = []
    combined = f'{artist} {album_title}'.strip()
    if combined:
        terms.append(combined)
    if album_title:
        terms.append(album_title)
    all_items = []
    seen_track_ids = set()
    for term in terms:
        try:
            response = APPLE_SESSION.get('https://itunes.apple.com/search', params={'term': term, 'country': 'KR', 'media': 'music', 'entity': 'song', 'limit': 200, 'explicit': 'Yes'}, timeout=12)
            response.raise_for_status()
            data = response.json()
            for item in data.get('results', []):
                if item.get('kind') != 'song':
                    continue
                track_id = str(item.get('trackId') or '')
                if track_id and track_id in seen_track_ids:
                    continue
                if track_id:
                    seen_track_ids.add(track_id)
                all_items.append(item)
        except Exception as e:
            print('KR SONG SEARCH ERROR:', e)
    if not all_items:
        return []
    groups = {}
    for item in all_items:
        collection_id = str(item.get('collectionId') or '')
        if not collection_id:
            continue
        groups.setdefault(collection_id, []).append(item)
    best_items = None
    best_score = -1
    for collection_id, items in groups.items():
        first = items[0]
        collection_name = first.get('collectionName', '')
        artist_name = first.get('artistName', '')
        release_date = first.get('releaseDate', '')[:10]
        year = release_date[:4] if release_date else ''
        collection_url = first.get('collectionViewUrl', '')
        localized_name = collection_name
        if contains_hangul(album_title) and (not contains_hangul(localized_name)):
            localized_name = get_kr_localized_album_title(collection_id, collection_url, collection_name)
        score = int(similarity(album_title, localized_name) * 2000)
        score += int(similarity(artist, artist_name) * 800)
        if normalize_text(album_title) == normalize_text(localized_name):
            score += 2200
        if release_year and year == release_year:
            score += 400
        score += len(items) * 10
        if score > best_score:
            best_score = score
            best_items = items
    if not best_items:
        return []
    tracks = []
    for item in best_items:
        track = track_from_itunes_item(item)
        if track:
            tracks.append(track)
    tracks.sort(key=lambda item: (item['disc_number'], item['track_number']))
    return tracks

def get_album_tracks(collection_id, artist='', album_title='', release_year=''):
    collection_id = str(collection_id or '').strip()
    cache_key = 'KR|' + collection_id + '|' + normalize_text(artist) + '|' + normalize_text(album_title) + '|' + str(release_year or '')
    cached = TRACK_CACHE.get(cache_key)
    if cached and time.time() - cached['time'] < TRACK_CACHE_SECONDS:
        return cached['data']
    if collection_id:
        page = fetch_apple_music_kr_page(collection_id)
        localized_match_title = get_kr_localized_album_title(collection_id, '', album_title) or album_title
        web_tracks = extract_tracks_from_apple_music_page(page, artist, localized_match_title)
        if web_tracks:
            result = {'tracks': web_tracks, 'store': 'KR', 'resolved_by': 'apple_music_web_stored_id'}
            TRACK_CACHE[cache_key] = {'time': time.time(), 'data': result}
            return result
        api_tracks = lookup_kr_tracks_by_id(collection_id)
        if api_tracks:
            result = {'tracks': api_tracks, 'store': 'KR', 'resolved_by': 'itunes_stored_id'}
            TRACK_CACHE[cache_key] = {'time': time.time(), 'data': result}
            return result
    kr_album = find_kr_equivalent_album(artist, album_title, release_year)
    if kr_album:
        kr_id = kr_album['collection_id']
        kr_url = kr_album.get('collection_url', '')
        page = fetch_apple_music_kr_page(kr_id, kr_url)
        localized_match_title = get_kr_localized_album_title(kr_id, kr_url, album_title) or album_title
        web_tracks = extract_tracks_from_apple_music_page(page, artist, localized_match_title)
        if web_tracks:
            result = {'tracks': web_tracks, 'store': 'KR', 'resolved_by': 'apple_music_web_kr_match'}
            TRACK_CACHE[cache_key] = {'time': time.time(), 'data': result}
            return result
        api_tracks = lookup_kr_tracks_by_id(kr_id)
        if api_tracks:
            result = {'tracks': api_tracks, 'store': 'KR', 'resolved_by': 'itunes_kr_match'}
            TRACK_CACHE[cache_key] = {'time': time.time(), 'data': result}
            return result
    tracks = search_kr_tracks_by_term(artist, album_title, release_year)
    result = {'tracks': tracks, 'store': 'KR', 'resolved_by': 'kr_song_search' if tracks else ''}
    TRACK_CACHE[cache_key] = {'time': time.time(), 'data': result}
    return result

def find_apple_artist_in_store(artist_name, store):
    if not artist_name:
        return None
    target = normalize_text(artist_name)
    best_candidate = None
    best_score = 0
    try:
        response = APPLE_SESSION.get('https://itunes.apple.com/search', params={'term': artist_name, 'country': store, 'media': 'music', 'entity': 'musicArtist', 'attribute': 'artistTerm', 'limit': 30, 'explicit': 'Yes'}, timeout=10)
        response.raise_for_status()
        data = response.json()
        for item in data.get('results', []):
            result_name = item.get('artistName', '')
            artist_id = item.get('artistId')
            if not artist_id:
                continue
            result_normalized = normalize_text(result_name)
            if result_normalized == target:
                return {'artist_id': str(artist_id), 'artist_name': result_name, 'store': store}
            score = similarity(artist_name, result_name)
            if score > best_score:
                best_score = score
                best_candidate = {'artist_id': str(artist_id), 'artist_name': result_name, 'store': store}
    except Exception as e:
        print(f'ARTIST SEARCH ERROR [{store}]:', e)
    if best_candidate and best_score >= 0.72:
        return best_candidate
    return None

def find_apple_artist(artist_name, stores=None):
    if stores is None:
        stores = ['KR', 'US', 'JP', 'GB']

    for store in stores:
        result = find_apple_artist_in_store(artist_name, store)
        if result:
            return result

    return None


def lookup_artist_albums(
    artist_id,
    search_album='',
    artist_name='',
    prefer_korean=False,
    stores=None
):
    if stores is None:
        stores = ['KR', 'US', 'JP', 'GB']

    for store in stores:
        store_results = []

        try:
            store_artist_id = artist_id

            if artist_name:
                local_artist = find_apple_artist_in_store(
                    artist_name,
                    store
                )

                if local_artist:
                    store_artist_id = local_artist['artist_id']

            response = APPLE_SESSION.get(
                'https://itunes.apple.com/lookup',
                params={
                    'id': store_artist_id,
                    'entity': 'album',
                    'limit': 100,
                    'country': store,
                    'explicit': 'Yes'
                },
                timeout=12
            )

            response.raise_for_status()
            data = response.json()

            seen_meta = set()

            for item in data.get('results', []):
                collection_id = item.get('collectionId')

                if not collection_id:
                    continue

                title = item.get('collectionName', '')

                if not title:
                    continue

                result_artist = item.get('artistName', '')
                release_date = item.get('releaseDate', '')[:10]
                release_year = release_date[:4] if release_date else ''

                meta_key = (
                    normalize_text(result_artist),
                    normalize_text(title),
                    release_year
                )

                if meta_key in seen_meta:
                    continue

                seen_meta.add(meta_key)

                score = 0

                if search_album:
                    target = normalize_text(search_album)
                    result_title = normalize_text(title)

                    if result_title == target:
                        score += 1000
                    elif target in result_title:
                        score += 600
                    elif result_title in target:
                        score += 400

                    score += int(
                        similarity(
                            search_album,
                            title
                        ) * 200
                    )

                store_results.append({
                    'apple_id': str(collection_id),
                    'artist_id': str(
                        item.get(
                            'artistId',
                            store_artist_id
                        )
                    ),
                    'artist': result_artist,
                    'title': title,
                    'release_year': release_year,
                    'cover': apple_high_res(
                        item.get(
                            'artworkUrl100',
                            ''
                        )
                    ),
                    'thumb': apple_artwork_size(
                        item.get(
                            'artworkUrl100',
                            ''
                        ),
                        400
                    ),
                    'album_type': normalize_apple_album_type(item, title),
                    'genre': extract_apple_genre(item),
                    'store': store,
                    'explicit': get_explicit_label(item),
                    'collection_url': item.get(
                        'collectionViewUrl',
                        ''
                    ),
                    'score': score
                })

        except Exception as e:
            print(
                f'ALBUM LOOKUP ERROR [{store}]:',
                e
            )

        if store_results:
            if search_album:
                store_results.sort(
                    key=lambda item: item.get(
                        'score',
                        0
                    ),
                    reverse=True
                )
            else:
                store_results.sort(
                    key=lambda item: item.get(
                        'release_year',
                        ''
                    ),
                    reverse=True
                )

            results = store_results[:40]

            if store == 'KR' and prefer_korean:
                results = localize_kr_results(
                    results,
                    max_items=8
                )

            return results

    return []


def search_artist_albums_in_stores(
    artist_name,
    album,
    stores,
    prefer_korean=False
):
    if not artist_name:
        return []

    for store in stores:
        apple_artist = find_apple_artist_in_store(
            artist_name,
            store
        )

        if not apple_artist:
            continue

        results = lookup_artist_albums(
            apple_artist['artist_id'],
            album,
            '',
            prefer_korean=(
                prefer_korean
                and store == 'KR'
            ),
            stores=[store]
        )

        if results:
            print(
                'ARTIST SEARCH MATCH:',
                artist_name,
                '→',
                store,
                '→',
                apple_artist['artist_name']
            )

            return results

    return []


def discover_by_album(search_artist, album):
    if not album:
        return []

    stores = ['KR', 'US', 'JP', 'GB']
    prefer_korean = (
        contains_hangul(search_artist)
        or contains_hangul(album)
    )

    for store in stores:
        store_results = []
        seen_meta = set()

        try:
            response = APPLE_SESSION.get(
                'https://itunes.apple.com/search',
                params={
                    'term': album,
                    'country': store,
                    'media': 'music',
                    'entity': 'album',
                    'attribute': 'albumTerm',
                    'limit': 80,
                    'explicit': 'Yes'
                },
                timeout=12
            )

            response.raise_for_status()
            data = response.json()

            for item in data.get('results', []):
                collection_id = item.get('collectionId')

                if not collection_id:
                    continue

                title = item.get('collectionName', '')

                if not title:
                    continue

                artist_name = item.get('artistName', '')
                release_date = item.get('releaseDate', '')[:10]
                release_year = release_date[:4] if release_date else ''

                meta_key = (
                    normalize_text(artist_name),
                    normalize_text(title),
                    release_year
                )

                if meta_key in seen_meta:
                    continue

                seen_meta.add(meta_key)

                target_album = normalize_text(album)
                result_album = normalize_text(title)
                score = 0

                if result_album == target_album:
                    score += 2000
                elif target_album in result_album:
                    score += 900
                elif result_album in target_album:
                    score += 700

                score += int(
                    similarity(
                        album,
                        title
                    ) * 300
                )

                if search_artist:
                    score += int(
                        similarity(
                            search_artist,
                            artist_name
                        ) * 100
                    )

                store_results.append({
                    'apple_id': str(collection_id),
                    'artist_id': str(
                        item.get(
                            'artistId',
                            ''
                        )
                    ),
                    'artist': artist_name,
                    'title': title,
                    'release_year': release_year,
                    'cover': apple_high_res(
                        item.get(
                            'artworkUrl100',
                            ''
                        )
                    ),
                    'thumb': apple_artwork_size(
                        item.get(
                            'artworkUrl100',
                            ''
                        ),
                        400
                    ),
                    'album_type': normalize_apple_album_type(item, title),
                    'genre': extract_apple_genre(item),
                    'store': store,
                    'explicit': get_explicit_label(item),
                    'collection_url': item.get(
                        'collectionViewUrl',
                        ''
                    ),
                    'score': score
                })

        except Exception as e:
            print(
                f'ALBUM DISCOVERY ERROR [{store}]:',
                e
            )

        if store_results:
            store_results.sort(
                key=lambda item: item.get(
                    'score',
                    0
                ),
                reverse=True
            )

            results = store_results[:40]

            if store == 'KR' and prefer_korean:
                results = localize_kr_results(
                    results,
                    max_items=8
                )

            return results

    return []



def album_item_to_result(item, store='KR'):
    collection_id = item.get('collectionId')
    if not collection_id:
        return None

    release_date = str(item.get('releaseDate') or '')[:10]

    return {
        'apple_id': str(collection_id),
        'artist_id': str(item.get('artistId') or ''),
        'artist': str(item.get('artistName') or '').strip(),
        'title': str(item.get('collectionName') or '').strip(),
        'release_year': release_date[:4] if release_date else '',
        'cover': apple_high_res(item.get('artworkUrl100', '') or ''),
        'thumb': apple_artwork_size(item.get('artworkUrl100', '') or '', 400),
        'album_type': normalize_apple_album_type(item),
        'genre': extract_apple_genre(item),
        'store': store,
        'explicit': get_explicit_label(item),
        'collection_url': str(item.get('collectionViewUrl') or '').strip(),
        'score': 0,
    }


def score_album_search_result(item, search_artist='', search_album=''):
    title = item.get('title', '')
    artist = item.get('artist', '')
    score = 0

    if search_album:
        title_similarity = similarity(search_album, title)
        score += int(title_similarity * 1400)

        target_title = normalize_text(search_album)
        result_title = normalize_text(title)

        if target_title and result_title == target_title:
            score += 2600
        elif target_title and (
            target_title in result_title
            or result_title in target_title
        ):
            score += 1100

    if search_artist:
        artist_similarity = similarity(search_artist, artist)
        score += int(artist_similarity * 850)

        target_artist = normalize_text(search_artist)
        result_artist = normalize_text(artist)

        if target_artist and result_artist == target_artist:
            score += 1500
        elif target_artist and (
            target_artist in result_artist
            or result_artist in target_artist
        ):
            score += 500

    item['score'] = score
    return score


def search_kr_album_query(
    term,
    search_artist='',
    search_album='',
    use_album_attribute=False,
    limit=50
):
    """Apple KR에서 앨범 자체를 직접 검색한다."""

    term = str(term or '').strip()
    if not term:
        return []

    try:
        params = {
            'term': term,
            'country': 'KR',
            'media': 'music',
            'entity': 'album',
            'limit': limit,
            'explicit': 'Yes'
        }

        if use_album_attribute:
            params['attribute'] = 'albumTerm'

        response = APPLE_SESSION.get(
            'https://itunes.apple.com/search',
            params=params,
            timeout=12
        )
        response.raise_for_status()

        results = []
        seen = set()

        for raw_item in response.json().get('results', []):
            item = album_item_to_result(raw_item, 'KR')
            if not item or not item['title']:
                continue

            dedupe_key = item['apple_id']
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            results.append(item)

        # 공개 iTunes API가 KR에서도 영어 제목을 주는 경우가 있으므로
        # 실제 한국 Apple Music 페이지 제목으로 보정한다.
        if results and (
            contains_hangul(search_artist)
            or contains_hangul(search_album)
        ):
            results = localize_kr_results(
                results,
                max_items=6
            )

        for item in results:
            score_album_search_result(
                item,
                search_artist,
                search_album
            )

        results.sort(
            key=lambda item: item.get('score', 0),
            reverse=True
        )

        return results[:30]

    except Exception as e:
        print('APPLE KR DIRECT ALBUM SEARCH ERROR:', e)
        return []


def confident_album_search(results, minimum_score=650):
    if not results:
        return False
    return results[0].get('score', 0) >= minimum_score


def extract_apple_music_album_id(apple_url):
    """music.apple.com 앨범 링크의 path에서 앨범 collectionId를 추출한다."""

    apple_url = str(apple_url or '').strip()
    if not apple_url:
        return ''

    try:
        parsed = urlparse(apple_url)
        host = (parsed.netloc or '').lower()

        if not (
            host == 'music.apple.com'
            or host.endswith('.music.apple.com')
            or host == 'itunes.apple.com'
        ):
            return ''

        path = unquote(parsed.path or '')
        if '/album/' not in path:
            return ''

        numbers = re.findall(r'(?<!\d)(\d{6,})(?!\d)', path)
        if not numbers:
            return ''

        return numbers[-1]

    except Exception:
        return ''


def extract_artist_from_apple_page_title(raw_title, album_title=''):
    raw_title = html_lib.unescape(str(raw_title or '')).strip()
    raw_title = (
        raw_title
        .replace('\u200e', '')
        .replace('\u200f', '')
        .replace('\ufeff', '')
        .replace('\xa0', ' ')
    )
    raw_title = re.sub(r'\s+', ' ', raw_title).strip()

    match = re.match(
        r'^Apple Music에서 감상하는 (.+?)의 (.+)$',
        raw_title
    )
    if match:
        return match.group(1).strip()

    cleaned = re.sub(
        r'\s*-\s*Apple\s*Music\s*$',
        '',
        raw_title,
        flags=re.I
    ).strip()

    parts = cleaned.rsplit(' - ', 1)
    if len(parts) == 2:
        artist_part = parts[1].strip()
        match = re.match(r'^(.+?)의\s*앨범$', artist_part)
        if match:
            return match.group(1).strip()

        match = re.match(r'^Album\s+by\s+(.+)$', artist_part, flags=re.I)
        if match:
            return match.group(1).strip()

    return ''


def extract_image_url(value):
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        for item in value:
            result = extract_image_url(item)
            if result:
                return result
    if isinstance(value, dict):
        for key in ('url', 'contentUrl', 'src'):
            if value.get(key):
                return str(value[key]).strip()
    return ''


def normalize_apple_artwork_url(url):
    url = str(url or '').strip()
    if not url:
        return ''
    url = url.replace('{w}', '1000').replace('{h}', '1000')
    return apple_high_res(url)


def find_album_metadata_in_page_data(page, collection_id=''):
    """Apple Music 페이지의 JSON-LD/serialized data에서 앨범 메타데이터를 찾는다."""

    if not page:
        return {}

    parser = page['parser']
    collection_id = str(collection_id or '').strip()
    candidates = []

    def inspect_object(value):
        if not isinstance(value, dict):
            return

        object_type = value.get('@type') or value.get('type') or ''
        attrs = value.get('attributes') if isinstance(value.get('attributes'), dict) else {}

        if object_type == 'MusicAlbum':
            artist = ''
            by_artist = value.get('byArtist')
            if isinstance(by_artist, dict):
                artist = str(by_artist.get('name') or '').strip()
            elif isinstance(by_artist, list):
                names = [
                    str(item.get('name') or '').strip()
                    for item in by_artist
                    if isinstance(item, dict) and item.get('name')
                ]
                artist = ', '.join(name for name in names if name)
            elif isinstance(by_artist, str):
                artist = by_artist.strip()

            release_date = str(value.get('datePublished') or '')
            candidates.append({
                'id': collection_id,
                'title': str(value.get('name') or '').strip(),
                'artist': artist,
                'release_year': release_date[:4] if release_date else '',
                'cover': normalize_apple_artwork_url(
                    extract_image_url(value.get('image'))
                ),
                'artist_id': '',
                'album_type': normalize_apple_album_type(value, value.get('name') or ''),
                'genre': extract_apple_genre(value)
            })

        if str(object_type).lower() == 'albums' and attrs:
            object_id = str(value.get('id') or '')
            artwork = attrs.get('artwork') if isinstance(attrs.get('artwork'), dict) else {}
            candidates.append({
                'id': object_id,
                'title': str(attrs.get('name') or '').strip(),
                'artist': str(attrs.get('artistName') or '').strip(),
                'release_year': str(attrs.get('releaseDate') or '')[:4],
                'cover': normalize_apple_artwork_url(artwork.get('url') or ''),
                'artist_id': str(attrs.get('artistId') or ''),
                'album_type': normalize_apple_album_type(value, attrs.get('name') or ''),
                'genre': extract_apple_genre(value)
            })

    for raw in parser.ld_json_scripts + parser.serialized_scripts:
        data = safe_json_decode(raw)
        if not data:
            continue

        stack = [data]
        safety = 0

        while stack and safety < 100000:
            value = stack.pop()
            safety += 1

            if isinstance(value, dict):
                inspect_object(value)
                for child in value.values():
                    if isinstance(child, (dict, list)):
                        stack.append(child)
            elif isinstance(value, list):
                for child in value:
                    if isinstance(child, (dict, list)):
                        stack.append(child)

    if not candidates:
        return {}

    if collection_id:
        for candidate in candidates:
            if candidate.get('id') == collection_id and candidate.get('title'):
                return candidate

    for candidate in candidates:
        if candidate.get('title'):
            return candidate

    return {}


def lookup_album_metadata_kr(collection_id):
    collection_id = str(collection_id or '').strip()
    if not collection_id:
        return {}

    try:
        response = APPLE_SESSION.get(
            'https://itunes.apple.com/lookup',
            params={
                'id': collection_id,
                'country': 'KR',
                'entity': 'album',
                'explicit': 'Yes'
            },
            timeout=12
        )
        response.raise_for_status()

        for raw_item in response.json().get('results', []):
            raw_id = str(
                raw_item.get('collectionId')
                or raw_item.get('trackId')
                or ''
            )

            if raw_item.get('collectionName') and (
                not raw_id
                or raw_id == collection_id
            ):
                item = album_item_to_result(raw_item, 'KR')
                if item:
                    item['title'] = get_kr_localized_album_title(
                        item['apple_id'],
                        item.get('collection_url', ''),
                        item['title']
                    )
                    return item

    except Exception as e:
        print('APPLE LINK LOOKUP ERROR:', e)

    return {}


def resolve_apple_music_album_link(apple_url):
    collection_id = extract_apple_music_album_id(apple_url)
    if not collection_id:
        return None

    # 1) KR iTunes lookup
    metadata = lookup_album_metadata_kr(collection_id)

    # 2) lookup이 약하면 실제 Apple Music KR 페이지에서 보강
    page = fetch_apple_music_kr_page(collection_id, apple_url)
    page_metadata = find_album_metadata_in_page_data(page, collection_id)

    if not metadata:
        metadata = {
            'apple_id': collection_id,
            'artist_id': '',
            'artist': '',
            'title': '',
            'release_year': '',
            'cover': '',
            'album_type': '',
            'genre': '',
            'store': 'KR',
            'explicit': '',
            'collection_url': apple_url,
            'score': 0
        }

    if page_metadata:
        if page_metadata.get('title'):
            metadata['title'] = clean_apple_music_page_title(
                page_metadata['title'],
                metadata.get('title', '')
            )
        if page_metadata.get('artist'):
            metadata['artist'] = page_metadata['artist']
        if page_metadata.get('release_year'):
            metadata['release_year'] = page_metadata['release_year']
        if page_metadata.get('cover'):
            metadata['cover'] = page_metadata['cover']
        if page_metadata.get('artist_id'):
            metadata['artist_id'] = page_metadata['artist_id']
        if page_metadata.get('album_type'):
            metadata['album_type'] = page_metadata['album_type']
        if page_metadata.get('genre'):
            metadata['genre'] = page_metadata['genre']

    if page:
        parser = page['parser']
        localized_title = get_kr_localized_album_title(
            collection_id,
            apple_url,
            metadata.get('title', '')
        )
        if localized_title:
            metadata['title'] = localized_title

        if not metadata.get('artist'):
            title_sources = [
                parser.meta_titles.get('og:title', ''),
                parser.page_title,
                parser.meta_titles.get('twitter:title', '')
            ]
            for raw_title in title_sources:
                artist = extract_artist_from_apple_page_title(
                    raw_title,
                    metadata.get('title', '')
                )
                if artist:
                    metadata['artist'] = artist
                    break

        if not metadata.get('cover'):
            metadata['cover'] = normalize_apple_artwork_url(
                parser.meta_values.get('og:image', '')
                or parser.meta_values.get('twitter:image', '')
            )

    metadata['apple_id'] = collection_id
    metadata['store'] = 'KR'
    metadata['collection_url'] = apple_url

    if not metadata.get('title') or not metadata.get('artist'):
        return None

    return metadata

def search_apple(artist, album):
    artist = str(artist or '').strip()
    album = str(album or '').strip()

    if not artist and not album:
        return []

    cache_key = (
        normalize_text(artist),
        normalize_text(album)
    )

    cached = SEARCH_CACHE.get(cache_key)
    if cached and time.time() - cached['time'] < CACHE_SECONDS:
        return cached['results']

    results = []
    learned_name = ''
    learned = None
    musicbrainz_names = []

    # =====================================================
    # 1순위: Apple KR 앨범명 직접검색
    # =====================================================
    if album:
        print(
            'SEARCH STEP 1: KR ALBUM DIRECT →',
            album
        )

        direct_results = search_kr_album_query(
            album,
            search_artist=artist,
            search_album=album,
            use_album_attribute=True
        )

        if confident_album_search(direct_results, 700):
            results = direct_results

    # =====================================================
    # 2순위: Apple KR 아티스트 + 앨범명 통합검색
    # =====================================================
    if not results and artist and album:
        combined_term = f'{artist} {album}'.strip()

        print(
            'SEARCH STEP 2: KR ARTIST+ALBUM →',
            combined_term
        )

        combined_results = search_kr_album_query(
            combined_term,
            search_artist=artist,
            search_album=album,
            use_album_attribute=False
        )

        if confident_album_search(combined_results, 550):
            results = combined_results

    # =====================================================
    # 3순위: 이미 학습한 Apple Artist ID가 있으면 검색 없이 즉시 lookup
    # =====================================================
    if not results and artist:
        learned = find_learned_artist(artist)

        if learned and learned.get('artist_id'):
            learned_name = learned.get('artist_name', '') or ''
            print(
                'SEARCH STEP 3: LEARNED APPLE ID →',
                artist,
                '→',
                learned['artist_id']
            )
            results = lookup_artist_albums(
                learned['artist_id'],
                album,
                '',
                prefer_korean=(
                    contains_hangul(artist)
                    or contains_hangul(album)
                ),
                stores=['KR']
            )

    # =====================================================
    # 4순위: Apple KR 아티스트검색
    # =====================================================
    if not results and artist:
        print(
            'SEARCH STEP 4: APPLE KR ARTIST →',
            artist
        )

        results = search_artist_albums_in_stores(
            artist,
            album,
            ['KR'],
            prefer_korean=(
                contains_hangul(artist)
                or contains_hangul(album)
            )
        )

    # =====================================================
    # 4순위: 우리 alias DB
    # =====================================================
    if not results and artist:
        if learned is None:
            learned = find_learned_artist(artist)

        if (
            learned
            and learned.get('artist_name')
            and normalize_text(learned['artist_name'])
            != normalize_text(artist)
        ):
            learned_name = learned['artist_name']

            print(
                'SEARCH STEP 4: LEARNED ALIAS → KR',
                artist,
                '→',
                learned_name
            )

            results = search_artist_albums_in_stores(
                learned_name,
                album,
                ['KR'],
                prefer_korean=True
            )

    # =====================================================
    # 5순위: MusicBrainz 이름/alias → Apple KR
    # Wikipedia는 사용하지 않는다.
    # =====================================================
    if not results and artist:
        musicbrainz_names = search_musicbrainz_artist_names(
            artist
        )

        for candidate_name in musicbrainz_names:
            if normalize_text(candidate_name) in {
                normalize_text(artist),
                normalize_text(learned_name)
            }:
                continue

            print(
                'SEARCH STEP 5: MUSICBRAINZ → APPLE KR',
                artist,
                '→',
                candidate_name
            )

            results = search_artist_albums_in_stores(
                candidate_name,
                album,
                ['KR'],
                prefer_korean=True
            )

            if results:
                break

    # =====================================================
    # KR에서 끝까지 못 찾은 경우에만 해외 Apple 보조검색
    # (Wikipedia 없음)
    # =====================================================
    if not results and artist:
        overseas_names = []
        seen_names = set()

        for name in [
            learned_name,
            *musicbrainz_names,
            artist
        ]:
            name = str(name or '').strip()
            key = normalize_text(name)
            if not name or not key or key in seen_names:
                continue
            seen_names.add(key)
            overseas_names.append(name)

        for candidate_name in overseas_names:
            print(
                'SEARCH FALLBACK: APPLE US/JP/GB →',
                candidate_name
            )

            results = search_artist_albums_in_stores(
                candidate_name,
                album,
                ['US', 'JP', 'GB'],
                prefer_korean=False
            )

            if results:
                break

    # =====================================================
    # 아티스트 경로도 실패한 경우 마지막 앨범 fallback
    # =====================================================
    if not results and album:
        results = discover_by_album(
            artist,
            album
        )

    SEARCH_CACHE[cache_key] = {
        'time': time.time(),
        'results': results
    }

    return results

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
    saved = request.args.get('saved', '')
    conn = get_db()
    conditions = []
    params = []
    if library_q:
        value = f'%{library_q}%'
        conditions.append('( artist LIKE ? OR album_title LIKE ? OR release_year LIKE ? )')
        params.extend([value, value, value])
    if classification_filter:
        conditions.append('classification = ?')
        params.append(classification_filter)
    if format_filter == 'CD':
        conditions.append("media_format = 'CD'")
    elif format_filter == 'LP':
        conditions.append("media_format = 'LP'")
    elif format_filter == 'ETC':
        conditions.append("media_format IN ( 'Casette', 'USB', '기타' )")
    if album_type_filter in ALBUM_TYPES:
        conditions.append('album_type = ?')
        params.append(album_type_filter)
    if genre_filter:
        conditions.append('genre = ?')
        params.append(genre_filter)
    query = 'SELECT * FROM albums'
    if conditions:
        query += ' WHERE ' + ' AND '.join(conditions)
    query += ' ORDER BY id DESC'
    albums = conn.execute(query, params).fetchall()
    total_album_count = conn.execute('SELECT COUNT(*) AS count FROM albums').fetchone()['count']
    genre_options = [row['genre'] for row in conn.execute("SELECT DISTINCT genre FROM albums WHERE genre IS NOT NULL AND TRIM(genre) != '' ORDER BY genre COLLATE NOCASE").fetchall()]
    conn.close()
    return render_template('index.html', albums=albums, library_q=library_q, classification_filter=classification_filter, format_filter=format_filter, album_type_filter=album_type_filter, genre_filter=genre_filter, genre_options=genre_options, total_album_count=total_album_count, saved=saved)

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
        manual_track_mode=manual_track_mode
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
            'album_detail',
            album_row_id=album_row_id
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

    TRACK_CACHE.clear()
    APPLE_PAGE_CACHE.clear()

    return redirect(
        url_for(
            'album_detail',
            album_row_id=album_row_id
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
    sort_order = request.args.get('sort_order', 'desc').strip().lower()
    updated = request.args.get('updated', '')
    deleted = request.args.get('deleted', '')
    conn = get_db()
    conditions = []
    params = []
    if library_q:
        value = f'%{library_q}%'
        conditions.append('( management_no LIKE ? OR album_id LIKE ? OR artist LIKE ? OR album_title LIKE ? OR release_year LIKE ? OR memo LIKE ? )')
        params.extend([value, value, value, value, value, value])
    if classification_filter:
        conditions.append('classification = ?')
        params.append(classification_filter)
    if format_filter == 'CD':
        conditions.append("media_format = 'CD'")
    elif format_filter == 'LP':
        conditions.append("media_format = 'LP'")
    elif format_filter == 'ETC':
        conditions.append("media_format IN ( 'Casette', 'USB', '기타' )")
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

    query = 'SELECT * FROM albums'
    if conditions:
        query += ' WHERE ' + ' AND '.join(conditions)
    query += f" ORDER BY {sort_columns[sort_by]} {sort_order.upper()}, id DESC"
    albums = conn.execute(query, params).fetchall()
    total_album_count = conn.execute('SELECT COUNT(*) AS count FROM albums').fetchone()['count']
    conn.close()
    return render_template('manage.html', albums=albums, total_album_count=total_album_count, library_q=library_q, classification_filter=classification_filter, format_filter=format_filter, status_filter=status_filter, signed_filter=signed_filter, sort_by=sort_by, sort_order=sort_order, updated=updated, deleted=deleted)

@app.route('/add')
def add_album_page():
    artist = request.args.get('artist', '').strip()
    album = request.args.get('album', '').strip()
    link_error = request.args.get('link_error', '').strip()
    manual_error = request.args.get('manual_error', '').strip()
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
        manual_error=manual_error
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
    genre = normalize_genre_name(request.form.get('genre', '').strip())
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

    SEARCH_CACHE.clear()
    TRACK_CACHE.clear()
    APPLE_PAGE_CACHE.clear()

    return redirect(
        url_for(
            'index',
            saved=management_no
        )
    )


@app.route('/register', methods=['POST'])
def register():
    apple_id = request.form.get('apple_id', '').strip()
    apple_artist_id = request.form.get('apple_artist_id', '').strip()
    search_artist = request.form.get('search_artist', '').strip()
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
    selected = {'apple_id': apple_id, 'apple_artist_id': apple_artist_id, 'search_artist': search_artist, 'artist': artist, 'album_title': album_title, 'release_year': release_year, 'cover_url': cover_url, 'album_type': album_type, 'genre': genre, 'preview_album_id': preview_album_id}
    return render_template('register.html', album=selected, duplicate_count=duplicate_count, duplicate_items=duplicate_items)

@app.route('/save', methods=['POST'])
def save_album():
    apple_id = request.form.get('apple_id', '').strip()
    apple_artist_id = request.form.get('apple_artist_id', '').strip()
    search_artist = request.form.get('search_artist', '').strip()
    cover_url = request.form.get('cover_url', '').strip()
    classification = request.form.get('classification', '').strip()
    media_format = request.form.get('media_format', '').strip()
    album_type = request.form.get('album_type', '').strip()
    genre = normalize_genre_name(request.form.get('genre', '').strip())
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
    SEARCH_CACHE.clear()
    TRACK_CACHE.clear()
    APPLE_PAGE_CACHE.clear()
    return redirect(url_for('index', saved=management_no))

@app.route('/edit/<int:album_row_id>')
def edit_album(album_row_id):
    conn = get_db()
    album = conn.execute('SELECT * FROM albums WHERE id = ?', (album_row_id,)).fetchone()
    conn.close()
    if album is None:
        return redirect(url_for('manage_albums'))
    return render_template('edit.html', album=album)

@app.route('/update/<int:album_row_id>', methods=['POST'])
def update_album(album_row_id):
    classification = request.form.get('classification', '').strip()
    media_format = request.form.get('media_format', '').strip()
    album_type = request.form.get('album_type', '').strip()
    genre = normalize_genre_name(request.form.get('genre', '').strip())
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
    TRACK_CACHE.clear()
    APPLE_PAGE_CACHE.clear()
    return redirect(url_for('manage_albums', updated=management_no))

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
    TRACK_CACHE.clear()
    APPLE_PAGE_CACHE.clear()
    return redirect(url_for('manage_albums', deleted=management_no))
if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=5001, debug=True)

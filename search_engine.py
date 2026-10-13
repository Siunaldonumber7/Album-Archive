import base64
import html as html_lib
import json
import re
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import unquote, urlparse

import requests
from requests.adapters import HTTPAdapter

from album_database import get_db, get_artist_profile_cache, save_artist_profile_cache
from cache_utils import BoundedCache
from catalog import (
    extract_apple_genre,
    normalize_apple_album_type,
    normalize_text,
)


APPLE_SESSION = requests.Session()
APPLE_SESSION.mount(
    'https://',
    HTTPAdapter(
        pool_connections=16,
        pool_maxsize=32,
        max_retries=0
    )
)
APPLE_WEB_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/154.0.0.0 Safari/537.36'
    ),
    'Accept-Language': 'ko-KR,ko;q=0.9,en;q=0.6',
    'Accept': (
        'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'
    )
}

SEARCH_CACHE = BoundedCache(300)
CACHE_SECONDS = 1800
APPLE_PAGE_CACHE = BoundedCache(300)
APPLE_PAGE_CACHE_SECONDS = 3600
KR_TITLE_CACHE = BoundedCache(1000)
APPLE_ARTIST_CACHE = BoundedCache(500)
APPLE_ARTIST_CACHE_SECONDS = 86400
ARTIST_ALBUM_CACHE = BoundedCache(150)
ARTIST_ALBUM_CACHE_SECONDS = 21600
ARTIST_PROFILE_MEMORY_CACHE = BoundedCache(1000)
ARTIST_PROFILE_MEMORY_SECONDS = 86400

MUSICBRAINZ_SESSION = requests.Session()
MUSICBRAINZ_SESSION.mount(
    'https://',
    HTTPAdapter(
        pool_connections=4,
        pool_maxsize=8,
        max_retries=0
    )
)
MUSICBRAINZ_SESSION.headers.update({
    'User-Agent': 'MyAlbumArchive/1.0 (personal music archive)',
    'Accept': 'application/json'
})
MUSICBRAINZ_CACHE = BoundedCache(300)
MUSICBRAINZ_CACHE_SECONDS = 86400
ARTIST_ALIAS_CACHE = BoundedCache(1000)
MUSICBRAINZ_RATE_LOCK = threading.Lock()
MUSICBRAINZ_LAST_REQUEST = 0.0


def clear_search_cache():
    SEARCH_CACHE.clear()


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

def contains_hangul(text):
    if not text:
        return False
    return bool(re.search('[가-힣]', str(text)))

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
    alias_key = normalize_text(
        artist_name
    )

    if not alias_key:
        return None

    cached = ARTIST_ALIAS_CACHE.get(
        alias_key
    )

    if cached is not None:
        return (
            cached
            if cached is not False
            else None
        )

    conn = get_db()

    try:
        result = conn.execute(
            '''
            SELECT
                canonical_name,
                apple_artist_id
            FROM artist_aliases
            WHERE alias_key = ?
            LIMIT 1
            ''',
            (alias_key,)
        ).fetchone()
    finally:
        conn.close()

    if not result:
        ARTIST_ALIAS_CACHE[
            alias_key
        ] = False
        return None

    value = {
        'artist_name': (
            result['canonical_name']
            or ''
        ),
        'artist_id': (
            result['apple_artist_id']
            or ''
        ),
    }
    ARTIST_ALIAS_CACHE[
        alias_key
    ] = value

    return value


def learn_artist_alias(
    conn,
    search_artist,
    canonical_name,
    apple_artist_id
):
    canonical_name = str(
        canonical_name
        or ''
    ).strip()
    apple_artist_id = str(
        apple_artist_id
        or ''
    ).strip()

    if (
        not canonical_name
        or not apple_artist_id
    ):
        return

    aliases = []

    if search_artist:
        aliases.append(
            str(search_artist).strip()
        )

    aliases.append(
        canonical_name
    )

    cache_value = {
        'artist_name': canonical_name,
        'artist_id': apple_artist_id,
    }

    seen = set()

    for alias in aliases:
        alias_key = normalize_text(
            alias
        )

        if (
            not alias_key
            or alias_key in seen
        ):
            continue

        seen.add(alias_key)

        conn.execute(
            '''
            INSERT INTO artist_aliases (
                alias_key,
                alias_text,
                canonical_name,
                apple_artist_id
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(alias_key)
            DO UPDATE SET
                alias_text = excluded.alias_text,
                canonical_name = excluded.canonical_name,
                apple_artist_id = excluded.apple_artist_id
            ''',
            (
                alias_key,
                alias,
                canonical_name,
                apple_artist_id,
            )
        )

        ARTIST_ALIAS_CACHE[
            alias_key
        ] = cache_value

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
                    urls.append('https://music.apple.com/kr/' + remainder + '?l=ko')
        except Exception:
            pass
    if collection_id:
        urls.append(f'https://music.apple.com/kr/album/x/{collection_id}?l=ko')
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

    # Apple Music 페이지의 JSON-LD / serialized data에는 화면에 쓰는
    # 한국어 앨범명이 들어오는 경우가 있다. 영문 메타 <title>보다 우선한다.
    page_metadata = find_album_metadata_in_page_data(
        page,
        collection_id
    )
    structured_title = str(
        page_metadata.get('title') or ''
    ).strip()

    if structured_title:
        KR_TITLE_CACHE[cache_key] = structured_title
        print(
            'KR APPLE DISPLAY TITLE:',
            fallback_title,
            '→',
            structured_title
        )
        return structured_title

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

def find_apple_artist_in_store(artist_name, store):
    if not artist_name:
        return None

    target = normalize_text(artist_name)
    cache_key = f'{store}|{target}'
    cached = APPLE_ARTIST_CACHE.get(cache_key)

    if cached and time.time() - cached['time'] < APPLE_ARTIST_CACHE_SECONDS:
        return cached['data']

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
                result = {
                    'artist_id': str(artist_id),
                    'artist_name': result_name,
                    'store': store
                }
                APPLE_ARTIST_CACHE[cache_key] = {
                    'time': time.time(),
                    'data': result
                }
                return result
            score = similarity(artist_name, result_name)
            if score > best_score:
                best_score = score
                best_candidate = {'artist_id': str(artist_id), 'artist_name': result_name, 'store': store}
    except Exception as e:
        print(f'ARTIST SEARCH ERROR [{store}]:', e)
    result = best_candidate if best_candidate and best_score >= 0.72 else None
    APPLE_ARTIST_CACHE[cache_key] = {
        'time': time.time(),
        'data': result
    }
    return result

def find_apple_artist_in_store(artist_name, store):
    if not artist_name:
        return None

    target = normalize_text(artist_name)
    cache_key = f'{store}|{target}'
    cached = APPLE_ARTIST_CACHE.get(cache_key)

    if cached and time.time() - cached['time'] < APPLE_ARTIST_CACHE_SECONDS:
        return cached['data']

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
                result = {
                    'artist_id': str(artist_id),
                    'artist_name': result_name,
                    'store': store
                }
                APPLE_ARTIST_CACHE[cache_key] = {
                    'time': time.time(),
                    'data': result
                }
                return result
            score = similarity(artist_name, result_name)
            if score > best_score:
                best_score = score
                best_candidate = {'artist_id': str(artist_id), 'artist_name': result_name, 'store': store}
    except Exception as e:
        print(f'ARTIST SEARCH ERROR [{store}]:', e)
    result = best_candidate if best_candidate and best_score >= 0.72 else None
    APPLE_ARTIST_CACHE[cache_key] = {
        'time': time.time(),
        'data': result
    }
    return result

def _artist_profile_cache_is_fresh(row, max_age_seconds=86400):
    if not row:
        return False

    updated_at = str(row['updated_at'] or '').strip()

    if not updated_at:
        return False

    try:
        checked = datetime.strptime(
            updated_at,
            '%Y-%m-%d %H:%M:%S'
        ).replace(tzinfo=timezone.utc)
        age = (
            datetime.now(timezone.utc) - checked
        ).total_seconds()
        return age < max_age_seconds
    except Exception:
        return False


def _get_artist_profile_memory(artist_key):
    cached = ARTIST_PROFILE_MEMORY_CACHE.get(
        artist_key
    )

    if cached is None:
        return None

    if (
        time.time() - cached['time']
        >= ARTIST_PROFILE_MEMORY_SECONDS
    ):
        return None

    return cached['url']


def _save_artist_profile_memory(
    artist_key,
    profile_url
):
    ARTIST_PROFILE_MEMORY_CACHE[
        artist_key
    ] = {
        'time': time.time(),
        'url': str(
            profile_url
            or ''
        ).strip(),
    }


def _find_artist_artwork_in_apple_page(page, artist_id=''):
    if not page:
        return ''

    parser = page.get('parser')
    artist_id = str(artist_id or '').strip()
    candidates = []

    def add_candidate(url, priority):
        url = normalize_apple_artwork_url(url)

        if not url or not url.startswith('http'):
            return

        candidates.append((priority, url))

    def inspect_object(value):
        if not isinstance(value, dict):
            return

        object_type = str(
            value.get('@type')
            or value.get('type')
            or ''
        ).lower()

        attrs = (
            value.get('attributes')
            if isinstance(value.get('attributes'), dict)
            else {}
        )

        object_id = str(
            value.get('id')
            or attrs.get('id')
            or ''
        ).strip()

        artwork = (
            attrs.get('artwork')
            if isinstance(attrs.get('artwork'), dict)
            else {}
        )

        if artwork.get('url'):
            priority = 0

            if object_type in {
                'artists',
                'artist',
                'musicgroup',
                'person',
            }:
                priority += 50

            if artist_id and object_id == artist_id:
                priority += 100

            add_candidate(
                artwork.get('url'),
                priority
            )

        if object_type in {
            'musicgroup',
            'person',
            'artist',
        }:
            add_candidate(
                extract_image_url(
                    value.get('image')
                ),
                60
            )

    if parser:
        for raw in (
            parser.ld_json_scripts
            + parser.serialized_scripts
        ):
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
                    stack.extend(value.values())
                elif isinstance(value, list):
                    stack.extend(value)

        for key in (
            'og:image',
            'twitter:image',
            'twitter:image:src',
        ):
            add_candidate(
                parser.meta_values.get(key, ''),
                20
            )

    if not candidates:
        return ''

    candidates.sort(
        key=lambda item: item[0],
        reverse=True
    )

    return candidates[0][1]


def get_artist_profile_image(
    artist_name,
    apple_artist_id=''
):
    artist_name = str(artist_name or '').strip()

    if not artist_name:
        return ''

    artist_key = normalize_text(artist_name)

    if not artist_key:
        return ''

    memory_profile_url = (
        _get_artist_profile_memory(
            artist_key
        )
    )

    if memory_profile_url is not None:
        return memory_profile_url

    cached = get_artist_profile_cache(
        artist_key
    )

    if cached:
        profile_url = str(
            cached['profile_url'] or ''
        ).strip()

        if profile_url:
            _save_artist_profile_memory(
                artist_key,
                profile_url
            )
            return profile_url

        # 실패 결과는 하루 동안만 재사용해서
        # 공개 페이지에서 반복 외부 호출이 발생하지 않게 한다.
        if _artist_profile_cache_is_fresh(
            cached
        ):
            _save_artist_profile_memory(
                artist_key,
                ''
            )
            return ''

    resolved_id = str(
        apple_artist_id or ''
    ).strip()
    preferred_store = 'KR'

    if not resolved_id:
        learned = find_learned_artist(
            artist_name
        )

        if learned:
            resolved_id = str(
                learned.get('artist_id')
                or ''
            ).strip()

    # 등록 과정에서 확보된 Apple Artist ID가 없는 경우에는
    # 공개 아티스트 페이지에서 느린 실시간 검색을 하지 않는다.
    # 수동 등록 아티스트는 즉시 앨범 커버 배경으로 fallback 한다.
    if not resolved_id:
        save_artist_profile_cache(
            artist_key,
            artist_name,
            '',
            '',
            'apple-id-missing'
        )
        _save_artist_profile_memory(
            artist_key,
            ''
        )
        return ''

    stores = []
    for store in (
        preferred_store,
        'KR',
        'US',
        'JP',
    ):
        store = str(store or '').upper()

        if (
            store
            and store not in stores
        ):
            stores.append(store)

    for store in stores:
        url = (
            'https://music.apple.com/'
            + store.lower()
            + '/artist/x/'
            + resolved_id
            + '?l=ko'
        )

        try:
            response = APPLE_SESSION.get(
                url,
                headers=APPLE_WEB_HEADERS,
                timeout=(2.0, 3.5),
                allow_redirects=True
            )

            if response.status_code != 200:
                continue

            html_text = response.content.decode(
                'utf-8',
                errors='replace'
            )

            if not html_text:
                continue

            page = {
                'url': response.url,
                'html': html_text,
                'parser': parse_apple_music_html(
                    html_text
                )
            }

            profile_url = (
                _find_artist_artwork_in_apple_page(
                    page,
                    resolved_id
                )
            )

            if profile_url:
                save_artist_profile_cache(
                    artist_key,
                    artist_name,
                    resolved_id,
                    profile_url,
                    'apple'
                )
                _save_artist_profile_memory(
                    artist_key,
                    profile_url
                )

                print(
                    'APPLE ARTIST PROFILE:',
                    artist_name,
                    resolved_id,
                    store
                )

                return profile_url

        except Exception as exc:
            print(
                'APPLE ARTIST PROFILE ERROR:',
                artist_name,
                resolved_id,
                store,
                exc
            )

    save_artist_profile_cache(
        artist_key,
        artist_name,
        resolved_id,
        '',
        'apple-miss'
    )
    _save_artist_profile_memory(
        artist_key,
        ''
    )

    return ''


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
        store_artist_id = str(artist_id or '').strip()

        if artist_name:
            local_artist = find_apple_artist_in_store(
                artist_name,
                store
            )

            if local_artist:
                store_artist_id = local_artist['artist_id']

        if not store_artist_id:
            continue

        cache_key = f'{store}|{store_artist_id}'
        cached = ARTIST_ALBUM_CACHE.get(cache_key)
        base_results = None

        if (
            cached
            and time.time() - cached['time'] < ARTIST_ALBUM_CACHE_SECONDS
        ):
            base_results = cached['results']
            print(
                'ARTIST ALBUM CACHE HIT:',
                store,
                store_artist_id,
                'COUNT:',
                len(base_results)
            )

        if base_results is None:
            store_results = []

            try:
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

                seen_ids = set()

                for item in data.get('results', []):
                    collection_id = item.get('collectionId')

                    if not collection_id:
                        continue

                    collection_id = str(collection_id)

                    if collection_id in seen_ids:
                        continue

                    title = item.get('collectionName', '')

                    if not title:
                        continue

                    seen_ids.add(collection_id)

                    result_artist = item.get('artistName', '')
                    release_date = item.get('releaseDate', '')[:10]
                    release_year = (
                        release_date[:4]
                        if release_date
                        else ''
                    )

                    store_results.append({
                        'apple_id': collection_id,
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
                        'album_type': normalize_apple_album_type(
                            item,
                            title
                        ),
                        'genre': extract_apple_genre(item),
                        'store': store,
                        'explicit': get_explicit_label(item),
                        'collection_url': item.get(
                            'collectionViewUrl',
                            ''
                        ),
                        'score': 0
                    })

            except Exception as e:
                print(
                    f'ALBUM LOOKUP ERROR [{store}]:',
                    e
                )
                continue

            if not store_results:
                continue

            ARTIST_ALBUM_CACHE[cache_key] = {
                'time': time.time(),
                'results': store_results
            }
            base_results = store_results

        # 캐시 원본은 수정하지 않는다. 검색어마다 점수/한글 제목이 달라질 수 있어
        # 얕은 복사본에만 점수와 표시 제목을 적용한다.
        results = [
            dict(item)
            for item in base_results
        ]

        if search_album:
            target = normalize_text(search_album)

            for item in results:
                result_title = normalize_text(
                    item.get('title', '')
                )
                score = 0

                if result_title == target:
                    score += 1000
                elif target and target in result_title:
                    score += 600
                elif result_title and result_title in target:
                    score += 400

                score += int(
                    similarity(
                        search_album,
                        item.get('title', '')
                    ) * 200
                )

                item['score'] = score

            results.sort(
                key=lambda item: item.get(
                    'score',
                    0
                ),
                reverse=True
            )

        else:
            results.sort(
                key=lambda item: item.get(
                    'release_year',
                    ''
                ),
                reverse=True
            )

        results = results[:40]

        if store == 'KR':
            localize_limit = (
                min(len(results), 40)
                if not search_album
                else min(len(results), 10)
            )

            results = localize_kr_results(
                results,
                max_items=localize_limit
            )

        return results

    return []

def discover_artist_albums_from_album_search(
    artist_name,
    stores=None
):
    """
    musicArtist 검색이 실패하는 아티스트를 위해 실제 앨범 검색 결과에서
    artistId를 역으로 찾아 전체 디스코그래피를 가져온다.
    """
    artist_name = str(artist_name or '').strip()

    if not artist_name:
        return []

    if stores is None:
        stores = ['KR', 'US']

    target = normalize_text(artist_name)

    for store in stores:
        try:
            response = APPLE_SESSION.get(
                'https://itunes.apple.com/search',
                params={
                    'term': artist_name,
                    'country': store,
                    'media': 'music',
                    'entity': 'album',
                    'limit': 100,
                    'explicit': 'Yes'
                },
                timeout=12
            )
            response.raise_for_status()
            rows = response.json().get('results', [])

        except Exception as e:
            print(
                f'ARTIST ALBUM FALLBACK SEARCH ERROR [{store}]:',
                e
            )
            continue

        candidates = {}

        for item in rows:
            artist_id = str(item.get('artistId') or '').strip()
            result_artist = str(item.get('artistName') or '').strip()

            if not artist_id or not result_artist:
                continue

            normalized_artist = normalize_text(result_artist)
            artist_similarity = similarity(
                artist_name,
                result_artist
            )

            exact = normalized_artist == target

            if not exact and artist_similarity < 0.72:
                continue

            candidate = candidates.setdefault(
                artist_id,
                {
                    'artist_id': artist_id,
                    'artist_name': result_artist,
                    'count': 0,
                    'best_score': 0
                }
            )

            candidate['count'] += 1

            score = int(artist_similarity * 1000)

            if exact:
                score += 3000

            if target and (
                target in normalized_artist
                or normalized_artist in target
            ):
                score += 700

            candidate['best_score'] = max(
                candidate['best_score'],
                score
            )

        if not candidates:
            continue

        ranked = sorted(
            candidates.values(),
            key=lambda item: (
                item['best_score'],
                item['count']
            ),
            reverse=True
        )

        best = ranked[0]

        print(
            'ARTIST RESOLVED FROM ALBUM SEARCH:',
            artist_name,
            '→',
            store,
            '→',
            best['artist_name'],
            best['artist_id']
        )

        resolved_artist = {
            'artist_id': best['artist_id'],
            'artist_name': best['artist_name'],
            'store': store
        }

        # musicArtist API에서 못 찾았던 이름도 한 번 앨범 검색으로 해결되면
        # 이후 24시간은 바로 이 artistId를 재사용한다.
        APPLE_ARTIST_CACHE[f'{store}|{target}'] = {
            'time': time.time(),
            'data': resolved_artist
        }

        canonical_key = normalize_text(
            best['artist_name']
        )
        if canonical_key:
            APPLE_ARTIST_CACHE[
                f'{store}|{canonical_key}'
            ] = {
                'time': time.time(),
                'data': resolved_artist
            }

        results = lookup_artist_albums(
            best['artist_id'],
            '',
            '',
            prefer_korean=(
                store == 'KR'
                and contains_hangul(artist_name)
            ),
            stores=[store]
        )

        if results:
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

                meta_key = str(collection_id)

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

    # Explicit/Clean 버전이 함께 있으면 Explicit 버전을 숨기지 않고
    # 사용자가 바로 찾을 수 있도록 같은 정확도에서 약간 우선한다.
    if item.get('explicit') == 'Explicit':
        score += 90

    item['score'] = score
    return score

def has_confident_explicit_result(
    results,
    search_artist='',
    search_album='',
    minimum_score=700
):
    if not results:
        return False

    best_explicit_score = -1

    for item in results:
        if item.get('explicit') != 'Explicit':
            continue

        score = score_album_search_result(
            item,
            search_artist,
            search_album
        )

        best_explicit_score = max(
            best_explicit_score,
            score
        )

    return best_explicit_score >= minimum_score

def merge_album_search_results(
    groups,
    search_artist='',
    search_album='',
    limit=40
):
    merged = []
    seen = set()

    for group in groups:
        for item in group or []:
            apple_id = str(item.get('apple_id') or '').strip()
            key = apple_id or (
                f"{item.get('store', '')}|"
                f"{normalize_text(item.get('artist', ''))}|"
                f"{normalize_text(item.get('title', ''))}|"
                f"{item.get('release_year', '')}|"
                f"{item.get('explicit', '')}"
            )

            if key in seen:
                continue

            seen.add(key)
            score_album_search_result(
                item,
                search_artist,
                search_album
            )
            merged.append(item)

    merged.sort(
        key=lambda item: (
            item.get('score', 0),
            1 if item.get('explicit') == 'Explicit' else 0,
            1 if item.get('store') == 'KR' else 0,
            item.get('release_year', '')
        ),
        reverse=True
    )

    return merged[:limit]

def localize_top_kr_candidates(
    results,
    search_artist='',
    search_album='',
    score_limit=3,
    api_limit=2
):
    """
    iTunes 결과 전체를 Apple Music 웹페이지로 보정하지 않고,
    1차 점수 상위 후보 + Apple API 원래 상위 후보만 한글 제목을 확인한다.
    """
    if not results:
        return results

    # 검색어 언어와 관계없이 한국 Apple Music 표시명을 최종 기준으로 쓴다.
    # 먼저 네트워크 호출 없이 1차 점수를 계산한다.
    for item in results:
        score_album_search_result(
            item,
            search_artist,
            search_album
        )

    ranked_indexes = sorted(
        range(len(results)),
        key=lambda index: results[index].get('score', 0),
        reverse=True
    )

    candidate_indexes = []
    seen = set()

    # 검색 점수 상위 후보.
    for index in ranked_indexes[:score_limit]:
        if index not in seen:
            seen.add(index)
            candidate_indexes.append(index)

    # iTunes API 자체 랭킹 상위 후보도 보존한다.
    # 한글 검색어인데 제목이 영어로 내려와 1차 점수가 낮은 경우를 방지한다.
    for index in range(min(api_limit, len(results))):
        if index not in seen:
            seen.add(index)
            candidate_indexes.append(index)

    localized_candidates = [
        dict(results[index])
        for index in candidate_indexes
    ]

    localized_candidates = localize_kr_results(
        localized_candidates,
        max_items=len(localized_candidates)
    )

    for index, localized in zip(
        candidate_indexes,
        localized_candidates
    ):
        localized_title = str(
            localized.get('title') or ''
        ).strip()

        if localized_title:
            results[index]['title'] = localized_title

    # 한글 보정 뒤 전체 결과를 다시 점수 계산한다.
    for item in results:
        score_album_search_result(
            item,
            search_artist,
            search_album
        )

    print(
        'KR LOCALIZE SHORTLIST:',
        len(candidate_indexes),
        '/',
        len(results)
    )

    return results

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

        # 1차 점수를 먼저 계산하고, 유력 후보만 Apple Music KR
        # 웹페이지에서 한글 제목을 확인한 뒤 최종 점수를 다시 계산한다.
        if results:
            results = localize_top_kr_candidates(
                results,
                search_artist,
                search_album,
                score_limit=3,
                api_limit=2
            )
        else:
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

def get_cached_artist_search_results(
    artist_name,
    search_album='',
    learned=None
):
    artist_name = str(artist_name or '').strip()

    if not artist_name:
        return []

    now = time.time()
    normalized_artist = normalize_text(artist_name)
    candidates = []
    seen = set()

    # 먼저 현재 프로세스에서 해결해 둔 아티스트 ID를 본다.
    for store in ['KR', 'US']:
        cached_artist = APPLE_ARTIST_CACHE.get(
            f'{store}|{normalized_artist}'
        )

        if (
            cached_artist
            and now - cached_artist['time'] < APPLE_ARTIST_CACHE_SECONDS
            and cached_artist.get('data')
        ):
            data = cached_artist['data']
            artist_id = str(
                data.get('artist_id') or ''
            ).strip()

            key = (store, artist_id)

            if artist_id and key not in seen:
                seen.add(key)
                candidates.append(key)

    # 등록 과정에서 학습한 artistId도 캐시 키 후보로 사용한다.
    if learned and learned.get('artist_id'):
        learned_id = str(
            learned.get('artist_id') or ''
        ).strip()

        for store in ['KR', 'US']:
            key = (store, learned_id)

            if learned_id and key not in seen:
                seen.add(key)
                candidates.append(key)

    for store, artist_id in candidates:
        discography = ARTIST_ALBUM_CACHE.get(
            f'{store}|{artist_id}'
        )

        if not (
            discography
            and now - discography['time'] < ARTIST_ALBUM_CACHE_SECONDS
        ):
            continue

        results = lookup_artist_albums(
            artist_id,
            search_album,
            '',
            prefer_korean=(
                store == 'KR'
                and (
                    contains_hangul(artist_name)
                    or contains_hangul(search_album)
                )
            ),
            stores=[store]
        )

        if not results:
            continue

        if search_album and not confident_album_search(
            results,
            650
        ):
            continue

        print(
            'SEARCH FAST CACHE:',
            artist_name,
            '→',
            store,
            artist_id
        )
        return results

    return []

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

def lookup_album_metadata_store(collection_id, store='KR'):
    collection_id = str(collection_id or '').strip()
    store = str(store or 'KR').strip().upper()

    if not collection_id or not collection_id.isdigit():
        return {}

    if store not in {'KR', 'US', 'JP', 'GB'}:
        store = 'KR'

    try:
        response = APPLE_SESSION.get(
            'https://itunes.apple.com/lookup',
            params={
                'id': collection_id,
                'country': store,
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

            if raw_item.get('collectionName') and raw_id == collection_id:
                item = album_item_to_result(raw_item, store)

                if item and store == 'KR':
                    item['title'] = get_kr_localized_album_title(
                        item['apple_id'],
                        item.get('collection_url', ''),
                        item['title']
                    )

                return item or {}

    except Exception as e:
        print(f'APPLE BULK LOOKUP ERROR [{store}]:', e)

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
    learned = find_learned_artist(artist) if artist else None
    musicbrainz_names = []

    # =====================================================
    # 0순위: 이미 받아둔 아티스트 디스코그래피가 있으면
    # 네트워크 요청 전에 메모리 캐시에서 먼저 찾는다.
    # =====================================================
    if artist:
        results = get_cached_artist_search_results(
            artist,
            album,
            learned
        )

        if learned:
            learned_name = learned.get(
                'artist_name',
                ''
            ) or ''

    # =====================================================
    # 1순위: Apple KR 앨범명 직접검색
    # =====================================================
    if not results and album:
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
    # 4.5순위: musicArtist 검색이 실패하면 실제 앨범 검색에서
    # artistId를 역추적한다. wave to earth 같은 케이스 대응.
    # =====================================================
    if not results and artist:
        print(
            'SEARCH STEP 4.5: ALBUM SEARCH → ARTIST ID →',
            artist
        )

        results = discover_artist_albums_from_album_search(
            artist,
            stores=['KR', 'US']
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

    # =====================================================
    # Explicit 보강
    # 이미 충분히 정확한 Explicit 결과가 있으면 전체 디스코그래피를
    # 다시 조회하지 않는다. Explicit가 없거나 애매할 때만 KR → US 순서로
    # 보강해서 Clean / Explicit 버전을 최대한 함께 살린다.
    # =====================================================
    if artist and album:
        result_groups = [results]

        if has_confident_explicit_result(
            results,
            artist,
            album,
            minimum_score=700
        ):
            print(
                'EXPLICIT SUPPLEMENT SKIP:',
                artist,
                '/',
                album
            )
        else:
            canonical_artist = (
                learned_name
                or (
                    results[0].get('artist', '')
                    if results
                    else ''
                )
                or artist
            )

            kr_discography = search_artist_albums_in_stores(
                canonical_artist,
                album,
                ['KR'],
                prefer_korean=False
            )
            result_groups.append(kr_discography)

            combined_now = merge_album_search_results(
                result_groups,
                artist,
                album,
                limit=40
            )

            # KR lookup에도 Explicit 버전이 없을 때만 US 카탈로그를 한 번 더 본다.
            if not has_confident_explicit_result(
                combined_now,
                artist,
                album,
                minimum_score=700
            ):
                us_discography = search_artist_albums_in_stores(
                    canonical_artist,
                    album,
                    ['US'],
                    prefer_korean=False
                )
                result_groups.append(us_discography)

        results = merge_album_search_results(
            result_groups,
            artist,
            album,
            limit=40
        )
    else:
        results = merge_album_search_results(
            [results],
            artist,
            album,
            limit=40
        )

    SEARCH_CACHE[cache_key] = {
        'time': time.time(),
        'results': results
    }

    return results


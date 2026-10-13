import hashlib
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

from album_database import get_db
from catalog import normalize_bugs_genres, normalize_genres


BUGS_SEARCH_URL = 'https://music.bugs.co.kr/search/album?q={query}'
BUGS_ALBUM_URL = 'https://music.bugs.co.kr/album/{album_id}'
BUGS_SEARCH_LIMIT = 5
BUGS_MATCH_THRESHOLD = 0.68

BUGS_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/122.0.0.0 Safari/537.36'
    ),
    'Accept': (
        'text/html,application/xhtml+xml,application/xml;'
        'q=0.9,*/*;q=0.8'
    ),
    'Accept-Language': 'ko-KR,ko;q=0.9,en-US;q=0.7,en;q=0.6',
    'Referer': 'https://music.bugs.co.kr/',
}


def _clean_text(value):
    value = unicodedata.normalize(
        'NFKC',
        str(value or '')
    )
    value = re.sub(r'\s+', ' ', value)
    return value.strip()


def _compact_text(value):
    value = _clean_text(value).casefold()
    return re.sub(
        r'[^0-9a-z가-힣]',
        '',
        value
    )


def _cache_key(artist, album_title, release_year=''):
    raw = '|'.join([
        _compact_text(artist),
        _compact_text(album_title),
        str(release_year or '').strip(),
    ])
    return hashlib.sha1(
        raw.encode('utf-8')
    ).hexdigest()


def _cached_genre(artist, album_title, release_year=''):
    key = _cache_key(
        artist,
        album_title,
        release_year
    )

    conn = get_db()

    try:
        row = conn.execute(
            '''
            SELECT normalized_genre, bugs_album_id
            FROM bugs_genre_cache
            WHERE cache_key = ?
            LIMIT 1
            ''',
            (key,)
        ).fetchone()
    finally:
        conn.close()

    if not row:
        return None

    genre = normalize_genres(
        row['normalized_genre']
    )

    if not genre:
        return None

    print(
        'BUGS GENRE CACHE HIT:',
        artist,
        '|',
        album_title,
        '→',
        genre
    )

    return genre


def _save_cache(
    artist,
    album_title,
    release_year,
    bugs_album_id,
    raw_genre,
    normalized_genre
):
    key = _cache_key(
        artist,
        album_title,
        release_year
    )

    conn = get_db()

    try:
        conn.execute(
            '''
            INSERT INTO bugs_genre_cache (
                cache_key,
                artist,
                album_title,
                release_year,
                bugs_album_id,
                raw_genre,
                normalized_genre,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(cache_key) DO UPDATE SET
                artist = excluded.artist,
                album_title = excluded.album_title,
                release_year = excluded.release_year,
                bugs_album_id = excluded.bugs_album_id,
                raw_genre = excluded.raw_genre,
                normalized_genre = excluded.normalized_genre,
                updated_at = CURRENT_TIMESTAMP
            ''',
            (
                key,
                artist,
                album_title,
                str(release_year or '').strip(),
                bugs_album_id,
                raw_genre,
                normalized_genre,
            )
        )
        conn.commit()
    finally:
        conn.close()


def _fetch_html(url, timeout=(2.5, 3.5)):
    response = requests.get(
        url,
        headers=BUGS_HEADERS,
        timeout=timeout
    )
    response.raise_for_status()
    return response.text


def _search_album_ids(artist, album_title):
    query = ' '.join(
        value
        for value in [
            _clean_text(artist),
            _clean_text(album_title),
        ]
        if value
    ).strip()

    if not query:
        return []

    url = BUGS_SEARCH_URL.format(
        query=quote_plus(query)
    )
    html = _fetch_html(url)
    soup = BeautifulSoup(
        html,
        'html.parser'
    )

    album_ids = []
    seen = set()

    def add_album_id(href):
        match = re.search(
            r'/album/(\d+)',
            str(href or '')
        )

        if not match:
            return

        album_id = match.group(1)

        if album_id in seen:
            return

        seen.add(album_id)
        album_ids.append(album_id)

    # 벅스 앨범 검색 결과 영역을 우선 사용한다.
    for row in soup.select(
        'table.byAlbum tbody tr'
    ):
        anchor = (
            row.select_one(
                'p.title a[href*="/album/"]'
            )
            or row.select_one(
                'a[href*="/album/"]'
            )
        )

        if anchor:
            add_album_id(
                anchor.get('href')
            )

        if len(album_ids) >= BUGS_SEARCH_LIMIT:
            break

    # 페이지 형태가 달라졌을 때 타일형 앨범 목록도 확인한다.
    if not album_ids:
        for anchor in soup.select(
            'ul.albumList a[href*="/album/"], '
            'ul.list.tileView a[href*="/album/"]'
        ):
            add_album_id(
                anchor.get('href')
            )

            if len(album_ids) >= BUGS_SEARCH_LIMIT:
                break

    # 마지막 폴백: 앨범 URL 패턴만 추출한다.
    if not album_ids:
        for match in re.finditer(
            r'(?:https?:)?//music\.bugs\.co\.kr/album/(\d+)|'
            r'/album/(\d+)',
            html
        ):
            album_id = (
                match.group(1)
                or match.group(2)
            )

            if not album_id or album_id in seen:
                continue

            seen.add(album_id)
            album_ids.append(album_id)

            if len(album_ids) >= BUGS_SEARCH_LIMIT:
                break

    return album_ids


def _parse_og_title(soup):
    tag = soup.select_one(
        'meta[property="og:title"]'
    )

    if not tag:
        return '', ''

    value = _clean_text(
        tag.get('content') or ''
    )

    value = re.sub(
        r'\s*-\s*벅스\s*$',
        '',
        value
    ).strip()

    if ' / ' in value:
        parts = value.split(' / ')
        return (
            _clean_text(parts[0]),
            _clean_text(
                ' / '.join(parts[1:])
            )
        )

    if '/' in value:
        parts = value.split('/')
        return (
            _clean_text(parts[0]),
            _clean_text(
                '/'.join(parts[1:])
            )
        )

    return value, ''


def _parse_album_page(album_id):
    html = _fetch_html(
        BUGS_ALBUM_URL.format(
            album_id=album_id
        )
    )
    soup = BeautifulSoup(
        html,
        'html.parser'
    )

    title = ''
    artist = ''
    raw_genre = ''
    release_date = ''

    heading = soup.select_one('h1')

    if heading:
        title = _clean_text(
            heading.get_text(
                ' ',
                strip=True
            )
        )

    og_title, og_artist = _parse_og_title(
        soup
    )

    if not title:
        title = og_title

    info_table = soup.select_one(
        'table.info'
    )

    if info_table:
        for row in info_table.select('tr'):
            th = row.select_one('th')
            td = row.select_one('td')

            if not th or not td:
                continue

            label = _clean_text(
                th.get_text(
                    ' ',
                    strip=True
                )
            )

            if label == '아티스트':
                artist_links = [
                    _clean_text(
                        node.get_text(
                            ' ',
                            strip=True
                        )
                    )
                    for node in td.select(
                        'a[href*="/artist/"]'
                    )
                ]
                artist_links = [
                    value
                    for value in artist_links
                    if value
                ]

                artist = (
                    ', '.join(artist_links)
                    if artist_links
                    else _clean_text(
                        td.get_text(
                            ' ',
                            strip=True
                        )
                    )
                )

            elif label == '장르':
                raw_genre = _clean_text(
                    td.get_text(
                        ' ',
                        strip=True
                    )
                )

            elif label == '발매일':
                release_date = _clean_text(
                    td.get_text(
                        ' ',
                        strip=True
                    )
                )

    if not artist:
        artist = og_artist

    if not title:
        title = og_title

    return {
        'album_id': str(album_id),
        'title': title,
        'artist': artist,
        'raw_genre': raw_genre,
        'genre': normalize_bugs_genres(
            raw_genre
        ),
        'release_year': (
            release_date[:4]
            if re.match(
                r'^\d{4}',
                release_date
            )
            else ''
        ),
    }


def _similarity(left, right):
    left = _compact_text(left)
    right = _compact_text(right)

    if not left or not right:
        return 0.0

    if left == right:
        return 1.0

    if left in right or right in left:
        shorter = min(
            len(left),
            len(right)
        )
        longer = max(
            len(left),
            len(right)
        )
        return max(
            0.78,
            shorter / longer
        )

    return SequenceMatcher(
        None,
        left,
        right
    ).ratio()


def _artist_similarity(query_artist, candidate_artist):
    direct = _similarity(
        query_artist,
        candidate_artist
    )

    query_parts = [
        value.strip()
        for value in re.split(
            r'\s+[&x×]\s+|,',
            _clean_text(query_artist),
            flags=re.IGNORECASE
        )
        if value.strip()
    ]
    candidate_parts = [
        value.strip()
        for value in re.split(
            r'\s+[&x×]\s+|,',
            _clean_text(candidate_artist),
            flags=re.IGNORECASE
        )
        if value.strip()
    ]

    best = direct

    for query_part in query_parts:
        for candidate_part in candidate_parts:
            best = max(
                best,
                _similarity(
                    query_part,
                    candidate_part
                )
            )

    return best


def _score_candidate(
    artist,
    album_title,
    release_year,
    candidate,
    position
):
    title_score = _similarity(
        album_title,
        candidate.get('title', '')
    )
    artist_score = _artist_similarity(
        artist,
        candidate.get('artist', '')
    )

    year = str(
        release_year or ''
    ).strip()
    candidate_year = str(
        candidate.get(
            'release_year',
            ''
        ) or ''
    ).strip()

    year_score = (
        1.0
        if (
            year
            and candidate_year
            and year == candidate_year
        )
        else 0.0
    )

    score = (
        title_score * 0.68
        + artist_score * 0.27
        + year_score * 0.05
        - min(position, 4) * 0.005
    )

    # 동명 앨범 오매칭 방지.
    if title_score < 0.68:
        return 0.0

    if (
        artist_score < 0.42
        and not (
            title_score >= 0.96
            and year_score == 1.0
        )
    ):
        return 0.0

    return score


def lookup_bugs_genre(
    artist,
    album_title,
    release_year=''
):
    artist = _clean_text(artist)
    album_title = _clean_text(album_title)
    release_year = str(
        release_year or ''
    ).strip()

    if not artist or not album_title:
        return ''

    cached = _cached_genre(
        artist,
        album_title,
        release_year
    )

    if cached:
        return cached

    try:
        album_ids = _search_album_ids(
            artist,
            album_title
        )

        if not album_ids:
            print(
                'BUGS GENRE NO SEARCH RESULT:',
                artist,
                '|',
                album_title
            )
            return ''

        worker_count = min(
            4,
            len(album_ids)
        )

        with ThreadPoolExecutor(
            max_workers=worker_count
        ) as executor:
            candidates = list(
                executor.map(
                    _parse_album_page,
                    album_ids
                )
            )

        scored = []

        for position, candidate in enumerate(
            candidates
        ):
            genre = candidate.get(
                'genre',
                ''
            )

            if not genre:
                continue

            score = _score_candidate(
                artist,
                album_title,
                release_year,
                candidate,
                position
            )

            if score > 0:
                scored.append(
                    (
                        score,
                        position,
                        candidate
                    )
                )

        if not scored:
            print(
                'BUGS GENRE NO MATCH:',
                artist,
                '|',
                album_title
            )
            return ''

        scored.sort(
            key=lambda item: (
                item[0],
                -item[1]
            ),
            reverse=True
        )

        best_score, _, best = scored[0]

        if best_score < BUGS_MATCH_THRESHOLD:
            print(
                'BUGS GENRE LOW CONFIDENCE:',
                round(best_score, 3),
                artist,
                '|',
                album_title
            )
            return ''

        genre = normalize_genres(
            best.get('genre', '')
        )

        if not genre:
            return ''

        _save_cache(
            artist,
            album_title,
            release_year,
            best.get('album_id', ''),
            best.get('raw_genre', ''),
            genre
        )

        print(
            'BUGS GENRE MATCH:',
            round(best_score, 3),
            artist,
            '|',
            album_title,
            '→',
            genre
        )

        return genre

    except Exception as exc:
        print(
            'BUGS GENRE ERROR:',
            artist,
            '|',
            album_title,
            exc
        )
        return ''


def prefer_bugs_genre(
    artist,
    album_title,
    release_year='',
    fallback_genre=''
):
    bugs_genre = lookup_bugs_genre(
        artist,
        album_title,
        release_year
    )

    if bugs_genre:
        return bugs_genre

    return normalize_genres(
        fallback_genre
    )

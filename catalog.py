import re


ALBUM_TYPES = ('앨범', 'EP(미니)', '싱글', '믹스테잎')

GENRE_OPTIONS = (
    '힙합', 'R&B', '소울', 'Pop', 'K-Pop', 'J-Pop',
    '록', '인디 록', '얼터너티브', '얼터너티브/인디', '인디',
    '펑크', '펑크 록', '메탈', '하드 록', '프로그레시브 록', '사이키델릭',
    '일렉트로닉', '일렉트로니카', '댄스', '하우스', '테크노', '트랜스',
    '앰비언트', '다운템포', '재즈', '블루스', '포크', '컨트리',
    '레게', '스카', '펑크(Funk)', '디스코', '싱어송라이터', '보컬',
    '이지 리스닝', '어덜트 컨템포러리', '클래식', '오페라', '뉴에이지',
    '사운드트랙', '영화음악', '뮤지컬', '라틴', '브라질', '아프리카',
    '월드뮤직', 'CCM', '가스펠', 'CCM/가스펠', '어린이 음악',
    '홀리데이', '크리스마스', '코미디', '스포큰 워드', '피트니스',
    '디즈니', '만도팝', '칸토팝', '프렌치 팝', '저먼 팝',
    '인도 음악', '볼리우드', '한국 음악', '일본 음악'
)


def normalize_genre_name(value):
    value = str(value or '').strip()

    if not value:
        return ''

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


def artist_credit_parts(value):
    """
    공동 아티스트 표기를 링크 가능한 개별 아티스트로 나눈다.
    원래 구분자(&, 쉼표)는 화면에서 그대로 다시 표시할 수 있게 보존한다.
    """
    text = str(value or '').strip()

    if not text:
        return []

    tokens = re.split(r'(\s+&\s+|,\s*)', text)
    parts = []
    separator = ''

    for token in tokens:
        if token is None or token == '':
            continue

        if re.fullmatch(r'\s+&\s+|,\s*', token):
            separator = token
            continue

        name = token.strip()
        if not name:
            continue

        parts.append({
            'name': name,
            'separator': separator if parts else ''
        })
        separator = ''

    return parts


def split_artists(value):
    return [
        part['name']
        for part in artist_credit_parts(value)
        if part.get('name')
    ]


HANGUL_INITIALS = (
    'ㄱ', 'ㄲ', 'ㄴ', 'ㄷ', 'ㄸ', 'ㄹ', 'ㅁ',
    'ㅂ', 'ㅃ', 'ㅅ', 'ㅆ', 'ㅇ', 'ㅈ', 'ㅉ',
    'ㅊ', 'ㅋ', 'ㅌ', 'ㅍ', 'ㅎ'
)


def artist_group_key(value):
    name = str(value or '').strip()

    if not name:
        return '#'

    first = name[0]
    code = ord(first)

    if 0xAC00 <= code <= 0xD7A3:
        initial_index = (code - 0xAC00) // 588
        return HANGUL_INITIALS[initial_index]

    upper = first.upper()
    if 'A' <= upper <= 'Z':
        return upper

    return '#'


def artist_group_sort_key(group):
    group = str(group or '#')

    # 한국 사용자 기준: 가나다(초성) → A-Z → 기타 순서.
    if group in HANGUL_INITIALS:
        return (0, HANGUL_INITIALS.index(group))

    if len(group) == 1 and 'A' <= group <= 'Z':
        return (1, ord(group))

    return (2, 0)


def split_genres(value):
    values = []
    seen = set()

    for raw in str(value or '').split(','):
        genre = normalize_genre_name(raw.strip())
        key = genre.casefold()

        if genre and key not in seen:
            seen.add(key)
            values.append(genre)

    return values


def normalize_genres(values):
    if values is None:
        return ''

    if isinstance(values, str):
        values = [values]

    flattened = []

    for value in values:
        flattened.extend(str(value or '').split(','))

    normalized = []
    seen = set()

    for raw in flattened:
        genre = normalize_genre_name(raw.strip())
        key = genre.casefold()

        if genre and key not in seen:
            seen.add(key)
            normalized.append(genre)

    return ', '.join(normalized)


def genre_choices(current=''):
    choices = list(GENRE_OPTIONS)

    for genre in split_genres(current):
        if genre not in choices:
            choices.append(genre)

    return choices


def extract_apple_genre(item):
    if not isinstance(item, dict):
        return ''

    attrs = (
        item.get('attributes')
        if isinstance(item.get('attributes'), dict)
        else {}
    )
    genre_names = (
        attrs.get('genreNames')
        or item.get('genreNames')
    )

    if isinstance(genre_names, list):
        cleaned = [
            str(value or '').strip()
            for value in genre_names
            if str(value or '').strip()
            and str(value or '').strip().lower()
            not in {'music', '음악'}
        ]

        if cleaned:
            return normalize_genres(cleaned)

    raw = (
        item.get('primaryGenreName')
        or attrs.get('primaryGenreName')
        or item.get('genre')
        or attrs.get('genre')
        or ''
    )

    if isinstance(raw, list):
        return normalize_genres(raw)

    return normalize_genres(raw)


def normalize_apple_album_type(item, title=''):
    item = item if isinstance(item, dict) else {}
    attrs = (
        item.get('attributes')
        if isinstance(item.get('attributes'), dict)
        else {}
    )
    title = str(
        title
        or item.get('collectionName')
        or item.get('name')
        or attrs.get('name')
        or ''
    ).strip()

    raw = ' '.join(
        str(value or '')
        for value in [
            item.get('collectionType'),
            item.get('kind'),
            item.get('albumType'),
            attrs.get('collectionType'),
            attrs.get('kind'),
            attrs.get('albumType')
        ]
    ).lower()
    title_lower = title.lower()

    if (
        'mixtape' in raw
        or 'mix tape' in raw
        or re.search(r'\bmixtape\b', title_lower)
    ):
        return '믹스테잎'

    if (
        raw.strip() == 'ep'
        or 'extended play' in raw
        or re.search(r'[-–—]\s*ep\s*$', title_lower)
        or re.search(r'\(ep\)\s*$', title_lower)
    ):
        return 'EP(미니)'

    is_single = item.get('isSingle')

    if is_single is None:
        is_single = attrs.get('isSingle')

    if (
        is_single is True
        or 'single' in raw
        or re.search(
            r'[-–—]\s*single\s*$',
            title_lower
        )
    ):
        return '싱글'

    return '앨범'

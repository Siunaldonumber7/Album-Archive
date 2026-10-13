import re


ALBUM_TYPES = ('앨범', 'EP(미니)', '싱글', '믹스테잎')


def normalize_text(value):
    if not value:
        return ''

    text = str(
        value
    ).casefold().strip()

    return re.sub(
        r'[^a-z0-9가-힣ぁ-んァ-ン一-龥]+',
        '',
        text
    )


def apple_artwork_size(
    url,
    size=400
):
    url = str(
        url
        or ''
    ).strip()

    if not url:
        return ''

    size = max(
        120,
        min(
            int(size or 400),
            2000
        )
    )

    url = re.sub(
        r'\d+x\d+bb',
        f'{size}x{size}bb',
        url
    )
    return re.sub(
        r'\d+x\d+-75',
        f'{size}x{size}-75',
        url
    )


def normalize_genre_name(value):
    """
    장르는 사용자가 만든 이름을 그대로 사용한다.
    코드에서 번역/통합/삭제 규칙을 적용하지 않는다.
    """
    return str(value or '').strip()


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


def name_sort_key(value):
    """
    사용자 컬렉션용 이름 정렬 기준:
    한글 → 영어 → 숫자 → 기타.
    숫자로 시작하면 선행 숫자를 실제 숫자값으로 비교한다.
    """
    name = str(value or '').strip()

    if not name:
        return (4, '', '')

    first = name[0]
    code = ord(first)

    if (
        0xAC00 <= code <= 0xD7A3
        or 0x3131 <= code <= 0x318E
        or 0x1100 <= code <= 0x11FF
    ):
        return (0, name, '')

    upper = first.upper()
    if 'A' <= upper <= 'Z':
        return (1, name.casefold(), '')

    if first.isdigit():
        match = re.match(r'^(\d+)', name)
        number = int(match.group(1)) if match else 0
        rest = name[match.end():].casefold() if match else name.casefold()
        return (2, number, rest)

    return (3, name.casefold(), '')


def artist_group_key(value):
    name = str(value or '').strip()

    if not name:
        return '#'

    first = name[0]
    code = ord(first)

    if first.isdigit():
        return '0-9'

    if 0xAC00 <= code <= 0xD7A3:
        initial_index = (code - 0xAC00) // 588
        return HANGUL_INITIALS[initial_index]

    if 0x3131 <= code <= 0x318E:
        return first

    upper = first.upper()
    if 'A' <= upper <= 'Z':
        return upper

    return '#'


def artist_group_sort_key(group):
    group = str(group or '#')

    # 가나다(초성) → A-Z → 숫자 → 기타.
    if group in HANGUL_INITIALS:
        return (0, HANGUL_INITIALS.index(group))

    if len(group) == 1 and 'A' <= group <= 'Z':
        return (1, ord(group))

    if group == '0-9':
        return (2, 0)

    return (3, 0)


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
    return split_genres(current)


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

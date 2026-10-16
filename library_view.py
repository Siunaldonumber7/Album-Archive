import re

from catalog import (
    ALBUM_TYPES,
    artist_group_key,
    artist_group_sort_key,
    name_sort_key,
    split_artists,
)


INDEX_STATE_KEYS = (
    'library_q',
    'classification',
    'media_format',
    'album_type',
    'genre',
    'sort',
)

INDEX_SORT_SQL = {
    'release_desc': (
        "CASE "
        "WHEN release_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' "
        "THEN 0 "
        "WHEN release_year GLOB '[0-9][0-9][0-9][0-9]' "
        "THEN 0 ELSE 1 END ASC, "
        "CASE "
        "WHEN release_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' "
        "THEN release_date "
        "WHEN release_year GLOB '[0-9][0-9][0-9][0-9]' "
        "THEN release_year || '-00-00' "
        "ELSE '' END DESC, "
        "album_title COLLATE NOCASE ASC, id DESC"
    ),
    'release_asc': (
        "CASE "
        "WHEN release_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' "
        "THEN 0 "
        "WHEN release_year GLOB '[0-9][0-9][0-9][0-9]' "
        "THEN 0 ELSE 1 END ASC, "
        "CASE "
        "WHEN release_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' "
        "THEN release_date "
        "WHEN release_year GLOB '[0-9][0-9][0-9][0-9]' "
        "THEN release_year || '-00-00' "
        "ELSE '' END ASC, "
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

INDEX_ALLOWED_SORTS = frozenset(
    set(INDEX_SORT_SQL)
    | {'artist', 'title'}
)

ARTIST_ALLOWED_SORTS = frozenset({
    'release_desc',
    'release_asc',
    'price_desc',
    'price_asc',
    'title',
})

FORMAT_ORDER = (
    'CD',
    'LP',
    'CASETTE',
    'ETC',
)


def clean_text(value):
    return str(value or '').strip()


def build_fts_query(value):
    tokens = re.findall(
        r'[0-9A-Za-z_가-힣]+',
        clean_text(value)
    )

    if not tokens:
        return ''

    return ' AND '.join(
        f'{token}*'
        for token in tokens[:8]
    )


def resolve_index_state(args, session):
    reset_filters = (
        clean_text(
            args.get('reset_filters', '')
        ) == '1'
    )
    has_explicit_state = any(
        key in args
        for key in INDEX_STATE_KEYS
    )

    if reset_filters:
        session.pop(
            'album_index_state',
            None
        )
        saved_state = {}
    else:
        saved_state = session.get(
            'album_index_state',
            {}
        )
        if not isinstance(
            saved_state,
            dict
        ):
            saved_state = {}

    def value_for(key, default=''):
        if has_explicit_state:
            return args.get(
                key,
                default
            )
        return saved_state.get(
            key,
            default
        )

    state = {
        'library_q': clean_text(
            value_for('library_q')
        ),
        'classification': clean_text(
            value_for('classification')
        ),
        'media_format': clean_text(
            value_for('media_format')
        ),
        'album_type': clean_text(
            value_for('album_type')
        ),
        'genre': clean_text(
            value_for('genre')
        ),
        'sort': clean_text(
            value_for(
                'sort',
                'release_desc'
            )
        ) or 'release_desc',
    }

    if (
        state['sort']
        not in INDEX_ALLOWED_SORTS
    ):
        state['sort'] = 'release_desc'

    if not reset_filters:
        session['album_index_state'] = {
            key: value
            for key, value in state.items()
            if value
        }

    return state


def build_index_where(
    state,
    use_fts=False
):
    conditions = [
        'is_primary_display = 1'
    ]
    params = []

    library_q = clean_text(
        state.get('library_q')
    )
    classification = clean_text(
        state.get('classification')
    )
    media_format = clean_text(
        state.get('media_format')
    )
    album_type = clean_text(
        state.get('album_type')
    )
    genre = clean_text(
        state.get('genre')
    )

    if library_q:
        fts_query = (
            build_fts_query(
                library_q
            )
            if use_fts
            else ''
        )

        value = f'%{library_q}%'

        if fts_query:
            conditions.append(
                '('
                ' id IN ('
                '  SELECT rowid'
                '  FROM album_search'
                '  WHERE album_search MATCH ?'
                ' )'
                ')'
            )
            params.extend([
                fts_query,
            ])
        else:
            conditions.append(
                '('
                ' management_no LIKE ?'
                ' OR album_id LIKE ?'
                ' OR artist LIKE ?'
                ' OR album_title LIKE ?'
                ' OR release_year LIKE ?'
                ' OR memo LIKE ?'
                ' OR barcode LIKE ?'
                ' OR catalog_no LIKE ?'
                ' OR release_country LIKE ?'
                ' OR edition LIKE ?'
                ' OR purchase_store LIKE ?'
                ' OR storage_location LIKE ?'
                ' )'
            )
            params.extend([
                value,
                value,
                value,
                value,
                value,
                value,
            ])

    if classification:
        conditions.append(
            'classification = ?'
        )
        params.append(
            classification
        )

    if media_format == 'CD':
        conditions.append(
            "media_format = 'CD'"
        )
    elif media_format == 'LP':
        conditions.append(
            "media_format = 'LP'"
        )
    elif media_format == 'ETC':
        conditions.append(
            "media_format IN "
            "( 'Casette', 'USB', '기타' )"
        )

    if album_type in ALBUM_TYPES:
        conditions.append(
            'album_type = ?'
        )
        params.append(
            album_type
        )

    if genre:
        conditions.append(
            "(',' || REPLACE("
            "COALESCE(genre, ''), "
            "', ', ','"
            ") || ',') LIKE ?"
        )
        params.append(
            f'%,{genre},%'
        )

    return (
        ' WHERE '
        + ' AND '.join(conditions),
        params,
    )


def sort_index_rows(rows, sort_order):
    if sort_order == 'artist':
        return sorted(
            rows,
            key=lambda album: (
                name_sort_key(
                    album['artist']
                ),
                name_sort_key(
                    album['album_title']
                ),
                -int(
                    album['id']
                    or 0
                ),
            )
        )

    if sort_order == 'title':
        return sorted(
            rows,
            key=lambda album: (
                name_sort_key(
                    album['album_title']
                ),
                name_sort_key(
                    album['artist']
                ),
                -int(
                    album['id']
                    or 0
                ),
            )
        )

    return list(rows)


def build_artist_directory(rows, query=''):
    artist_map = {}

    for row in rows:
        for artist_name in split_artists(
            row['artist']
        ):
            name = clean_text(
                artist_name
            )
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

            if (
                not item['cover_url']
                and row['cover_url']
            ):
                item['cover_url'] = (
                    row['cover_url']
                )

    total_artist_count = len(
        artist_map
    )
    artists = list(
        artist_map.values()
    )

    query = clean_text(query)

    if query:
        query_key = query.casefold()
        artists = [
            item
            for item in artists
            if query_key
            in item['name'].casefold()
        ]

    artists.sort(
        key=lambda item: (
            artist_group_sort_key(
                artist_group_key(
                    item['name']
                )
            ),
            name_sort_key(
                item['name']
            ),
        )
    )

    grouped = {}

    for item in artists:
        group = artist_group_key(
            item['name']
        )
        grouped.setdefault(
            group,
            []
        ).append(
            item
        )

    grouped_artists = [
        {
            'key': group,
            'anchor': (
                'other'
                if group == '#'
                else (
                    'group-'
                    + str(
                        ord(group[0])
                    )
                )
            ),
            'artists': grouped[group],
        }
        for group in sorted(
            grouped,
            key=artist_group_sort_key
        )
    ]

    return {
        'grouped_artists':
            grouped_artists,
        'total_artist_count':
            total_artist_count,
        'filtered_artist_count':
            len(artists),
    }


def collect_artist_albums(
    rows,
    artist_name
):
    target_key = clean_text(
        artist_name
    ).casefold()
    matched_albums = []
    canonical_artist = ''

    for album in rows:
        for credit_name in split_artists(
            album['artist']
        ):
            if (
                credit_name.casefold()
                != target_key
            ):
                continue

            matched_albums.append(
                album
            )
            if not canonical_artist:
                canonical_artist = (
                    credit_name
                )
            break

    return (
        matched_albums,
        canonical_artist,
        target_key,
    )


def build_format_summary(
    rows,
    target_key
):
    counts = {
        name: 0
        for name in FORMAT_ORDER
    }

    for album in rows:
        artist_match = any(
            credit_name.casefold()
            == target_key
            for credit_name
            in split_artists(
                album['artist']
            )
        )

        if not artist_match:
            continue

        media_key = clean_text(
            album['media_format']
        ).casefold()

        if media_key == 'cd':
            key = 'CD'
        elif media_key == 'lp':
            key = 'LP'
        elif media_key in {
            'casette',
            'cassette',
            '카세트',
        }:
            key = 'CASETTE'
        else:
            key = 'ETC'

        counts[key] += 1

    summary = [
        {
            'name': name,
            'count': counts[name],
        }
        for name in FORMAT_ORDER
        if counts[name] > 0
    ]

    return counts, summary


def build_album_type_summary(
    albums
):
    counts = {
        type_name: 0
        for type_name in ALBUM_TYPES
    }

    for album in albums:
        album_type = (
            clean_text(
                album['album_type']
            )
            or '앨범'
        )

        if album_type in counts:
            counts[album_type] += 1

    return [
        {
            'name': type_name,
            'count': counts[
                type_name
            ],
        }
        for type_name in ALBUM_TYPES
        if counts[type_name] > 0
    ]


def release_year_number(album):
    value = clean_text(
        album['release_year']
    )
    return (
        int(value)
        if value.isdigit()
        else None
    )


def release_date_key(album):
    value = clean_text(
        album['release_date']
    )

    if (
        len(value) == 10
        and value[4] == '-'
        and value[7] == '-'
    ):
        compact = value.replace(
            '-',
            ''
        )

        if compact.isdigit():
            return int(compact)

    year = release_year_number(
        album
    )

    if year is None:
        return None

    return year * 10000


def purchase_price_number(album):
    value = album['purchase_price']

    if value is None:
        return None

    try:
        return int(value)
    except Exception:
        return None


def _release_sort_key(
    album,
    descending=False
):
    release_key = release_date_key(
        album
    )
    numeric_release = (
        release_key
        if release_key is not None
        else 0
    )

    return (
        release_key is None,
        (
            -numeric_release
            if descending
            else numeric_release
        ),
        name_sort_key(
            album['album_title']
        ),
        (
            -int(
                album['id']
                or 0
            )
            if descending
            else int(
                album['id']
                or 0
            )
        ),
    )


def _price_sort_key(
    album,
    descending=False
):
    price = purchase_price_number(
        album
    )
    numeric_price = (
        price
        if price is not None
        else 0
    )

    return (
        price is None,
        (
            -numeric_price
            if descending
            else numeric_price
        ),
        name_sort_key(
            album['album_title']
        ),
        -int(
            album['id']
            or 0
        ),
    )


def sort_artist_albums(
    albums,
    sort_order
):
    rows = list(albums)

    if sort_order == 'release_asc':
        rows.sort(
            key=lambda album: (
                _release_sort_key(
                    album,
                    descending=False
                )
            )
        )
    elif sort_order == 'price_desc':
        rows.sort(
            key=lambda album: (
                _price_sort_key(
                    album,
                    descending=True
                )
            )
        )
    elif sort_order == 'price_asc':
        rows.sort(
            key=lambda album: (
                _price_sort_key(
                    album,
                    descending=False
                )
            )
        )
    elif sort_order == 'title':
        rows.sort(
            key=lambda album: (
                name_sort_key(
                    album['album_title']
                ),
                -int(
                    album['id']
                    or 0
                ),
            )
        )
    else:
        rows.sort(
            key=lambda album: (
                _release_sort_key(
                    album,
                    descending=True
                )
            )
        )

    return rows


def resolve_artist_sort(
    args,
    session
):
    requested_sort = args.get('sort')

    if requested_sort is None:
        sort_order = clean_text(
            session.get(
                'album_artist_sort',
                'release_desc'
            )
        ) or 'release_desc'
    else:
        sort_order = (
            clean_text(
                requested_sort
            )
            or 'release_desc'
        )

    if (
        sort_order
        not in ARTIST_ALLOWED_SORTS
    ):
        sort_order = 'release_desc'

    session[
        'album_artist_sort'
    ] = sort_order

    return sort_order

from catalog import normalize_text


def make_album_key(artist, album_title, release_year):
    return (
        normalize_text(artist)
        + '|'
        + normalize_text(album_title)
        + '|'
        + str(release_year or '').strip()
    )


def get_management_prefix(media_format):
    if media_format == 'CD':
        return 'CD'
    if media_format == 'LP':
        return 'LP'
    return 'ETC'


def get_next_management_no(
    conn,
    media_format
):
    prefix = get_management_prefix(
        media_format
    )
    counter_type = (
        f'MANAGEMENT_{prefix}'
    )

    row = conn.execute(
        '''
        INSERT INTO number_counters (
            counter_type,
            last_number
        ) VALUES (?, 1)
        ON CONFLICT(counter_type)
        DO UPDATE SET
            last_number =
                number_counters.last_number + 1
        RETURNING last_number
        ''',
        (counter_type,)
    ).fetchone()

    next_number = int(
        row['last_number']
    )

    return (
        f'{prefix}-{next_number:04d}'
    )


def get_or_create_album_id(
    conn,
    apple_id,
    artist,
    album_title,
    release_year,
    cover_url
):
    album_key = make_album_key(
        artist,
        album_title,
        release_year
    )

    apple_id = str(
        apple_id
        or ''
    ).strip()

    existing = None

    if apple_id:
        existing = conn.execute(
            '''
            SELECT album_id
            FROM album_master
            WHERE apple_collection_id = ?
            LIMIT 1
            ''',
            (apple_id,)
        ).fetchone()

    if existing is None:
        existing = conn.execute(
            '''
            SELECT album_id
            FROM album_master
            WHERE album_key = ?
            LIMIT 1
            ''',
            (album_key,)
        ).fetchone()

    if existing:
        return existing['album_id']

    cursor = conn.execute(
        'INSERT INTO album_master ( album_key, apple_collection_id, artist, album_title, release_year, cover_url ) VALUES (?, ?, ?, ?, ?, ?)',
        (
            album_key,
            apple_id,
            artist,
            album_title,
            release_year,
            cover_url
        )
    )

    master_id = cursor.lastrowid
    album_id = f'ALB-{master_id:04d}'

    conn.execute(
        'UPDATE album_master SET album_id = ? WHERE id = ?',
        (album_id, master_id)
    )

    return album_id

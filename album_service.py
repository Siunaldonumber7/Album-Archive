from search_engine import normalize_text


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


def get_next_management_no(conn, media_format):
    prefix = get_management_prefix(media_format)
    counter_type = f'MANAGEMENT_{prefix}'

    row = conn.execute(
        'SELECT last_number FROM number_counters WHERE counter_type = ?',
        (counter_type,)
    ).fetchone()

    if row:
        next_number = row['last_number'] + 1
        conn.execute(
            'UPDATE number_counters SET last_number = ? WHERE counter_type = ?',
            (next_number, counter_type)
        )
    else:
        next_number = 1
        conn.execute(
            'INSERT INTO number_counters ( counter_type, last_number ) VALUES (?, ?)',
            (counter_type, next_number)
        )

    return f'{prefix}-{next_number:04d}'


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

    existing = conn.execute(
        "SELECT * FROM album_master WHERE album_key = ? OR ( ? != '' AND apple_collection_id = ? ) LIMIT 1",
        (album_key, apple_id, apple_id)
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

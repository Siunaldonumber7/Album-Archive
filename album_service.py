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


def find_existing_album_id(
    conn,
    apple_id='',
    artist='',
    album_title='',
    release_year=''
):
    apple_id = str(
        apple_id
        or ''
    ).strip()

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

        if existing:
            return str(
                existing['album_id']
                or ''
            ).strip()

    album_key = make_album_key(
        artist,
        album_title,
        release_year
    )

    existing = conn.execute(
        '''
        SELECT album_id
        FROM album_master
        WHERE album_key = ?
        LIMIT 1
        ''',
        (album_key,)
    ).fetchone()

    if not existing:
        return ''

    return str(
        existing['album_id']
        or ''
    ).strip()


def get_or_create_album_id(
    conn,
    apple_id,
    artist,
    album_title,
    release_year,
    cover_url,
    release_date=''
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

    existing_album_id = (
        find_existing_album_id(
            conn,
            apple_id=apple_id,
            artist=artist,
            album_title=album_title,
            release_year=release_year
        )
    )

    if existing_album_id:
        release_date = str(
            release_date
            or ''
        ).strip()

        if release_date:
            conn.execute(
                '''
                UPDATE album_master
                SET
                    release_date = CASE
                        WHEN release_date IS NULL
                          OR TRIM(release_date) = ''
                        THEN ?
                        ELSE release_date
                    END,
                    release_year = CASE
                        WHEN release_year IS NULL
                          OR TRIM(release_year) = ''
                        THEN ?
                        ELSE release_year
                    END
                WHERE album_id = ?
                ''',
                (
                    release_date,
                    release_date[:4],
                    existing_album_id,
                )
            )
            conn.execute(
                '''
                UPDATE albums
                SET
                    release_date = CASE
                        WHEN release_date IS NULL
                          OR TRIM(release_date) = ''
                        THEN ?
                        ELSE release_date
                    END,
                    release_year = CASE
                        WHEN release_year IS NULL
                          OR TRIM(release_year) = ''
                        THEN ?
                        ELSE release_year
                    END
                WHERE album_id = ?
                ''',
                (
                    release_date,
                    release_date[:4],
                    existing_album_id,
                )
            )

        return existing_album_id

    cursor = conn.execute(
        '''
        INSERT INTO album_master (
            album_key,
            apple_collection_id,
            artist,
            album_title,
            release_year,
            release_date,
            cover_url
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ''',
        (
            album_key,
            apple_id,
            artist,
            album_title,
            release_year,
            str(
                release_date
                or ''
            ).strip(),
            cover_url,
        )
    )

    master_id = cursor.lastrowid
    album_id = f'ALB-{master_id:04d}'

    conn.execute(
        'UPDATE album_master SET album_id = ? WHERE id = ?',
        (album_id, master_id)
    )

    return album_id



def sync_album_master_metadata(
    conn,
    album_id,
    artist,
    album_title,
    release_year='',
    release_date='',
    cover_url=None
):
    album_id = str(
        album_id
        or ''
    ).strip()

    if not album_id:
        return

    album_key = make_album_key(
        artist,
        album_title,
        release_year
    )
    release_date = str(
        release_date
        or ''
    ).strip()

    existing = conn.execute(
        '''
        SELECT id
        FROM album_master
        WHERE album_id = ?
        LIMIT 1
        ''',
        (album_id,)
    ).fetchone()

    if existing is None:
        conn.execute(
            '''
            INSERT INTO album_master (
                album_id,
                album_key,
                artist,
                album_title,
                release_year,
                release_date,
                cover_url
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                album_id,
                album_key,
                artist,
                album_title,
                release_year,
                release_date,
                str(
                    cover_url
                    or ''
                ).strip(),
            )
        )
        return

    if cover_url is None:
        conn.execute(
            '''
            UPDATE album_master
            SET
                album_key = ?,
                artist = ?,
                album_title = ?,
                release_year = ?,
                release_date = ?
            WHERE album_id = ?
            ''',
            (
                album_key,
                artist,
                album_title,
                release_year,
                release_date,
                album_id,
            )
        )
    else:
        conn.execute(
            '''
            UPDATE album_master
            SET
                album_key = ?,
                artist = ?,
                album_title = ?,
                release_year = ?,
                release_date = ?,
                cover_url = ?
            WHERE album_id = ?
            ''',
            (
                album_key,
                artist,
                album_title,
                release_year,
                release_date,
                str(
                    cover_url
                    or ''
                ).strip(),
                album_id,
            )
        )


def delete_album_master_if_orphan(
    conn,
    album_id
):
    album_id = str(
        album_id
        or ''
    ).strip()

    if not album_id:
        return False

    remaining = conn.execute(
        '''
        SELECT COUNT(*) AS count
        FROM albums
        WHERE album_id = ?
        ''',
        (album_id,)
    ).fetchone()['count']

    if int(
        remaining
        or 0
    ) > 0:
        return False

    conn.execute(
        '''
        DELETE FROM album_master
        WHERE album_id = ?
        ''',
        (album_id,)
    )
    return True

import os
import sqlite3


DB_NAME = os.environ.get('ALBUM_DB_PATH', 'albums.db')
DATA_DIR = os.path.dirname(os.path.abspath(DB_NAME))
os.makedirs(DATA_DIR, exist_ok=True)


def get_db():
    conn = sqlite3.connect(
        DB_NAME,
        timeout=10
    )
    conn.row_factory = sqlite3.Row
    conn.execute(
        'PRAGMA busy_timeout = 10000'
    )
    conn.execute(
        'PRAGMA synchronous = NORMAL'
    )
    return conn


def init_db():
    conn = get_db()

    conn.execute(
        'PRAGMA journal_mode = WAL'
    )

    conn.execute(
        'CREATE TABLE IF NOT EXISTS albums ( '
        'id INTEGER PRIMARY KEY AUTOINCREMENT, '
        'management_no TEXT UNIQUE NOT NULL, '
        'album_id TEXT NOT NULL, '
        'classification TEXT, '
        'media_format TEXT, '
        'album_type TEXT, '
        'genre TEXT, '
        'artist TEXT NOT NULL, '
        'album_title TEXT NOT NULL, '
        'release_year TEXT, '
        'open_status TEXT, '
        'signed TEXT, '
        'purchase_price INTEGER, '
        'memo TEXT, '
        'apple_collection_id TEXT, '
        'cover_url TEXT, '
        'is_primary_display INTEGER NOT NULL DEFAULT 0 )'
    )

    existing_columns = {
        row['name']
        for row in conn.execute(
            'PRAGMA table_info(albums)'
        ).fetchall()
    }

    if 'album_type' not in existing_columns:
        conn.execute(
            'ALTER TABLE albums ADD COLUMN album_type TEXT'
        )

    if 'genre' not in existing_columns:
        conn.execute(
            'ALTER TABLE albums ADD COLUMN genre TEXT'
        )

    if 'is_primary_display' not in existing_columns:
        conn.execute(
            'ALTER TABLE albums '
            'ADD COLUMN is_primary_display INTEGER NOT NULL DEFAULT 0'
        )

    conn.execute(
        'CREATE TABLE IF NOT EXISTS album_master ( '
        'id INTEGER PRIMARY KEY AUTOINCREMENT, '
        'album_id TEXT UNIQUE, '
        'album_key TEXT UNIQUE, '
        'apple_collection_id TEXT, '
        'artist TEXT, '
        'album_title TEXT, '
        'release_year TEXT, '
        'cover_url TEXT )'
    )

    conn.execute(
        'CREATE TABLE IF NOT EXISTS number_counters ( '
        'counter_type TEXT PRIMARY KEY, '
        'last_number INTEGER NOT NULL DEFAULT 0 )'
    )

    conn.execute(
        'CREATE TABLE IF NOT EXISTS artist_aliases ( '
        'id INTEGER PRIMARY KEY AUTOINCREMENT, '
        'alias_key TEXT UNIQUE NOT NULL, '
        'alias_text TEXT, '
        'canonical_name TEXT, '
        'apple_artist_id TEXT )'
    )

    conn.execute(
        'CREATE TABLE IF NOT EXISTS album_tracks ( '
        'id INTEGER PRIMARY KEY AUTOINCREMENT, '
        'album_id TEXT NOT NULL, '
        'disc_number INTEGER NOT NULL DEFAULT 1, '
        'track_number INTEGER NOT NULL DEFAULT 1, '
        'title TEXT NOT NULL, '
        'artist TEXT, '
        'duration TEXT )'
    )

    conn.execute(
        'CREATE INDEX IF NOT EXISTS '
        'idx_album_tracks_album_id '
        'ON album_tracks (album_id)'
    )
    conn.execute(
        'CREATE INDEX IF NOT EXISTS '
        'idx_albums_album_id '
        'ON albums (album_id)'
    )
    conn.execute(
        'CREATE INDEX IF NOT EXISTS '
        'idx_albums_classification '
        'ON albums (classification)'
    )
    conn.execute(
        'CREATE INDEX IF NOT EXISTS '
        'idx_albums_media_format '
        'ON albums (media_format)'
    )
    conn.execute(
        'CREATE INDEX IF NOT EXISTS '
        'idx_albums_album_type '
        'ON albums (album_type)'
    )
    conn.execute(
        'CREATE INDEX IF NOT EXISTS '
        'idx_albums_genre '
        'ON albums (genre)'
    )
    conn.execute(
        'CREATE INDEX IF NOT EXISTS '
        'idx_albums_artist '
        'ON albums (artist)'
    )
    conn.execute(
        'CREATE INDEX IF NOT EXISTS '
        'idx_albums_release_year '
        'ON albums (release_year)'
    )
    conn.execute(
        'CREATE INDEX IF NOT EXISTS '
        'idx_albums_apple_collection_id '
        'ON albums (apple_collection_id)'
    )
    conn.execute(
        'CREATE INDEX IF NOT EXISTS '
        'idx_albums_primary_display '
        'ON albums (is_primary_display)'
    )
    conn.execute(
        'CREATE INDEX IF NOT EXISTS '
        'idx_albums_album_primary '
        'ON albums (album_id, is_primary_display, id)'
    )
    conn.execute(
        'CREATE INDEX IF NOT EXISTS '
        'idx_album_master_apple_collection_id '
        'ON album_master (apple_collection_id)'
    )

    conn.execute(
        'CREATE TABLE IF NOT EXISTS album_track_settings ( '
        'album_id TEXT PRIMARY KEY, '
        'manual_override INTEGER NOT NULL DEFAULT 0, '
        'updated_at TEXT DEFAULT CURRENT_TIMESTAMP )'
    )

    conn.execute(
        'CREATE TABLE IF NOT EXISTS genre_registry ( '
        'name TEXT PRIMARY KEY COLLATE NOCASE, '
        'created_at TEXT DEFAULT CURRENT_TIMESTAMP )'
    )

    conn.execute(
        'CREATE TABLE IF NOT EXISTS artist_profiles ( '
        'artist_key TEXT PRIMARY KEY, '
        'artist_name TEXT NOT NULL, '
        'apple_artist_id TEXT, '
        'profile_url TEXT, '
        'source TEXT, '
        'updated_at TEXT DEFAULT CURRENT_TIMESTAMP )'
    )

    genre_registry_count = conn.execute(
        'SELECT COUNT(*) AS count '
        'FROM genre_registry'
    ).fetchone()['count']

    if genre_registry_count == 0:
        genre_rows = conn.execute(
            "SELECT genre FROM albums "
            "WHERE genre IS NOT NULL "
            "AND TRIM(genre) != ''"
        ).fetchall()

        for row in genre_rows:
            remember_genres(
                conn,
                row['genre']
            )

    # Repair only invalid groups. Healthy groups already have one public
    # representative and should not generate needless writes on every boot.
    conn.execute(
        '''
        WITH invalid_groups AS (
            SELECT album_id
            FROM albums
            WHERE album_id IS NOT NULL
              AND TRIM(album_id) != ''
            GROUP BY album_id
            HAVING SUM(
                CASE
                    WHEN is_primary_display = 1
                    THEN 1
                    ELSE 0
                END
            ) != 1
        ),
        selected_rows AS (
            SELECT
                candidate.album_id,
                COALESCE(
                    MIN(
                        CASE
                            WHEN candidate.is_primary_display = 1
                            THEN candidate.id
                        END
                    ),
                    MIN(candidate.id)
                ) AS selected_id
            FROM albums AS candidate
            INNER JOIN invalid_groups
                ON invalid_groups.album_id = candidate.album_id
            GROUP BY candidate.album_id
        )
        UPDATE albums
        SET is_primary_display = CASE
            WHEN id = (
                SELECT selected_id
                FROM selected_rows
                WHERE selected_rows.album_id = albums.album_id
            )
            THEN 1
            ELSE 0
        END
        WHERE album_id IN (
            SELECT album_id
            FROM invalid_groups
        )
        '''
    )

    conn.execute(
        'PRAGMA optimize'
    )
    conn.commit()
    conn.close()



def ensure_primary_display(
    conn,
    album_id
):
    album_id = str(
        album_id
        or ''
    ).strip()

    if not album_id:
        return

    conn.execute(
        '''
        UPDATE albums
        SET is_primary_display = CASE
            WHEN id = (
                SELECT COALESCE(
                    MIN(
                        CASE
                            WHEN candidate.is_primary_display = 1
                            THEN candidate.id
                        END
                    ),
                    MIN(candidate.id)
                )
                FROM albums AS candidate
                WHERE candidate.album_id = ?
            )
            THEN 1
            ELSE 0
        END
        WHERE album_id = ?
          AND (
              SELECT COUNT(*)
              FROM albums AS selected
              WHERE selected.album_id = ?
                AND selected.is_primary_display = 1
          ) != 1
        ''',
        (
            album_id,
            album_id,
            album_id,
        )
    )


def set_primary_display(
    conn,
    album_row_id
):
    row = conn.execute(
        '''
        SELECT
            id,
            album_id
        FROM albums
        WHERE id = ?
        ''',
        (album_row_id,)
    ).fetchone()

    if row is None:
        return None

    conn.execute(
        '''
        UPDATE albums
        SET is_primary_display = CASE
            WHEN id = ?
            THEN 1
            ELSE 0
        END
        WHERE album_id = ?
        ''',
        (
            row['id'],
            row['album_id'],
        )
    )

    return row['album_id']


def get_genre_options(conn=None):
    owns_connection = conn is None

    if owns_connection:
        conn = get_db()

    try:
        rows = conn.execute(
            'SELECT name FROM genre_registry '
            'ORDER BY name COLLATE NOCASE ASC'
        ).fetchall()

        return [
            str(
                row['name']
                or ''
            ).strip()
            for row in rows
            if str(
                row['name']
                or ''
            ).strip()
        ]
    finally:
        if owns_connection:
            conn.close()


def remember_genres(conn, value):
    seen = set()

    for raw in str(value or '').split(','):
        genre = str(raw or '').strip()

        if not genre:
            continue

        key = genre.casefold()

        if key in seen:
            continue

        seen.add(key)

        conn.execute(
            'INSERT OR IGNORE INTO genre_registry (name) '
            'VALUES (?)',
            (genre,)
        )



def get_artist_profile_cache(artist_key):
    artist_key = str(artist_key or '').strip()

    if not artist_key:
        return None

    conn = get_db()

    try:
        return conn.execute(
            'SELECT * FROM artist_profiles '
            'WHERE artist_key = ? LIMIT 1',
            (artist_key,)
        ).fetchone()
    finally:
        conn.close()


def save_artist_profile_cache(
    artist_key,
    artist_name,
    apple_artist_id='',
    profile_url='',
    source='apple'
):
    artist_key = str(artist_key or '').strip()
    artist_name = str(artist_name or '').strip()

    if not artist_key or not artist_name:
        return

    conn = get_db()

    try:
        conn.execute(
            '''
            INSERT INTO artist_profiles (
                artist_key,
                artist_name,
                apple_artist_id,
                profile_url,
                source,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(artist_key) DO UPDATE SET
                artist_name = excluded.artist_name,
                apple_artist_id = excluded.apple_artist_id,
                profile_url = excluded.profile_url,
                source = excluded.source,
                updated_at = CURRENT_TIMESTAMP
            ''',
            (
                artist_key,
                artist_name,
                str(apple_artist_id or '').strip(),
                str(profile_url or '').strip(),
                str(source or '').strip(),
            )
        )
        conn.commit()
    finally:
        conn.close()

import os
import sqlite3


DB_NAME = os.environ.get('ALBUM_DB_PATH', 'albums.db')
DATA_DIR = os.path.dirname(os.path.abspath(DB_NAME))
os.makedirs(DATA_DIR, exist_ok=True)


def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()

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
        'cover_url TEXT )'
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
        'CREATE TABLE IF NOT EXISTS album_track_settings ( '
        'album_id TEXT PRIMARY KEY, '
        'manual_override INTEGER NOT NULL DEFAULT 0, '
        'updated_at TEXT DEFAULT CURRENT_TIMESTAMP )'
    )

    conn.commit()
    conn.close()

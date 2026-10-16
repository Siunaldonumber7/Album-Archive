import os
import tempfile
import unittest

import album_database
import release_date_backfill
import media_service
import track_service
from PIL import Image
from album_service import (
    delete_album_master_if_orphan,
    find_existing_album_id,
    get_next_management_no,
    sync_album_master_metadata,
)
from album_routes import _normalize_release_date
from catalog import normalize_text
from library_view import (
    build_index_where,
    resolve_index_state,
    sort_artist_albums,
)
from media_service import (
    generate_cover_thumbnails,
    local_cover_thumbnail_path,
)


class DatabaseOptimizationTests(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_db_name = album_database.DB_NAME
        album_database.DB_NAME = os.path.join(
            self.temp_dir.name,
            'test.db'
        )
        album_database.init_db()

    def tearDown(self):
        album_database.DB_NAME = self.old_db_name
        self.temp_dir.cleanup()

    def test_artist_map_backfills_and_splits_credits(self):
        conn = album_database.get_db()

        try:
            cursor = conn.execute(
                '''
                INSERT INTO albums (
                    management_no,
                    album_id,
                    artist,
                    album_title
                ) VALUES (?, ?, ?, ?)
                ''',
                (
                    'CD-9401',
                    'ALB-9401',
                    'Artist A & Artist B',
                    'Mapped Album',
                )
            )
            row_id = cursor.lastrowid
            conn.commit()
        finally:
            conn.close()

        album_database.init_db()
        conn = album_database.get_db()

        try:
            rows = conn.execute(
                '''
                SELECT artist_key, artist_name
                FROM album_artist_map
                WHERE album_row_id = ?
                ORDER BY artist_name
                ''',
                (row_id,)
            ).fetchall()
        finally:
            conn.close()

        self.assertEqual(
            [
                (
                    row['artist_key'],
                    row['artist_name'],
                )
                for row in rows
            ],
            [
                ('artist a', 'Artist A'),
                ('artist b', 'Artist B'),
            ]
        )

    def test_master_metadata_sync_and_orphan_cleanup(self):
        conn = album_database.get_db()

        try:
            conn.execute(
                '''
                INSERT INTO album_master (
                    album_id,
                    album_key,
                    artist,
                    album_title,
                    release_year
                ) VALUES (?, ?, ?, ?, ?)
                ''',
                (
                    'ALB-9402',
                    'old|title|2020',
                    'Old',
                    'Title',
                    '2020',
                )
            )
            conn.execute(
                '''
                INSERT INTO albums (
                    management_no,
                    album_id,
                    artist,
                    album_title
                ) VALUES (?, ?, ?, ?)
                ''',
                (
                    'CD-9402',
                    'ALB-9402',
                    'Old',
                    'Title',
                )
            )

            sync_album_master_metadata(
                conn,
                'ALB-9402',
                'New Artist',
                'New Title',
                '2024',
                '2024-08-09',
                '/static/uploads/new.jpg'
            )

            master = conn.execute(
                '''
                SELECT
                    artist,
                    album_title,
                    release_year,
                    release_date,
                    cover_url
                FROM album_master
                WHERE album_id = ?
                ''',
                ('ALB-9402',)
            ).fetchone()

            conn.execute(
                '''
                DELETE FROM albums
                WHERE album_id = ?
                ''',
                ('ALB-9402',)
            )
            removed = (
                delete_album_master_if_orphan(
                    conn,
                    'ALB-9402'
                )
            )
            conn.commit()

            remaining = conn.execute(
                '''
                SELECT COUNT(*) AS count
                FROM album_master
                WHERE album_id = ?
                ''',
                ('ALB-9402',)
            ).fetchone()['count']
        finally:
            conn.close()

        self.assertEqual(
            master['artist'],
            'New Artist'
        )
        self.assertEqual(
            master['album_title'],
            'New Title'
        )
        self.assertEqual(
            master['release_date'],
            '2024-08-09'
        )
        self.assertTrue(removed)
        self.assertEqual(remaining, 0)

    def test_persistent_track_cache_survives_memory_clear(self):
        result = {
            'tracks': [
                {
                    'id': '1',
                    'title': 'Track One',
                    'artist': 'Artist',
                    'album_name': 'Album',
                    'duration_ms': 180000,
                    'duration': '3:00',
                    'disc_number': 1,
                    'track_number': 1,
                }
            ],
            'store': 'KR',
            'resolved_by': 'test',
        }

        track_service._save_persistent_track_cache(
            'ALB-9403',
            result
        )
        track_service.TRACK_CACHE.clear()

        cached = (
            track_service
            ._load_persistent_track_cache(
                'ALB-9403'
            )
        )

        self.assertIsNotNone(cached)
        self.assertEqual(
            cached['tracks'][0]['title'],
            'Track One'
        )
        self.assertEqual(
            cached['resolved_by'],
            'test'
        )

        track_service.clear_album_track_cache(
            'ALB-9403'
        )

        self.assertIsNone(
            track_service
            ._load_persistent_track_cache(
                'ALB-9403'
            )
        )

    def test_primary_display_is_repaired_in_one_group(self):
        conn = album_database.get_db()

        try:
            conn.executemany(
                '''
                INSERT INTO albums (
                    management_no,
                    album_id,
                    artist,
                    album_title,
                    is_primary_display
                ) VALUES (?, ?, ?, ?, ?)
                ''',
                [
                    (
                        'CD-9001',
                        'ALB-9001',
                        'Artist',
                        'Album',
                        1,
                    ),
                    (
                        'CD-9002',
                        'ALB-9001',
                        'Artist',
                        'Album',
                        1,
                    ),
                    (
                        'CD-9003',
                        'ALB-9001',
                        'Artist',
                        'Album',
                        0,
                    ),
                ]
            )
            conn.commit()
        finally:
            conn.close()

        album_database.init_db()

        conn = album_database.get_db()

        try:
            rows = conn.execute(
                '''
                SELECT
                    id,
                    is_primary_display
                FROM albums
                WHERE album_id = 'ALB-9001'
                ORDER BY id ASC
                '''
            ).fetchall()
        finally:
            conn.close()

        selected = [
            row['id']
            for row in rows
            if row['is_primary_display'] == 1
        ]

        self.assertEqual(
            selected,
            [rows[0]['id']]
        )

    def test_genre_registry_backfills_existing_data(self):
        conn = album_database.get_db()

        try:
            conn.execute(
                '''
                INSERT INTO albums (
                    management_no,
                    album_id,
                    genre,
                    artist,
                    album_title
                ) VALUES (?, ?, ?, ?, ?)
                ''',
                (
                    'CD-9101',
                    'ALB-9101',
                    '힙합, R&B',
                    'Artist',
                    'Album',
                )
            )
            conn.commit()
        finally:
            conn.close()

        album_database.init_db()

        self.assertEqual(
            album_database.get_genre_options(),
            ['R&B', '힙합']
        )

    def test_management_number_increment_is_atomic_sql(self):
        conn = album_database.get_db()

        try:
            first = get_next_management_no(
                conn,
                'CD'
            )
            second = get_next_management_no(
                conn,
                'CD'
            )
            lp_first = get_next_management_no(
                conn,
                'LP'
            )
            conn.commit()
        finally:
            conn.close()

        self.assertEqual(first, 'CD-0001')
        self.assertEqual(second, 'CD-0002')
        self.assertEqual(lp_first, 'LP-0001')


    def test_management_counter_recovers_from_existing_numbers(self):
        conn = album_database.get_db()

        try:
            conn.execute(
                '''
                INSERT INTO albums (
                    management_no,
                    album_id,
                    artist,
                    album_title
                ) VALUES (?, ?, ?, ?)
                ''',
                (
                    'CD-0099',
                    'ALB-9999',
                    'Artist',
                    'Recovered',
                )
            )
            conn.execute(
                'DELETE FROM number_counters'
            )
            conn.commit()
        finally:
            conn.close()

        album_database.init_db()

        conn = album_database.get_db()

        try:
            next_number = (
                get_next_management_no(
                    conn,
                    'CD'
                )
            )
            conn.commit()
        finally:
            conn.close()

        self.assertEqual(
            next_number,
            'CD-0100'
        )


    def test_release_date_backfill_updates_all_owned_copies(self):
        conn = album_database.get_db()

        try:
            conn.execute(
                '''
                INSERT INTO album_master (
                    album_id,
                    album_key,
                    apple_collection_id,
                    artist,
                    album_title,
                    release_year
                ) VALUES (?, ?, ?, ?, ?, ?)
                ''',
                (
                    'ALB-9301',
                    'artist|album|2026',
                    '9301001',
                    'Artist',
                    'Album',
                    '2026',
                )
            )
            conn.executemany(
                '''
                INSERT INTO albums (
                    management_no,
                    album_id,
                    artist,
                    album_title,
                    release_year,
                    apple_collection_id,
                    is_primary_display
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ''',
                [
                    (
                        'CD-9301',
                        'ALB-9301',
                        'Artist',
                        'Album',
                        '2026',
                        '9301001',
                        1,
                    ),
                    (
                        'LP-9301',
                        'ALB-9301',
                        'Artist',
                        'Album',
                        '2026',
                        '9301001',
                        0,
                    ),
                ]
            )
            conn.commit()
        finally:
            conn.close()

        original_lookup = (
            release_date_backfill
            ._lookup_batch
        )

        def fake_lookup(
            collection_ids,
            store
        ):
            if (
                store == 'KR'
                and '9301001'
                in collection_ids
            ):
                return {
                    '9301001':
                        '2026-09-18'
                }
            return {}

        release_date_backfill._lookup_batch = (
            fake_lookup
        )

        try:
            result = (
                release_date_backfill
                .backfill_release_dates()
            )
        finally:
            release_date_backfill._lookup_batch = (
                original_lookup
            )

        conn = album_database.get_db()

        try:
            album_rows = conn.execute(
                '''
                SELECT release_date
                FROM albums
                WHERE album_id = ?
                ORDER BY id
                ''',
                ('ALB-9301',)
            ).fetchall()
            master = conn.execute(
                '''
                SELECT release_date
                FROM album_master
                WHERE album_id = ?
                ''',
                ('ALB-9301',)
            ).fetchone()
        finally:
            conn.close()

        self.assertEqual(
            result['updated'],
            1
        )
        self.assertEqual(
            [
                row['release_date']
                for row in album_rows
            ],
            [
                '2026-09-18',
                '2026-09-18',
            ]
        )
        self.assertEqual(
            master['release_date'],
            '2026-09-18'
        )


    def test_release_date_columns_exist_after_migration(self):
        conn = album_database.get_db()

        try:
            album_columns = {
                row['name']
                for row in conn.execute(
                    'PRAGMA table_info(albums)'
                ).fetchall()
            }
            master_columns = {
                row['name']
                for row in conn.execute(
                    'PRAGMA table_info(album_master)'
                ).fetchall()
            }
        finally:
            conn.close()

        self.assertIn(
            'release_date',
            album_columns
        )
        self.assertIn(
            'release_date',
            master_columns
        )


    def test_existing_album_lookup_uses_apple_id_then_key(self):
        conn = album_database.get_db()

        try:
            conn.execute(
                '''
                INSERT INTO album_master (
                    album_id,
                    album_key,
                    apple_collection_id,
                    artist,
                    album_title,
                    release_year
                ) VALUES (?, ?, ?, ?, ?, ?)
                ''',
                (
                    'ALB-9201',
                    'artist|album|2026',
                    '123456789',
                    'Artist',
                    'Album',
                    '2026',
                )
            )
            conn.commit()

            by_apple = find_existing_album_id(
                conn,
                apple_id='123456789',
                artist='Wrong',
                album_title='Wrong',
                release_year='1900'
            )
            by_key = find_existing_album_id(
                conn,
                artist='Artist',
                album_title='Album',
                release_year='2026'
            )
        finally:
            conn.close()

        self.assertEqual(
            by_apple,
            'ALB-9201'
        )
        self.assertEqual(
            by_key,
            'ALB-9201'
        )


class MediaServiceTests(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_upload_folder = (
            media_service.UPLOAD_FOLDER
        )
        self.old_thumbnail_folder = (
            media_service.THUMBNAIL_FOLDER
        )

        media_service.UPLOAD_FOLDER = (
            self.temp_dir.name
        )
        media_service.THUMBNAIL_FOLDER = (
            os.path.join(
                self.temp_dir.name,
                '.thumbs'
            )
        )

        os.makedirs(
            media_service.THUMBNAIL_FOLDER,
            exist_ok=True
        )

    def tearDown(self):
        media_service.UPLOAD_FOLDER = (
            self.old_upload_folder
        )
        media_service.THUMBNAIL_FOLDER = (
            self.old_thumbnail_folder
        )
        self.temp_dir.cleanup()

    def test_thumbnail_names_do_not_collide_across_extensions(self):
        jpg_path = local_cover_thumbnail_path(
            'cover.jpg',
            400
        )
        png_path = local_cover_thumbnail_path(
            'cover.png',
            400
        )

        self.assertNotEqual(
            jpg_path,
            png_path
        )

    def test_uploaded_cover_thumbnails_are_prebuilt(self):
        source_path = os.path.join(
            self.temp_dir.name,
            'cover.jpg'
        )

        Image.new(
            'RGB',
            (
                1200,
                1200,
            ),
            'white'
        ).save(
            source_path,
            'JPEG'
        )

        created = generate_cover_thumbnails(
            'cover.jpg'
        )

        self.assertEqual(
            created,
            3
        )

        for size in (
            180,
            400,
            800,
        ):
            thumb_path = (
                local_cover_thumbnail_path(
                    'cover.jpg',
                    size
                )
            )

            self.assertTrue(
                os.path.isfile(
                    thumb_path
                )
            )

            with Image.open(
                thumb_path
            ) as image:
                self.assertLessEqual(
                    max(image.size),
                    size
                )

    def test_thumbnail_url_version_changes_when_source_changes(self):
        source_path = os.path.join(
            self.temp_dir.name,
            'cover.jpg'
        )

        Image.new(
            'RGB',
            (600, 600),
            'white'
        ).save(
            source_path,
            'JPEG'
        )

        first_path = local_cover_thumbnail_path(
            'cover.jpg',
            400
        )

        Image.new(
            'RGB',
            (700, 700),
            'black'
        ).save(
            source_path,
            'JPEG'
        )

        second_path = local_cover_thumbnail_path(
            'cover.jpg',
            400
        )

        self.assertNotEqual(
            first_path,
            second_path
        )


class AlbumRouteHelperTests(unittest.TestCase):

    def test_normalize_release_date_accepts_real_iso_date(self):
        self.assertEqual(
            _normalize_release_date(
                '2016-02-25'
            ),
            '2016-02-25'
        )

    def test_normalize_release_date_rejects_invalid_date(self):
        self.assertIsNone(
            _normalize_release_date(
                '2016-02-31'
            )
        )
        self.assertEqual(
            _normalize_release_date(''),
            ''
        )


class LibraryViewTests(unittest.TestCase):

    def test_saved_index_state_is_reused_without_query_params(self):
        state_store = {
            'album_index_state': {
                'classification': '한국',
                'media_format': 'LP',
                'sort': 'title',
            }
        }

        state = resolve_index_state(
            {},
            state_store
        )

        self.assertEqual(
            state['classification'],
            '한국'
        )
        self.assertEqual(
            state['media_format'],
            'LP'
        )
        self.assertEqual(
            state['sort'],
            'title'
        )

    def test_index_where_keeps_supported_filters(self):
        where_sql, params = build_index_where({
            'library_q': 'free',
            'classification': '해외',
            'media_format': 'CD',
            'album_type': '앨범',
            'genre': '힙합',
        })

        self.assertIn(
            'is_primary_display = 1',
            where_sql
        )
        self.assertIn(
            "media_format = 'CD'",
            where_sql
        )
        self.assertEqual(
            params,
            [
                '%free%',
                '%free%',
                '%free%',
                '해외',
                '앨범',
                '%,힙합,%',
            ]
        )

    def test_normalize_text_is_shared_and_stable(self):
        self.assertEqual(
            normalize_text(' B-Free! '),
            'bfree'
        )
        self.assertEqual(
            normalize_text('비 프리'),
            '비프리'
        )


    def test_release_sort_uses_month_and_day(self):
        rows = [
            {
                'id': 1,
                'album_title': 'January',
                'release_year': '2026',
                'release_date': '2026-01-10',
                'purchase_price': None,
            },
            {
                'id': 2,
                'album_title': 'November',
                'release_year': '2026',
                'release_date': '2026-11-03',
                'purchase_price': None,
            },
            {
                'id': 3,
                'album_title': 'Unknown month',
                'release_year': '2026',
                'release_date': '',
                'purchase_price': None,
            },
        ]

        newest = sort_artist_albums(
            rows,
            'release_desc'
        )
        oldest = sort_artist_albums(
            rows,
            'release_asc'
        )

        self.assertEqual(
            [row['id'] for row in newest],
            [2, 1, 3]
        )
        self.assertEqual(
            [row['id'] for row in oldest],
            [3, 1, 2]
        )


if __name__ == '__main__':
    unittest.main()

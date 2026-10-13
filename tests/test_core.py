import os
import tempfile
import unittest

import album_database
from album_service import (
    find_existing_album_id,
    get_next_management_no,
)
from catalog import normalize_text
from library_view import (
    build_index_where,
    resolve_index_state,
)
from media_service import (
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


if __name__ == '__main__':
    unittest.main()

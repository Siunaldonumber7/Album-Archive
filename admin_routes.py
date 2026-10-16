import csv
import hmac
import io
import os
import secrets
import shutil
import sqlite3
import tarfile
import tempfile
import time
from datetime import datetime

from flask import (
    abort,
    redirect,
    render_template,
    request,
    send_file,
    session,
    Response,
    url_for,
)

from album_database import (
    DATA_DIR,
    DB_NAME,
    get_db,
    init_db,
    set_primary_display,
)
from media_service import (
    prewarm_thumbnail_directory,
)
from search_engine import (
    clear_search_cache,
)
from track_service import (
    clear_track_caches,
)
from pagination import (
    ALBUMS_PER_PAGE,
    parse_page_number,
    build_pagination_pages,
)


ADMIN_PIN = ''
LOGIN_ATTEMPTS = None


def register_admin_routes(app, admin_pin, login_attempts):
    global ADMIN_PIN, LOGIN_ATTEMPTS

    ADMIN_PIN = admin_pin
    LOGIN_ATTEMPTS = login_attempts

    app.add_url_rule(
        '/admin/login',
        endpoint='admin_login',
        view_func=admin_login,
        methods=['GET', 'POST']
    )
    app.add_url_rule(
        '/admin/logout',
        endpoint='admin_logout',
        view_func=admin_logout,
        methods=['POST']
    )
    app.add_url_rule(
        '/admin',
        endpoint='admin_dashboard',
        view_func=admin_dashboard
    )
    app.add_url_rule(
        '/manage',
        endpoint='manage_albums',
        view_func=manage_albums
    )
    app.add_url_rule(
        '/manage/main/<int:album_row_id>',
        endpoint='set_main_album',
        view_func=set_main_album,
        methods=['POST']
    )
    app.add_url_rule(
        '/admin/database',
        endpoint='admin_database',
        view_func=admin_database
    )
    app.add_url_rule(
        '/admin/database/backup',
        endpoint='create_database_backup',
        view_func=create_database_backup,
        methods=['POST']
    )
    app.add_url_rule(
        '/admin/database/export.csv',
        endpoint='export_database_csv',
        view_func=export_database_csv
    )
    app.add_url_rule(
        '/admin/database/backup/<path:filename>',
        endpoint='download_database_backup',
        view_func=download_database_backup
    )
    app.add_url_rule(
        '/admin/database/cleanup',
        endpoint='cleanup_database',
        view_func=cleanup_database,
        methods=['POST']
    )
    app.add_url_rule(
        '/admin/database/restore',
        endpoint='restore_database_backup',
        view_func=restore_database_backup,
        methods=['POST']
    )


def admin_login():
    if session.get('admin_authenticated'):
        return redirect(url_for('admin_dashboard'))

    error = ''

    forwarded_for = request.headers.get('X-Forwarded-For', '')
    client_key = (
        forwarded_for.split(',')[0].strip()
        if forwarded_for
        else (request.remote_addr or 'unknown')
    )

    attempt = LOGIN_ATTEMPTS.get(
        client_key,
        {'count': 0, 'started_at': time.time()}
    )

    if time.time() - attempt.get('started_at', 0) > 900:
        attempt = {'count': 0, 'started_at': time.time()}
        LOGIN_ATTEMPTS[client_key] = attempt

    if request.method == 'POST':
        if attempt.get('count', 0) >= 5:
            error = 'PIN 입력을 여러 번 실패했습니다. 15분 뒤 다시 시도해 주세요.'
            return render_template('admin_login.html', error=error), 429

        submitted_pin = str(request.form.get('pin', '') or '').strip()

        if submitted_pin and hmac.compare_digest(submitted_pin, ADMIN_PIN):
            LOGIN_ATTEMPTS.pop(client_key, None)
            session.clear()
            session['admin_authenticated'] = True
            session.permanent = True
            session['_csrf_token'] = secrets.token_urlsafe(32)
            return redirect(url_for('admin_dashboard'))

        attempt['count'] = attempt.get('count', 0) + 1
        LOGIN_ATTEMPTS[client_key] = attempt
        remaining = max(0, 5 - attempt['count'])
        error = f'PIN이 올바르지 않습니다. 남은 시도 {remaining}회'

    return render_template('admin_login.html', error=error)

def admin_logout():
    session.clear()
    return redirect(url_for('index'))

def admin_dashboard():
    conn = get_db()

    try:
        row = conn.execute(
            '''
            SELECT
                COUNT(*) AS total,
                SUM(
                    CASE
                        WHEN classification = '한국'
                        THEN 1 ELSE 0
                    END
                ) AS korean,
                SUM(
                    CASE
                        WHEN classification = '해외'
                        THEN 1 ELSE 0
                    END
                ) AS overseas,
                SUM(
                    CASE
                        WHEN media_format = 'CD'
                        THEN 1 ELSE 0
                    END
                ) AS cd,
                SUM(
                    CASE
                        WHEN media_format = 'LP'
                        THEN 1 ELSE 0
                    END
                ) AS lp
            FROM albums
            '''
        ).fetchone()

        stats = {
            'total': int(
                row['total']
                or 0
            ),
            'korean': int(
                row['korean']
                or 0
            ),
            'overseas': int(
                row['overseas']
                or 0
            ),
            'cd': int(
                row['cd']
                or 0
            ),
            'lp': int(
                row['lp']
                or 0
            ),
        }

        recent_albums = conn.execute(
            '''
            SELECT
                id,
                management_no,
                artist,
                album_title,
                album_type,
                cover_url
            FROM albums
            ORDER BY id DESC
            LIMIT 5
            '''
        ).fetchall()
    finally:
        conn.close()

    return render_template(
        'admin.html',
        stats=stats,
        recent_albums=recent_albums
    )


def _backup_directory():
    path = os.path.join(
        DATA_DIR,
        'backups'
    )
    os.makedirs(
        path,
        exist_ok=True
    )
    return path


def _format_file_size(value):
    size = float(
        value
        or 0
    )

    for unit in (
        'B',
        'KB',
        'MB',
        'GB',
    ):
        if (
            size < 1024
            or unit == 'GB'
        ):
            if unit == 'B':
                return f'{int(size)} {unit}'
            return f'{size:.1f} {unit}'
        size /= 1024

    return f'{size:.1f} GB'


def _list_backups():
    backup_dir = (
        _backup_directory()
    )
    items = []

    try:
        filenames = os.listdir(
            backup_dir
        )
    except OSError:
        filenames = []

    for filename in filenames:
        if (
            not filename.startswith(
                'album-archive-'
            )
            or not filename.endswith(
                '.tar.gz'
            )
        ):
            continue

        path = os.path.join(
            backup_dir,
            filename
        )

        if not os.path.isfile(path):
            continue

        stat = os.stat(path)
        items.append({
            'filename': filename,
            'size': _format_file_size(
                stat.st_size
            ),
            'created_at': datetime.fromtimestamp(
                stat.st_mtime
            ).strftime(
                '%Y-%m-%d %H:%M:%S'
            ),
        })

    items.sort(
        key=lambda item:
            item['filename'],
        reverse=True
    )
    return items[:20]


def _safe_backup_path(filename):
    safe_name = os.path.basename(
        str(
            filename
            or ''
        )
    )

    if (
        safe_name != filename
        or not safe_name.startswith(
            'album-archive-'
        )
        or not safe_name.endswith(
            '.tar.gz'
        )
    ):
        return '', ''

    path = os.path.join(
        _backup_directory(),
        safe_name
    )

    if not os.path.isfile(path):
        return '', ''

    return safe_name, path


def _create_backup_archive():
    backup_dir = (
        _backup_directory()
    )
    stamp = datetime.now().strftime(
        '%Y%m%d-%H%M%S'
    )
    filename = (
        f'album-archive-{stamp}.tar.gz'
    )
    archive_path = os.path.join(
        backup_dir,
        filename
    )

    suffix = 2

    while os.path.exists(
        archive_path
    ):
        filename = (
            f'album-archive-{stamp}'
            f'-{suffix}.tar.gz'
        )
        archive_path = os.path.join(
            backup_dir,
            filename
        )
        suffix += 1

    snapshot_handle = (
        tempfile.NamedTemporaryFile(
            prefix='album-db-',
            suffix='.db',
            dir=backup_dir,
            delete=False
        )
    )
    snapshot_path = (
        snapshot_handle.name
    )
    snapshot_handle.close()

    source = get_db()
    target = sqlite3.connect(
        snapshot_path
    )

    try:
        source.backup(target)
    finally:
        target.close()
        source.close()

    upload_dir = os.path.join(
        DATA_DIR,
        'uploads'
    )

    try:
        with tarfile.open(
            archive_path,
            'w:gz'
        ) as archive:
            archive.add(
                snapshot_path,
                arcname='albums.db'
            )

            if os.path.isdir(
                upload_dir
            ):
                for root, dirs, files in os.walk(
                    upload_dir
                ):
                    dirs[:] = [
                        name
                        for name in dirs
                        if name != '.thumbs'
                    ]

                    for name in files:
                        source_path = os.path.join(
                            root,
                            name
                        )
                        relative = os.path.relpath(
                            source_path,
                            DATA_DIR
                        )
                        archive.add(
                            source_path,
                            arcname=relative,
                            recursive=False
                        )
    finally:
        try:
            os.remove(
                snapshot_path
            )
        except OSError:
            pass

    return filename


def admin_database():
    created = request.args.get(
        'created',
        ''
    ).strip()
    cleaned = request.args.get(
        'cleaned',
        ''
    ).strip()
    restored = request.args.get(
        'restored',
        ''
    ).strip()
    safety_backup = request.args.get(
        'safety_backup',
        ''
    ).strip()

    conn = get_db()

    try:
        stats_row = conn.execute(
            '''
            SELECT
                COUNT(*) AS total,
                SUM(
                    CASE
                        WHEN release_date IS NULL
                          OR TRIM(release_date) = ''
                        THEN 1 ELSE 0
                    END
                ) AS release_date_missing,
                SUM(
                    CASE
                        WHEN (
                            release_date IS NULL
                            OR TRIM(release_date) = ''
                        )
                        AND apple_collection_id
                            IS NOT NULL
                        AND TRIM(
                            apple_collection_id
                        ) != ''
                        THEN 1 ELSE 0
                    END
                ) AS apple_date_missing,
                SUM(
                    CASE
                        WHEN apple_collection_id
                            IS NULL
                          OR TRIM(
                            apple_collection_id
                          ) = ''
                        THEN 1 ELSE 0
                    END
                ) AS apple_id_missing,
                SUM(
                    CASE
                        WHEN cover_url IS NULL
                          OR TRIM(cover_url) = ''
                        THEN 1 ELSE 0
                    END
                ) AS cover_missing
            FROM albums
            '''
        ).fetchone()

        duplicate_groups = conn.execute(
            '''
            SELECT COUNT(*) AS count
            FROM (
                SELECT album_id
                FROM albums
                GROUP BY album_id
                HAVING COUNT(*) > 1
            )
            '''
        ).fetchone()['count']

        identity_mismatch_groups = conn.execute(
            '''
            SELECT COUNT(*) AS count
            FROM (
                SELECT album_id
                FROM albums
                WHERE album_id IS NOT NULL
                  AND TRIM(album_id) != ''
                GROUP BY album_id
                HAVING
                    COUNT(DISTINCT COALESCE(classification, '')) > 1
                    OR COUNT(DISTINCT COALESCE(album_type, '')) > 1
                    OR COUNT(DISTINCT COALESCE(genre, '')) > 1
                    OR COUNT(DISTINCT COALESCE(artist, '')) > 1
                    OR COUNT(DISTINCT COALESCE(album_title, '')) > 1
                    OR COUNT(DISTINCT COALESCE(release_year, '')) > 1
                    OR COUNT(DISTINCT COALESCE(release_date, '')) > 1
            )
            '''
        ).fetchone()['count']

        master_mismatch_groups = conn.execute(
            '''
            SELECT COUNT(*) AS count
            FROM album_master
            INNER JOIN albums
                ON albums.album_id =
                    album_master.album_id
               AND albums.is_primary_display = 1
            WHERE
                COALESCE(album_master.artist, '')
                    != COALESCE(albums.artist, '')
                OR COALESCE(album_master.album_title, '')
                    != COALESCE(albums.album_title, '')
                OR COALESCE(album_master.release_year, '')
                    != COALESCE(albums.release_year, '')
                OR COALESCE(album_master.release_date, '')
                    != COALESCE(albums.release_date, '')
            '''
        ).fetchone()['count']

        orphan_masters = conn.execute(
            '''
            SELECT COUNT(*) AS count
            FROM album_master
            WHERE NOT EXISTS (
                SELECT 1
                FROM albums
                WHERE albums.album_id =
                    album_master.album_id
            )
            '''
        ).fetchone()['count']

        orphan_artist_map = conn.execute(
            '''
            SELECT COUNT(*) AS count
            FROM album_artist_map
            WHERE NOT EXISTS (
                SELECT 1
                FROM albums
                WHERE albums.id =
                    album_artist_map.album_row_id
            )
            '''
        ).fetchone()['count']

        track_cache_count = conn.execute(
            '''
            SELECT COUNT(*) AS count
            FROM album_track_cache
            '''
        ).fetchone()['count']

        orphan_track_cache = conn.execute(
            '''
            SELECT COUNT(*) AS count
            FROM album_track_cache
            WHERE NOT EXISTS (
                SELECT 1
                FROM albums
                WHERE albums.album_id =
                    album_track_cache.album_id
            )
            '''
        ).fetchone()['count']

        release_date_rows = conn.execute(
            '''
            SELECT
                id,
                management_no,
                album_id,
                artist,
                album_title,
                release_year,
                apple_collection_id
            FROM albums
            WHERE release_date IS NULL
               OR TRIM(release_date) = ''
            ORDER BY
                artist COLLATE NOCASE,
                album_title COLLATE NOCASE,
                id
            '''
        ).fetchall()

        apple_id_rows = conn.execute(
            '''
            SELECT
                id,
                management_no,
                album_id,
                artist,
                album_title
            FROM albums
            WHERE apple_collection_id IS NULL
               OR TRIM(apple_collection_id) = ''
            ORDER BY
                artist COLLATE NOCASE,
                album_title COLLATE NOCASE,
                id
            '''
        ).fetchall()

        cover_rows = conn.execute(
            '''
            SELECT
                id,
                management_no,
                album_id,
                artist,
                album_title
            FROM albums
            WHERE cover_url IS NULL
               OR TRIM(cover_url) = ''
            ORDER BY
                artist COLLATE NOCASE,
                album_title COLLATE NOCASE,
                id
            '''
        ).fetchall()

        identity_rows = conn.execute(
            '''
            SELECT
                album_id,
                MIN(id) AS id,
                MIN(management_no) AS management_no,
                MIN(artist) AS artist,
                MIN(album_title) AS album_title,
                COUNT(*) AS copy_count,
                COUNT(DISTINCT COALESCE(classification, ''))
                    AS classification_count,
                COUNT(DISTINCT COALESCE(album_type, ''))
                    AS album_type_count,
                COUNT(DISTINCT COALESCE(genre, ''))
                    AS genre_count,
                COUNT(DISTINCT COALESCE(artist, ''))
                    AS artist_count,
                COUNT(DISTINCT COALESCE(album_title, ''))
                    AS album_title_count,
                COUNT(DISTINCT COALESCE(release_year, ''))
                    AS release_year_count,
                COUNT(DISTINCT COALESCE(release_date, ''))
                    AS release_date_count
            FROM albums
            WHERE album_id IS NOT NULL
              AND TRIM(album_id) != ''
            GROUP BY album_id
            HAVING
                classification_count > 1
                OR album_type_count > 1
                OR genre_count > 1
                OR artist_count > 1
                OR album_title_count > 1
                OR release_year_count > 1
                OR release_date_count > 1
            ORDER BY album_id
            '''
        ).fetchall()

        master_rows = conn.execute(
            '''
            SELECT
                albums.id,
                albums.management_no,
                albums.album_id,
                albums.artist AS album_artist,
                albums.album_title AS album_title,
                albums.release_year AS album_release_year,
                albums.release_date AS album_release_date,
                album_master.artist AS master_artist,
                album_master.album_title AS master_album_title,
                album_master.release_year AS master_release_year,
                album_master.release_date AS master_release_date
            FROM album_master
            INNER JOIN albums
                ON albums.album_id =
                    album_master.album_id
               AND albums.is_primary_display = 1
            WHERE
                COALESCE(album_master.artist, '')
                    != COALESCE(albums.artist, '')
                OR COALESCE(album_master.album_title, '')
                    != COALESCE(albums.album_title, '')
                OR COALESCE(album_master.release_year, '')
                    != COALESCE(albums.release_year, '')
                OR COALESCE(album_master.release_date, '')
                    != COALESCE(albums.release_date, '')
            ORDER BY albums.album_id
            '''
        ).fetchall()

        disconnected_master_rows = conn.execute(
            '''
            SELECT
                album_id,
                artist,
                album_title
            FROM album_master
            WHERE NOT EXISTS (
                SELECT 1
                FROM albums
                WHERE albums.album_id =
                    album_master.album_id
            )
            ORDER BY album_id
            '''
        ).fetchall()

        disconnected_artist_rows = conn.execute(
            '''
            SELECT
                album_row_id,
                artist_name
            FROM album_artist_map
            WHERE NOT EXISTS (
                SELECT 1
                FROM albums
                WHERE albums.id =
                    album_artist_map.album_row_id
            )
            ORDER BY album_row_id, artist_name
            '''
        ).fetchall()

        disconnected_track_rows = conn.execute(
            '''
            SELECT
                album_id,
                store,
                resolved_by
            FROM album_track_cache
            WHERE NOT EXISTS (
                SELECT 1
                FROM albums
                WHERE albums.album_id =
                    album_track_cache.album_id
            )
            ORDER BY album_id
            '''
        ).fetchall()
    finally:
        conn.close()

    db_size = 0

    try:
        db_size = os.path.getsize(
            DB_NAME
        )
    except OSError:
        pass

    stats = {
        'total': int(
            stats_row['total']
            or 0
        ),
        'release_date_missing': int(
            stats_row[
                'release_date_missing'
            ]
            or 0
        ),
        'apple_date_missing': int(
            stats_row[
                'apple_date_missing'
            ]
            or 0
        ),
        'apple_id_missing': int(
            stats_row[
                'apple_id_missing'
            ]
            or 0
        ),
        'cover_missing': int(
            stats_row[
                'cover_missing'
            ]
            or 0
        ),
        'duplicate_groups': int(
            duplicate_groups
            or 0
        ),
        'identity_mismatch_groups': int(
            identity_mismatch_groups
            or 0
        ),
        'master_mismatch_groups': int(
            master_mismatch_groups
            or 0
        ),
        'orphan_masters': int(
            orphan_masters
            or 0
        ),
        'orphan_artist_map': int(
            orphan_artist_map
            or 0
        ),
        'track_cache_count': int(
            track_cache_count
            or 0
        ),
        'orphan_track_cache': int(
            orphan_track_cache
            or 0
        ),
        'db_size': _format_file_size(
            db_size
        ),
    }

    release_date_issues = [
        {
            'id': row['id'],
            'management_no': row['management_no'],
            'album_id': row['album_id'],
            'artist': row['artist'],
            'album_title': row['album_title'],
            'detail': (
                'Apple ID 있음 · 자동 발매일 확인 대상'
                if str(
                    row['apple_collection_id']
                    or ''
                ).strip()
                else 'Apple ID 없음 · 발매일 직접 입력 필요'
            ),
        }
        for row in release_date_rows
    ]

    apple_id_issues = [
        {
            'id': row['id'],
            'management_no': row['management_no'],
            'album_id': row['album_id'],
            'artist': row['artist'],
            'album_title': row['album_title'],
            'detail': 'Apple Music 연결 정보가 없습니다.',
        }
        for row in apple_id_rows
    ]

    cover_issues = [
        {
            'id': row['id'],
            'management_no': row['management_no'],
            'album_id': row['album_id'],
            'artist': row['artist'],
            'album_title': row['album_title'],
            'detail': '표시할 커버 이미지가 없습니다.',
        }
        for row in cover_rows
    ]

    identity_issues = []

    identity_fields = (
        ('classification_count', '분류'),
        ('album_type_count', '앨범 종류'),
        ('genre_count', '장르'),
        ('artist_count', '아티스트'),
        ('album_title_count', '앨범명'),
        ('release_year_count', '발매년도'),
        ('release_date_count', '발매일'),
    )

    for row in identity_rows:
        different_fields = [
            label
            for key, label in identity_fields
            if int(
                row[key]
                or 0
            ) > 1
        ]
        identity_issues.append({
            'id': row['id'],
            'management_no': row['management_no'],
            'album_id': row['album_id'],
            'artist': row['artist'],
            'album_title': row['album_title'],
            'detail': (
                f"{row['copy_count']}개 보유본에서 "
                + ', '.join(different_fields)
                + ' 정보가 서로 다릅니다.'
            ),
        })

    master_issues = []

    for row in master_rows:
        differences = []

        for label, master_key, album_key in (
            (
                '아티스트',
                'master_artist',
                'album_artist',
            ),
            (
                '앨범명',
                'master_album_title',
                'album_title',
            ),
            (
                '발매년도',
                'master_release_year',
                'album_release_year',
            ),
            (
                '발매일',
                'master_release_date',
                'album_release_date',
            ),
        ):
            master_value = str(
                row[master_key]
                or ''
            )
            album_value = str(
                row[album_key]
                or ''
            )

            if master_value != album_value:
                differences.append(
                    f'{label}: '
                    f'{master_value or "(없음)"}'
                    f' → {album_value or "(없음)"}'
                )

        master_issues.append({
            'id': row['id'],
            'management_no': row['management_no'],
            'album_id': row['album_id'],
            'artist': row['album_artist'],
            'album_title': row['album_title'],
            'detail': ' / '.join(differences),
        })

    disconnected_issues = []

    for row in disconnected_master_rows:
        disconnected_issues.append({
            'id': None,
            'management_no': '-',
            'album_id': row['album_id'],
            'artist': row['artist'],
            'album_title': row['album_title'],
            'detail': (
                '실제 보유 앨범은 없고 '
                '앨범 기본정보만 남아 있습니다.'
            ),
        })

    for row in disconnected_artist_rows:
        disconnected_issues.append({
            'id': None,
            'management_no': '-',
            'album_id': '-',
            'artist': row['artist_name'],
            'album_title': '-',
            'detail': (
                '삭제된 앨범 행을 가리키는 '
                f"아티스트 연결정보입니다. "
                f"(row {row['album_row_id']})"
            ),
        })

    for row in disconnected_track_rows:
        disconnected_issues.append({
            'id': None,
            'management_no': '-',
            'album_id': row['album_id'],
            'artist': '-',
            'album_title': '-',
            'detail': (
                '실제 앨범은 없고 '
                '수록곡 캐시만 남아 있습니다.'
            ),
        })

    issue_groups = [
        {
            'key': 'release-date',
            'title': '발매일 없음',
            'description':
                '정확한 발매일이 저장되지 않은 보유본입니다.',
            'items': release_date_issues,
            'important': False,
        },
        {
            'key': 'apple-id',
            'title': 'Apple ID 없음',
            'description':
                'Apple Music과 연결되지 않은 보유본입니다. 직접 등록 음반이면 정상일 수 있습니다.',
            'items': apple_id_issues,
            'important': False,
        },
        {
            'key': 'cover',
            'title': '커버 없음',
            'description':
                '커버 이미지가 없는 보유본입니다.',
            'items': cover_issues,
            'important': False,
        },
        {
            'key': 'identity',
            'title': '공통정보 불일치',
            'description':
                '같은 album_id로 묶인 보유본 사이에서 공통정보가 서로 다릅니다.',
            'items': identity_issues,
            'important': True,
        },
        {
            'key': 'master',
            'title': 'Master 정보 불일치',
            'description':
                '앨범 기본정보와 현재 대표 보유본의 정보가 서로 다릅니다.',
            'items': master_issues,
            'important': True,
        },
        {
            'key': 'disconnected',
            'title': '연결 끊긴 데이터',
            'description':
                '삭제된 앨범을 가리키는 연결정보 또는 캐시가 남아 있습니다.',
            'items': disconnected_issues,
            'important': True,
        },
    ]

    return render_template(
        'database_admin.html',
        stats=stats,
        issue_groups=issue_groups,
        backups=_list_backups(),
        created=created,
        cleaned=cleaned,
        restored=restored,
        safety_backup=safety_backup
    )


def export_database_csv():
    conn = get_db()

    try:
        rows = conn.execute(
            '''
            SELECT
                management_no,
                album_id,
                classification,
                media_format,
                album_type,
                genre,
                artist,
                album_title,
                release_year,
                release_date,
                open_status,
                signed,
                purchase_price,
                barcode,
                catalog_no,
                release_country,
                edition,
                purchase_date,
                purchase_store,
                storage_location,
                memo,
                apple_collection_id,
                cover_url,
                is_primary_display
            FROM albums
            ORDER BY id ASC
            '''
        ).fetchall()
    finally:
        conn.close()

    columns = [
        'management_no',
        'album_id',
        'classification',
        'media_format',
        'album_type',
        'genre',
        'artist',
        'album_title',
        'release_year',
        'release_date',
        'open_status',
        'signed',
        'purchase_price',
        'barcode',
        'catalog_no',
        'release_country',
        'edition',
        'purchase_date',
        'purchase_store',
        'storage_location',
        'memo',
        'apple_collection_id',
        'cover_url',
        'is_primary_display',
    ]

    output = io.StringIO(
        newline=''
    )
    writer = csv.writer(
        output
    )
    writer.writerow(
        columns
    )

    for row in rows:
        writer.writerow([
            (
                row[column]
                if row[column] is not None
                else ''
            )
            for column in columns
        ])

    payload = (
        '\ufeff'
        + output.getvalue()
    ).encode(
        'utf-8'
    )
    filename = (
        'album-archive-'
        + datetime.now().strftime(
            '%Y%m%d-%H%M%S'
        )
        + '.csv'
    )

    return Response(
        payload,
        mimetype='text/csv; charset=utf-8',
        headers={
            'Content-Disposition':
                f'attachment; filename="{filename}"'
        }
    )


def create_database_backup():
    filename = (
        _create_backup_archive()
    )

    return redirect(
        url_for(
            'admin_database',
            created=filename
        )
    )


def download_database_backup(
    filename
):
    safe_name, path = (
        _safe_backup_path(
            filename
        )
    )

    if not path:
        abort(404)

    return send_file(
        path,
        as_attachment=True,
        download_name=safe_name
    )


def restore_database_backup():
    filename = request.form.get(
        'filename',
        ''
    ).strip()
    safe_name, archive_path = (
        _safe_backup_path(
            filename
        )
    )

    if not archive_path:
        abort(404)

    safety_backup = (
        _create_backup_archive()
    )

    with tempfile.TemporaryDirectory(
        prefix='album-restore-'
    ) as temp_dir:
        with tarfile.open(
            archive_path,
            'r:gz'
        ) as archive:
            safe_members = []

            for member in archive.getmembers():
                name = str(
                    member.name
                    or ''
                ).replace(
                    '\\',
                    '/'
                )

                if (
                    not name
                    or name.startswith('/')
                    or '..' in name.split('/')
                    or member.issym()
                    or member.islnk()
                ):
                    raise ValueError(
                        '안전하지 않은 백업 파일입니다.'
                    )

                if (
                    name == 'albums.db'
                    or name.startswith(
                        'uploads/'
                    )
                ):
                    safe_members.append(
                        member
                    )

            archive.extractall(
                temp_dir,
                members=safe_members
            )

        snapshot_path = os.path.join(
            temp_dir,
            'albums.db'
        )

        if not os.path.isfile(
            snapshot_path
        ):
            raise ValueError(
                '백업에 albums.db가 없습니다.'
            )

        source = sqlite3.connect(
            snapshot_path
        )

        try:
            integrity = source.execute(
                'PRAGMA integrity_check'
            ).fetchone()[0]

            if str(
                integrity
                or ''
            ).lower() != 'ok':
                raise ValueError(
                    '백업 DB 무결성 검사에 실패했습니다.'
                )

            target = get_db()

            try:
                source.backup(
                    target
                )
            finally:
                target.close()
        finally:
            source.close()

        restored_uploads = os.path.join(
            temp_dir,
            'uploads'
        )
        live_uploads = os.path.join(
            DATA_DIR,
            'uploads'
        )

        if os.path.isdir(
            live_uploads
        ):
            shutil.rmtree(
                live_uploads
            )

        if os.path.isdir(
            restored_uploads
        ):
            shutil.copytree(
                restored_uploads,
                live_uploads
            )
        else:
            os.makedirs(
                live_uploads,
                exist_ok=True
            )

    init_db()
    clear_search_cache()
    clear_track_caches()
    prewarm_thumbnail_directory(
        os.path.join(
            DATA_DIR,
            'uploads'
        )
    )

    return redirect(
        url_for(
            'admin_database',
            restored=safe_name,
            safety_backup=safety_backup
        )
    )


def cleanup_database():
    conn = get_db()

    try:
        orphan_masters = conn.execute(
            '''
            DELETE FROM album_master
            WHERE NOT EXISTS (
                SELECT 1
                FROM albums
                WHERE albums.album_id =
                    album_master.album_id
            )
            '''
        ).rowcount

        orphan_artist_map = conn.execute(
            '''
            DELETE FROM album_artist_map
            WHERE NOT EXISTS (
                SELECT 1
                FROM albums
                WHERE albums.id =
                    album_artist_map.album_row_id
            )
            '''
        ).rowcount

        orphan_track_cache = conn.execute(
            '''
            DELETE FROM album_track_cache
            WHERE NOT EXISTS (
                SELECT 1
                FROM albums
                WHERE albums.album_id =
                    album_track_cache.album_id
            )
            '''
        ).rowcount

        conn.execute(
            '''
            DELETE FROM album_tracks
            WHERE NOT EXISTS (
                SELECT 1
                FROM albums
                WHERE albums.album_id =
                    album_tracks.album_id
            )
            '''
        )
        conn.execute(
            '''
            DELETE FROM album_track_settings
            WHERE NOT EXISTS (
                SELECT 1
                FROM albums
                WHERE albums.album_id =
                    album_track_settings.album_id
            )
            '''
        )
        conn.execute(
            'PRAGMA optimize'
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    cleaned_count = (
        max(
            int(
                orphan_masters
                or 0
            ),
            0
        )
        + max(
            int(
                orphan_artist_map
                or 0
            ),
            0
        )
        + max(
            int(
                orphan_track_cache
                or 0
            ),
            0
        )
    )

    return redirect(
        url_for(
            'admin_database',
            cleaned=str(
                cleaned_count
            )
        )
    )


def set_main_album(album_row_id):
    conn = get_db()

    try:
        row = conn.execute(
            'SELECT management_no, album_id FROM albums WHERE id = ?',
            (album_row_id,)
        ).fetchone()

        if row is None:
            return redirect(url_for('manage_albums'))

        set_primary_display(conn, album_row_id)
        conn.commit()
        management_no = row['management_no'] or ''

    finally:
        conn.close()

    redirect_args = {
        'featured': management_no,
    }

    for name in (
        'library_q',
        'classification',
        'media_format',
        'open_status',
        'signed',
        'sort_by',
        'sort_order',
        'page',
    ):
        value = request.form.get(name, '').strip()
        if value:
            redirect_args[name] = value

    return redirect(
        url_for(
            'manage_albums',
            **redirect_args
        )
    )


def manage_albums():
    library_q = request.args.get('library_q', '').strip()
    classification_filter = request.args.get('classification', '').strip()
    format_filter = request.args.get('media_format', '').strip()
    status_filter = request.args.get('open_status', '').strip()
    signed_filter = request.args.get('signed', '').strip()
    sort_by = request.args.get('sort_by', 'id').strip()
    sort_order = request.args.get(
        'sort_order',
        'desc'
    ).strip().lower()
    page = parse_page_number(request.args.get('page', '1'))
    updated = request.args.get('updated', '')
    deleted = request.args.get('deleted', '')
    featured = request.args.get('featured', '')

    conn = get_db()
    conditions = []
    params = []

    if library_q:
        value = f'%{library_q}%'
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
            ')'
        )
        params.extend(
            [value] * 12
        )

    if classification_filter:
        conditions.append('classification = ?')
        params.append(classification_filter)

    if format_filter == 'CD':
        conditions.append("media_format = 'CD'")
    elif format_filter == 'LP':
        conditions.append("media_format = 'LP'")
    elif format_filter == 'ETC':
        conditions.append(
            "media_format IN ( 'Casette', 'USB', '기타' )"
        )

    if status_filter:
        conditions.append('open_status = ?')
        params.append(status_filter)

    if signed_filter:
        conditions.append('signed = ?')
        params.append(signed_filter)

    sort_columns = {
        'id': 'id',
        'management_no':
            'management_no COLLATE NOCASE',
        'artist':
            'artist COLLATE NOCASE',
        'album_title':
            'album_title COLLATE NOCASE',
        'release_year':
            'release_year',
        'release_date': (
            "CASE "
            "WHEN release_date "
            "GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' "
            "THEN release_date "
            "WHEN release_year "
            "GLOB '[0-9][0-9][0-9][0-9]' "
            "THEN release_year || '-00-00' "
            "ELSE '' END"
        ),
        'purchase_price':
            'purchase_price',
        'purchase_date':
            "COALESCE(purchase_date, '')",
        'catalog_no':
            'catalog_no COLLATE NOCASE',
        'storage_location':
            'storage_location COLLATE NOCASE',
    }

    if sort_by not in sort_columns:
        sort_by = 'id'

    if sort_order not in {'asc', 'desc'}:
        sort_order = 'desc'

    where_sql = (
        ' WHERE ' + ' AND '.join(conditions)
        if conditions
        else ''
    )

    try:
        filtered_album_count = conn.execute(
            'SELECT COUNT(*) AS count '
            'FROM albums'
            + where_sql,
            params
        ).fetchone()['count']

        total_pages = max(
            1,
            (
                filtered_album_count
                + ALBUMS_PER_PAGE
                - 1
            ) // ALBUMS_PER_PAGE
        )
        page = min(
            page,
            total_pages
        )
        offset = (
            page - 1
        ) * ALBUMS_PER_PAGE

        query = (
            'SELECT albums.*, '
            'COALESCE(copies.copy_count, 1) AS copy_count '
            'FROM albums '
            'LEFT JOIN ( '
            '    SELECT '
            '        album_id AS copy_album_id, '
            '        COUNT(*) AS copy_count '
            '    FROM albums '
            '    GROUP BY album_id '
            ') AS copies '
            'ON copies.copy_album_id = albums.album_id'
            + where_sql
            + (
                f" ORDER BY {sort_columns[sort_by]} "
                f"{sort_order.upper()}, albums.id DESC"
            )
            + ' LIMIT ? OFFSET ?'
        )
        albums = conn.execute(
            query,
            params + [
                ALBUMS_PER_PAGE,
                offset,
            ]
        ).fetchall()

        total_album_count = conn.execute(
            'SELECT COUNT(*) AS count '
            'FROM albums'
        ).fetchone()['count']
    finally:
        conn.close()

    return render_template(
        'manage.html',
        albums=albums,
        total_album_count=total_album_count,
        filtered_album_count=filtered_album_count,
        library_q=library_q,
        classification_filter=classification_filter,
        format_filter=format_filter,
        status_filter=status_filter,
        signed_filter=signed_filter,
        sort_by=sort_by,
        sort_order=sort_order,
        page=page,
        total_pages=total_pages,
        pagination_pages=build_pagination_pages(
            page,
            total_pages
        ),
        updated=updated,
        deleted=deleted,
        featured=featured
    )


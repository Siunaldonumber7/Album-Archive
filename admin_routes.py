import hmac
import secrets
import time

from flask import render_template, request, redirect, url_for, session

from album_database import get_db, set_primary_display
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
            '( management_no LIKE ? OR album_id LIKE ? OR artist LIKE ? OR album_title LIKE ? OR release_year LIKE ? OR memo LIKE ? )'
        )
        params.extend(
            [value, value, value, value, value, value]
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
        'management_no': 'management_no COLLATE NOCASE',
        'artist': 'artist COLLATE NOCASE',
        'album_title': 'album_title COLLATE NOCASE',
        'release_year': 'release_year',
        'purchase_price': 'purchase_price'
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


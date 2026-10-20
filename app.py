import hmac
import os
import secrets
from datetime import timedelta

from flask import (
    Flask,
    abort,
    redirect,
    request,
    session,
    url_for,
)

from flask_compress import Compress

from album_database import DATA_DIR, get_db, init_db
from catalog import (
    split_genres,
    artist_credit_parts,
    split_artists,
)

from cache_utils import BoundedCache
from media_service import configure_media, cover_thumbnail_url
from release_date_backfill import start_release_date_backfill
from admin_routes import register_admin_routes
from album_routes import register_album_routes
from library_routes import register_library_routes
from track_routes import register_track_routes

app = Flask(__name__)
app.config.update(
    COMPRESS_MIMETYPES=[
        'text/html',
        'text/css',
        'text/javascript',
        'application/javascript',
        'application/json',
    ],
    COMPRESS_LEVEL=6,
    COMPRESS_MIN_SIZE=500,
)
Compress(app)


def load_or_create_private_value(env_name, filename, generator):
    env_value = str(os.environ.get(env_name, '') or '').strip()
    if env_value:
        return env_value

    path = os.path.join(DATA_DIR, filename)

    try:
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as handle:
                value = handle.read().strip()
                if value:
                    return value

        value = generator()
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write(value + '\n')
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
        return value
    except Exception as exc:
        print('PRIVATE VALUE FILE ERROR:', filename, exc)
        return generator()


APP_SECRET = load_or_create_private_value(
    'APP_SECRET_KEY',
    'flask_secret.key',
    lambda: secrets.token_urlsafe(48)
)
ADMIN_PIN = load_or_create_private_value(
    'ADMIN_PIN',
    'admin_pin.txt',
    lambda: f'{secrets.randbelow(100_000_000):08d}'
)

app.secret_key = APP_SECRET
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=True,
    PERMANENT_SESSION_LIFETIME=timedelta(days=30)
)

LOGIN_ATTEMPTS = BoundedCache(500)

configure_media(app)

if (
    os.environ.get(
        'ALBUM_BACKGROUND_MAINTENANCE',
        ''
    ).strip() == '1'
):
    start_release_date_backfill()

ADMIN_ENDPOINTS = {
    'admin_dashboard',
    'admin_database',
    'create_database_backup',
    'export_database_csv',
    'download_database_backup',
    'cleanup_database',
    'restore_database_backup',
    'manage_albums',
    'set_main_album',
    'add_album_page',
    'add_album_from_link',
    'add_album_manual',
    'register',
    'register_bulk',
    'save_album',
    'save_bulk_albums',
    'edit_album',
    'update_album',
    'delete_album',
    'delete_genre',
    'edit_album_tracks',
    'save_album_tracks',
    'reset_album_tracks',
    'admin_logout',
}


def get_csrf_token():
    token = session.get(
        '_csrf_token'
    )

    if not token:
        token = secrets.token_urlsafe(32)
        session['_csrf_token'] = token

    return token


def has_valid_csrf():
    expected = session.get(
        '_csrf_token',
        ''
    )
    received = request.form.get(
        '_csrf_token',
        ''
    )

    return bool(
        expected
        and received
        and hmac.compare_digest(
            expected,
            received
        )
    )


app.jinja_env.globals[
    'csrf_token'
] = get_csrf_token


@app.before_request
def protect_admin_routes():
    endpoint = request.endpoint or ''

    if endpoint == 'admin_login':
        if (
            request.method == 'POST'
            and not has_valid_csrf()
        ):
            abort(400)
        return None

    if endpoint not in ADMIN_ENDPOINTS:
        return None

    if not session.get(
        'admin_authenticated'
    ):
        return redirect(
            url_for('admin_login')
        )

    if (
        request.method == 'POST'
        and not has_valid_csrf()
    ):
        abort(400)

    return None


@app.after_request
def add_security_headers(response):
    response.headers.setdefault(
        'X-Content-Type-Options',
        'nosniff'
    )
    response.headers.setdefault(
        'X-Frame-Options',
        'SAMEORIGIN'
    )
    response.headers.setdefault(
        'Referrer-Policy',
        'strict-origin-when-cross-origin'
    )
    response.headers.setdefault(
        'Content-Security-Policy',
        "default-src 'self'; img-src 'self' https: data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'self'; frame-ancestors 'self'"
    )

    if request.path.startswith(
        '/static/uploads/.thumbs/'
    ):
        response.headers[
            'Cache-Control'
        ] = (
            'public, max-age=31536000, '
            'immutable'
        )
    elif request.path.startswith(
        '/static/uploads/'
    ):
        response.headers[
            'Cache-Control'
        ] = 'public, max-age=86400'

    forwarded_proto = request.headers.get(
        'X-Forwarded-Proto',
        ''
    ).lower()
    if request.is_secure or forwarded_proto == 'https':
        response.headers.setdefault(
            'Strict-Transport-Security',
            'max-age=31536000; includeSubDomains'
        )

    return response

app.jinja_env.globals['split_genres'] = split_genres
app.jinja_env.globals['artist_credit_parts'] = artist_credit_parts
app.jinja_env.globals['split_artists'] = split_artists
app.jinja_env.globals['cover_thumbnail_url'] = cover_thumbnail_url

@app.get('/healthz')
def healthz():
    conn = get_db()

    try:
        conn.execute(
            'SELECT 1'
        ).fetchone()
    finally:
        conn.close()

    return {
        'status': 'ok'
    }


register_admin_routes(
    app,
    ADMIN_PIN,
    LOGIN_ATTEMPTS
)
register_album_routes(app)
register_track_routes(app)
register_library_routes(app)


if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=5001, debug=True)

import hashlib
import os
import threading
from functools import lru_cache

from flask import current_app, url_for
from PIL import Image, UnidentifiedImageError

from catalog import apple_artwork_size


UPLOAD_FOLDER = ''
THUMBNAIL_FOLDER = ''
ALLOWED_IMAGE_EXTENSIONS = {
    'jpg',
    'jpeg',
    'png',
    'webp',
    'gif',
}
THUMBNAIL_SIZES = (
    400,
    180,
    800,
)
THUMBNAIL_WRITE_LOCK = threading.Lock()
_PREWARM_START_LOCK = threading.Lock()
_PREWARM_STARTED = False


def _set_media_paths(upload_folder):
    global UPLOAD_FOLDER, THUMBNAIL_FOLDER

    _thumbnail_name_parts.cache_clear()
    _cached_local_thumbnail_name.cache_clear()

    UPLOAD_FOLDER = os.path.abspath(
        str(
            upload_folder
            or ''
        )
    )
    THUMBNAIL_FOLDER = os.path.join(
        UPLOAD_FOLDER,
        '.thumbs'
    )

    os.makedirs(
        UPLOAD_FOLDER,
        exist_ok=True
    )
    os.makedirs(
        THUMBNAIL_FOLDER,
        exist_ok=True
    )


def configure_media(app):
    upload_folder = os.path.join(
        app.root_path,
        'static',
        'uploads'
    )

    _set_media_paths(
        upload_folder
    )

    app.config[
        'MAX_CONTENT_LENGTH'
    ] = 15 * 1024 * 1024
    app.config[
        'UPLOAD_FOLDER'
    ] = UPLOAD_FOLDER

    Image.MAX_IMAGE_PIXELS = (
        40_000_000
    )

    if (
        os.environ.get(
            'ALBUM_SKIP_BACKGROUND_THUMBNAIL_PREWARM',
            ''
        ).strip() != '1'
    ):
        start_thumbnail_prewarm()


def allowed_image_file(filename):
    if (
        not filename
        or '.' not in filename
    ):
        return False

    extension = (
        filename
        .rsplit('.', 1)[1]
        .lower()
    )

    return (
        extension
        in ALLOWED_IMAGE_EXTENSIONS
    )


def _safe_filename(filename):
    return os.path.basename(
        str(
            filename
            or ''
        ).replace(
            '\\',
            '/'
        )
    ).strip()


def _source_path(filename):
    safe_name = _safe_filename(
        filename
    )

    if not safe_name:
        return ''

    upload_root = os.path.abspath(
        UPLOAD_FOLDER
    )
    source_path = os.path.abspath(
        os.path.join(
            upload_root,
            safe_name
        )
    )

    if not source_path.startswith(
        upload_root + os.sep
    ):
        return ''

    return source_path


@lru_cache(maxsize=4096)
def _thumbnail_name_parts(
    filename
):
    safe_name = _safe_filename(
        filename
    )
    stem = (
        os.path.splitext(
            safe_name
        )[0].strip(' .')
        or 'cover'
    )

    name_digest = hashlib.sha1(
        safe_name.encode(
            'utf-8',
            errors='ignore'
        )
    ).hexdigest()[:10]

    source_path = _source_path(
        safe_name
    )
    version_seed = safe_name

    try:
        stat = os.stat(
            source_path
        )
        version_seed += (
            f'|{stat.st_size}'
            f'|{stat.st_mtime_ns}'
        )
    except OSError:
        pass

    version_digest = hashlib.sha1(
        version_seed.encode(
            'utf-8',
            errors='ignore'
        )
    ).hexdigest()[:10]

    return (
        stem,
        name_digest,
        version_digest,
    )


def local_cover_thumbnail_path(
    filename,
    size=400
):
    (
        stem,
        name_digest,
        version_digest,
    ) = _thumbnail_name_parts(
        filename
    )

    return os.path.join(
        THUMBNAIL_FOLDER,
        (
            f'{stem}__{name_digest}'
            f'__{version_digest}'
            f'__thumb_{int(size)}.webp'
        )
    )


@lru_cache(maxsize=8192)
def _cached_local_thumbnail_name(
    filename,
    size
):
    safe_name = _safe_filename(
        filename
    )

    if not safe_name:
        return ''

    source_path = _source_path(
        safe_name
    )

    if (
        not source_path
        or not os.path.isfile(
            source_path
        )
    ):
        return ''

    thumb_path = (
        local_cover_thumbnail_path(
            safe_name,
            size
        )
    )

    if not os.path.isfile(
        thumb_path
    ):
        return ''

    return os.path.basename(
        thumb_path
    )


def _prepare_image(
    image,
    max_size
):
    try:
        image.seek(0)
    except Exception:
        pass

    if (
        image.format == 'JPEG'
        and max_size > 0
    ):
        try:
            image.draft(
                'RGB',
                (
                    max_size,
                    max_size,
                )
            )
        except Exception:
            pass

    image.load()

    if image.mode not in {
        'RGB',
        'RGBA',
    }:
        has_transparency = (
            'transparency'
            in image.info
        )
        image = image.convert(
            (
                'RGBA'
                if has_transparency
                else 'RGB'
            )
        )
    else:
        image = image.copy()

    image.thumbnail(
        (
            max_size,
            max_size,
        ),
        Image.Resampling.LANCZOS,
        reducing_gap=3.0
    )

    return image


def _write_thumbnail(
    image,
    thumb_path
):
    os.makedirs(
        THUMBNAIL_FOLDER,
        exist_ok=True
    )

    temp_path = (
        thumb_path
        + '.tmp-'
        + str(
            threading.get_ident()
        )
    )

    try:
        image.save(
            temp_path,
            'WEBP',
            quality=80,
            method=4
        )
        os.replace(
            temp_path,
            thumb_path
        )
    finally:
        if os.path.isfile(
            temp_path
        ):
            os.remove(
                temp_path
            )


def generate_cover_thumbnails(
    filename,
    sizes=THUMBNAIL_SIZES
):
    safe_name = _safe_filename(
        filename
    )

    if (
        not safe_name
        or not allowed_image_file(
            safe_name
        )
    ):
        return 0

    source_path = _source_path(
        safe_name
    )

    if (
        not source_path
        or not os.path.isfile(
            source_path
        )
    ):
        return 0

    normalized_sizes = sorted(
        {
            max(
                120,
                min(
                    int(size),
                    800
                )
            )
            for size in sizes
        },
        reverse=True
    )

    missing_sizes = [
        size
        for size in normalized_sizes
        if not os.path.isfile(
            local_cover_thumbnail_path(
                safe_name,
                size
            )
        )
    ]

    if not missing_sizes:
        return 0

    created = 0

    with THUMBNAIL_WRITE_LOCK:
        missing_sizes = [
            size
            for size in missing_sizes
            if not os.path.isfile(
                local_cover_thumbnail_path(
                    safe_name,
                    size
                )
            )
        ]

        if not missing_sizes:
            return 0

        try:
            with Image.open(
                source_path
            ) as source:
                largest_size = max(
                    missing_sizes
                )
                base = _prepare_image(
                    source,
                    largest_size
                )

            for size in missing_sizes:
                thumb_path = (
                    local_cover_thumbnail_path(
                        safe_name,
                        size
                    )
                )

                if os.path.isfile(
                    thumb_path
                ):
                    continue

                if (
                    base.width <= size
                    and base.height <= size
                ):
                    output = base.copy()
                else:
                    output = base.copy()
                    output.thumbnail(
                        (
                            size,
                            size,
                        ),
                        Image.Resampling.LANCZOS,
                        reducing_gap=3.0
                    )

                try:
                    _write_thumbnail(
                        output,
                        thumb_path
                    )
                    created += 1
                finally:
                    output.close()
        except (
            UnidentifiedImageError,
            OSError,
            ValueError,
            Image.DecompressionBombError,
        ) as exc:
            print(
                'COVER THUMBNAIL ERROR:',
                safe_name,
                exc
            )
        finally:
            try:
                base.close()
            except Exception:
                pass

    if created:
        _cached_local_thumbnail_name.cache_clear()

    return created


def _prewarm_worker():
    created = 0
    checked = 0

    try:
        filenames = sorted(
            os.listdir(
                UPLOAD_FOLDER
            )
        )
    except OSError:
        return

    for filename in filenames:
        if (
            filename.startswith('.')
            or not allowed_image_file(
                filename
            )
        ):
            continue

        source_path = _source_path(
            filename
        )

        if not os.path.isfile(
            source_path
        ):
            continue

        checked += 1
        created += generate_cover_thumbnails(
            filename
        )

    if checked:
        print(
            'COVER THUMBNAIL PREWARM:',
            checked,
            'files /',
            created,
            'created'
        )


def prewarm_thumbnail_directory(
    upload_folder
):
    _set_media_paths(
        upload_folder
    )
    Image.MAX_IMAGE_PIXELS = (
        40_000_000
    )
    _prewarm_worker()


def start_thumbnail_prewarm():
    global _PREWARM_STARTED

    with _PREWARM_START_LOCK:
        if _PREWARM_STARTED:
            return

        _PREWARM_STARTED = True

        thread = threading.Thread(
            target=_prewarm_worker,
            name='cover-thumbnail-prewarm',
            daemon=True
        )
        thread.start()


def save_uploaded_cover(file):
    if (
        not file
        or not file.filename
    ):
        return None

    if not allowed_image_file(
        file.filename
    ):
        return None

    try:
        file.stream.seek(0)

        with Image.open(
            file.stream
        ) as image:
            width, height = image.size

            if (
                width < 1
                or height < 1
                or width * height
                > 40_000_000
            ):
                return None

            image.verify()

        file.stream.seek(0)
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        Image.DecompressionBombError,
    ):
        return None

    original_name = os.path.basename(
        file.filename.replace(
            '\\',
            '/'
        )
    ).strip()

    original_name = ''.join(
        ch
        for ch in original_name
        if (
            ch not in {
                '/',
                '\\',
            }
            and ord(ch) >= 32
        )
    ).strip(' .')

    if (
        not original_name
        or '.' not in original_name
    ):
        return None

    stem, extension = os.path.splitext(
        original_name
    )
    extension = extension.lower()
    stem = (
        stem.strip(' .')
        or 'cover'
    )

    filename = (
        f'{stem}{extension}'
    )
    save_path = os.path.join(
        current_app.config[
            'UPLOAD_FOLDER'
        ],
        filename
    )

    counter = 2

    while os.path.exists(
        save_path
    ):
        filename = (
            f'{stem}_{counter}'
            f'{extension}'
        )
        save_path = os.path.join(
            current_app.config[
                'UPLOAD_FOLDER'
            ],
            filename
        )
        counter += 1

    file.save(
        save_path
    )

    # Pay the resize cost once at upload time so normal browsing never has
    # to open a large original just to build a list thumbnail.
    generate_cover_thumbnails(
        filename
    )

    return url_for(
        'static',
        filename=(
            f'uploads/{filename}'
        )
    )


def delete_local_cover(cover_url):
    if not cover_url:
        return

    prefix = '/static/uploads/'

    if not cover_url.startswith(
        prefix
    ):
        return

    filename = cover_url[
        len(prefix):
    ]
    safe_name = _safe_filename(
        filename
    )
    source_path = _source_path(
        safe_name
    )

    if not source_path:
        return

    try:
        if os.path.isfile(
            source_path
        ):
            os.remove(
                source_path
            )

        stem = (
            os.path.splitext(
                safe_name
            )[0].strip(' .')
            or 'cover'
        )
        name_digest = hashlib.sha1(
            safe_name.encode(
                'utf-8',
                errors='ignore'
            )
        ).hexdigest()[:10]

        if os.path.isdir(
            THUMBNAIL_FOLDER
        ):
            prefixes = (
                (
                    f'{stem}'
                    f'__{name_digest}__'
                ),
                (
                    f'{stem}'
                    f'__thumb_'
                ),
            )

            for thumb_name in os.listdir(
                THUMBNAIL_FOLDER
            ):
                if not thumb_name.startswith(
                    prefixes
                ):
                    continue

                thumb_path = os.path.join(
                    THUMBNAIL_FOLDER,
                    thumb_name
                )

                if os.path.isfile(
                    thumb_path
                ):
                    os.remove(
                        thumb_path
                    )

    except Exception as exc:
        print(
            'LOCAL COVER DELETE ERROR:',
            exc
        )
    finally:
        _thumbnail_name_parts.cache_clear()
        _cached_local_thumbnail_name.cache_clear()


def cover_thumbnail_url(
    cover_url,
    size=400
):
    cover_url = str(
        cover_url
        or ''
    ).strip()

    if not cover_url:
        return ''

    size = max(
        120,
        min(
            int(size or 400),
            800
        )
    )

    if cover_url.startswith(
        (
            'http://',
            'https://',
        )
    ):
        return apple_artwork_size(
            cover_url,
            size
        )

    prefix = '/static/uploads/'

    if not cover_url.startswith(
        prefix
    ):
        return cover_url

    filename = _safe_filename(
        cover_url[
            len(prefix):
        ]
    )

    if not filename:
        return cover_url

    thumb_name = (
        _cached_local_thumbnail_name(
            filename,
            size
        )
    )

    if not thumb_name:
        generate_cover_thumbnails(
            filename,
            sizes=(size,)
        )
        _cached_local_thumbnail_name.cache_clear()
        thumb_name = (
            _cached_local_thumbnail_name(
                filename,
                size
            )
        )

    if not thumb_name:
        return cover_url

    return url_for(
        'static',
        filename=(
            'uploads/.thumbs/'
            + thumb_name
        )
    )

import os

from flask import current_app, url_for
from PIL import Image, UnidentifiedImageError


UPLOAD_FOLDER = ''
THUMBNAIL_FOLDER = ''
ALLOWED_IMAGE_EXTENSIONS = {'jpg', 'jpeg', 'png', 'webp', 'gif'}


def configure_media(app):
    global UPLOAD_FOLDER, THUMBNAIL_FOLDER

    UPLOAD_FOLDER = os.path.join(
        app.root_path,
        'static',
        'uploads'
    )
    THUMBNAIL_FOLDER = os.path.join(
        UPLOAD_FOLDER,
        '.thumbs'
    )

    app.config['MAX_CONTENT_LENGTH'] = 15 * 1024 * 1024
    app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

    Image.MAX_IMAGE_PIXELS = 40_000_000

    os.makedirs(
        UPLOAD_FOLDER,
        exist_ok=True
    )
    os.makedirs(
        THUMBNAIL_FOLDER,
        exist_ok=True
    )


def allowed_image_file(filename):
    if not filename or '.' not in filename:
        return False
    extension = filename.rsplit('.', 1)[1].lower()
    return extension in ALLOWED_IMAGE_EXTENSIONS

def save_uploaded_cover(file):
    if not file or not file.filename:
        return None
    if not allowed_image_file(file.filename):
        return None

    try:
        file.stream.seek(0)
        with Image.open(file.stream) as image:
            width, height = image.size
            if width < 1 or height < 1 or width * height > 40_000_000:
                return None
            image.verify()
        file.stream.seek(0)
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        return None

    # 원본 파일명(한글/공백 포함)을 최대한 유지하되 경로 문자는 제거한다.
    original_name = os.path.basename(file.filename.replace('\\', '/')).strip()
    original_name = ''.join(
        ch for ch in original_name
        if ch not in {'/', '\\'} and ord(ch) >= 32
    ).strip(' .')

    if not original_name or '.' not in original_name:
        return None

    stem, extension = os.path.splitext(original_name)
    extension = extension.lower()

    # 파일명이 "." 등으로만 구성된 경우를 방지한다.
    stem = stem.strip(' .') or 'cover'
    filename = f'{stem}{extension}'
    save_path = os.path.join(current_app.config['UPLOAD_FOLDER'], filename)

    # 같은 파일명이 이미 있으면 _2, _3 ... 을 붙여 기존 파일을 덮어쓰지 않는다.
    counter = 2
    while os.path.exists(save_path):
        filename = f'{stem}_{counter}{extension}'
        save_path = os.path.join(current_app.config['UPLOAD_FOLDER'], filename)
        counter += 1

    file.save(save_path)
    return url_for('static', filename=f'uploads/{filename}')

def delete_local_cover(cover_url):
    if not cover_url:
        return
    prefix = '/static/uploads/'
    if not cover_url.startswith(prefix):
        return
    filename = cover_url[len(prefix):]
    upload_folder_abs = os.path.abspath(current_app.config['UPLOAD_FOLDER'])
    file_path = os.path.abspath(os.path.join(upload_folder_abs, filename))
    if not file_path.startswith(upload_folder_abs + os.sep):
        return
    try:
        if os.path.isfile(file_path):
            os.remove(file_path)

        stem = os.path.splitext(os.path.basename(filename))[0].strip(' .') or 'cover'
        if os.path.isdir(THUMBNAIL_FOLDER):
            prefix_name = f'{stem}__thumb_'
            for thumb_name in os.listdir(THUMBNAIL_FOLDER):
                if thumb_name.startswith(prefix_name):
                    thumb_path = os.path.join(THUMBNAIL_FOLDER, thumb_name)
                    if os.path.isfile(thumb_path):
                        os.remove(thumb_path)

    except Exception as e:
        print('LOCAL COVER DELETE ERROR:', e)

def local_cover_thumbnail_path(filename, size=400):
    safe_name = os.path.basename(str(filename or '').replace('\\', '/'))
    stem = os.path.splitext(safe_name)[0].strip(' .') or 'cover'
    return os.path.join(
        THUMBNAIL_FOLDER,
        f'{stem}__thumb_{int(size)}.webp'
    )

def cover_thumbnail_url(cover_url, size=400):
    """
    목록 화면용 경량 커버 URL을 반환한다.
    Apple 이미지는 CDN 크기만 줄이고, 직접 업로드한 이미지는
    최초 1회만 WEBP 썸네일을 생성해 재사용한다.
    """
    cover_url = str(cover_url or '').strip()
    if not cover_url:
        return ''

    size = max(120, min(int(size or 400), 800))

    if cover_url.startswith('http://') or cover_url.startswith('https://'):
        return apple_artwork_size(cover_url, size)

    prefix = '/static/uploads/'
    if not cover_url.startswith(prefix):
        return cover_url

    filename = os.path.basename(cover_url[len(prefix):].replace('\\', '/'))
    if not filename:
        return cover_url

    source_path = os.path.abspath(
        os.path.join(current_app.config['UPLOAD_FOLDER'], filename)
    )
    upload_root = os.path.abspath(current_app.config['UPLOAD_FOLDER'])

    if not source_path.startswith(upload_root + os.sep):
        return cover_url
    if not os.path.isfile(source_path):
        return cover_url

    thumb_path = local_cover_thumbnail_path(filename, size)

    try:
        if not os.path.isfile(thumb_path):
            os.makedirs(THUMBNAIL_FOLDER, exist_ok=True)

            with Image.open(source_path) as image:
                try:
                    image.seek(0)
                except Exception:
                    pass

                image.thumbnail(
                    (size, size),
                    Image.Resampling.LANCZOS
                )

                if image.mode not in {'RGB', 'RGBA'}:
                    image = image.convert('RGBA' if 'transparency' in image.info else 'RGB')

                image.save(
                    thumb_path,
                    'WEBP',
                    quality=82,
                    method=4
                )

        thumb_name = os.path.basename(thumb_path)
        return url_for(
            'static',
            filename=f'uploads/.thumbs/{thumb_name}'
        )

    except Exception as e:
        print('COVER THUMBNAIL ERROR:', e)
        return cover_url


app.jinja_env.globals['cover_thumbnail_url'] = cover_thumbnail_url


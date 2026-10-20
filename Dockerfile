FROM python:3.13-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV ALBUM_DB_PATH=/data/albums.db
ENV TZ=Asia/Seoul
ENV ALBUM_SKIP_BACKGROUND_THUMBNAIL_PREWARM=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/static /data

EXPOSE 5001

CMD ["sh", "-c", "mkdir -p /data/uploads && rm -rf /app/static/uploads && ln -s /data/uploads /app/static/uploads && python -c 'from album_database import init_db; init_db()' && python -c 'from media_service import prewarm_thumbnail_directory; prewarm_thumbnail_directory(\"/data/uploads\")' && exec gunicorn --bind 0.0.0.0:${PORT:-5001} --workers 1 --threads 4 --keep-alive 10 --timeout 120 app:app"]

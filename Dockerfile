FROM python:3.13-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/static /data

EXPOSE 5001

CMD ["sh", "-c", "mkdir -p /data/uploads && rm -rf /app/static/uploads && ln -s /data/uploads /app/static/uploads && python -c 'from app import init_db; init_db()' && exec gunicorn --bind 0.0.0.0:${PORT:-5001} --workers 1 --threads 4 --timeout 120 app:app"]

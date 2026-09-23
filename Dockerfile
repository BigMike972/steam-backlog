FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STEAM_BACKLOG_DB=/data/steam_backlog.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run as an unprivileged user; /data holds the SQLite database (API key,
# hidden games, filters and cached lookups), so mount a volume there.
RUN useradd --create-home --uid 1000 app \
    && mkdir -p /data \
    && chown app:app /data
USER app
VOLUME /data

EXPOSE 5002
CMD ["gunicorn", "--workers", "2", "--threads", "2", "--bind", "0.0.0.0:5002", "app:app"]

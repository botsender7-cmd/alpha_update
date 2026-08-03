FROM python:3.12-slim

# ffmpeg/ffprobe are not pip-installable — this is the actual fix for the
# "Install ffmpeg or convert to mp3" error path in a.py
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Non-root user — don't run the process as root in prod
RUN useradd -m appuser
USER appuser

ENV PORT=5000 \
    MAX_UPLOAD_MB=100 \
    CLEANUP_TTL_SECONDS=1800 \
    CLEANUP_INTERVAL_SECONDS=300 \
    FLASK_DEBUG=false

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request as u, os; u.urlopen('http://127.0.0.1:' + os.environ['PORT'] + '/health')" || exit 1

CMD gunicorn a:app --workers 2 --threads 4 --timeout 120 --bind 0.0.0.0:${PORT}

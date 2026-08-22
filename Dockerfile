FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PHOTO_DATA=/data \
    TMPDIR=/data/tmp/uploads \
    XDG_CACHE_HOME=/data/tmp/runtime

WORKDIR /app
COPY requirements.txt .
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg poppler-utils \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt

RUN groupadd --gid 10001 davidpi \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin davidpi

COPY --chown=10001:10001 app.py ./
COPY --chown=10001:10001 modules ./modules
COPY --chown=10001:10001 templates ./templates
COPY --chown=10001:10001 static ./static
COPY --chown=10001:10001 assets ./assets
COPY --chown=10001:10001 knowledge ./knowledge
COPY --chown=10001:10001 docker-entrypoint.sh /usr/local/bin/docker-entrypoint
RUN chmod 0755 /usr/local/bin/docker-entrypoint

EXPOSE 8000
USER 10001:10001
ENTRYPOINT ["/usr/local/bin/docker-entrypoint"]
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "2", "--timeout", "600", "--worker-tmp-dir", "/dev/shm", "app:app"]

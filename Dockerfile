ARG PYTHON_BASE_IMAGE=python:3.13-alpine3.22
ARG PIP_VERSION=26.2.1
FROM ${PYTHON_BASE_IMAGE} AS runtime-base
ARG PIP_VERSION

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PHOTO_DATA=/data \
    TMPDIR=/data/tmp/uploads \
    XDG_CACHE_HOME=/data/tmp/runtime

WORKDIR /app
COPY requirements.txt .
RUN apk upgrade --no-cache \
    && apk add --no-cache ffmpeg poppler-utils \
    && python -m pip install --no-cache-dir --upgrade "pip==${PIP_VERSION}" \
    && python -m pip install --no-cache-dir -r requirements.txt \
    && python -m pip uninstall --yes setuptools \
    && python -m pip uninstall --yes pip

RUN addgroup -S -g 10001 davidpi \
    && adduser -S -D -H -u 10001 -G davidpi -h /nonexistent -s /sbin/nologin davidpi \
    && adduser -S -D -H -u 10002 -G davidpi -h /nonexistent -s /sbin/nologin davidpi-maintenance

FROM runtime-base AS app-source

COPY --chown=10001:10001 app.py ./
COPY --chown=10001:10001 modules ./modules
COPY --chown=10001:10001 config ./config
COPY --chown=10001:10001 installer ./installer
# Optional for local development; copy only the two allowed public Android files.
# Stable publication separately requires and verifies both artifacts.
RUN --mount=type=bind,source=artifacts,target=/release-artifacts,readonly \
    mkdir -p /app/artifacts/android \
    && for name in david-pi-backup.apk david-pi-backup.manifest.json; do \
         if [ -f "/release-artifacts/android/$name" ]; then \
           cp "/release-artifacts/android/$name" "/app/artifacts/android/$name"; \
           chown 10001:10001 "/app/artifacts/android/$name"; \
         fi; \
       done
COPY --chown=10001:10001 templates ./templates
COPY --chown=10001:10001 static ./static
COPY --chown=10001:10001 assets ./assets
COPY --chown=10001:10001 knowledge ./knowledge
COPY --chown=10001:10001 docker-entrypoint.sh /usr/local/bin/docker-entrypoint
# Public application source may arrive with private host-side modes. The separate
# maintenance UID must be able to import shared code, while runtime data retains
# its independently managed, restrictive permissions.
RUN find /app -type d -exec chmod 0755 {} + \
    && find /app -type f -exec chmod 0644 {} + \
    && chmod 0755 /usr/local/bin/docker-entrypoint \
    && python -m compileall -q /app

EXPOSE 8000
USER 10001:10001
ENTRYPOINT ["/usr/local/bin/docker-entrypoint"]
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "2", "--timeout", "600", "--worker-tmp-dir", "/dev/shm", "app:app"]

FROM app-source AS runtime-test

USER root
RUN python -m ensurepip --upgrade \
    && python -m pip install --no-cache-dir pytest
COPY --chown=10001:10001 tests ./tests
USER 10001:10001

FROM app-source AS runtime

ARG DAVID_PI_VERSION=development
ARG DAVID_PI_VCS_REF=unknown
ARG PYTHON_BASE_IMAGE=python:3.13-alpine3.22
ARG PYTHON_BASE_DIGEST=unverified
LABEL org.opencontainers.image.title="David-Pi" \
      org.opencontainers.image.version="${DAVID_PI_VERSION}" \
      org.opencontainers.image.revision="${DAVID_PI_VCS_REF}" \
      org.opencontainers.image.source="https://github.com/divad815-gif/david-pi" \
      org.opencontainers.image.base.name="${PYTHON_BASE_IMAGE}" \
      org.opencontainers.image.base.digest="${PYTHON_BASE_DIGEST}"

#!/bin/sh
set -eu

sentinel="${DAVID_PI_DATA_SENTINEL:-/data/.david-pi-storage}"
expected="${DAVID_PI_DATA_ID:-david-pi-family-storage-v1}"

mount_is_read_only() {
  mountpoint="$1"
  awk -v mountpoint="$mountpoint" '
    $5 == mountpoint {
      count = split($6, options, ",")
      for (position = 1; position <= count; position++) {
        if (options[position] == "ro") {
          found = 1
        }
      }
    }
    END { exit(found ? 0 : 1) }
  ' /proc/self/mountinfo
}

if [ "${DAVID_PI_WORKER_MODE:-}" = "slideshow" ]; then
  slideshow_runtime="${DAVID_PI_SLIDESHOW_RUNTIME:-/run/david-pi-slideshow}"
  if [ "${DAVID_PI_SLIDESHOW_EXECUTOR_MODE:-}" != "worker" ] || \
     [ ! -d /data ] || [ -L /data ] || [ ! -w /data ] || \
     [ ! -f "$sentinel" ] || [ -L "$sentinel" ] || \
     [ ! -f /data/photos.db ] || [ -L /data/photos.db ] || [ ! -w /data/photos.db ] || \
     [ ! -d "$slideshow_runtime" ] || [ -L "$slideshow_runtime" ] || \
     [ ! -w "$slideshow_runtime" ]; then
    echo "David-Pi slideshow guard: required worker state is unavailable." >&2
    exit 78
  fi
  for path in /data/originals /data/previews /data/viewer-previews /data/thumbs \
              /data/incoming /data/quarantine /data/platform; do
    if [ ! -d "$path" ] || [ -L "$path" ] || [ ! -r "$path" ] || [ ! -w "$path" ]; then
      echo "David-Pi slideshow guard: managed media path is unavailable: $path" >&2
      exit 78
    fi
  done
  actual="$(tr -d '\r\n' < "$sentinel")"
  if [ "$actual" != "$expected" ]; then
    echo "David-Pi slideshow guard: storage identity does not match." >&2
    exit 78
  fi
  exec "$@"
fi

if [ "${DAVID_PI_WORKER_MODE:-}" = "audiobook" ]; then
  audiobook_state="${DAVID_PI_AUDIOBOOK_DERIVATIVE_STATE:-/audiobook-state}"
  if [ ! -f "$sentinel" ] || [ -L "$sentinel" ] || \
     [ ! -d /data/audiobooks ] || [ -w /data/audiobooks ] || \
     [ ! -d /data/audiobooks/originals ] || [ ! -r /data/audiobooks/originals ] || \
     ! mount_is_read_only /data/audiobooks/originals; then
    echo "David-Pi audiobook guard: required external storage is unavailable." >&2
    exit 78
  fi
  for path in "$audiobook_state" /data/audiobooks/streaming /data/audiobooks/incoming/streaming; do
    if [ ! -d "$path" ] || [ -L "$path" ] || [ ! -w "$path" ]; then
      echo "David-Pi audiobook guard: derivative path is unavailable: $path" >&2
      exit 78
    fi
  done
  actual="$(tr -d '\r\n' < "$sentinel")"
  if [ "$actual" != "$expected" ]; then
    echo "David-Pi audiobook guard: storage identity does not match." >&2
    exit 78
  fi
  exec "$@"
fi

if [ "${DAVID_PI_WORKER_MODE:-}" = "mytube" ]; then
  if [ ! -f "$sentinel" ] || [ -L "$sentinel" ] || \
     [ ! -d /data/mytube ] || [ -L /data/mytube ] || [ ! -w /data/mytube ] || \
     [ ! -d /data/platform ] || [ -L /data/platform ] || [ ! -w /data/platform ] || \
     [ ! -d /data/originals ] || [ -L /data/originals ] || [ ! -r /data/originals ] || \
     [ ! -f /data/photos.db ] || [ -L /data/photos.db ] || [ ! -r /data/photos.db ] || \
     ! mount_is_read_only /data/originals || ! mount_is_read_only /data/photos.db; then
    echo "David-Pi MyTube guard: required isolated storage is unavailable." >&2
    exit 78
  fi
  actual="$(tr -d '\r\n' < "$sentinel")"
  if [ "$actual" != "$expected" ]; then
    echo "David-Pi MyTube guard: storage identity does not match." >&2
    exit 78
  fi
  exec "$@"
fi

if [ "${DAVID_PI_WORKER_MODE:-}" = "device-backup" ]; then
  device_backup_runtime="${DAVID_PI_DEVICE_BACKUP_RUNTIME:-/run/david-pi-device-backup}"
  if [ ! -d /data ] || [ -L /data ] || \
     [ ! -f "$sentinel" ] || [ -L "$sentinel" ] || \
     [ ! -f /data/photos.db ] || [ -L /data/photos.db ] || \
     [ ! -r /data/photos.db ] || [ ! -w /data/photos.db ] || \
     [ ! -d /data/originals ] || [ -L /data/originals ] || \
     [ ! -r /data/originals ] || \
     [ ! -d /data/incoming/device-backup ] || \
     [ -L /data/incoming/device-backup ] || \
     [ ! -r /data/incoming/device-backup ] || \
     [ ! -w /data/incoming/device-backup ] || \
     [ ! -d "$device_backup_runtime" ] || \
     [ -L "$device_backup_runtime" ] || \
     [ ! -w "$device_backup_runtime" ]; then
    echo "David-Pi device backup guard: required worker state is unavailable." >&2
    exit 78
  fi
  actual="$(tr -d '\r\n' < "$sentinel")"
  if [ "$actual" != "$expected" ]; then
    echo "David-Pi device backup guard: storage identity does not match." >&2
    exit 78
  fi
  exec "$@"
fi

if [ "${DAVID_PI_WORKER_MODE:-}" = "maintenance" ]; then
  maintenance_state="${DAVID_PI_MAINTENANCE_STATE:-/maintenance-state}"
  maintenance_anchor="${DAVID_PI_MAINTENANCE_ANCHOR:-/data/.david-pi-operations/maintenance}"
  maintenance_runtime="${DAVID_PI_MAINTENANCE_RUNTIME:-/run/david-pi-maintenance}"
  if [ ! -d /data ] || [ ! -f "$sentinel" ] || [ -L "$sentinel" ] || \
     [ ! -d "$maintenance_state" ] || [ -L "$maintenance_state" ] || \
     [ ! -w "$maintenance_state" ] || [ ! -d "$maintenance_anchor" ] || \
     [ -L "$maintenance_anchor" ] || [ ! -d "$maintenance_runtime" ] || \
     [ ! -w "$maintenance_runtime" ]; then
    echo "David-Pi maintenance guard: required external storage is unavailable." >&2
    exit 78
  fi
  actual="$(tr -d '\r\n' < "$sentinel")"
  if [ "$actual" != "$expected" ]; then
    echo "David-Pi maintenance guard: storage identity does not match." >&2
    exit 78
  fi
  exec "$@"
fi

if [ ! -d /data ] || [ ! -f "$sentinel" ]; then
  echo "David-Pi storage guard: required external storage is unavailable." >&2
  exit 78
fi
actual="$(tr -d '\r\n' < "$sentinel")"
if [ "$actual" != "$expected" ]; then
  echo "David-Pi storage guard: storage identity does not match." >&2
  exit 78
fi

for path in /data/incoming /data/tmp/uploads /data/tmp/runtime /data/quarantine; do
  if [ ! -d "$path" ] || [ ! -w "$path" ]; then
    echo "David-Pi storage guard: required writable path is unavailable: $path" >&2
    exit 78
  fi
done

exec "$@"

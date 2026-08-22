#!/bin/sh
set -eu

sentinel="${DAVID_PI_DATA_SENTINEL:-/data/.david-pi-storage}"
expected="${DAVID_PI_DATA_ID:-david-pi-family-storage-v1}"

if [ "${DAVID_PI_WORKER_MODE:-}" = "audiobook" ]; then
  if [ ! -f "$sentinel" ] || [ ! -d /data/audiobooks ] || [ ! -w /data/audiobooks ]; then
    echo "David-Pi audiobook guard: required external storage is unavailable." >&2
    exit 78
  fi
  actual="$(tr -d '\r\n' < "$sentinel")"
  if [ "$actual" != "$expected" ]; then
    echo "David-Pi audiobook guard: storage identity does not match." >&2
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

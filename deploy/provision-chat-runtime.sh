#!/bin/sh
set -eu

SOURCE_ROOT=/srv/compose/photo-portal
SECRET_ROOT="$SOURCE_ROOT/secrets"
BACKUP_ROOT=/srv/backups/photo-portal
IMAGE=david-family-photos:9.16.0-chat
CHAT_KEY="$SECRET_ROOT/chat-master.key"
VAPID_KEY="$SECRET_ROOT/chat-vapid-private.pem"
ENV_FILE="$SOURCE_ROOT/.env"

install -d -o root -g 10001 -m 0750 "$SECRET_ROOT"
install -d -o root -g root -m 0700 "$BACKUP_ROOT"

if [ ! -s "$CHAT_KEY" ]; then
  umask 027
  openssl rand -base64 32 > "$CHAT_KEY"
fi

if [ ! -s "$VAPID_KEY" ]; then
  umask 027
  openssl ecparam -name prime256v1 -genkey -noout -out "$VAPID_KEY"
fi

chown root:10001 "$CHAT_KEY" "$VAPID_KEY"
chmod 0440 "$CHAT_KEY" "$VAPID_KEY"

for secret in "$CHAT_KEY" "$VAPID_KEY"; do
  [ ! -L "$secret" ] && [ -f "$secret" ]
  [ "$(stat -Lc '%u:%g:%a:%h' "$secret")" = '0:10001:440:1' ]
done

VAPID_PUBLIC="$(docker run --rm --entrypoint python \
  -v "$VAPID_KEY:/run/key.pem:ro" "$IMAGE" -c \
  'import base64; from cryptography.hazmat.primitives import serialization; key=serialization.load_pem_private_key(open("/run/key.pem","rb").read(), password=None); raw=key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint); print(base64.urlsafe_b64encode(raw).rstrip(b"=").decode())')"

test -n "$VAPID_PUBLIC"
test -f "$ENV_FILE"
ENV_TMP="$(mktemp "$SOURCE_ROOT/.env.chat.XXXXXX")"
awk -v value="$VAPID_PUBLIC" '
  BEGIN { replaced=0 }
  /^DAVID_PI_VAPID_PUBLIC_KEY=/ { print "DAVID_PI_VAPID_PUBLIC_KEY=" value; replaced=1; next }
  { print }
  END { if (!replaced) print "DAVID_PI_VAPID_PUBLIC_KEY=" value }
' "$ENV_FILE" > "$ENV_TMP"
chown "$(stat -c %u "$ENV_FILE")":"$(stat -c %g "$ENV_FILE")" "$ENV_TMP"
chmod 0600 "$ENV_TMP"
mv -f "$ENV_TMP" "$ENV_FILE"

STAMP="$(date -u +%Y%m%d-%H%M%S)"
RECOVERY="$BACKUP_ROOT/${STAMP}-chat-key-recovery.tgz"
tar -C "$SECRET_ROOT" -czf "$RECOVERY" chat-master.key chat-vapid-private.pem
chown root:root "$RECOVERY"
chmod 0600 "$RECOVERY"

printf 'chat_runtime_ready=yes\n'
printf 'recovery_archive=%s\n' "$RECOVERY"

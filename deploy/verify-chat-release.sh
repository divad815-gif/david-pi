#!/bin/sh
set -eu

HOST="${DAVID_PI_HOST:?Set DAVID_PI_HOST to the installed Tailscale DNS name}"
TAIL_IP="$(tailscale ip -4 | head -n 1)"
RESOLVE="--resolve $HOST:443:$TAIL_IP"

status() {
  label="$1"
  url="$2"
  shift 2
  code="$(curl -k -sS "$@" -o /dev/null -w '%{http_code}' "$url")"
  printf '%s=%s\n' "$label" "$code"
}

status loopback_health http://127.0.0.1:8090/health
status private_home "https://$HOST/" $RESOLVE
status private_chat "https://$HOST/chat" $RESOLVE
status chat_users "https://$HOST/api/chat/users" $RESOLVE
status chat_conversations "https://$HOST/api/chat/conversations" $RESOLVE
status vapid_endpoint "https://$HOST/api/chat/push/public-key" $RESOLVE

if curl -sS --connect-timeout 2 -o /dev/null "http://${DAVID_PI_LAN_HOST:-127.0.0.1}:80/" 2>/dev/null; then
  echo lan_port_80=reachable
  exit 1
else
  echo lan_port_80=closed
fi

ss -ltn | grep -q '127.0.0.1:8090' && echo loopback_mapping=pass
if ss -ltn | grep -Eq '(^|[[:space:]])(0\.0\.0\.0|\*|\[::\]):80([[:space:]]|$)'; then
  echo all_interface_port_80=present
  exit 1
else
  echo all_interface_port_80=absent
fi

tailscale serve status | grep -q '127.0.0.1:8090' && echo tailscale_serve_loopback=pass
tailscale funnel status | grep -q 'tailnet only' && echo tailscale_funnel=disabled

VAPID_JSON="$(curl -k -sS $RESOLVE "https://$HOST/api/chat/push/public-key")"
python -c 'import json,sys; value=json.loads(sys.stdin.read()).get("public_key",""); assert len(value)>50' <<EOF
$VAPID_JSON
EOF
echo web_push_key=configured

docker inspect family-photo-portal --format 'portal_user={{.Config.User}} readonly={{.HostConfig.ReadonlyRootfs}} pids={{.HostConfig.PidsLimit}}'
docker inspect david-pi-chat-notifier --format 'notifier_user={{.Config.User}} readonly={{.HostConfig.ReadonlyRootfs}} pids={{.HostConfig.PidsLimit}}'
docker exec family-photo-portal sh -c 'grep -E "^(Uid|Gid|CapEff|NoNewPrivs|Seccomp):" /proc/1/status'
docker exec family-photo-portal sh -c 'test ! -e /app/.env && test ! -e /app/tests && test ! -e /var/run/docker.sock'
echo image_secret_checks=pass

if docker inspect family-photo-portal --format '{{range .Mounts}}{{.Source}}=>{{.Destination}};{{end}}' | grep -Eq 'docker\.sock|/etc/pihole|/var/lib/pihole'; then
  echo prohibited_mount=present
  exit 1
else
  echo prohibited_mount=absent
fi

stat -c 'chat_db_mode=%a owner=%u:%g' /srv/data/family-photos/platform/chat.db
stat -c 'chat_root_mode=%a owner=%u:%g' /srv/data/family-photos/chat
CHAT_KEY=/srv/compose/photo-portal/secrets/chat-master.key
VAPID_KEY=/srv/compose/photo-portal/secrets/chat-vapid-private.pem
for secret in "$CHAT_KEY" "$VAPID_KEY"; do
  [ ! -L "$secret" ] && [ -f "$secret" ] || {
    echo chat_secret_metadata=invalid >&2
    exit 1
  }
  [ "$(stat -Lc '%u:%g:%a:%h' "$secret")" = '0:10001:440:1' ] || {
    echo chat_secret_metadata=invalid >&2
    exit 1
  }
done
echo chat_secret_metadata=pass

# Validate through each service's real unprivileged identity. These commands
# print no key material and reject unreadable or malformed credentials.
docker exec family-photo-portal python -c '
import base64
from pathlib import Path
raw = Path("/run/secrets/chat-master.key").read_bytes().strip()
if len(raw) != 32:
    raw = base64.b64decode(raw, validate=True)
assert len(raw) == 32
'
docker exec david-pi-chat-notifier python -c '
from pathlib import Path
from cryptography.hazmat.primitives.serialization import load_pem_private_key
load_pem_private_key(Path("/run/secrets/chat-vapid-private.pem").read_bytes(), password=None)
'
echo chat_secret_container_access=pass

[ "$(curl -k -sS $RESOLVE -o /dev/null -w '%{http_code}' "https://$HOST/api/chat/conversations")" = 200 ] || {
  echo chat_conversations=failed >&2
  exit 1
}

if grep -q '^GIPHY_API_KEY=.' /srv/compose/photo-portal/.env; then echo giphy_provider=configured; else echo giphy_provider=manual_configuration_required; fi
if grep -q '^DAVID_PI_FCM_CREDENTIAL_FILE=.' /srv/compose/photo-portal/.env; then echo android_push_provider=configured; else echo android_push_provider=manual_configuration_required; fi

echo verification=pass

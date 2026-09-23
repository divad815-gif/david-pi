from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_chat_provisioner_enforces_the_unprivileged_runtime_secret_contract():
    source = (ROOT / "deploy" / "provision-chat-runtime.sh").read_text(
        encoding="utf-8"
    )
    assert 'chown root:10001 "$CHAT_KEY" "$VAPID_KEY"' in source
    assert 'chmod 0440 "$CHAT_KEY" "$VAPID_KEY"' in source
    assert "stat -Lc '%u:%g:%a:%h'" in source
    assert "0:10001:440:1" in source


def test_chat_release_verification_checks_metadata_runtime_access_and_real_api():
    source = (ROOT / "deploy" / "verify-chat-release.sh").read_text(
        encoding="utf-8"
    )
    assert source.count("0:10001:440:1") == 1
    assert "docker exec family-photo-portal python -c" in source
    assert "docker exec david-pi-chat-notifier python -c" in source
    assert 'Path("/run/secrets/chat-master.key").read_bytes()' in source
    assert 'Path("/run/secrets/chat-vapid-private.pem").read_bytes()' in source
    assert "load_pem_private_key" in source
    assert 'https://$HOST/api/chat/conversations' in source
    assert "chat_secret_container_access=pass" in source


def test_portal_lifecycle_checks_secret_metadata_without_repairing_it():
    source = (ROOT / "deploy" / "david-pi-portal-lifecycle").read_text(
        encoding="utf-8"
    )
    start = source.split("start_portal() {", 1)[1].split("\n}", 1)[0]
    assert start.index("validate_chat_secret_metadata") < start.index(
        '"$DOCKER_BIN" compose stop'
    )
    validation = source.split("validate_chat_secret_metadata() {", 1)[1].split(
        "\n}", 1
    )[0]
    assert "0:10001:440:1" not in validation
    assert "CHAT_SECRET_UID:$CHAT_SECRET_GID:440:1" in validation
    assert "chown" not in validation
    assert "chmod" not in validation

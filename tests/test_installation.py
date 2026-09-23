"""Installation identity, permissions and module boundaries use synthetic data."""
import copy
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from flask import Flask, jsonify, render_template_string

from modules.installation import InstallationError, load_installation, save_installation, validate_installation


def configuration(root):
    return {"schema_version": 1, "instance_id": str(uuid.uuid4()), "display_name": "John's home",
            "hostname": "john-pi-1", "public_url": "https://john-pi-1.example-tail.ts.net",
            "timezone": "America/Denver", "country": "US",
            "members": [{"login": "john@example.test", "name": "John", "role": "admin"},
                        {"login": "sam@example.test", "name": "Sam", "role": "household"},
                        {"login": "alex@example.test", "name": "Alex", "role": "household"}],
            "storage": {"mode": "folder", "data_root": str(root / "data"), "backup_root": None},
            "modules": {"notes": "enabled", "movies": "manual", "recipes": "manual"},
            "integrations": {"web_push": False}}


@pytest.fixture
def installed(tmp_path, monkeypatch):
    monkeypatch.setenv("DAVID_PI_PLATFORM_DATA", str(tmp_path / "platform"))
    path = tmp_path / "installation.json"
    monkeypatch.setenv("DAVID_PI_CONFIG_FILE", str(path))
    config = save_installation(configuration(tmp_path), path)
    return config, path


def test_name_changes_do_not_change_identity_address_or_storage(installed):
    cfg, path = installed
    previous = copy.deepcopy(cfg)
    cfg["display_name"] = "Sam's server"
    save_installation(cfg, path)
    saved = load_installation(path)
    for field in ("instance_id", "public_url", "hostname", "storage"):
        assert saved[field] == previous[field]
    cfg["instance_id"] = str(uuid.uuid4())
    with pytest.raises(InstallationError, match="cannot be changed"):
        save_installation(cfg, path)
    assert load_installation(path)["instance_id"] == previous["instance_id"]
    assert path.stat().st_mode & 0o777 == 0o640


@pytest.mark.parametrize("mutation", [
    lambda c: c.update(schema_version=True),
    lambda c: c.update(api_key="secret"),
    lambda c: c.update(public_url="https://evil.example"),
    lambda c: c.update(public_url="https://user@john-pi.example-tail.ts.net"),
    lambda c: c.update(public_url="https://john-pi.example-tail.ts.net/path"),
    lambda c: c.update(timezone="Not/AZone"),
    lambda c: c.update(members=[]),
    lambda c: c["members"][0].update(role="household"),
    lambda c: c["members"].append(c["members"][0]),
    lambda c: c["modules"].update(device_backup="enabled", media="disabled"),
    lambda c: c["integrations"].update(web_push=True),
    lambda c: c["storage"].update(backup_root=c["storage"]["data_root"] + "/backup"),
])
def test_invalid_configuration_fails_without_overwriting(installed, mutation):
    cfg, path = installed
    before = path.read_bytes()
    mutation(cfg)
    with pytest.raises(InstallationError):
        save_installation(cfg, path)
    assert path.read_bytes() == before


def test_symlink_and_fifo_configuration_are_rejected(installed, tmp_path):
    _, path = installed
    link = tmp_path / "link.json"
    link.symlink_to(path)
    fifo = tmp_path / "fifo.json"
    os.mkfifo(fifo)
    for unsafe in (link, fifo):
        with pytest.raises(InstallationError):
            load_installation(unsafe)


def portal():
    from modules.access_control import init_access_control
    from modules.security import init_security
    from modules.portal_configuration import init_portal_configuration
    app = Flask(__name__)
    # Production semantics: no test identity shortcuts, and explicit legacy off
    # cannot turn off admission after installation has been claimed.
    app.config["TESTING"] = False
    init_security(app)
    init_access_control(app, mode="off")
    init_portal_configuration(app)
    @app.get("/")
    def home():
        return render_template_string("<title>{{ server_name }}</title><script type='application/json'>{{ public_installation|tojson }}</script>")
    @app.get("/api/media-test")
    def other():
        return jsonify(ok=True)
    @app.get("/api/photos")
    def photos():
        return jsonify(ok=True)
    return app


def test_tailnet_membership_does_not_grant_admission_or_admin(installed):
    client = portal().test_client()
    for login, expected in [("", 403), ("unknown@example.test", 403), ("sam@example.test", 200), ("alex@example.test", 200), ("john@example.test", 200)]:
        response = client.get("/api/installation", headers={"Tailscale-User-Login": login})
        assert response.status_code == expected
        if expected == 200:
            data = response.get_json()
            assert data["member_id"] == login
            assert data["installation_id"] == installed[0]["instance_id"]
            assert data["is_admin"] == (login == "john@example.test")
            assert "members" not in data and "storage" not in data
    assert client.get("/settings", headers={"Tailscale-User-Login": "sam@example.test"}).status_code == 403


def test_helper_requires_admin_and_csrf_before_dispatch(installed, monkeypatch):
    from installer import client as host_client
    calls = []
    monkeypatch.setattr(host_client, "request", lambda *a, **k: calls.append((a, k)) or {"ok": True})
    client = portal().test_client()
    token = "synthetic-csrf-token-more-than-32-characters"
    client.set_cookie("david_pi_csrf", token)
    for login, csrf, code in [("john@example.test", "", 403), ("sam@example.test", token, 403), ("john@example.test", token, 200)]:
        response = client.post("/api/admin/operations/status", json={}, headers={"Tailscale-User-Login": login, "X-CSRF-Token": csrf})
        assert response.status_code == code
    assert len(calls) == 1 and calls[0][1]["identity"] == "john@example.test"


def test_modules_manual_mode_and_deferred_features_are_closed(installed):
    client = portal().test_client()
    headers = {"Tailscale-User-Login": "john@example.test"}
    for route in ("/photos", "/api/photos", "/media/thumb/anything", "/ios-backup", "/api/chat/gifs/search"):
        assert client.get(route, headers=headers).status_code == 404
    for route in ("/api/movies/search", "/api/recipes/discover"):
        assert client.get(route, headers=headers).status_code == 409
    assert client.get("/api/media-test", headers=headers).status_code == 200


def test_custom_name_is_escaped_in_html_and_json(installed):
    cfg, path = installed
    cfg["display_name"] = "John </script><img src=x onerror=alert(1)>"
    save_installation(cfg, path)
    page = portal().test_client().get("/", headers={"Tailscale-User-Login": "john@example.test"}).text
    assert "<img src=x" not in page
    assert "&lt;img" in page
    assert "\\u003c/script\\u003e" in page


def test_disabled_domains_are_not_imported_or_migrated(installed, tmp_path):
    cfg, path = installed
    cfg["modules"] = {"notes": "enabled"}
    save_installation(cfg, path)
    data = tmp_path / "runtime"
    environment = os.environ.copy()
    environment.update(PHOTO_DATA=str(data), DAVID_PI_PLATFORM_DATA=str(data / "platform"),
                       DAVID_PI_FILES_DATA=str(data / "files"), DAVID_PI_CHAT_DATA=str(data / "chat"),
                       DAVID_PI_AUDIOBOOKS_DATA=str(data / "audiobooks"), DAVID_PI_MYTUBE_DATA=str(data / "mytube"),
                       DAVID_PI_DISABLE_METRICS="1", DAVID_PI_DISABLE_DEVICE_HOUSEKEEPING="1")
    script = '''
import app, sys, json
client=app.app.test_client()
headers={"Tailscale-User-Login":"john@example.test"}
assert client.get("/",headers=headers).status_code==200
assert client.get("/notes",headers=headers).status_code==200
assert client.get("/movies",headers=headers).status_code==404
assert client.get("/connect",headers=headers).status_code==200
assert client.get("/manifest.webmanifest").json["name"]=="John's home"
for module in ("chat","movies","recipes","audiobooks","mytube","assistant"):
    assert "modules."+module not in sys.modules, module
for path in (app.ORIGINALS, app.PREVIEWS, app.VIEWER_PREVIEWS, app.THUMBS):
    assert not path.exists(), path
# The historical photos.db also stores core companion credentials. Its Media
# domain tables must remain absent while companion pairing stays available.
with app.db() as connection:
    assert connection.execute("SELECT 1 FROM sqlite_master WHERE name='photos'").fetchone() is None
print("isolated module startup verified")
'''
    result = subprocess.run([sys.executable, "-c", script], env=environment, cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr[-3000:]


def test_member_status_history_uses_admitted_identity_without_admin_privileges(installed, tmp_path):
    cfg, path = installed
    cfg["modules"] = {"notes": "enabled"}
    save_installation(cfg, path)
    data = tmp_path / "runtime"
    environment = os.environ.copy()
    environment.update(PHOTO_DATA=str(data), DAVID_PI_PLATFORM_DATA=str(data / "platform"),
                       DAVID_PI_DISABLE_METRICS="1", DAVID_PI_DISABLE_DEVICE_HOUSEKEEPING="1")
    script = '''
import app
from installer import client as helper
assert not app.app.testing
calls=[]
def request(operation,payload,identity):
    calls.append((operation,payload,identity))
    return {"metric":"cpu","range":"7d","points":[{"timestamp":1770000000,"value":12.5}],"sampled":False}
helper.request=request
client=app.app.test_client()
headers={"Tailscale-User-Login":"sam@example.test","X-Owner-Id":"john@example.test"}
response=client.get("/api/status/history?metric=cpu&range=7d",headers=headers)
assert response.status_code==200, response.json
assert response.json["points"]==[{"timestamp":1770000000,"value":12.5}]
assert calls==[("metrics_history",{"metric":"cpu","range":"7d"},"sam@example.test")]
assert client.get("/api/status/history?metric=credentials",headers=headers).status_code==400
assert client.get("/api/status/history",headers={"Tailscale-User-Login":"outsider@example.test"}).status_code==403
token="synthetic-csrf-token-more-than-32-characters"
client.set_cookie("david_pi_csrf",token)
assert client.post("/api/admin/operations/status",json={},headers={**headers,"X-CSRF-Token":token}).status_code==403
assert len(calls)==1
print("household metrics authorization verified")
'''
    result = subprocess.run([sys.executable, "-c", script], env=environment, cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr[-3000:]

"""Terminal claim renewal preserves identity and invalidates browser authority."""
import copy
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
import uuid
from http.server import ThreadingHTTPServer

import pytest

from installer import host

ROOT = Path(__file__).resolve().parents[2]
ORIGIN = "https://john-pi-2.example.ts.net"
IMAGE = "ghcr.io/example/david-pi@sha256:" + "1" * 64


@pytest.fixture
def pending(tmp_path):
    status = {"BackendState": "Running", "Self": {"DNSName": "john-pi-2.example.ts.net.", "UserID": 42}, "User": {"42": {"LoginName": "john@example.test"}}}
    serve = {"TCP": {"443": {"HTTPS": True}}, "Web": {"john-pi-2.example.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8091"}, "/other": {"Proxy": "http://127.0.0.1:3000"}}}}}
    calls = []
    def run(args):
        calls.append(args)
        if args == ["tailscale", "status", "--json"]:
            return json.dumps(status)
        if args == ["tailscale", "serve", "status", "--json"]:
            return json.dumps(serve)
        return ""
    controller = host.Controller(tmp_path / "etc", runner=run)
    setup = {"admin": "john@example.test", "hostname": "john-pi-2", "origin": ORIGIN, "instance_id": str(uuid.uuid4()), "timezone": "America/Denver", "expires": time.time()+900, "token_hash": hashlib.sha256(b"old-token").hexdigest(), "claimed": False}
    host.atomic_json(controller.state / "setup.json", setup)
    host.atomic_json(controller.etc / "release.json", {"version": "10.0.0", "image": IMAGE, "repository": "example/david-pi"})
    return controller, status, serve, calls


def new_token(capsys):
    output = capsys.readouterr().out
    assert ORIGIN + "/" in output
    assert "previous code and browser session no longer work" in output
    assert "sudo david-pi setup" in output
    return re.search(r"One-use claim token \(valid 15 minutes\):\n   (\S+)", output).group(1)


def test_renewal_rotates_real_http_claim_and_session_without_rewriting_choices(pending, capsys):
    controller, _, serve, calls = pending
    setup_path = controller.state / "setup.json"
    original = host.read_json(setup_path)
    release = (controller.etc / "release.json").read_bytes()
    serve_before = copy.deepcopy(serve)
    server = ThreadingHTTPServer(("127.0.0.1", 0), host.SetupHandler)
    server.controller = controller
    server.claim_lock = threading.Lock()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    def request(path, body=None, cookie=None):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
        headers = {"Host": ORIGIN.removeprefix("https://"), "Origin": ORIGIN, "Tailscale-User-Login": original["admin"], "Content-Type": "application/json"}
        if cookie:
            headers["Cookie"] = cookie
        connection.request("POST" if body is not None else "GET", path, body=json.dumps(body) if body is not None else None, headers=headers)
        response = connection.getresponse()
        result = response.status, response.getheader("Set-Cookie"), json.loads(response.read())
        connection.close()
        return result
    try:
        host.renew_claim(controller)
        token = new_token(capsys)
        assert request("/api/claim", {"token": "old-token"})[0] == 400
        code, cookie, _ = request("/api/claim", {"token": token})
        assert code == 200
        cookie = cookie.split(";")[0]
        # A claimed session may also be renewed before installation starts.
        host.renew_claim(controller)
        replacement = new_token(capsys)
        assert replacement != token
        assert request("/api/job?id=" + "a"*32, cookie=cookie)[0] == 403
        assert request("/api/claim", {"token": token})[0] == 400
        code, fresh_cookie, _ = request("/api/claim", {"token": replacement})
        assert code == 200 and fresh_cookie != cookie
        updated = host.read_json(setup_path)
        for field in ("admin", "hostname", "origin", "instance_id", "timezone"):
            assert updated[field] == original[field]
        assert updated["session_expires"] > time.time()
        assert (controller.etc / "release.json").read_bytes() == release
        assert serve == serve_before
        assert not controller.config_path.exists()
        assert not any(args[:2] == ["tailscale", "set"] or args[0] in {"apt-get", "docker"} or "restart" in args for args in calls)
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("problem,expected", [
    ("address", "address changed"), ("account", "account does not match"),
    ("offline", "Connect Tailscale"), ("funnel", "Funnel"),
    ("serve_conflict", "another service"), ("bad_state", "cannot be read safely"),
    ("bad_release", "No verified release"), ("installed", "already begun"),
    ("job", "recorded as running"), ("helper_failure", "helper unavailable"),
])
def test_refused_renewal_preserves_previous_claim(pending, problem, expected):
    controller, status, serve, calls = pending
    setup_path = controller.state / "setup.json"
    if problem == "address": status["Self"]["DNSName"] = "other.example.ts.net."
    elif problem == "account": status["User"]["42"]["LoginName"] = "someone@example.test"
    elif problem == "offline": status["BackendState"] = "Stopped"
    elif problem == "funnel": serve["AllowFunnel"] = {"john-pi-2.example.ts.net:443": True}
    elif problem == "serve_conflict": serve["Web"]["john-pi-2.example.ts.net:443"]["Handlers"]["/"] = {"Proxy": "http://127.0.0.1:3000"}
    elif problem == "bad_state": setup_path.write_text("{}")
    elif problem == "bad_release": (controller.etc / "release.json").write_text("{}")
    elif problem == "installed": controller.config_path.write_text("{}")
    elif problem == "job": host.atomic_json(controller.jobs / "active.json", {"state": "running"})
    elif problem == "helper_failure":
        original_runner = controller.runner
        def runner(args):
            if args[0] == "systemctl": raise host.HostError("helper unavailable")
            return original_runner(args)
        controller.runner = runner
    original = setup_path.read_bytes()
    with pytest.raises(host.HostError, match=expected):
        host.renew_claim(controller)
    assert setup_path.read_bytes() == original
    assert not any("--set-path=/" in args for args in calls)


def test_active_operation_or_browser_write_cannot_be_disrupted(pending):
    controller, _, _, calls = pending
    original = (controller.state / "setup.json").read_bytes()
    with controller.external_operation():
        with pytest.raises(host.HostError, match="already running"):
            host.renew_claim(controller)
    with controller.setup_claim_lock():
        with pytest.raises(host.HostError, match="being renewed or submitted"):
            host.renew_claim(controller)
    assert (controller.state / "setup.json").read_bytes() == original
    assert not calls


def test_expired_claim_keeps_saved_node_owner_distinct_from_setup_admin(pending, capsys):
    controller, status, _, _ = pending
    setup = host.read_json(controller.state / "setup.json")
    setup.update(expires=1, node_account="owner@example.test")
    status["User"]["42"]["LoginName"] = "owner@example.test"
    host.atomic_json(controller.state / "setup.json", setup)
    host.renew_claim(controller)
    new_token(capsys)
    updated = host.read_json(controller.state / "setup.json")
    assert updated["admin"] == "john@example.test"
    assert updated["node_account"] == "owner@example.test"
    assert 890 < updated["expires"] - time.time() <= 900


@pytest.mark.parametrize("configured,installed,expected", [(False, False, "renew-claim"), (True, False, "operation repair"), (True, True, "Already configured")])
def test_shell_resume_skips_questions_preflight_and_package_work(tmp_path, configured, installed, expected):
    etc = tmp_path / "etc"
    (etc / "host-state").mkdir(parents=True)
    (etc / "host-state/setup.json").write_text("{}")
    if configured: (etc / "installation.json").write_text("{}")
    if installed: (etc / "host-state/installed.json").write_text("{}")
    script = '''
source "$DAVID_PI_CLI_ROOT/installer/lib/setup.sh"
DP_LOG="$DAVID_PI_ETC/test.log"
dp_require_root() { :; }
dp_preflight() { echo unexpected-preflight; return 91; }
dp_install_packages() { echo unexpected-packages; return 92; }
read() { echo unexpected-question; return 93; }
python3() { printf '%s\\n' "$*"; }
dp_setup
'''
    result = subprocess.run(["bash", "-c", script], env={**os.environ, "DAVID_PI_CLI_ROOT": str(ROOT), "DAVID_PI_ETC": str(etc)}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert expected in result.stdout
    assert "unexpected-" not in result.stdout
    if not configured:
        assert f"--etc {etc} renew-claim" in result.stdout

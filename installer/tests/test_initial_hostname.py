"""Fresh setup waits for Tailscale's assigned DNS before binding ownership."""
import copy
import json
import uuid

import pytest

from installer import host

IMAGE = "ghcr.io/example/david-pi@sha256:" + "1" * 64
DOMAIN = "example.ts.net"


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    status = {"BackendState": "Running", "Self": {"ID": "node-1", "UserID": 42, "DNSName": "install-test." + DOMAIN + "."}, "User": {"42": {"LoginName": "owner@example.test"}}, "CurrentTailnet": {"Name": "home-network"}}
    serve = {"TCP": {"8443": {"TCPForward": "127.0.0.1:3000"}}}
    calls = []
    def runner(args):
        calls.append(args)
        if args == ["tailscale", "status", "--json"]:
            return json.dumps(status)
        if args == ["tailscale", "serve", "status", "--json"]:
            return json.dumps(serve)
        if "--set-path=/" in args:
            dns = status["Self"]["DNSName"].rstrip(".")
            serve.setdefault("TCP", {})["443"] = {"HTTPS": True}
            serve.setdefault("Web", {}).setdefault(dns + ":443", {}).setdefault("Handlers", {})["/"] = {"Proxy": args[-1]}
        return ""
    controller = host.Controller(tmp_path / "etc", runner=runner)
    monkeypatch.setattr(host.sys, "stdin", type("Interactive", (), {"isatty": lambda self: True})())
    monkeypatch.setattr(host.time, "sleep", lambda seconds: None)
    return controller, status, serve, calls


@pytest.mark.parametrize("when", ["during_https", "after_https", "during_serve_verification"])
def test_initial_claim_uses_eventually_assigned_collision_name(fresh, monkeypatch, capsys, when):
    controller, status, serve, calls = fresh
    assigned = "john-pi-1." + DOMAIN
    base_runner = controller.runner
    after_prompt = False
    stale_reads = 0
    changes = []
    def confirm(prompt):
        nonlocal after_prompt
        assert "HTTPS Certificates" in prompt
        assert not (controller.state / "setup.json").exists()
        after_prompt = True
        if when == "during_https":
            status["Self"]["DNSName"] = assigned + "."
        elif when == "during_serve_verification":
            status["Self"]["DNSName"] = "john-pi." + DOMAIN + "."
        return ""
    def runner(args):
        nonlocal stale_reads
        if after_prompt and when == "after_https" and args == ["tailscale", "status", "--json"]:
            stale_reads += 1
            if stale_reads > 3:
                status["Self"]["DNSName"] = assigned + "."
        if when == "during_serve_verification" and "--set-path=/" in args and not changes:
            # The daemon switches to its final name just before Serve applies.
            assert not (controller.state / "setup.json").exists()
            status["Self"]["DNSName"] = assigned + "."
            changes.append(True)
        return base_runner(args)
    monkeypatch.setattr("builtins.input", confirm)
    controller.runner = runner
    host.initialize(controller, "admin@example.test", "john-pi", IMAGE, "example/david-pi")
    setup = host.read_json(controller.state / "setup.json")
    assert setup["hostname"] == "john-pi-1"
    assert setup["origin"] == "https://" + assigned
    assert setup["admin"] == "admin@example.test"
    assert setup["node_account"] == "owner@example.test"
    assert setup["node_id"] == "node-1" and setup["tailnet"] == "home-network"
    assert not setup["claimed"] and setup["expires"] > host.time.time()
    assert len(setup["token_hash"]) == 64
    assert serve["TCP"]["8443"] == {"TCPForward": "127.0.0.1:3000"}
    assert serve["Web"][assigned + ":443"]["Handlers"]["/"]["Proxy"] == "http://127.0.0.1:8091"
    assert len([call for call in calls if call[:2] == ["tailscale", "set"]]) == 1
    output = capsys.readouterr().out
    assert "Checking the assigned private address..." in output
    assert "https://" + assigned + "/" in output
    assert "https://install-test." not in output
    assert "https://john-pi." not in output
    if when == "after_https":
        assert stale_reads > 3
    if when == "during_serve_verification":
        assert len([call for call in calls if "--set-path=/" in call]) == 2


@pytest.mark.parametrize("change", ["node", "owner", "network", "tailnet_name", "offline", "conflict", "funnel", "never_settles"])
def test_initial_rename_never_claims_changed_identity_or_conflicting_serve(fresh, monkeypatch, change):
    controller, status, serve, calls = fresh
    original_serve = copy.deepcopy(serve)
    def confirm(prompt):
        status["Self"]["DNSName"] = "john-pi-1." + DOMAIN + "."
        if change == "node": status["Self"]["ID"] = "another-node"
        elif change == "owner": status["User"]["42"]["LoginName"] = "someone@example.test"
        elif change == "network": status["Self"]["DNSName"] = "john-pi-1.another.ts.net."
        elif change == "tailnet_name": status["CurrentTailnet"]["Name"] = "another-network"
        elif change == "offline": status["BackendState"] = "Stopped"
        elif change == "conflict": serve["Web"] = {"john-pi-1." + DOMAIN + ":443": {"Handlers": {"/": {"Path": "/srv/unrelated"}}}}
        elif change == "funnel": serve["AllowFunnel"] = {"john-pi-1." + DOMAIN + ":443": True}
        elif change == "never_settles": status["Self"]["DNSName"] = "install-test." + DOMAIN + "."
        return ""
    monkeypatch.setattr("builtins.input", confirm)
    with pytest.raises(host.HostError):
        host.initialize(controller, "admin@example.test", "john-pi", IMAGE, "example/david-pi")
    assert not (controller.state / "setup.json").exists()
    assert not any("--set-path=/" in call for call in calls)
    assert serve["TCP"] == original_serve["TCP"]


def test_existing_unclaimed_identity_cannot_use_fresh_rename_discovery(fresh):
    controller, status, _, calls = fresh
    original = {"admin": "admin@example.test", "origin": "https://install-test." + DOMAIN, "hostname": "install-test", "instance_id": str(uuid.uuid4()), "expires": 0, "claimed": False}
    host.atomic_json(controller.state / "setup.json", original)
    with pytest.raises(host.HostError, match="already saved"):
        host.initialize(controller, "admin@example.test", "john-pi", IMAGE, "example/david-pi")
    with pytest.raises(host.HostError, match="already saved"):
        host.fresh_setup_private_root(controller, "john-pi", status)
    assert host.read_json(controller.state / "setup.json") == original
    assert not calls

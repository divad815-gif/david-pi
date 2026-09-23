"""Behavioral installer checks run without Tailscale, Docker or household data."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import time
import uuid

import pytest

from installer.host import Controller, HostError, assert_private_serve, atomic_json, parse_manifest, safe_path, validate_archive

IMAGE = "ghcr.io/example/david-pi@sha256:" + "1" * 64


@pytest.fixture
def config(tmp_path):
    return {"schema_version": 1, "instance_id": str(uuid.uuid4()), "display_name": "John's home", "hostname": "john-pi", "public_url": "https://john-pi.example.ts.net", "timezone": "America/Denver", "country": "US", "members": [{"login": "john@example.test", "name": "John", "role": "admin"}, {"login": "jane@example.test", "name": "Jane", "role": "household"}], "storage": {"mode": "folder", "data_root": str(tmp_path / "data"), "backup_root": None}, "modules": {"media": "enabled", "device_backup": "enabled", "chat": "enabled", "movies": "manual", "recipes": "manual", "audiobooks": "enabled", "mytube": "enabled"}, "integrations": {"web_push": False}}


@pytest.fixture
def controller(tmp_path, config):
    calls = []
    def runner(args, **kwargs):
        calls.append(args)
        return ""
    instance = Controller(tmp_path / "etc", runner=runner)
    instance.calls = calls
    instance.save_config(config)
    atomic_json(instance.etc / "release.json", {"version": "10.0.0", "image": IMAGE, "repository": "example/david-pi", "data_schema_version": 1, "rollback_min_data_schema": 1})
    return instance


def test_only_admitted_admin_can_dispatch(controller):
    for login in ("", "stranger@example.test", "jane@example.test"):
        with pytest.raises(HostError, match="administrator"):
            controller.dispatch("status", {}, login)
    assert controller.dispatch("status", {}, "JOHN@example.test")["configuration"]["display_name"] == "John's home"
    with pytest.raises(HostError, match="Unsupported"):
        controller.dispatch("exec", {"command": "touch /etc/sentinel"}, "john@example.test")


def test_secrets_never_enter_status_or_configuration(controller):
    controller.secret_update({"TMDB_API_READ_TOKEN": "private-secret-value"})
    value = controller.status()
    assert value["integrations"]["TMDB_API_READ_TOKEN"] is True
    assert "private-secret-value" not in json.dumps(value)
    assert (controller.etc / "secrets/integrations.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(HostError):
        controller.secret_update({"SHELL": "malicious"})
    with pytest.raises(HostError):
        controller.secret_update({"TMDB_API_READ_TOKEN": "first\nINJECT=value"})


def test_compose_workers_follow_modules_and_share_immutable_image(controller):
    cfg = controller.config()
    value = controller.compose(cfg, IMAGE)
    assert value["services"]["portal"]["ports"] == ["127.0.0.1:8090:8000"]
    assert {"portal", "slideshow", "device-backup", "audiobook-preparer", "mytube-preparer", "maintenance"} == value["services"].keys()
    assert "chat-notifier" not in value["services"]
    for service in value["services"].values():
        assert service["image"] == IMAGE
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert not any("docker.sock" in v["source"] for v in service["volumes"])
    cfg["modules"].update(media="disabled", device_backup="disabled", audiobooks="disabled")
    disabled = controller.compose(cfg, IMAGE)
    assert set(disabled["services"]) == {"portal", "mytube-preparer", "maintenance"}
    assert not controller.calls


def test_maintenance_state_is_not_owned_by_portal(controller):
    cfg = controller.config()
    controller.create_data_directories(cfg)
    directory = Path(cfg["storage"]["data_root"]) / ".david-pi-operations/maintenance"
    assert directory.stat().st_mode & 0o777 == 0o700
    worker = controller.compose(cfg, IMAGE)["services"]["maintenance"]
    assert worker["user"] == "10002:10001"
    assert next(v for v in worker["volumes"] if v["target"] == "/data")["read_only"]


def test_restrictive_helper_umask_preserves_worker_group_access(controller):
    cfg = controller.config()
    previous = os.umask(0o077)
    try:
        controller.create_data_directories(cfg)
    finally:
        os.umask(previous)
    data = Path(cfg["storage"]["data_root"])
    for relative in ("", ".david-pi-operations", "originals", "tmp", "tmp/uploads", "audiobooks", "audiobooks/incoming/streaming"):
        assert (data / relative).stat().st_mode & 0o777 == 0o750
    assert (data / ".david-pi-operations/maintenance").stat().st_mode & 0o777 == 0o700


def test_same_installation_repairs_named_directories_without_changing_content(controller):
    cfg = controller.config()
    controller.create_data_directories(cfg)
    data = Path(cfg["storage"]["data_root"])
    (data / ".david-pi-storage").write_text(cfg["instance_id"] + "\n")
    for directory in (data, *(p for p in data.rglob("*") if p.is_dir())):
        directory.chmod(0o700)
    album = data / "originals/private-album"
    album.mkdir(mode=0o700)
    content = album / "photo"
    content.write_bytes(b"Household content remains private")
    content.chmod(0o600)
    original = {path: (path.stat().st_mode, path.stat().st_uid, path.stat().st_gid) for path in (data.parent, album, content, data / ".david-pi-operations/maintenance")}
    controller.create_data_directories(cfg)
    controller.create_data_directories(cfg)
    assert data.stat().st_mode & 0o777 == 0o750
    assert (data / ".david-pi-operations").stat().st_mode & 0o777 == 0o750
    assert content.read_bytes() == b"Household content remains private"
    assert {path: (path.stat().st_mode, path.stat().st_uid, path.stat().st_gid) for path in original} == original


def test_directory_repair_refuses_foreign_installation_or_symlink(controller, tmp_path):
    cfg = controller.config()
    controller.create_data_directories(cfg)
    data = Path(cfg["storage"]["data_root"])
    marker = data / ".david-pi-storage"
    marker.write_text(str(uuid.uuid4()))
    data.chmod(0o700)
    with pytest.raises(HostError, match="another installation"):
        controller.create_data_directories(cfg)
    assert data.stat().st_mode & 0o777 == 0o700
    marker.write_text(cfg["instance_id"])
    outside = tmp_path / "unrelated"
    outside.mkdir(mode=0o700)
    state = data / ".david-pi-operations/maintenance"
    state.rmdir()
    state.symlink_to(outside, target_is_directory=True)
    with pytest.raises(HostError, match="symbolic link"):
        controller.create_data_directories(cfg)
    assert outside.stat().st_mode & 0o777 == 0o700


@pytest.mark.skipif(os.geteuid() != 0, reason="Real worker identity checks require an isolated root test run")
def test_worker_identities_can_traverse_managed_storage_but_portal_cannot_read_maintenance(controller):
    cfg = controller.config()
    previous = os.umask(0o077)
    try:
        controller.create_data_directories(cfg)
    finally:
        os.umask(previous)
    data = Path(cfg["storage"]["data_root"])

    def worker(uid, source):
        def identity():
            os.setgroups([])
            os.setgid(10001)
            os.setuid(uid)
        return subprocess.run([sys.executable, "-c", source], cwd=data, preexec_fn=identity, capture_output=True, text=True)

    maintenance = worker(10002, "from pathlib import Path; assert 'originals' in [p.name for p in Path('.').iterdir()]; Path('.david-pi-operations/maintenance/worker-state').write_text('private worker state')")
    assert maintenance.returncode == 0, maintenance.stderr
    portal = worker(10001, "from pathlib import Path; Path('originals/portal-content').write_text('portal works'); Path('.david-pi-operations/maintenance/worker-state').read_text()")
    assert portal.returncode != 0 and "PermissionError" in portal.stderr
    assert (data / "originals/portal-content").read_text() == "portal works"


def test_disabled_module_files_are_preserved(controller):
    cfg = controller.config()
    controller.create_data_directories(cfg)
    content = Path(cfg["storage"]["data_root"]) / "chat/precious-content"
    content.write_text("keep me")
    cfg["modules"]["chat"] = "disabled"
    controller.create_data_directories(cfg)
    assert content.read_text() == "keep me"


def test_settings_disable_and_reenable_workers_preserves_content_and_keys(controller, monkeypatch):
    monkeypatch.setattr(controller, "storage_guard", lambda: None)
    monkeypatch.setattr(controller, "readiness", lambda: {"ready": True})
    original = controller.config()
    controller.write_runtime(original, IMAGE)
    data = Path(original["storage"]["data_root"])
    for directory in ("chat", "mytube", "audiobooks/originals", "originals"):
        (data / directory / "saved-content").write_bytes(b"household content")
    key = (controller.etc / "secrets/chat-master.key").read_bytes()
    disabled = {name: "disabled" for name in original["modules"]}
    controller.settings({"configuration": {"modules": disabled}}, {"id": "a"*32})
    assert set(json.loads(controller.compose_path.read_text())["services"]) == {"portal", "maintenance"}
    controller.settings({"configuration": {"modules": original["modules"]}}, {"id": "b"*32})
    assert set(json.loads(controller.compose_path.read_text())["services"]) == {"portal", "maintenance", "slideshow", "device-backup", "audiobook-preparer", "mytube-preparer"}
    assert (controller.etc / "secrets/chat-master.key").read_bytes() == key
    for directory in ("chat", "mytube", "audiobooks/originals", "originals"):
        assert (data / directory / "saved-content").read_bytes() == b"household content"
    starts = [args for args in controller.calls if "up" in args]
    assert all("--remove-orphans" in args and "--force-recreate" in args and "--wait" in args for args in starts)


def test_settings_missing_drive_fails_before_secrets_or_storage_writes(controller, monkeypatch):
    def missing():
        raise HostError("Required application storage is missing")
    monkeypatch.setattr(controller, "storage_guard", missing)
    original = controller.config_path.read_bytes()
    with pytest.raises(HostError, match="storage is missing"):
        controller.settings({"configuration": {"display_name": "Changed"}, "secrets": {"TMDB_API_READ_TOKEN": "new-secret"}}, {"id": "a"*32})
    assert controller.config_path.read_bytes() == original
    assert not (controller.etc / "secrets").exists()
    assert not Path(controller.config()["storage"]["data_root"]).exists()
    assert not controller.calls


def test_failed_settings_restores_credentials_backup_status_and_pihole_timer(controller, monkeypatch, tmp_path):
    monkeypatch.setattr(controller, "storage_guard", lambda: None)
    monkeypatch.setattr(controller, "provision_backup", lambda cfg: None)
    cfg = controller.config()
    cfg["modules"]["pihole"] = "enabled"
    controller.save_config(cfg)
    controller.secret_update({"TMDB_API_READ_TOKEN": "old-secret"})
    backup_status = {"state": "integrity_verified", "snapshot_id": "c"*32}
    atomic_json(controller.state / "backup-status.json", backup_status)
    def unavailable():
        raise HostError("Selected service did not become healthy")
    monkeypatch.setattr(controller, "readiness", unavailable)
    changed = {**cfg["modules"], "pihole": "disabled", "mytube": "disabled"}
    with pytest.raises(HostError, match="did not become healthy"):
        controller.settings({"configuration": {"modules": changed, "storage": {"backup_root": str(tmp_path / "backup")}}, "secrets": {"TMDB_API_READ_TOKEN": "new-secret"}}, {"id": "d"*32})
    assert controller.config() == cfg
    assert json.loads((controller.etc / "secrets/integrations.json").read_text()) == {"TMDB_API_READ_TOKEN": "old-secret"}
    assert "old-secret" in (controller.etc / "runtime.env").read_text()
    assert json.loads((controller.state / "backup-status.json").read_text()) == backup_status
    assert "mytube-preparer" in json.loads(controller.compose_path.read_text())["services"]
    assert ["systemctl", "disable", "--now", "david-pi-pihole-summary.timer"] in controller.calls
    assert ["systemctl", "enable", "--now", "david-pi-pihole-summary.timer"] in controller.calls


def test_readiness_checks_all_selected_workers(controller, monkeypatch):
    cfg = controller.config()
    atomic_json(controller.compose_path, controller.compose(cfg, IMAGE))
    monkeypatch.setattr(controller, "storage_guard", lambda: None)
    monkeypatch.setattr(controller, "docker", lambda *args: json.dumps([{"Service": name, "State": "running", "Health": "healthy"} for name in ("portal", "maintenance", "slideshow", "device-backup", "mytube-preparer")]))
    with pytest.raises(HostError, match="selected services"):
        controller.readiness()


@pytest.mark.parametrize("module", list(__import__("modules.installation", fromlist=["MODULES"]).MODULES))
def test_registry_owns_selected_services_storage_and_healthchecks(controller, module):
    from modules.installation import MODULES, selected_substrates, selected_workers, validate_installation
    cfg = controller.config()
    cfg["modules"] = {module: MODULES[module]["modes"][0]}
    for dependency in MODULES[module].get("requires", ()):
        cfg["modules"][dependency] = MODULES[dependency]["modes"][0]
    if module == "chat":
        cfg["integrations"]["web_push"] = True
    cfg = validate_installation(cfg)
    controller.create_data_directories(cfg)
    data = Path(cfg["storage"]["data_root"])
    for name in MODULES[module].get("storage", ()):
        assert (data / name).is_dir()
    if "media" in selected_substrates(cfg):
        assert (data / "photos.db").is_file() and (data / "originals").is_dir()
    services = controller.compose(cfg, IMAGE)["services"]
    assert set(services) == {"portal", *selected_workers(cfg)}
    for name, spec in selected_workers(cfg).items():
        assert services[name]["command"] == ["python", "-m", spec["entrypoint"]]
        assert bool(services[name].get("healthcheck")) == spec["healthcheck"]
        if spec["healthcheck"]:
            assert services[name]["healthcheck"]["timeout"] == "20s"


def test_supported_storage_paths_reject_system_locations_and_symlinks(tmp_path):
    for value in ("/", "/etc", "/etc/shadow", "relative", "/srv/../etc", "/srv/path\nline"):
        with pytest.raises(HostError):
            safe_path(value, exists=False)
    assert str(safe_path("/srv/household/data", exists=False)) == "/srv/household/data"


def test_existing_tailscale_routes_are_preserved():
    assert_private_serve({"Web": {"other.example.ts.net:8443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:3000"}}}}}, "john-pi.example.ts.net")
    for value in ({"AllowFunnel": {"john-pi.example.ts.net:443": True}}, {"TCP": {"443": {"TCPForward": "localhost:22"}}}, {"Web": {"john-pi.example.ts.net:443": {"Handlers": {"/": {"Proxy": "http://localhost:3000"}}}}}, {"Web": {"john-pi.example.ts.net:443": {"Handlers": {"/": {"Path": "/srv/other-site"}}}}}):
        with pytest.raises(HostError):
            assert_private_serve(value, "john-pi.example.ts.net")


@pytest.fixture
def reconnect_host(controller, monkeypatch):
    dns = "john-pi-2.new-tail.ts.net"
    state = {"TCP": {"443": {"HTTPS": True}, "8443": {"HTTPS": True}}, "Web": {f"{dns}:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8090"}, "/music": {"Proxy": "http://127.0.0.1:3000"}}}, f"{dns}:8443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:3001"}}}}}
    status = {"BackendState": "Running", "Self": {"DNSName": dns + "."}}
    def runner(args, **kwargs):
        controller.calls.append(args)
        if args == ["tailscale", "status", "--json"]:
            return json.dumps(status)
        if args == ["tailscale", "serve", "status", "--json"]:
            return json.dumps(state)
        if args[:2] == ["tailscale", "serve"]:
            assert args[2:6] == ["--bg", "--https=443", "--set-path=/", "--yes"]
            handlers = state["Web"][f"{dns}:443"]["Handlers"]
            if args[-1] == "off":
                handlers.pop("/", None)
            else:
                handlers["/"] = {"Proxy": args[-1]}
        return ""
    controller.runner = runner
    monkeypatch.setattr(controller, "storage_guard", lambda: None)
    monkeypatch.setattr(controller, "readiness", lambda: {"ready": True})
    controller.write_runtime(controller.config(), IMAGE)
    controller.calls.clear()
    return controller, state, status, "https://" + dns


def test_reconnect_retains_installation_and_preserves_other_serve_routes(reconnect_host):
    controller, state, status, origin = reconnect_host
    cfg = controller.config()
    before = copy.deepcopy(state)
    data = Path(cfg["storage"]["data_root"])
    (data / "chat/retained").write_bytes(b"family conversation")
    key = (controller.etc / "secrets/chat-master.key").read_bytes()
    result = controller.reconnect(origin)
    expected = {**cfg, "public_url": origin, "hostname": "john-pi-2"}
    assert controller.config() == expected
    assert state == before
    assert result["instance_id"] == cfg["instance_id"] and result["reconnected"]
    assert (data / "chat/retained").read_bytes() == b"family conversation"
    assert (controller.etc / "secrets/chat-master.key").read_bytes() == key
    compose = json.loads(controller.compose_path.read_text())
    assert compose["services"]["portal"]["environment"]["DAVID_PI_PUBLIC_URL"] == origin
    assert all(args[:2] != ["tailscale", "set"] for args in controller.calls)
    starts = [args for args in controller.calls if "up" in args]
    assert len(starts) == 1 and "--force-recreate" in starts[0] and "--wait" in starts[0]
    job = json.loads(next(controller.jobs.glob("*.json")).read_text())
    assert job["state"] == "complete" and job["operation"] == "reconnect"


@pytest.mark.parametrize("problem", ["different", "trailing_slash", "disconnected", "funnel", "foreign_proxy", "file_server"])
def test_reconnect_requires_exact_origin_and_safe_serve_before_mutation(reconnect_host, problem):
    controller, state, status, origin = reconnect_host
    accepted = origin
    handlers = state["Web"][origin.removeprefix("https://") + ":443"]["Handlers"]
    if problem == "different": accepted = "https://other.new-tail.ts.net"
    if problem == "trailing_slash": accepted += "/"
    if problem == "disconnected": status["BackendState"] = "Stopped"
    if problem == "funnel": state["AllowFunnel"] = {"other.new-tail.ts.net:8443": True}
    if problem == "foreign_proxy": handlers["/"] = {"Proxy": "http://127.0.0.1:3000"}
    if problem == "file_server": handlers["/"] = {"Path": "/srv/another-site"}
    before = controller.config_path.read_bytes()
    with pytest.raises(HostError): controller.reconnect(accepted)
    assert controller.config_path.read_bytes() == before
    assert not list(controller.jobs.glob("*.json"))
    assert all(args in (["tailscale", "status", "--json"], ["tailscale", "serve", "status", "--json"]) for args in controller.calls)


@pytest.mark.parametrize("existing_root", [True, False])
def test_reconnect_failure_restores_configuration_without_restoring_data(reconnect_host, monkeypatch, existing_root):
    controller, state, status, origin = reconnect_host
    if not existing_root:
        state["Web"][origin.removeprefix("https://") + ":443"]["Handlers"].pop("/")
    before = copy.deepcopy(state)
    cfg = controller.config()
    content = Path(cfg["storage"]["data_root"]) / "household.txt"
    content.write_text("Original content")
    attempts = []
    def readiness():
        attempts.append(True)
        if len(attempts) == 1:
            content.write_text("Newer application content")
            raise HostError("Candidate did not become ready")
        return {"ready": True}
    monkeypatch.setattr(controller, "readiness", readiness)
    with pytest.raises(HostError, match="previous application configuration"):
        controller.reconnect(origin)
    assert controller.config() == cfg
    assert content.read_text() == "Newer application content"
    assert state == before
    assert json.loads(controller.compose_path.read_text())["services"]["portal"]["environment"]["DAVID_PI_PUBLIC_URL"] == cfg["public_url"]
    job = json.loads(next(controller.jobs.glob("*.json")).read_text())
    assert job["state"] == "failed" and job["configuration_recovered"]


def test_reconnect_is_not_exposed_to_portal_helper(controller):
    with pytest.raises(HostError, match="Unsupported management operation"):
        controller.dispatch("reconnect", {"accept_origin": "https://john-pi.example.ts.net"}, "john@example.test")


def test_reconnect_cli_requires_explicit_acceptance():
    import subprocess, sys
    result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "host.py"), "reconnect"], capture_output=True, text=True)
    assert result.returncode == 2 and "--accept-origin" in result.stderr


def configured_operation(reconnect_host, monkeypatch, operation):
    controller, state, status, origin = reconnect_host
    cfg = controller.config()
    cfg.update(public_url=origin, hostname="john-pi-2")
    controller.save_config(cfg)
    if operation == "install":
        controller.config_path.unlink()
        atomic_json(controller.state / "setup.json", {"admin": cfg["members"][0]["login"]})
        monkeypatch.setattr(controller, "provision_storage", lambda configuration: None)
    return cfg, lambda: getattr(controller, operation)({"configuration": cfg}, {"id": "f"*32})


@pytest.mark.parametrize("operation", ["install", "repair", "update"])
@pytest.mark.parametrize("problem", ["foreign_proxy", "funnel", "different_address", "disconnected"])
def test_host_operations_reject_conflicting_or_stale_tailscale_state_before_changes(reconnect_host, monkeypatch, operation, problem):
    controller, state, status, origin = reconnect_host
    cfg, perform = configured_operation(reconnect_host, monkeypatch, operation)
    if problem == "foreign_proxy":
        state["Web"][origin.removeprefix("https://") + ":443"]["Handlers"]["/"] = {"Proxy": "http://127.0.0.1:3000"}
    if problem == "funnel":
        state["AllowFunnel"] = {"other.new-tail.ts.net:8443": True}
    if problem == "different_address":
        status["Self"]["DNSName"] = "renamed.new-tail.ts.net."
    if problem == "disconnected":
        status["BackendState"] = "Stopped"
    def no_download(): raise AssertionError("An update must check private access before downloading")
    monkeypatch.setattr(controller, "update_check", no_download)
    before = copy.deepcopy(state)
    with pytest.raises(HostError): perform()
    assert state == before
    assert not list(controller.jobs.glob("*.json"))
    assert all(args in (["tailscale", "status", "--json"], ["tailscale", "serve", "status", "--json"]) for args in controller.calls)
    if operation == "install": assert not controller.config_path.exists()


@pytest.mark.parametrize("operation", ["install", "repair"])
def test_install_and_repair_preserve_unrelated_serve_routes(reconnect_host, monkeypatch, operation):
    controller, state, status, origin = reconnect_host
    cfg, perform = configured_operation(reconnect_host, monkeypatch, operation)
    before = copy.deepcopy(state)
    result = perform()
    assert state == before
    assert result.get("ready") is True or result.get("public_url") == origin
    mutations = [args for args in controller.calls if args[:2] == ["tailscale", "serve"] and "--set-path=/" in args]
    assert len(mutations) == 1
    assert mutations[0][-1] == "http://127.0.0.1:8090"
    assert json.loads((controller.state / "installed.json").read_text())["instance_id"] == cfg["instance_id"]


@pytest.mark.parametrize("operation", ["install", "repair"])
@pytest.mark.parametrize("change", ["foreign_proxy", "extra_route", "funnel", "different_address"])
def test_install_and_repair_recheck_after_startup_before_changing_serve(reconnect_host, monkeypatch, operation, change):
    controller, state, status, origin = reconnect_host
    _, perform = configured_operation(reconnect_host, monkeypatch, operation)
    def ready_after_external_change():
        handlers = state["Web"][origin.removeprefix("https://") + ":443"]["Handlers"]
        if change == "foreign_proxy": handlers["/"] = {"Proxy": "http://127.0.0.1:3000"}
        if change == "extra_route": handlers["/added-later"] = {"Proxy": "http://127.0.0.1:3005"}
        if change == "funnel": state["AllowFunnel"] = {"other.new-tail.ts.net:8443": True}
        if change == "different_address": status["Self"]["DNSName"] = "changed.new-tail.ts.net."
        return {"ready": True}
    monkeypatch.setattr(controller, "readiness", ready_after_external_change)
    with pytest.raises(HostError): perform()
    assert not any("--set-path=/" in args for args in controller.calls)
    assert not (controller.state / "installed.json").exists()


def test_private_root_change_checks_readback(reconnect_host):
    controller, state, status, origin = reconnect_host
    before = copy.deepcopy(state)
    runner = controller.runner
    def ignored_change(args, **kwargs):
        if "--set-path=/" in args:
            controller.calls.append(args)
            return ""
        return runner(args, **kwargs)
    controller.runner = ignored_change
    with pytest.raises(HostError, match="could not be verified"):
        controller.set_private_root(origin, "http://127.0.0.1:8091", before)
    assert state == before


def test_initial_setup_rechecks_serve_after_helper_restart(reconnect_host):
    from installer.host import initialize
    controller, state, status, origin = reconnect_host
    controller.config_path.unlink()
    runner = controller.runner
    def external_change(args, **kwargs):
        result = runner(args, **kwargs)
        if args == ["systemctl", "restart", "david-pi-helper.service"]:
            state["Web"][origin.removeprefix("https://") + ":443"]["Handlers"]["/"] = {"Path": "/srv/other-site"}
        return result
    controller.runner = external_change
    with pytest.raises(HostError, match="already used by another service"):
        initialize(controller, "john@example.test", "john-pi-2", IMAGE, "example/david-pi")
    assert not any("--set-path=/" in args for args in controller.calls)
    assert state["Web"][origin.removeprefix("https://") + ":443"]["Handlers"]["/"] == {"Path": "/srv/other-site"}


def test_manifest_requires_stable_version_repository_digest_and_compatibility():
    content = "\n".join(["VERSION=10.0.1", "ARCHIVE=david-pi-10.0.1.tar.gz", "ARCHIVE_SHA256="+"2"*64, "IMAGE="+IMAGE, "DATA_SCHEMA_VERSION=1", "ROLLBACK_MIN_DATA_SCHEMA=1"])
    assert parse_manifest(content, "example/david-pi")["DATA_SCHEMA_VERSION"] == "1"
    for broken in (content.replace("10.0.1", "10.0.1-preview"), content.replace("example/", "attacker/"), content.replace("DATA_SCHEMA_VERSION=1", ""), content + "\nVERSION=1.0.0"):
        with pytest.raises(HostError):
            parse_manifest(broken, "example/david-pi")


@pytest.mark.parametrize("name,kind", [("../outside", "file"), ("/tmp/outside", "file"), ("release/link", "link"), ("release/device", "device")])
def test_archive_rejects_traversal_links_and_special_files(tmp_path, name, kind):
    archive = tmp_path / "release.tgz"
    with tarfile.open(archive, "w:gz") as stream:
        item = tarfile.TarInfo(name)
        if kind == "link":
            item.type = tarfile.SYMTYPE
            item.linkname = "/etc"
        if kind == "device":
            item.type = tarfile.CHRTYPE
        stream.addfile(item)
    with pytest.raises(HostError):
        validate_archive(archive, tmp_path / "extract")
    assert not (tmp_path / "extract").exists()


def test_jobs_survive_errors_without_retaining_credentials(controller):
    def fail(payload, job):
        controller.phase(job, "testing")
        raise HostError("A useful safe error")
    controller.settings = fail
    result = controller.enqueue("settings", {"secrets": {"TMDB_API_READ_TOKEN": "not-in-journal"}})
    for _ in range(100):
        job = json.loads((controller.jobs / (result["job_id"] + ".json")).read_text())
        if job["state"] == "failed":
            break
        time.sleep(.01)
    assert job["state"] == "failed"
    assert job["error"] == "A useful safe error"
    assert "not-in-journal" not in json.dumps(job)


def test_restart_marks_jobs_interrupted_without_retry(controller):
    path = controller.jobs / ("a"*32 + ".json")
    atomic_json(path, {"id": "a"*32, "state": "running", "operation": "update", "phase": "migrating", "created_at": 0})
    controller.recover_jobs()
    assert json.loads(path.read_text())["state"] == "interrupted"
    assert not controller.calls


def test_online_settings_reject_storage_move_and_identity_change(controller):
    for configuration in ({"instance_id": str(uuid.uuid4())}, {"public_url": "https://other.example.ts.net"}, {"storage": {"data_root": "/srv/elsewhere"}}):
        with pytest.raises(HostError):
            controller.settings({"configuration": configuration}, {"id": "b"*32})
    assert not controller.calls


def test_manual_integrations_do_not_require_external_network(controller):
    with pytest.raises(HostError, match="manual watchlist"):
        controller.test_integration({"name": "movies"})
    with pytest.raises(HostError, match="demonstration"):
        controller.test_integration({"name": "recipes", "credential": "1"})


def test_claim_http_rejects_other_identity_origin_replay_and_csrf(tmp_path):
    import http.client
    import threading
    from http.server import ThreadingHTTPServer
    from installer.host import SetupHandler
    controller = Controller(tmp_path / 'etc')
    token = 'correct-private-token'
    atomic_json(controller.state/'setup.json', {'admin':'john@example.test','origin':'https://john-pi.example.ts.net','hostname':'john-pi','instance_id':str(uuid.uuid4()),'expires':time.time()+60,'token_hash':hashlib.sha256(token.encode()).hexdigest(),'claimed':False})
    server = ThreadingHTTPServer(('127.0.0.1', 0), SetupHandler)
    server.controller=controller; server.claim_lock=threading.Lock()
    threading.Thread(target=server.serve_forever,daemon=True).start()
    def post(path, payload, **extra):
        headers={'Host':'john-pi.example.ts.net','Origin':'https://john-pi.example.ts.net','Tailscale-User-Login':'john@example.test','Content-Type':'application/json',**extra}
        connection=http.client.HTTPConnection('127.0.0.1', server.server_port)
        connection.request('POST',path,body=json.dumps(payload),headers=headers)
        response=connection.getresponse(); status=response.status; cookie=response.getheader('Set-Cookie'); result=json.loads(response.read());connection.close();return status,cookie,result
    try:
        assert post('/api/claim',{'token':token}, **{'Tailscale-User-Login':'intruder@example.test'})[0] == 400
        assert post('/api/claim',{'token':token}, Origin='https://evil.example')[0] == 400
        assert post('/api/claim',{'token':'wrong'})[0] == 400
        status,cookie,result=post('/api/claim',{'token':token})
        assert status == 200 and result['claimed']
        assert 'Secure; HttpOnly; SameSite=Strict' in cookie
        assert post('/api/claim',{'token':token})[0] == 400
        assert post('/api/install',{},Cookie=cookie.split(';')[0])[0] == 400
    finally:
        server.shutdown();server.server_close()


def test_snapshot_preserves_content_and_detects_corruption(controller, monkeypatch, tmp_path):
    cfg=controller.config();data=Path(cfg['storage']['data_root']);controller.create_data_directories(cfg)
    controller.secret_update({'TMDB_API_READ_TOKEN':'recovery-secret'})
    controller.provision_secrets(cfg)
    controller.compose_path.write_text('{}');(controller.etc/'runtime.env').write_text('')
    atomic_json(controller.state/'storage.json',{'uuid':'test','data_root':str(data)})
    (data/'household.txt').write_text('Family content survives')
    import sqlite3
    with sqlite3.connect(data/'notes.db') as db:
        db.execute('CREATE TABLE notes(id INTEGER PRIMARY KEY,body TEXT)');db.execute('INSERT INTO notes(body) VALUES(?)',('A fixture note',))
    monkeypatch.setattr(controller,'storage_guard',lambda:None)
    backup=tmp_path/'backup';backup.mkdir();cfg['storage']['backup_root']=str(backup);controller.save_config(cfg)
    job={'id':'c'*32};destination=backup/job['id']
    controller.snapshot(destination,job,independent=True)
    result=controller.restore_test({'snapshot_id':job['id']}, {'id':'d'*32})
    assert result['state']=='integrity_verified'
    assert result['clean_host_restore_verified'] is False
    assert (destination/'data/household.txt').read_text()=='Family content survives'
    assert json.loads((destination/'secrets/integrations.json').read_text())['TMDB_API_READ_TOKEN']=='recovery-secret'
    (destination/'data/household.txt').write_text('corrupt')
    with pytest.raises(HostError,match='checksum'):
        controller.restore_test({'snapshot_id':job['id']}, {'id':'e'*32})
    assert (data/'household.txt').read_text()=='Family content survives'


def test_update_failure_never_restores_snapshot_over_current_content(controller):
    # All automated recovery is image-only; full restore is deliberately local.
    import inspect
    source=inspect.getsource(Controller.update)
    exception_branch=source[source.index('        except Exception:'):]
    assert 'self.write_runtime(cfg, old["image"])' in exception_branch
    assert 'copytree' not in exception_branch
    assert 'restore_test' not in exception_branch


def test_host_operation_lock_serializes_cli_and_helper(controller, tmp_path):
    second = Controller(controller.etc)
    with controller.external_operation():
        with pytest.raises(HostError, match='already running'):
            second.acquire_operation()
        with pytest.raises(HostError, match='already running'):
            second.enqueue('backup', {})
    # A rejected request releases its thread lock too.
    assert second.lock.acquire(blocking=False)
    second.lock.release()


def test_redirect_cannot_forward_provider_credentials_to_another_host():
    from installer.host import RestrictedRedirect
    from urllib.request import Request
    handler=RestrictedRedirect({'api.themoviedb.org'})
    request=Request('https://api.themoviedb.org/3/authentication',headers={'Authorization':'Bearer private'})
    for url in ('https://attacker.example/','http://api.themoviedb.org/','https://user@api.themoviedb.org/'):
        with pytest.raises(HostError,match='approved'):
            handler.redirect_request(request,None,302,'Found',{},url)


def test_failed_journal_write_releases_operation_lock(controller, monkeypatch):
    import installer.host as host
    def full(*args,**kwargs):raise OSError('No space left on device')
    monkeypatch.setattr(host,'atomic_json',full)
    with pytest.raises(OSError):controller.enqueue('backup',{})
    assert controller.lock.acquire(blocking=False)
    controller.lock.release()
    with controller.external_operation():pass


def test_host_release_activation_and_rollback_keep_executable_pointer(controller, tmp_path, monkeypatch):
    import installer.host as host
    lib=tmp_path/'lib';releases=lib/'david-pi-releases';old=releases/'old';old.mkdir(parents=True)
    unit_root=tmp_path/'units';unit_root.mkdir()
    def release(path,version):
        (path/'installer/systemd').mkdir(parents=True,exist_ok=True)
        (path/'david-pi').write_text('#!/bin/sh\n')
        (path/'installer/host.py').write_text('# '+version)
        for name in ('david-pi-helper.service','david-pi-portal.service','david-pi-status.service','david-pi-status.timer'):
            (path/'installer/systemd'/name).write_text('# '+version)
    release(old,'old');candidate=tmp_path/'candidate';release(candidate,'new')
    installed=lib/'david-pi';installed.symlink_to(old)
    monkeypatch.setattr(host,'INSTALL_ROOT',installed);monkeypatch.setattr(host,'SYSTEMD_ROOT',unit_root)
    controller.activate_release(candidate,'f'*32)
    assert installed.is_symlink() and (installed/'installer/host.py').read_text()=='# new'
    assert old.is_dir()
    controller.rollback_activation('f'*32)
    assert installed.resolve()==old
    assert (unit_root/'david-pi-helper.service').read_text()=='# old'


def metrics_fixture(controller):
    import os,sqlite3
    cfg=controller.config();controller.create_data_directories(cfg)
    directory=Path(cfg['storage']['data_root'])/'.david-pi-operations/maintenance'
    # Production is fixed to uid10002. Tests use their own unprivileged owner.
    controller.operations_uid=os.geteuid()
    path=directory/'metrics.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE system_metrics(timestamp INTEGER PRIMARY KEY,cpu REAL,temperature REAL)')
        db.execute('CREATE TABLE secrets(value TEXT)')
        db.execute('INSERT INTO secrets VALUES (?)',('private-text-never-returned',))
        now=int(time.time())
        db.executemany('INSERT INTO system_metrics VALUES (?,?,?)',((now-i,float(i%100),20+i%30) for i in range(1800)))
        db.execute('INSERT INTO system_metrics VALUES (?,?,?)',(now-2000,'private-text-never-returned',None))
        db.execute('INSERT INTO system_metrics VALUES (?,?,?)',(now-90000,999,999))
    return path,now


def test_metrics_history_admits_members_but_never_admin_operations(controller):
    _,now=metrics_fixture(controller)
    result=controller.dispatch('metrics_history',{'metric':'cpu','range':'24h'},'jane@example.test')
    assert set(result)=={'metric','range','points','sampled'}
    assert result['sampled'] is True and len(result['points'])<=601
    assert result['points'][-1]=={'timestamp':now,'value':0.0}
    assert all(isinstance(p['timestamp'],int) and isinstance(p['value'],(int,float)) for p in result['points'])
    assert 'private-text' not in json.dumps(result)
    assert not any(p['value']==999 for p in result['points'])
    for operation in ('status','settings','backup','update','restore_test'):
        with pytest.raises(HostError,match='administrator'):
            controller.dispatch(operation,{},'jane@example.test')
    with pytest.raises(HostError,match='member'):
        controller.dispatch('metrics_history',{},'outsider@example.test')


def test_metrics_history_rejects_dynamic_columns_paths_ranges_and_symlinks(controller,tmp_path):
    path,_=metrics_fixture(controller)
    for payload in ({'metric':'value FROM secrets'},{'range':'all'},{'metric':'cpu','path':'/etc/shadow'},{'metric':['cpu']}):
        with pytest.raises(HostError,match='supported'):
            controller.dispatch('metrics_history',payload,'jane@example.test')
    original=path.read_bytes();outside=tmp_path/'other.db';outside.write_bytes(original);path.unlink();path.symlink_to(outside)
    with pytest.raises(HostError,match='temporarily unavailable'):
        controller.dispatch('metrics_history',{'metric':'cpu'},'jane@example.test')
    assert outside.read_bytes()==original

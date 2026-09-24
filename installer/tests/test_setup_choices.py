"""Wizard discovery stays read-only and revalidates what the browser selected."""
import copy
import hashlib
import http.client
import json
import os
from pathlib import Path
from types import SimpleNamespace
import threading
import time
from http.server import ThreadingHTTPServer

import pytest

import installer.host as host

IMAGE = "ghcr.io/example/david-pi@sha256:" + "1" * 64


@pytest.fixture
def discovery(tmp_path, monkeypatch):
    mounts = [
        {"source": "/dev/vda1", "fstype": "ext4", "uuid": "os-id", "target": "/", "options": "rw,relatime", "fsroot": "/", "label": "OS"},
        {"source": "/dev/vdb", "fstype": "ext4", "uuid": "data-id", "target": "/mnt/data", "options": "rw,relatime", "fsroot": "/", "label": "Family photos"},
        {"source": "/dev/vdc", "fstype": "ext4", "uuid": "backup-id", "target": "/mnt/backup", "options": "rw,relatime", "fsroot": "/", "label": "Backups"},
    ]
    disks = {"blockdevices": [
        {"name": "/dev/vda", "type": "disk", "maj:min": "252:0", "serial": "os-serial", "children": [{"name": "/dev/vda1", "type": "part", "pkname": "/dev/vda", "maj:min": "252:1"}]},
        {"name": "/dev/vdb", "type": "disk", "maj:min": "252:16", "serial": "data-serial"},
        {"name": "/dev/vdc", "type": "disk", "maj:min": "252:32", "serial": "backup-serial"},
    ]}
    calls = []
    def runner(args, **kwargs):
        calls.append(args)
        if args[0] == "findmnt" and "--list" in args:
            return json.dumps({"filesystems": mounts})
        if args[0] == "findmnt" and "--target" in args:
            target = args[args.index("--target") + 1]
            return json.dumps({"filesystems": [next(m for m in mounts if m["target"] == ("/" if target == "/srv" else target))]})
        if args[0] == "lsblk":
            return json.dumps(disks)
        raise AssertionError(f"Unexpected command {args}")
    controller = host.Controller(tmp_path / "etc", runner=runner)
    monkeypatch.setattr(host, "safe_path", lambda path, **kwargs: Path(path))
    monkeypatch.setattr(host.os, "access", lambda *args: True)
    monkeypatch.setattr(host.os, "statvfs", lambda *args: SimpleNamespace(f_flag=0))
    capacity = SimpleNamespace(total=20 * 1024**3, free=12 * 1024**3)
    monkeypatch.setattr(host.shutil, "disk_usage", lambda path: capacity)
    return controller, mounts, disks, capacity, calls


def test_discovery_offers_capacity_and_physical_disk_identity_without_mutation(discovery):
    controller, _, _, _, calls = discovery
    choices = controller.storage_choices()["choices"]
    assert [c["parent"] for c in choices] == ["/mnt/backup", "/mnt/data", "/srv"]
    data, system = choices[1:]
    assert data["label"] == "Family photos — /mnt/data"
    assert data["mode"] == "drive" and not data["system_disk"]
    assert system["mode"] == "folder" and system["system_disk"]
    assert data["free_bytes"] == 12 * 1024**3 and data["total_bytes"] == 20 * 1024**3
    assert len({c["device_id"] for c in choices}) == 3
    assert all(call[0] in {"findmnt", "lsblk"} for call in calls)
    assert not controller.config_path.exists()


@pytest.mark.parametrize("changes", [
    {"options": "ro,relatime"}, {"options": "rw,bind"}, {"fsroot": "/subfolder"},
    {"source": "/dev/vdb[/subfolder]"}, {"source": "server:/share"}, {"fstype": "xfs"},
    {"fstype": "tmpfs"}, {"uuid": None}, {"target": "/mnt/with spaces"},
])
def test_discovery_omits_unsupported_or_ambiguous_mounts(discovery, changes):
    controller, mounts, _, _, _ = discovery
    mounts[1].update(changes)
    assert not any(c["parent"] == mounts[1]["target"] for c in controller.storage_choices()["choices"])


def test_discovery_omits_bind_aliases_and_unwritable_mounts(discovery, monkeypatch):
    controller, mounts, _, _, _ = discovery
    mounts.append({**mounts[1], "target": "/mnt/alias"})
    monkeypatch.setattr(host.os, "access", lambda folder, mode: str(folder) != "/mnt/backup")
    assert [c["parent"] for c in controller.storage_choices()["choices"]] == ["/srv"]


def test_systemd_mount_layers_preserve_choices_and_device_identity(discovery):
    controller, mounts, _, _, calls = discovery
    original = controller.storage_choices()["choices"]
    mounts.extend({**mount, "options": "rw,nosuid,relatime"} for mount in copy.deepcopy(mounts))
    writable_srv = {**mounts[0], "source": "/dev/vda1[/srv]", "target": "/srv", "fsroot": "/srv"}
    mounts.append(writable_srv)
    runner = controller.runner
    def effective_namespace(args, **kwargs):
        if args[0] == "findmnt" and "--target" in args and args[args.index("--target")+1].startswith("/srv"):
            calls.append(args)
            return json.dumps({"filesystems": [writable_srv]})
        return runner(args, **kwargs)
    controller.runner = effective_namespace
    choices = controller.storage_choices()["choices"]
    assert choices == original
    # Provisioning needs the real block device too, not findmnt's [/srv] suffix.
    inspected = controller.inspect_storage("/srv/david-pi-data")
    assert inspected["source"] == "/dev/vda1" and inspected["target"] == "/"
    cfg, selection = selected_configuration(choices)
    controller.validate_storage_selection(cfg, selection)


@pytest.mark.parametrize("changed", ["subfolder", "device", "uuid"])
def test_system_folder_discovery_rejects_redirected_namespace_view(discovery, changed):
    controller, mounts, _, _, calls = discovery
    view = {**mounts[0], "source": "/dev/vda1[/srv]", "target": "/srv", "fsroot": "/srv"}
    if changed == "subfolder": view.update(source="/dev/vda1[/other]", fsroot="/other")
    elif changed == "device": view["source"] = "/dev/vdb[/srv]"
    elif changed == "uuid": view["uuid"] = "replaced-filesystem"
    runner = controller.runner
    def effective_namespace(args, **kwargs):
        if args[0] == "findmnt" and "--target" in args and args[args.index("--target")+1] == "/srv":
            calls.append(args)
            return json.dumps({"filesystems": [view]})
        return runner(args, **kwargs)
    controller.runner = effective_namespace
    assert [c["parent"] for c in controller.storage_choices()["choices"]] == ["/mnt/backup", "/mnt/data"]


def test_stacked_mounts_do_not_hide_a_distinct_bind_alias(discovery):
    controller, mounts, _, _, _ = discovery
    mounts.extend([{**mounts[1], "options": "rw,nosuid"}, {**mounts[1], "target": "/mnt/alias"}])
    assert [c["parent"] for c in controller.storage_choices()["choices"]] == ["/mnt/backup", "/srv"]


@pytest.mark.parametrize("replacement", ["device", "subfolder", "readonly"])
def test_stacked_drive_rechecks_the_effective_namespace_mount(discovery, monkeypatch, replacement):
    controller, mounts, _, _, calls = discovery
    top = {**mounts[1], "options": "rw,nosuid"}
    mounts.append(top)
    if replacement == "device": top.update(source="/dev/vdc", uuid="backup-id")
    elif replacement == "subfolder": top.update(source="/dev/vdb[/private]", fsroot="/private")
    elif replacement == "readonly":
        top["options"] = "ro,nosuid"
        monkeypatch.setattr(host.os, "statvfs", lambda path: SimpleNamespace(f_flag=host.os.ST_RDONLY if str(path) == "/mnt/data" else 0))
    runner = controller.runner
    def effective_namespace(args, **kwargs):
        if args[0] == "findmnt" and "--target" in args and args[args.index("--target")+1] == "/mnt/data":
            calls.append(args)
            return json.dumps({"filesystems": [top]})
        return runner(args, **kwargs)
    controller.runner = effective_namespace
    assert not any(c["parent"] == "/mnt/data" for c in controller.storage_choices()["choices"])


def test_discovery_rechecks_mount_under_candidate_path(discovery):
    controller, _, _, _, _ = discovery
    controller.inspect_storage = lambda folder: {"source": "/dev/surprise", "uuid": "changed", "target": str(folder)}
    result = controller.storage_choices()
    assert result["choices"] == [] and "No supported" in result["warning"]


def test_discovery_failure_is_recoverable_warning(discovery):
    controller, _, _, _, _ = discovery
    controller.runner = lambda args: (_ for _ in ()).throw(host.HostError("local command failed"))
    result = controller.storage_choices()
    assert result["choices"] == [] and "Advanced" in result["warning"]


def selected_configuration(choices):
    data = next(c for c in choices if c["parent"] == "/mnt/data")
    backup = next(c for c in choices if c["parent"] == "/mnt/backup")
    return {"storage": {"mode": "drive", "data_root": "/mnt/data/david-pi-data", "backup_root": "/mnt/backup/david-pi-backups"}}, {"data": data["id"], "backup": backup["id"]}


def test_choice_identity_is_stable_across_capacity_changes_but_rechecks_space(discovery):
    controller, _, _, capacity, _ = discovery
    before = controller.storage_choices()["choices"]
    cfg, selection = selected_configuration(before)
    controller.validate_storage_selection(cfg, selection)
    capacity.free -= 1024**3
    assert [c["id"] for c in controller.storage_choices()["choices"]] == [c["id"] for c in before]
    capacity.free = 1024
    with pytest.raises(host.HostError, match="at least 1 GiB"):
        controller.validate_storage_selection(cfg, selection)


@pytest.mark.parametrize("change", ["uuid", "removed", "serial", "readonly"])
def test_selection_rejects_removed_or_replaced_device(discovery, change):
    controller, mounts, disks, _, _ = discovery
    cfg, selection = selected_configuration(controller.storage_choices()["choices"])
    if change == "uuid": mounts[1]["uuid"] = "replacement"
    if change == "removed": mounts.pop(1)
    if change == "serial": disks["blockdevices"][1]["serial"] = "replacement-disk"
    if change == "readonly": mounts[1]["options"] = "ro"
    with pytest.raises(host.HostError, match="missing or has changed"):
        controller.validate_storage_selection(cfg, selection)


@pytest.mark.parametrize("change", ["path", "mode", "backup", "extra"])
def test_selection_rejects_forged_path_or_mode(discovery, change):
    controller, _, _, _, _ = discovery
    cfg, selection = selected_configuration(controller.storage_choices()["choices"])
    if change == "path": cfg["storage"]["data_root"] = "/mnt/elsewhere/david-pi-data"
    if change == "mode": cfg["storage"]["mode"] = "folder"
    if change == "backup": cfg["storage"]["backup_root"] = None
    if change == "extra": selection["command"] = "format"
    with pytest.raises(host.HostError): controller.validate_storage_selection(cfg, selection)


def test_partitions_on_one_disk_are_not_offered_as_independent_backup(discovery):
    controller, mounts, disks, _, _ = discovery
    mounts[1]["source"], mounts[2]["source"] = "/dev/vdb1", "/dev/vdb2"
    disks["blockdevices"][1]["children"] = [
        {"name": f"/dev/vdb{index}", "type": "part", "pkname": "/dev/vdb", "maj:min": f"252:{16+index}"} for index in (1, 2)
    ]
    choices = controller.storage_choices()["choices"]
    cfg, selection = selected_configuration(choices)
    assert choices[0]["device_id"] == choices[1]["device_id"]
    with pytest.raises(host.HostError, match="same physical drive"):
        controller.validate_storage_selection(cfg, selection)


def test_advanced_manual_storage_does_not_require_discovery(discovery):
    controller, _, _, _, _ = discovery
    controller.runner = lambda args: (_ for _ in ()).throw(AssertionError("manual selection must use normal provisioning checks"))
    controller.validate_storage_selection({"storage": {}}, None)
    controller.validate_storage_selection({"storage": {}}, {})


def test_address_reads_only_actual_saved_origin_without_token_or_writes(tmp_path, capsys):
    missing = tmp_path / "unconfigured"
    with pytest.raises(host.HostError, match="No private address"):
        host.print_address(missing)
    assert not missing.exists()
    state = tmp_path / "host-state/setup.json"
    host.atomic_json(state, {"origin": "https://john-pi-2.example.ts.net", "token_hash": "secret-token", "session_hash": "secret-session"})
    before = state.read_bytes()
    host.print_address(tmp_path)
    output = capsys.readouterr().out
    assert "https://john-pi-2.example.ts.net/" in output
    assert "secret-" not in output and state.read_bytes() == before
    host.atomic_json(tmp_path / "installation.json", {"public_url": "https://current.example.ts.net"})
    assert host.saved_address(tmp_path) == "https://current.example.ts.net"
    host.atomic_json(tmp_path / "installation.json", {"public_url": "https://attacker.example/?token=secret"})
    with pytest.raises(host.HostError, match="invalid"):
        host.saved_address(tmp_path)


def test_resume_release_arguments_are_validated_without_shell_evaluation(tmp_path):
    host.atomic_json(tmp_path / "release.json", {"image": IMAGE, "repository": "example/david-pi"})
    assert host.saved_release_arguments(tmp_path) == (IMAGE, "example/david-pi")
    for value in ("$(touch /tmp/nope)", "other/david-pi", "example/david-pi\nEXTRA=1"):
        host.atomic_json(tmp_path / "release.json", {"image": IMAGE, "repository": value})
        with pytest.raises(host.HostError, match="No verified release"):
            host.saved_release_arguments(tmp_path)


def test_authenticated_setup_discovery_and_active_job_survive_configuration_write(tmp_path, monkeypatch):
    controller = host.Controller(tmp_path / "etc")
    session = "private-session"
    state = {"admin": "john@example.test", "origin": "https://john-pi.example.ts.net", "hostname": "john-pi", "instance_id": "example-id", "csrf": "private-csrf", "session_hash": hashlib.sha256(session.encode()).hexdigest(), "session_expires": time.time()+60}
    host.atomic_json(controller.state / "setup.json", state)
    job = {"id": "a"*32, "operation": "install", "state": "running", "phase": "starting selected services", "created_at": time.time()}
    host.atomic_json(controller.jobs / f"{job['id']}.json", job)
    host.atomic_json(controller.config_path, {"partway": True})
    calls = []
    monkeypatch.setattr(controller, "storage_choices", lambda: calls.append("discover") or {"choices": []})
    server = ThreadingHTTPServer(("127.0.0.1", 0), host.SetupHandler)
    server.controller = controller
    threading.Thread(target=server.serve_forever, daemon=True).start()
    def get(path, cookie=None):
        headers = {"Host": "john-pi.example.ts.net", "Tailscale-User-Login": "john@example.test"}
        if cookie: headers["Cookie"] = cookie
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        status, body = response.status, response.read()
        connection.close()
        return status, body
    try:
        assert get("/api/setup")[0] == 403 and calls == []
        status, body = get("/api/setup", "dp_setup="+session)
        value = json.loads(body)
        assert status == 200 and value["active_job"] == job
        assert "America/Denver" in value["timezones"] and "UTC" in value["timezones"]
        assert get("/")[0] == 200
        host.atomic_json(controller.state / "installed.json", {"completed": True})
        assert get("/")[0] == 503
    finally:
        server.shutdown()
        server.server_close()

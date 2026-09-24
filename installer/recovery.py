"""Local, verified replacement-machine recovery. Never creates a new household.

The host API is passed explicitly because host.py also runs as a CLI script.
Only the local root CLI calls preparation, drive mounting and restoration.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import time
import uuid
from urllib.parse import urlsplit


def _read(api, path, default=None):
    try:
        value = api.read_json(path, default)
    except (OSError, ValueError):
        raise api.HostError(f"Recovery metadata cannot be read at {path}; check the selected backup or verified installer. Existing content was preserved") from None
    if value is not None and not isinstance(value, dict):
        raise api.HostError(f"Recovery metadata is not a JSON object at {path}")
    return value


def verified_release(api):
    path = api.ROOT / "verified-release.json"
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 8192:
        raise api.HostError("Run the verified installer for the backup's exact release with --prepare-recovery first; verified release metadata is missing")
    record = _read(api, path)
    if not isinstance(record, dict) or not api.REPOSITORY.fullmatch(record.get("repository", "")):
        raise api.HostError("Invalid verified recovery release metadata")
    selection = record.get("selected_version", "")
    canonical = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-beta\.[1-9][0-9]*)?"
    if selection != "latest" and not re.fullmatch(canonical, selection):
        raise api.HostError("Recovery requires an exact published release selection")
    raw = record.get("manifest")
    if not isinstance(raw, str) or len(raw) > 4096:
        raise api.HostError("Invalid verified recovery release manifest")
    manifest = api.parse_manifest(raw, record["repository"], allow_prerelease="-beta." in selection)
    if selection != "latest" and manifest["VERSION"] != selection:
        raise api.HostError("Verified recovery release selection does not match its manifest")
    if (api.ROOT / "VERSION").read_text().strip() != manifest["VERSION"]:
        raise api.HostError("Recovery management code does not match its verified release")
    return {"version": manifest["VERSION"], "image": manifest["IMAGE"], "repository": record["repository"],
            "data_schema_version": int(manifest["DATA_SCHEMA_VERSION"]),
            "rollback_min_data_schema": int(manifest["ROLLBACK_MIN_DATA_SCHEMA"])}


def clean_target(controller, api):
    if controller.config_path.exists():
        raise api.HostError("Restore requires a clean installation; existing configuration and newer content were preserved. If this is an interrupted restored installation, run sudo david-pi repair")
    if any((controller.state / name).exists() for name in ("setup.json", "pending-install.json", "installed.json")):
        raise api.HostError("This machine already has new-home setup state. Use a clean replacement machine; do not delete that state to bypass recovery checks")
    if (controller.etc / "secrets").exists() and not (controller.state / "restore-progress.json").is_file():
        raise api.HostError("Existing keys were preserved. Use a clean replacement machine for recovery")
    for path in controller.jobs.glob("*.json"):
        if _read(api, path, {}).get("state") in {"queued", "running"}:
            raise api.HostError("A host operation is active; wait for it before recovery")


def preflight(controller, api):
    clean_target(controller, api)
    return verified_release(api)


def prepare(controller, api, admin, hostname):
    release = preflight(controller, api)
    if not re.fullmatch(r"[^\s@]+@[^\s@]+", admin) or len(admin) > 254:
        raise api.HostError("Enter the exact Tailscale account login")
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", hostname):
        raise api.HostError("Hostname must be a lowercase DNS label")
    status = json.loads(controller.runner(["tailscale", "status", "--json"]))
    if status.get("BackendState") != "Running" or api.setup_node_account(status) != admin.casefold():
        raise api.HostError("The replacement node must be connected using the exact account entered in this terminal")
    origin = api.validate_origin("https://" + status.get("Self", {}).get("DNSName", "").rstrip(".").lower())
    before = controller.inspect_private_root(origin)
    previous = _read(api, controller.state / "recovery.json", {})
    if previous and (previous.get("admin") != admin.casefold() or previous.get("origin") != origin):
        raise api.HostError("Recovery preparation already saved a different account or address; restore that connection before retrying")
    if previous:
        if previous.get("release") != release and (controller.state / "restore-progress.json").exists():
            raise api.HostError("An interrupted recovery already selected a different verified release; resume that saved release before changing recovery preparation")
        hostname = previous["hostname"]
    elif hostname != urlsplit(origin).hostname.split(".")[0] and before.get("Web"):
        raise api.HostError("This node already serves content. Keep its current hostname to preserve those addresses")
    if not previous:
        controller.runner(["tailscale", "set", "--hostname", hostname])
    print(f"\n2. Enable private HTTPS\n   Open https://console.tailscale.com/admin/dns using {admin}.\n   Under HTTPS Certificates, choose Enable HTTPS and review the confirmation.\n   If already enabled, continue. No new household or claim code will be created.", flush=True)
    input("   Press Enter after HTTPS Certificates is enabled: ")
    controller.runner(["systemctl", "enable", "david-pi-helper.service"])
    controller.runner(["systemctl", "restart", "david-pi-helper.service"])
    assigned, assigned_status, serve = api.fresh_setup_private_root(controller, hostname, status)
    record = {"mode": "recovery", "admin": admin.casefold(), "origin": assigned,
              "hostname": urlsplit(assigned).hostname.split(".")[0],
              "node_id": assigned_status.get("Self", {}).get("ID"), "release": release, "prepared_at": time.time()}
    api.atomic_json(controller.state / "recovery.json", record)
    api.atomic_json(controller.state / "tailscale-serve.before.json", serve)
    print(f"\n3. Recovery machine prepared\n   Private address: {assigned}/\n   Return to this terminal and run: sudo david-pi restore\n   You will choose the backup and an empty destination from detected ext4 drives.\n   This address shows recovery guidance until restored services pass verification.", flush=True)
    return record


def prepared_origin(controller, api, release):
    record = _read(api, controller.state / "recovery.json", {})
    if record.get("mode") != "recovery" or record.get("release") != release:
        raise api.HostError("Run sudo david-pi prepare-recovery using the backup's verified release before restoring")
    status = json.loads(controller.runner(["tailscale", "status", "--json"]))
    if (status.get("BackendState") != "Running" or status.get("Self", {}).get("ID") != record.get("node_id")
            or api.setup_node_account(status) != record.get("admin")):
        raise api.HostError("The prepared Tailscale node or account changed; restore its original connection before recovery")
    controller.private_origin(record["origin"])
    controller.inspect_private_root(record["origin"])
    controller.runner(["systemctl", "is-active", "--quiet", "david-pi-helper.service"])
    return record["origin"]


def inventory(controller, api):
    raw = json.loads(controller.runner(["lsblk", "--json", "--bytes", "--paths", "--output", "NAME,TYPE,SIZE,FSTYPE,UUID,LABEL,MOUNTPOINTS"]))
    devices = controller.storage_devices()
    nodes = []
    def collect(items):
        for item in items:
            nodes.append(item)
            collect(item.get("children", []))
    collect(raw.get("blockdevices", []))
    candidates = []
    for item in nodes:
        if (item.get("name") not in devices or item.get("fstype") != "ext4"
                or not re.fullmatch(r"[A-Fa-f0-9-]{16,64}", item.get("uuid") or "")):
            continue
        if sum(n.get("uuid") == item["uuid"] for n in nodes) != 1:
            continue
        mounts = sorted({m for m in item.get("mountpoints", []) if m})
        if len(mounts) > 1:
            continue
        parent = Path("/srv" if mounts == ["/"] else mounts[0]) if mounts else None
        if parent:
            try:
                api.safe_path(parent)
                info = controller.inspect_storage(parent)
                if info["uuid"] != item["uuid"] or info.get("fsroot", "/") != "/":
                    continue
            except (api.HostError, OSError):
                continue
        label = re.sub(r"[\x00-\x1f\x7f]", "", str(item.get("label") or "Prepared ext4 drive"))[:100]
        candidates.append({"device": item["name"], "uuid": item["uuid"], "device_id": devices[item["name"]],
                           "label": label, "size": int(item["size"]), "parent": str(parent) if parent else None})
    return candidates


def choose(items, describe, prompt, api):
    if not items:
        raise api.HostError("No supported choices found. Attach a prepared local ext4 drive and retry; see the recovery and storage guides")
    for index, item in enumerate(items, 1):
        print(f"   {index}. {describe(item)}", flush=True)
    reply = input(prompt).strip()
    if not reply.isdecimal() or not 1 <= int(reply) <= len(items):
        raise api.HostError("Choose one of the numbered options; nothing was restored")
    return items[int(reply)-1]


def select_drive(controller, api, *, backup, exclude_device=None):
    choices = [item for item in inventory(controller, api) if item["device_id"] != exclude_device]
    def label(item):
        capacity = f"{item['size']/1024**3:.1f} GiB total"
        if item["parent"]:
            capacity += f", {shutil.disk_usage(item['parent']).free/1024**3:.1f} GiB free"
        return f"{item['label']} — {item['parent'] or item['device'] + ' (not mounted)'} — {capacity}"
    selected = choose(choices, label, "Choose backup drive number: " if backup else "Choose replacement data drive number: ", api)
    # Device and mount identity are checked again after the person decides.
    if selected not in inventory(controller, api):
        raise api.HostError("The selected drive changed or disappeared. Reconnect it and retry")
    if selected["parent"]:
        parent = Path(selected["parent"])
    else:
        parent = Path("/mnt") / ("david-pi-recovery-" + selected["uuid"].lower())
        if parent.exists() or parent.is_symlink():
            raise api.HostError("Recovery mount location already exists; mount the selected drive at an unused location with OS tools and retry")
        parent.mkdir(mode=0o755)
        try:
            controller.runner(["mount", "-t", "ext4", "-o", "ro" if backup else "rw", "UUID=" + selected["uuid"], str(parent)])
        except Exception:
            # Remove only the empty mount point just created by this operation.
            # rmdir refuses new files; never remove a pre-existing directory.
            try:
                parent.rmdir()
            except OSError:
                pass
            raise
    info = controller.inspect_storage(parent)
    if info["uuid"] != selected["uuid"]:
        raise api.HostError("Mounted filesystem identity does not match the selected drive")
    if not backup and os.statvfs(parent).f_flag & os.ST_RDONLY:
        raise api.HostError("Replacement data drive is read-only; prepare a writable destination with OS tools")
    return parent, selected


def guided_paths(controller, api):
    previous = _read(api, controller.state / "restore-progress.json", {})
    if previous:
        print(f"Resume recovery from: {previous.get('snapshot')}\nDestination: {previous.get('data_root')}", flush=True)
        if input("Type RESUME to verify and continue that same recovery: ").strip() != "RESUME":
            raise api.HostError("Recovery resume cancelled; saved data and keys were preserved")
        return Path(previous["snapshot"]), Path(previous["data_root"])
    print("\nChoose the existing backup drive. An unmounted drive is mounted read-only; no drive is formatted.", flush=True)
    parent, backup = select_drive(controller, api, backup=True)
    folder = parent / "david-pi-backups"
    snapshots = []
    if folder.is_dir() and not folder.is_symlink():
        for path in sorted(folder.iterdir(), reverse=True):
            if len(snapshots) >= 100:
                break
            if path.is_dir() and not path.is_symlink() and (path / "snapshot.json").is_file():
                metadata = _read(api, path / "snapshot.json", {})
                if metadata.get("complete"):
                    snapshots.append(path)
    def snapshot_label(path):
        metadata = _read(api, path / "snapshot.json", {})
        created = metadata.get("created_at")
        date = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(created)) if isinstance(created, (int, float)) else "date unavailable"
        version = metadata.get("release", {}).get("version", "version checked before restore")
        return f"{date} — {version} — {path.name}"
    source = choose(snapshots, snapshot_label, "Choose completed snapshot number: ", api)
    print("\nChoose a different physical drive for restored data. Only its empty david-pi-data folder will be used.", flush=True)
    data, _ = select_drive(controller, api, backup=False, exclude_device=backup["device_id"])
    target = data / "david-pi-data"
    print(f"\nRestore from: {source}\nRestore into: {target}\nThe saved household identity, membership and keys will be retained.", flush=True)
    if input("Type RESTORE to continue: ").strip() != "RESTORE":
        raise api.HostError("Restore cancelled; no household data was copied")
    return source, target


def inspect_snapshot(source, api):
    source = Path(source)
    if not source.is_absolute() or ".." in source.parts or any(p.is_symlink() for p in [source, *source.parents]) or not source.is_dir():
        raise api.HostError("Choose a completed local snapshot directory without symbolic links")
    paths = list(source.rglob("*"))
    if any(not (stat.S_ISREG(p.lstat().st_mode) or stat.S_ISDIR(p.lstat().st_mode)) for p in paths):
        raise api.HostError("Snapshot contains a symbolic link or unsupported special file")
    manifest = _read(api, source / "snapshot.json", {})
    if not manifest.get("complete") or not isinstance(manifest.get("files"), dict):
        raise api.HostError("Snapshot is incomplete")
    actual = {str(p.relative_to(source)) for p in paths if p.is_file() and p != source / "snapshot.json"}
    if actual != set(manifest["files"]):
        raise api.HostError("Snapshot file inventory does not match its manifest")
    for name, expected in manifest["files"].items():
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or not re.fullmatch(r"[a-f0-9]{64}", str(expected)):
            raise api.HostError("Unsafe snapshot path or checksum")
        with (source / relative).open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                raise api.HostError("Snapshot checksum mismatch")
    for required in ("installation.json", "release.json", "secrets/chat-master.key", "data/.david-pi-storage"):
        if required not in manifest["files"]:
            raise api.HostError("Snapshot is missing recovery configuration, storage identity or keys")
    cfg = api.validate_installation(_read(api, source / "installation.json"))
    if cfg["instance_id"] != manifest.get("instance_id") or (source / "data/.david-pi-storage").read_text().strip() != cfg["instance_id"]:
        raise api.HostError("Snapshot identity mismatch")
    for original in [source / "data", *(source / "data").rglob("*")]:
        metadata = manifest.get("data_ownership", {}).get(str(original.relative_to(source / "data")))
        if (not isinstance(metadata, list) or len(metadata) != 3 or any(type(v) is not int for v in metadata)
                or metadata[0] not in {0, 10001, 10002} or metadata[1] not in {0, 10001} or not 0 <= metadata[2] <= 0o777):
            raise api.HostError("Snapshot lacks supported data ownership metadata")
    return cfg, manifest


def restore(controller, api, snapshot, data_root):
    release = preflight(controller, api)
    origin = prepared_origin(controller, api, release)
    source = Path(snapshot)
    cfg, manifest = inspect_snapshot(source, api)
    saved_release = _read(api, source / "release.json")
    if saved_release != release:
        version = saved_release.get("version", "") if isinstance(saved_release, dict) else ""
        hint = f"v{version}" if api.VERSION.fullmatch(version) else "the backup's exact release"
        raise api.HostError(f"Backup release does not match the verified recovery installer. Prepare a clean target using {hint}; no data was copied")
    target = api.safe_path(data_root, exists=False)
    api.safe_path(target.parent)
    journal_path = controller.state / "restore-progress.json"
    journal = _read(api, journal_path, {})
    identity = {"snapshot": str(source), "data_root": str(target), "instance_id": cfg["instance_id"],
                "manifest_sha256": hashlib.sha256((source / "snapshot.json").read_bytes()).hexdigest(), "release": release}
    if journal and any(journal.get(key) != value for key, value in identity.items()):
        raise api.HostError("An interrupted recovery owns a different snapshot or destination. Resume its exact saved paths; existing content and keys were preserved")
    if not journal and target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise api.HostError("Recovery destination must be empty; existing content was preserved")
    if source == target or source.is_relative_to(target) or target.is_relative_to(source):
        raise api.HostError("Recovery source and destination must not overlap")
    info = controller.inspect_storage(target.parent)
    source_info = controller.inspect_storage(source)
    devices = controller.storage_devices()
    if not devices.get(info["source"]) or not devices.get(source_info["source"]):
        raise api.HostError("Recovery requires identifiable ordinary local ext4 storage devices")
    if manifest.get("independent") is True and (source_info["uuid"] == info["uuid"] or devices[source_info["source"]] == devices[info["source"]]):
        raise api.HostError("Recover onto a different physical drive so the independent backup stays intact")
    cfg["storage"].update(data_root=str(target), mode="drive" if info["target"] != "/" and target.parent == Path(info["target"]) else "folder",
                          backup_root=None, update_snapshot_root=None)
    cfg.update(public_url=origin, hostname=urlsplit(origin).hostname.split(".")[0])
    cfg = api.validate_installation(cfg)
    identifier = journal.get("stage_id") if journal else uuid.uuid4().hex
    if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{32}", identifier):
        raise api.HostError("Recovery journal lacks a safe staging identity; preserve its files for local review")
    stage = target.parent / (".david-pi-restore-" + identifier)
    key_stage = controller.etc / (".restore-secrets-" + identifier)

    def discard_incomplete(path, prefix):
        if not path.exists() and not path.is_symlink():
            return
        metadata = path.lstat()
        record = journal.get("staging", {}).get(prefix)
        if not stat.S_ISDIR(metadata.st_mode):
            raise api.HostError("Recovery staging location changed type; existing files were preserved")
        if record is None:
            # An interruption between mkdir and saving its inode can leave only
            # an empty root-owned directory: no copy starts before that save.
            if metadata.st_uid == os.geteuid() and stat.S_IMODE(metadata.st_mode) == 0o700 and not any(path.iterdir()):
                path.rmdir()
                return
            raise api.HostError("Recovery staging ownership is unrecorded; existing files were preserved")
        if record != {"device": metadata.st_dev, "inode": metadata.st_ino}:
            raise api.HostError("Recovery staging filesystem identity changed; existing files were preserved")
        allowed = {str(p.relative_to(source / prefix)) for p in (source / prefix).rglob("*")}
        for item in path.rglob("*"):
            item_stat = item.lstat()
            if (str(item.relative_to(path)) not in allowed or item_stat.st_dev != metadata.st_dev
                    or not (stat.S_ISDIR(item_stat.st_mode) or stat.S_ISREG(item_stat.st_mode))):
                raise api.HostError("Recovery staging contains unknown files or links; they were preserved for local review")
        # This is only the inode recorded before this restore started copying,
        # outside both committed destination and backup. Free its partial copy
        # before measuring space, rather than accumulating copies on every retry.
        shutil.rmtree(path)

    if journal:
        discard_incomplete(stage, "data")
        discard_incomplete(key_stage, "secrets")
    total = sum(p.stat().st_size for p in (source / "data").rglob("*") if p.is_file())
    required = 1024**3 if journal and target.is_dir() and any(target.iterdir()) else total + 1024**3
    if shutil.disk_usage(target.parent).free < required:
        raise api.HostError("Recovery storage needs the content still to copy plus 1 GiB reserve")
    controller.runner(["docker", "pull", release["image"]], timeout=900)
    # The snapshot cannot redirect the verified immutable image selection.
    clean_target(controller, api)
    if controller.inspect_storage(target.parent) != info or controller.inspect_storage(source) != source_info:
        raise api.HostError("Recovery storage changed during image preparation; reconnect the selected drives")
    if shutil.disk_usage(target.parent).free < required:
        raise api.HostError("Recovery space changed during image preparation; free space for the remaining copy plus 1 GiB before retrying")
    if not journal:
        journal = {**identity, "stage_id": identifier, "staging": {}, "started_at": time.time()}
        api.atomic_json(journal_path, journal)

    def start_staging(path, prefix):
        path.mkdir(mode=0o700)
        metadata = path.stat()
        journal.setdefault("staging", {})[prefix] = {"device": metadata.st_dev, "inode": metadata.st_ino}
        api.atomic_json(journal_path, journal)

    def verify_tree(path, prefix):
        if path.is_symlink() or not path.is_dir():
            raise api.HostError("Recovery copy is not a dedicated directory")
        paths = list(path.rglob("*"))
        if any(not (stat.S_ISREG(p.lstat().st_mode) or stat.S_ISDIR(p.lstat().st_mode)) for p in paths):
            raise api.HostError("Recovery copy contains an unsupported file")
        expected = {name[len(prefix)+1:]: digest for name, digest in manifest["files"].items() if name.startswith(prefix + "/")}
        actual = {str(p.relative_to(path)) for p in paths if p.is_file()}
        if actual != set(expected):
            raise api.HostError("Recovery copy inventory changed; existing content was preserved for local review")
        for name, digest in expected.items():
            with (path / name).open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                    raise api.HostError("Recovery copy checksum changed; existing content was preserved for local review")

    print("Copying and verifying the selected recovery point. Keep this terminal open.", flush=True)
    try:
        # Whole-directory atomic commits make interruption distinguishable from
        # unrelated partial content. Only the exact journaled recovery resumes.
        if target.exists() and any(target.iterdir()):
            verify_tree(target, "data")
        else:
            start_staging(stage, "data")
            shutil.copytree(source / "data", stage, dirs_exist_ok=True)
            verify_tree(stage, "data")
            for original in [source / "data", *(source / "data").rglob("*")]:
                relative = original.relative_to(source / "data")
                uid, gid, mode = manifest["data_ownership"][str(relative)]
                os.chown(stage / relative, uid, gid)
                os.chmod(stage / relative, mode)
            if target.exists():
                target.rmdir()  # refuses any content added during the copy
            stage.rename(target)
        keys = controller.etc / "secrets"
        if keys.exists():
            verify_tree(keys, "secrets")
        else:
            start_staging(key_stage, "secrets")
            shutil.copytree(source / "secrets", key_stage, dirs_exist_ok=True)
            verify_tree(key_stage, "secrets")
            os.chmod(key_stage, 0o700)
            for path in key_stage.rglob("*"):
                os.chmod(path, 0o700 if path.is_dir() else 0o600)
            for name in ("chat-master.key", "chat-vapid-private.pem"):
                key = key_stage / name
                if key.exists():
                    os.chown(key, 0, 10001)
                    os.chmod(key, 0o440)
            key_stage.rename(keys)
        controller.provision_storage(cfg)
        api.atomic_json(controller.etc / "release.json", release)
        controller.save_config(cfg)
        controller.write_runtime(cfg, release["image"])
        api.atomic_json(controller.state / "restored.json", {"snapshot_id": source.name, "instance_id": cfg["instance_id"], "restored_at": time.time(), "application_verified": False})
        journal_path.unlink()
    except Exception as error:
        instruction = ("Run sudo david-pi repair; configuration is saved, so restore will not copy older data again."
                       if controller.config_path.exists() else "Run sudo david-pi restore to verify and resume this exact recovery. Do not delete its saved state or keys.")
        raise api.HostError(f"Recovery stopped; original backup preserved. {instruction} Incomplete private copies, if present, remain at {stage} and {key_stage}. Detail: {error}") from None
    return {"restored": True, "public_url": origin, "instance_id": cfg["instance_id"],
            "next": "Run sudo david-pi repair, follow sudo david-pi status until complete, then sudo david-pi verify. Verify actual household content and reconnect Android to this same installation. Routine backup is not configured; select its destination again after verification."}

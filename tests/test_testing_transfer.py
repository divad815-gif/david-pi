import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import testing_transfer as transfer
from testing_artifacts import ReleaseError


def fixture(tmp_path):
    identity = {"revision": "a" * 40, "version": "10.0.0-beta.1", "source_sha256": "b" * 64}
    record = {**identity, "schema_version": 1, "kind": "private-testing-candidate", "repository": "Example/david-pi",
              "created_at": "2026-09-23T00:00:00Z", "python_base": "python@sha256:" + "c" * 64,
              "android_apk_sha256": "d" * 64, "android_source_sha256": "e" * 64,
              "limitations": ["Synthetic unit fixture; never publication evidence"], "architectures": {}}
    files = {}
    for name in transfer.transfer_names(identity["version"]) - {"candidate.json", "evidence.json"}:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"unit fixture " + name.encode())
        files[name] = transfer.sha(path)
    for arch in transfer.ARCHITECTURES:
        record["architectures"][arch] = {"oci": f"{arch}/image.oci.tar", "status": "pass", "test_log_sha256": "f" * 64}
    (tmp_path / "private-build.log").write_text("PRIVATE ORIGINAL LOG CONTENT")
    files["private-build.log"] = transfer.sha(tmp_path / "private-build.log")
    record["files"] = files
    (tmp_path / "candidate.json").write_text(json.dumps(record))
    evidence = tmp_path / "reviewed.json"
    evidence.write_text('{"unit_fixture":true}')
    args = SimpleNamespace(output=tmp_path, private_markers=None, evidence=evidence)
    return args, record, identity


def test_transfer_contains_only_sanitized_allowlist_and_repeats_exactly(tmp_path):
    args, record, identity = fixture(tmp_path)
    bundle = transfer.pack_transfer(args, record)
    digest = transfer.sha(bundle)
    assert transfer.pack_transfer(args, record) == bundle
    assert transfer.sha(bundle) == digest
    with tarfile.open(bundle) as archive:
        assert set(archive.getnames()) == transfer.transfer_names(identity["version"])
        assert all(entry.isfile() and not entry.uname and not entry.gname for entry in archive)
        assert b"PRIVATE ORIGINAL LOG CONTENT" not in bundle.read_bytes()
    extracted = transfer.extract_transfer(bundle, tmp_path / "received", digest, identity)
    assert extracted["kind"] == "testing-candidate-transfer"
    assert "private-build.log" not in extracted["files"]
    assert extracted["local_candidate_receipt_sha256"] == transfer.sha(tmp_path / "candidate.json")


@pytest.mark.parametrize("problem", ["checksum", "source", "extra", "symlink", "tampered"])
def test_transfer_rejects_wrong_source_hash_members_and_unsafe_files(tmp_path, problem):
    args, record, identity = fixture(tmp_path)
    bundle = transfer.pack_transfer(args, record)
    digest = transfer.sha(bundle)
    if problem == "checksum":
        digest = "0" * 64
    elif problem == "source":
        identity["revision"] = "0" * 40
    else:
        with tarfile.open(bundle) as original:
            members = [(entry.name, original.extractfile(entry).read()) for entry in original]
        with tarfile.open(bundle, "w") as archive:
            for name, data in members:
                if problem == "tampered" and name == "evidence.json":
                    data += b"changed"
                entry = tarfile.TarInfo(name)
                entry.size = len(data)
                if problem == "symlink" and name == "evidence.json":
                    entry.type = tarfile.SYMTYPE
                    entry.linkname = "/outside"
                    entry.size = 0
                archive.addfile(entry, io.BytesIO(data))
            if problem == "extra":
                archive.addfile(tarfile.TarInfo("extra-secret.bin"), io.BytesIO())
        digest = transfer.sha(bundle)
    with pytest.raises(ReleaseError):
        transfer.extract_transfer(bundle, tmp_path / "received", digest, identity)
    assert not (tmp_path / "received").exists()


def test_failed_local_acceptance_cannot_stage_a_draft(monkeypatch):
    def failed(args):
        raise ReleaseError("VM checks pending")
    monkeypatch.setattr(transfer, "candidate", failed)
    monkeypatch.setattr(transfer, "command", lambda *args, **kwargs: pytest.fail("gate failure reached GitHub"))
    with pytest.raises(ReleaseError, match="VM checks pending"):
        transfer.stage(SimpleNamespace())


def test_tag_requires_exact_annotated_source_and_checksum(monkeypatch):
    identity = {"revision": "a" * 40, "version": "10.0.0-beta.1", "source_sha256": "b" * 64}
    note = {"kind": "testing-release-transfer", "version": identity["version"], "revision": identity["revision"], "transfer_sha256": "c" * 64}
    monkeypatch.setattr(transfer, "checkout", lambda: identity)
    def run(*args):
        if args[1:3] == ("cat-file", "-t"):
            return "tag"
        if args[1] == "rev-parse":
            return identity["revision"]
        return "object " + identity["revision"] + "\ntype commit\ntag v10.0.0-beta.1\n\n" + json.dumps(note)
    monkeypatch.setattr(transfer, "command", run)
    assert transfer.tag_identity("v10.0.0-beta.1") == (identity, note)
    with pytest.raises(ReleaseError):
        transfer.tag_identity("v10.0.0")
    note["transfer_sha256"] = "bad"
    with pytest.raises(ReleaseError, match="checksum"):
        transfer.tag_identity("v10.0.0-beta.1")

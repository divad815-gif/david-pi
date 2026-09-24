import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import testing_artifacts as artifacts
import testing_release as release


def tar_bytes(files):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, data in files.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))
    return output.getvalue()


def oci_fixture(tmp_path, *, corrupt=None, payload=b"synthetic fixture"):
    blobs = {}

    def blob(data):
        if isinstance(data, dict):
            data = json.dumps(data).encode()
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        blobs["blobs/sha256/" + digest.split(":")[1]] = data
        return digest

    layer_bytes = tar_bytes({"app/fixture.txt": payload})
    layer = {"mediaType": "application/vnd.oci.image.layer.v1.tar", "digest": blob(layer_bytes), "size": len(layer_bytes)}
    config_digest = blob({"architecture": "amd64", "os": "linux", "rootfs": {"type": "layers", "diff_ids": ["sha256:" + "f" * 64 if corrupt == "diff-id" else layer["digest"]]}})
    manifest = {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "config": {"digest": config_digest}, "layers": [layer]}
    manifest_digest = blob(manifest)
    index = {"schemaVersion": 2, "manifests": [{"digest": manifest_digest}]}
    if corrupt == "blob":
        blobs["blobs/sha256/" + layer["digest"].split(":")[1]] += b"tampered"
    if corrupt == "duplicate-platform":
        second = copy.deepcopy(manifest)
        second["annotations"] = {"fixture": "second platform"}
        index["manifests"].append({"digest": blob(second)})
    path = tmp_path / "fixture.oci.tar"
    path.write_bytes(tar_bytes({**blobs, "index.json": json.dumps(index).encode(), "oci-layout": b'{"imageLayoutVersion":"1.0.0"}'}))
    return path, manifest, manifest_digest


def test_actual_oci_bytes_bind_manifest_config_compressed_and_uncompressed_layers(tmp_path):
    path, manifest, digest = oci_fixture(tmp_path)
    result = artifacts.verify_oci(path, "amd64", [b"not-present"])
    assert result["manifest_digest"] == digest
    assert result["config_digest"] == manifest["config"]["digest"]
    assert result["layers"] == manifest["layers"]
    assert result["regular_files_screened"] == 1
    artifacts.remote_platform(manifest, "amd64", result)


@pytest.mark.parametrize("corrupt,match", [("blob", "checksum"), ("diff-id", "uncompressed"), ("duplicate-platform", "exactly one")])
def test_oci_rejects_tampered_or_ambiguous_content(tmp_path, corrupt, match):
    path, _, _ = oci_fixture(tmp_path, corrupt=corrupt)
    with pytest.raises(artifacts.ReleaseError, match=match):
        artifacts.verify_oci(path, "amd64")


def test_oci_screen_rejects_private_content_without_echoing_it(tmp_path):
    marker = b"unit-only-private-marker"
    path, _, _ = oci_fixture(tmp_path, payload=marker)
    with pytest.raises(artifacts.ReleaseError) as error:
        artifacts.verify_oci(path, "amd64", [marker])
    assert "Private marker" in str(error.value)
    assert marker.decode() not in str(error.value)


def test_generated_home_marker_matches_actual_paths_without_prefix_collisions(tmp_path, monkeypatch):
    monkeypatch.setattr(artifacts.Path, "home", lambda: Path("/home/fixture"))
    markers = artifacts.private_markers(None, tmp_path)
    artifacts.screen_stream(io.BytesIO(b"/mnt/home/fixture-pi-data"), markers)
    with pytest.raises(artifacts.ReleaseError, match="Private marker"):
        artifacts.screen_stream(io.BytesIO(b"/home/fixture/private-build/tool"), markers)


@pytest.mark.parametrize("field", ["config", "layers"])
def test_pushed_image_must_match_tested_manifest_content(tmp_path, field):
    path, manifest, _ = oci_fixture(tmp_path)
    expected = artifacts.verify_oci(path, "amd64")
    observed = copy.deepcopy(manifest)
    if field == "config":
        observed["config"]["digest"] = "sha256:" + "e" * 64
    else:
        observed["layers"][0]["size"] += 1
    with pytest.raises(artifacts.ReleaseError, match="Remote image"):
        artifacts.remote_platform(observed, "amd64", expected)


@pytest.mark.parametrize("problem", ["extra", "duplicate", "missing", "digest"])
def test_multiarch_index_cannot_relabel_wrong_platforms(problem):
    expected = {"amd64": "sha256:" + "a" * 64, "arm64": "sha256:" + "b" * 64}
    doc = {"digest": "sha256:" + "c" * 64, "manifests": [{"digest": digest, "platform": {"os": "linux", "architecture": arch}} for arch, digest in expected.items()]}
    assert artifacts.remote_index(doc, expected) == doc["digest"]
    if problem == "extra":
        doc["manifests"].append({"digest": "sha256:" + "e" * 64, "platform": {"os": "linux", "architecture": "unknown"}})
    elif problem == "duplicate":
        doc["manifests"].append(doc["manifests"][0])
    elif problem == "missing":
        doc["manifests"].pop()
    else:
        doc["manifests"][0]["digest"] = "sha256:" + "f" * 64
    with pytest.raises(artifacts.ReleaseError):
        artifacts.remote_index(doc, expected)


def test_publication_dry_run_makes_no_remote_calls(monkeypatch, capsys):
    record = {"version": "10.0.0-beta.1", "revision": "a" * 40, "android_apk_sha256": "b" * 64, "repository": "Example/david-pi"}
    monkeypatch.setattr(release, "candidate", lambda args: (record, {}))
    monkeypatch.setattr(release, "command", lambda *args, **kwargs: pytest.fail("dry run performed a remote command"))
    monkeypatch.setattr(release.subprocess, "run", lambda *args, **kwargs: pytest.fail("dry run invoked a subprocess"))
    release.publish(SimpleNamespace(repository="Example/david-pi", execute=False))
    result = json.loads(capsys.readouterr().out)
    assert result["prerelease"] is True and result["latest"] is False
    assert result["remote_writes"] is False
    assert all(":latest" not in tag for tag in result["image_tags"])


def test_failed_acceptance_blocks_every_publication_command(monkeypatch):
    def failed(args):
        raise artifacts.ReleaseError("missing actual VM receipts")
    monkeypatch.setattr(release, "candidate", failed)
    monkeypatch.setattr(release, "command", lambda *args, **kwargs: pytest.fail("gate failure reached a publication command"))
    with pytest.raises(artifacts.ReleaseError, match="VM receipts"):
        release.publish(SimpleNamespace(repository="Example/david-pi", execute=True))


def test_source_archive_requires_complete_exact_inventory(tmp_path):
    import gzip
    path = tmp_path / "source.tar.gz"
    files = {"VERSION": hashlib.sha256(b"10.0.0-beta.1\n").hexdigest()}
    path.write_bytes(gzip.compress(tar_bytes({"david-pi-10.0.0-beta.1/VERSION": b"10.0.0-beta.1\n"})))
    artifacts.verify_source_archive(path, "10.0.0-beta.1", files)
    for changed in ({}, {**files, "missing.py": "a" * 64}, {"VERSION": "f" * 64}):
        with pytest.raises(artifacts.ReleaseError):
            artifacts.verify_source_archive(path, "10.0.0-beta.1", changed)


@pytest.mark.parametrize("problem", ["extra-apk", "extra-key", "directory", "symlink", "changed", "missing"])
def test_only_exact_regular_allowlisted_assets_can_be_uploaded(tmp_path, problem):
    path = tmp_path / "source.tar.gz"
    path.write_bytes(b"verified fixture")
    expected = {path.name: artifacts.sha(path)}
    artifacts.verify_assets(tmp_path, expected)
    if problem.startswith("extra"):
        (tmp_path / ("stray.apk" if problem == "extra-apk" else "stray.jks")).write_bytes(b"unreviewed bytes")
    elif problem == "directory":
        path.unlink()
        path.mkdir()
    elif problem == "symlink":
        path.unlink()
        path.symlink_to("elsewhere")
    elif problem == "changed":
        path.write_bytes(b"changed bytes")
    else:
        path.unlink()
    with pytest.raises(artifacts.ReleaseError):
        artifacts.verify_assets(tmp_path, expected)

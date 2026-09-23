import importlib.util
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("public_release_privacy", ROOT / "scripts/check-public-release.py")
scanner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scanner)


@pytest.fixture
def repository(tmp_path, monkeypatch):
    root = tmp_path / "public"
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    monkeypatch.setattr(scanner, "ROOT", root)
    return root


@pytest.mark.parametrize("split_literal", [False, True])
def test_external_private_markers_reject_content_without_echoing_values(repository, tmp_path, capsys, split_literal):
    marker = "Private Fixture Person"
    denylist = tmp_path / "local-markers.txt"
    denylist.write_text(marker + "\n")
    value = 'owner = "Private Fixture" + " Person"\n' if split_literal else marker.lower()
    (repository / "app.py").write_text(value)

    assert scanner.main(["--private-markers", str(denylist)]) == 1
    output = capsys.readouterr()
    assert "private marker in app.py" in output.err
    assert marker.casefold() not in (output.out + output.err).casefold()
    assert str(denylist) not in output.err


def test_private_markers_are_optional_and_do_not_block_unrelated_public_content(repository, tmp_path, capsys):
    (repository / "README.md").write_text("Public installation instructions\n")
    assert scanner.main([]) == 0
    denylist = tmp_path / "local-markers.txt"
    denylist.write_text("Private Fixture Person\n")
    assert scanner.main(["--private-markers", str(denylist)]) == 0
    assert "Private Fixture Person" not in capsys.readouterr().out


@pytest.mark.parametrize("kind", ["missing", "empty", "invalid-utf8", "public"])
def test_invalid_or_public_denylist_fails_without_echoing_private_details(repository, tmp_path, capsys, kind):
    denylist = (repository if kind == "public" else tmp_path) / "private-fixture-location.txt"
    if kind == "empty":
        denylist.write_text("\n  \n")
    elif kind == "invalid-utf8":
        denylist.write_bytes(bytes([255]))
    elif kind == "public":
        denylist.write_text("Private Fixture Person\n")
    assert scanner.main(["--private-markers", str(denylist)]) == 1
    output = capsys.readouterr()
    assert "private marker file" in output.err
    assert str(denylist) not in output.err
    assert "Private Fixture Person" not in output.err


def test_generic_private_key_detection_stays_enabled_without_a_denylist(repository, capsys):
    (repository / "app.py").write_text("BEGIN OPENSSH" + " PRIVATE KEY\n")
    assert scanner.main([]) == 1
    assert "credential marker in app.py" in capsys.readouterr().err

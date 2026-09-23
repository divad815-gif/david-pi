import hashlib
import importlib.util
import json
import sqlite3
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "deploy" / "david_pi_ownership_inventory.py"
SPEC = importlib.util.spec_from_file_location("ownership_inventory", SOURCE)
inventory = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = inventory
SPEC.loader.exec_module(inventory)


def database_digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_photo_clone(path):
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE photos(
          id TEXT PRIMARY KEY, owner_id TEXT, visibility TEXT, deleted_at TEXT,
          stored_path TEXT, preview_name TEXT, thumb_name TEXT, title TEXT
        );
        CREATE TABLE collections(
          id TEXT PRIMARY KEY, owner_id TEXT, visibility TEXT, created_by TEXT, title TEXT
        );
        CREATE TABLE collection_photos(collection_id TEXT, photo_id TEXT);
        """
    )
    connection.executemany(
        "INSERT INTO photos VALUES(?,?,?,?,?,?,?,?)",
        [
            ("p1", "david@example.test", "shared", None, "secret-david.jpg", "p1", "t1", "Private birthday"),
            ("p2", "diana@example.test", "private", "2026-01-01T00:00:00Z", "secret-diana.jpg", "p2", "t2", "Hidden title"),
            ("p3", None, "shared", None, "legacy-secret.jpg", "p3", "t3", "Legacy title"),
            ("p4", "unknown@example.test", "unexpected", None, "shared-object.jpg", "p4", "t4", "Other owner"),
            ("p5", "david@example.test", "shared", None, "shared-object.jpg", "p5", "t5", "Duplicate object"),
        ],
    )
    connection.executemany(
        "INSERT INTO collections VALUES(?,?,?,?,?)",
        [
            ("c1", "david@example.test", "shared", "David", "Secret collection"),
            ("c2", None, "shared", "home", "Legacy collection"),
        ],
    )
    connection.executemany(
        "INSERT INTO collection_photos VALUES(?,?)",
        [("c1", "p1"), ("c1", "p2"), ("missing", "p1"), ("c1", "missing")],
    )
    connection.commit()
    connection.close()


def test_inventory_is_read_only_deterministic_and_content_neutral():
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "photos.db"
        build_photo_clone(path)
        before = database_digest(path)
        principals = {"david": "david@example.test", "diana": "diana@example.test"}
        first = inventory.build_inventory({"photos": path}, principals)
        second = inventory.build_inventory({"photos": path}, principals)
        after = database_digest(path)

        assert first == second
        assert before == after
        assert not path.with_name("photos.db-wal").exists()
        assert not path.with_name("photos.db-shm").exists()
        photo = first["domains"]["photos"]["resources"]["photos"]
        assert photo["rows"] == {"total": 5, "active": 4, "deleted": 1}
        assert photo["ownership"] == {
            "column": "owner_id",
            "legacy_unclaimed": 1,
            "recognized": {"david": 2, "diana": 1},
            "other_nonempty": 1,
        }
        assert photo["visibility"] == {"shared": 3, "private": 1, "unset": 0, "other": 1}
        assert photo["object_references"]["stored_path"] == {
            "populated": 5, "distinct": 4, "duplicate_references": 1,
        }
        relation = first["domains"]["photos"]["relationships"]["collection_photos.collection_id"]
        assert relation == {
            "present": True, "rows": 4, "orphan_parents": 1,
            "orphan_children": 1, "cross_owner_links": 1,
        }
        encoded = json.dumps(first, sort_keys=True)
        for secret in (
            "Private birthday", "Hidden title", "Secret collection",
            "secret-david.jpg", "secret-diana.jpg", "david@example.test", "diana@example.test",
        ):
            assert secret not in encoded
        assert len(first["plan_digest"]) == 64


def test_legacy_table_without_owner_stays_unclaimed_and_reports_schema_gates():
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "recipes.db"
        connection = sqlite3.connect(path)
        connection.execute(
            "CREATE TABLE recipes(id TEXT PRIMARY KEY, created_by TEXT, visibility TEXT, deleted_at TEXT, title TEXT)"
        )
        connection.executemany(
            "INSERT INTO recipes VALUES(?,?,?,?,?)",
            [("r1", "home", "shared", None, "Secret soup"), ("r2", "Diana", "shared", "2026-01-01", "Secret cake")],
        )
        connection.commit()
        connection.close()

        report = inventory.build_inventory({"recipes": path}, {})
        resource = report["domains"]["recipes"]["resources"]["recipes"]
        assert resource["ownership"]["column"] is None
        assert resource["ownership"]["legacy_unclaimed"] == 2
        assert resource["legacy_actor_evidence_rows"] == 2
        assert resource["lifecycle_columns"] == {
            "ownership_state": False, "version": False, "deleted_at": True,
            "deleted_by_id": False, "purge_after": False,
        }
        assert "Secret soup" not in json.dumps(report)


def test_inventory_rejects_live_sidecars_and_existing_output(capsys):
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        path = root / "photos.db"
        build_photo_clone(path)
        sidecar = root / "photos.db-wal"
        sidecar.write_bytes(b"not a safe immutable clone")
        try:
            inventory.build_inventory({"photos": path}, {})
        except ValueError as error:
            assert "sidecar" in str(error)
        else:
            raise AssertionError("live sidecar was accepted")
        sidecar.unlink()

        output = root / "report.json"
        output.write_text("keep", encoding="utf-8")
        try:
            inventory.main(["--database", f"photos={path}", "--output", str(output)])
        except SystemExit as error:
            assert error.code == 2
        else:
            raise AssertionError("existing report was overwritten")
        assert output.read_text(encoding="utf-8") == "keep"
        assert "File exists" in capsys.readouterr().err


def test_inventory_rejects_symlinked_clones_and_duplicate_principals(capsys):
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        path = root / "photos.db"
        build_photo_clone(path)
        alias = root / "alias.db"
        alias.symlink_to(path)
        try:
            inventory.build_inventory({"photos": alias}, {})
        except ValueError as error:
            assert "symbolic link" in str(error)
        else:
            raise AssertionError("symlinked clone was accepted")

        try:
            inventory.main([
                "--database", f"photos={path}",
                "--principal", "first=same@example.test",
                "--principal", "second=SAME@example.test",
            ])
        except SystemExit as error:
            assert error.code == 2
        else:
            raise AssertionError("duplicate principal owner IDs were accepted")
        assert "must be unique" in capsys.readouterr().err

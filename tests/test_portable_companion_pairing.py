"""Pairing binds the configured installation and verified household person."""
import sqlite3
from contextlib import contextmanager
from unittest.mock import patch

from flask import Flask



def test_companion_pairing_returns_verified_identity_without_personal_origin(tmp_path, monkeypatch):
    monkeypatch.setenv("DAVID_PI_DISABLE_DEVICE_HOUSEKEEPING", "1")
    monkeypatch.setenv("DAVID_PI_PLATFORM_DATA", str(tmp_path / "platform"))
    from modules.device_backup import init_device_backup
    db = tmp_path / "portal.db"

    @contextmanager
    def connect():
        connection = sqlite3.connect(db)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    app = Flask(__name__)
    app.config["TESTING"] = True
    init_device_backup(app, connect, lambda *a, **k: {}, lambda *a, **k: None, tmp_path)
    owner = {"verified": True, "owner_id": "person-john", "name": "John"}
    config = {"instance_id": "ba7d3d57-9844-4053-bbf0-8ee2aaf5b1d1", "members": [{"login": "person-john"}]}
    origin = "https://john-pi.tail123456.ts.net"
    with patch("modules.device_backup.current_device", return_value=owner), \
            patch("modules.device_backup.public_url", return_value=origin), \
            patch("modules.device_backup.get_installation", return_value=config), \
            patch("modules.device_backup.installation_display_name", return_value="John's home"), \
            patch("modules.device_backup.module_catalog", return_value=[{"id": "audiobooks", "mode": "enabled"}]):
        client = app.test_client()
        token = client.post("/api/device-backup/pairing-token").get_json()
        assert token["server_url"] == origin
        response = client.post("/api/v1/device-backup/pair", json={"pairing_token": token["pairing_token"], "device_name": "Test Android"})
        assert response.status_code == 201
        pairing = response.get_json()
        assert pairing["installation_id"] == config["instance_id"]
        assert pairing["member_id"] == "person-john"
        assert pairing["server_url"] == origin
        assert pairing["display_name"] == "John's home"
        assert pairing["enabled_modules"] == ["audiobooks"]
        assert client.post("/api/v1/device-backup/pair", json={"pairing_token": token["pairing_token"]}).status_code == 401
        status = client.get("/api/v1/device-backup/status", headers={"Authorization": "Bearer " + pairing["device_credential"]})
        assert status.status_code == 200
        assert status.get_json()["member_id"] == pairing["member_id"]
        assert status.get_json()["installation_id"] == pairing["installation_id"]
        assert client.post("/api/v1/ios-backup/pair").status_code == 404
        config["members"] = []
        assert client.get("/api/v1/device-backup/status", headers={"Authorization": "Bearer " + pairing["device_credential"]}).status_code == 401
        assert client.post("/api/device-backup/pairing-token").status_code == 403

import importlib
import os
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from modules.push_endpoint_policy import PushEndpointPolicyError


@pytest.fixture
def worker_database(tmp_path, monkeypatch):
    monkeypatch.setenv("PHOTO_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("DAVID_PI_PLATFORM_DATA", str(tmp_path / "platform"))
    monkeypatch.setenv("DAVID_PI_CHAT_DATA", str(tmp_path / "chat-objects"))
    monkeypatch.setenv(
        "DAVID_PI_CHAT_KEY_B64",
        "ZGF2aWQtcGktY2hhdC10ZXN0LWtleS0zMi1ieXRlcyE=",
    )
    chat = importlib.import_module("modules.chat")
    worker = importlib.import_module("modules.chat_notify_worker")
    database = tmp_path / "worker-chat.db"
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        chat._migrate(connection)
        connection.commit()
    finally:
        connection.close()
    monkeypatch.setattr(worker, "DB_PATH", database)
    return worker, database


def _seed_delivery(
    database,
    now,
    *,
    job_status="pending",
    attempts=0,
    lease_token=None,
    lease_expires_at=None,
):
    connection = sqlite3.connect(database)
    try:
        conversation_id = "conversation-one"
        recipient_id = "diana@example.test"
        timestamp = now.isoformat()
        connection.execute(
            """INSERT INTO conversations
               (id,kind,created_by,created_at,updated_at,version)
               VALUES(?,?,?,?,?,1)""",
            (
                conversation_id,
                "direct",
                "david@example.test",
                timestamp,
                timestamp,
            ),
        )
        connection.executemany(
            """INSERT INTO conversation_members
               (conversation_id,owner_id,joined_at,version) VALUES(?,?,?,1)""",
            [
                (conversation_id, "david@example.test", timestamp),
                (conversation_id, recipient_id, timestamp),
            ],
        )
        message_id = connection.execute(
            """INSERT INTO messages
               (conversation_id,sender_id,sender_name,created_at,version)
               VALUES(?,?,?,?,1)""",
            (conversation_id, "david@example.test", "David", timestamp),
        ).lastrowid
        credential_id = "credential-one"
        connection.execute(
            """INSERT INTO push_subscriptions
               (id,owner_id,platform,endpoint,p256dh,auth,created_at,updated_at,version)
               VALUES(?,?,?,?,?,?,?,?,1)""",
            (
                credential_id,
                recipient_id,
                "web",
                "https://push.example.test/original",
                "public-key",
                "auth-key",
                timestamp,
                timestamp,
            ),
        )
        job_id = connection.execute(
            """INSERT INTO notification_jobs
               (conversation_id,message_id,recipient_id,status,attempts,
                available_at,created_at,lease_token,lease_expires_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                conversation_id,
                message_id,
                recipient_id,
                job_status,
                attempts,
                (now - timedelta(minutes=1)).isoformat(),
                timestamp,
                lease_token,
                lease_expires_at,
            ),
        ).lastrowid
        connection.commit()
    finally:
        connection.close()
    return {
        "conversation_id": conversation_id,
        "recipient_id": recipient_id,
        "message_id": message_id,
        "credential_id": credential_id,
        "job_id": job_id,
    }


def _row(database, statement, parameters=()):
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(statement, parameters).fetchone()
    finally:
        connection.close()


def test_chat_migration_preserves_legacy_push_and_job_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("PHOTO_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("DAVID_PI_PLATFORM_DATA", str(tmp_path / "platform"))
    chat = importlib.import_module("modules.chat")
    database = tmp_path / "legacy-chat.db"
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        connection.executescript(
            """
            CREATE TABLE push_subscriptions (
              id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, platform TEXT NOT NULL,
              endpoint TEXT, p256dh TEXT, auth TEXT, device_token TEXT,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              UNIQUE(owner_id, platform, endpoint),
              UNIQUE(owner_id, platform, device_token)
            );
            CREATE TABLE notification_jobs (
              id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL,
              message_id INTEGER NOT NULL, recipient_id TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
              available_at TEXT NOT NULL, created_at TEXT NOT NULL,
              last_error_code TEXT
            );
            INSERT INTO push_subscriptions
              (id,owner_id,platform,endpoint,p256dh,auth,created_at,updated_at)
              VALUES('legacy-credential','david@example.test','web',
                     'https://push.example.test/legacy','key','auth','then','then');
            INSERT INTO notification_jobs
              (conversation_id,message_id,recipient_id,status,attempts,available_at,created_at)
              VALUES('legacy-conversation',1,'david@example.test','working',1,'then','then');
            """
        )
        chat._migrate(connection)
        connection.commit()
        credential = connection.execute(
            "SELECT id,version FROM push_subscriptions"
        ).fetchone()
        job = connection.execute(
            "SELECT status,lease_token,lease_expires_at FROM notification_jobs"
        ).fetchone()
    finally:
        connection.close()
    assert tuple(credential) == ("legacy-credential", 1)
    assert tuple(job) == ("working", None, None)


def test_worker_reclaims_only_expired_leases(worker_database, monkeypatch):
    worker, database = worker_database
    now = datetime(2026, 1, 2, tzinfo=timezone.utc)
    seeded = _seed_delivery(
        database,
        now,
        job_status="working",
        attempts=1,
        lease_token="dead-worker",
        lease_expires_at=(now - timedelta(seconds=1)).isoformat(),
    )
    delivered = []
    monkeypatch.setattr(
        worker,
        "_web",
        lambda subscription, conversation_id: delivered.append(
            (subscription["id"], conversation_id)
        ),
    )

    assert worker.process_one(now=now) is True
    job = _row(
        database,
        "SELECT * FROM notification_jobs WHERE id=?",
        (seeded["job_id"],),
    )
    assert delivered == [(seeded["credential_id"], seeded["conversation_id"])]
    assert job["status"] == "sent"
    assert job["attempts"] == 2
    assert job["lease_token"] is None
    assert job["lease_expires_at"] is None

    connection = sqlite3.connect(database)
    try:
        connection.execute("DELETE FROM notification_jobs")
        connection.execute("DELETE FROM push_subscriptions")
        connection.execute("DELETE FROM messages")
        connection.execute("DELETE FROM conversation_members")
        connection.execute("DELETE FROM conversations")
        connection.commit()
    finally:
        connection.close()
    active = _seed_delivery(
        database,
        now,
        job_status="working",
        attempts=2,
        lease_token="live-worker",
        lease_expires_at=(now + timedelta(minutes=1)).isoformat(),
    )
    delivered.clear()
    assert worker.process_one(now=now) is False
    active_job = _row(
        database,
        "SELECT * FROM notification_jobs WHERE id=?",
        (active["job_id"],),
    )
    assert active_job["status"] == "working"
    assert active_job["attempts"] == 2
    assert active_job["lease_token"] == "live-worker"
    assert delivered == []


def test_worker_revalidates_credential_version_before_provider(
    worker_database, monkeypatch
):
    worker, database = worker_database
    now = datetime(2026, 1, 2, tzinfo=timezone.utc)
    seeded = _seed_delivery(database, now)
    original_validate = worker._validated_subscription
    raced = False

    def change_after_snapshot(job, lease_token, snapshot, checked_at):
        nonlocal raced
        if not raced:
            raced = True
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    "UPDATE push_subscriptions SET endpoint=?,version=version+1 "
                    "WHERE id=?",
                    (
                        "https://push.example.test/replaced",
                        seeded["credential_id"],
                    ),
                )
                connection.commit()
            finally:
                connection.close()
        return original_validate(job, lease_token, snapshot, checked_at)

    delivered = []
    monkeypatch.setattr(worker, "_validated_subscription", change_after_snapshot)
    monkeypatch.setattr(
        worker,
        "_web",
        lambda subscription, conversation_id: delivered.append(subscription["endpoint"]),
    )

    assert worker.process_one(now=now) is True
    job = _row(
        database,
        "SELECT * FROM notification_jobs WHERE id=?",
        (seeded["job_id"],),
    )
    assert delivered == []
    assert job["status"] == "pending"
    assert job["last_error_code"] == "credential_changed"
    assert job["lease_token"] is None
    assert job["lease_expires_at"] is None


def test_worker_revalidates_active_membership_before_provider(
    worker_database, monkeypatch
):
    worker, database = worker_database
    now = datetime(2026, 1, 2, tzinfo=timezone.utc)
    seeded = _seed_delivery(database, now)
    original_snapshots = worker._subscription_snapshots

    def leave_after_claim(job, lease_token, checked_at):
        connection = sqlite3.connect(database)
        try:
            connection.execute(
                "UPDATE conversation_members SET left_at=?,version=version+1 "
                "WHERE conversation_id=? AND owner_id=?",
                (
                    now.isoformat(),
                    seeded["conversation_id"],
                    seeded["recipient_id"],
                ),
            )
            connection.commit()
        finally:
            connection.close()
        return original_snapshots(job, lease_token, checked_at)

    delivered = []
    monkeypatch.setattr(worker, "_subscription_snapshots", leave_after_claim)
    monkeypatch.setattr(
        worker,
        "_web",
        lambda subscription, conversation_id: delivered.append(conversation_id),
    )

    assert worker.process_one(now=now) is True
    job = _row(
        database,
        "SELECT * FROM notification_jobs WHERE id=?",
        (seeded["job_id"],),
    )
    assert delivered == []
    assert job["status"] == "skipped"
    assert job["last_error_code"] == "recipient_not_active"
    assert job["lease_token"] is None
    assert job["lease_expires_at"] is None


def test_web_delivery_rejects_unsafe_legacy_endpoint_before_provider(
    worker_database, monkeypatch, tmp_path
):
    worker, _ = worker_database
    vapid_key = tmp_path / "vapid-private.pem"
    vapid_key.write_text("not-used-because-provider-is-never-called", encoding="utf-8")
    monkeypatch.setattr(worker, "VAPID_PRIVATE", vapid_key)
    provider_calls = []
    monkeypatch.setattr(
        worker,
        "webpush",
        lambda **kwargs: provider_calls.append(kwargs),
    )

    with pytest.raises(PushEndpointPolicyError):
        worker._web(
            {
                "endpoint": "https://127.0.0.1/internal",
                "p256dh": "legacy-key",
                "auth": "legacy-auth",
            },
            "conversation-one",
        )

    assert provider_calls == []

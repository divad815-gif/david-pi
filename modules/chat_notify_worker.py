"""Deliver generic chat notifications without exposing message content."""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pywebpush import WebPushException, webpush
from urllib.request import Request, urlopen

from .chat import DB_PATH
from .push_endpoint_policy import pinned_push_session, resolve_push_endpoint
from .installation import display_name, get_installation, public_url


VAPID_PRIVATE = Path(os.environ.get("DAVID_PI_VAPID_PRIVATE_KEY_FILE", "/run/secrets/chat-vapid-private.pem"))
VAPID_SUBJECT = os.environ.get("DAVID_PI_VAPID_SUBJECT") or public_url() or "https://github.com/divad815-gif/david-pi"
LEASE_SECONDS = 5 * 60


def _connection():
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def _web(subscription, conversation_id):
    if not VAPID_PRIVATE.is_file():
        raise RuntimeError("web_push_not_configured")
    resolved = resolve_push_endpoint(subscription["endpoint"])
    payload = {"title": display_name(), "body": "New household message", "url": f"/chat/{conversation_id}"}
    with pinned_push_session(resolved) as session:
        webpush(
            subscription_info={
                "endpoint": resolved.endpoint,
                "keys": {"p256dh": subscription["p256dh"], "auth": subscription["auth"]},
            },
            data=json.dumps(payload, separators=(",", ":")),
            vapid_private_key=str(VAPID_PRIVATE),
            vapid_claims={"sub": VAPID_SUBJECT},
            timeout=10,
            requests_session=session,
        )


def _active_job(connection, job, lease_token, now):
    config = get_installation()
    if config and job["recipient_id"] not in {member["login"] for member in config["members"]}:
        return None, "recipient_not_active"
    claimed = connection.execute(
        "SELECT * FROM notification_jobs "
        "WHERE id=? AND status='working' AND lease_token=? "
        "AND lease_expires_at>?",
        (job["id"], lease_token, now.isoformat()),
    ).fetchone()
    if claimed is None:
        return None, "lease_lost"
    authorized = connection.execute(
        """SELECT 1 FROM conversations c
           JOIN conversation_members m ON m.conversation_id=c.id
           JOIN messages message
             ON message.id=? AND message.conversation_id=c.id
           WHERE c.id=? AND c.deleted_at IS NULL
             AND m.owner_id=? AND m.left_at IS NULL
             AND message.deleted_at IS NULL""",
        (job["message_id"], job["conversation_id"], job["recipient_id"]),
    ).fetchone()
    if authorized is None:
        return None, "recipient_not_active"
    return claimed, None


def _subscription_snapshots(job, lease_token, now):
    with _connection() as connection:
        _, error_code = _active_job(connection, job, lease_token, now)
        if error_code:
            return None, error_code
        rows = connection.execute(
            "SELECT * FROM push_subscriptions WHERE owner_id=? ORDER BY id",
            (job["recipient_id"],),
        ).fetchall()
    return [dict(row) for row in rows], None


def _validated_subscription(job, lease_token, snapshot, now):
    """Renew the job lease and bind delivery to one credential version."""
    with _connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        _, error_code = _active_job(connection, job, lease_token, now)
        if error_code:
            connection.commit()
            return None, error_code
        current = connection.execute(
            "SELECT * FROM push_subscriptions WHERE id=?",
            (snapshot["id"],),
        ).fetchone()
        compared = (
            "owner_id",
            "platform",
            "endpoint",
            "p256dh",
            "auth",
            "device_token",
            "version",
        )
        if current is None or any(current[key] != snapshot[key] for key in compared):
            connection.commit()
            return None, "credential_changed"
        lease_expires_at = (now + timedelta(seconds=LEASE_SECONDS)).isoformat()
        renewed = connection.execute(
            "UPDATE notification_jobs SET lease_expires_at=? "
            "WHERE id=? AND status='working' AND lease_token=? "
            "AND lease_expires_at>?",
            (lease_expires_at, job["id"], lease_token, now.isoformat()),
        )
        connection.commit()
        if not renewed.rowcount:
            return None, "lease_lost"
    return dict(current), None


def _finish_job(job, lease_token, *, status, error_code, available_at=None):
    with _connection() as connection:
        if status == "pending":
            changed = connection.execute(
                "UPDATE notification_jobs SET status='pending',available_at=?,"
                "last_error_code=?,lease_token=NULL,lease_expires_at=NULL "
                "WHERE id=? AND status='working' AND lease_token=?",
                (available_at, error_code, job["id"], lease_token),
            )
        else:
            changed = connection.execute(
                "UPDATE notification_jobs SET status=?,last_error_code=?,"
                "lease_token=NULL,lease_expires_at=NULL "
                "WHERE id=? AND status='working' AND lease_token=?",
                (status, error_code, job["id"], lease_token),
            )
        connection.commit()
    return bool(changed.rowcount)


def process_one(now=None):
    config = get_installation()
    if config and (config["modules"]["chat"] == "disabled" or not config["integrations"]["web_push"]):
        return False
    fixed_now = now is not None
    now = now or datetime.now(timezone.utc)
    lease_token = uuid.uuid4().hex
    lease_expires_at = (now + timedelta(seconds=LEASE_SECONDS)).isoformat()
    with _connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE notification_jobs SET status='pending',lease_token=NULL,"
            "lease_expires_at=NULL,last_error_code='lease_expired' "
            "WHERE status='working' AND "
            "(lease_expires_at IS NULL OR lease_expires_at<=?)",
            (now.isoformat(),),
        )
        job = connection.execute(
            "SELECT * FROM notification_jobs WHERE status='pending' AND available_at<=? ORDER BY id LIMIT 1",
            (now.isoformat(),),
        ).fetchone()
        if not job:
            connection.commit()
            return False
        claimed = connection.execute(
            "UPDATE notification_jobs SET status='working',attempts=attempts+1,"
            "lease_token=?,lease_expires_at=? WHERE id=? AND status='pending'",
            (lease_token, lease_expires_at, job["id"]),
        )
        if not claimed.rowcount:
            connection.rollback()
            return False
        job = connection.execute(
            "SELECT * FROM notification_jobs WHERE id=?", (job["id"],)
        ).fetchone()
        connection.commit()
    error_code = None
    delivered = 0
    try:
        subscriptions, error_code = _subscription_snapshots(job, lease_token, now)
        if error_code == "lease_lost":
            return True
        if error_code == "recipient_not_active":
            _finish_job(
                job,
                lease_token,
                status="skipped",
                error_code=error_code,
            )
            return True
        if not subscriptions:
            error_code = "no_subscription"
        for subscription in subscriptions:
            validation_now = now if fixed_now else datetime.now(timezone.utc)
            current, validation_error = _validated_subscription(
                job, lease_token, subscription, validation_now
            )
            if validation_error == "lease_lost":
                return True
            if validation_error == "recipient_not_active":
                error_code = validation_error
                break
            if validation_error == "credential_changed":
                error_code = validation_error
                continue
            try:
                if current["platform"] == "web":
                    _web(current, job["conversation_id"])
                elif current["platform"] == "android":
                    error_code = "native_push_unavailable"
                    continue
                else:
                    error_code = "credential_changed"
                    continue
                delivered += 1
            except (WebPushException, OSError, RuntimeError, ValueError):
                error_code = "provider_unavailable"
        terminal_without_delivery = error_code in {
            "no_subscription",
            "recipient_not_active",
            "native_push_unavailable",
        }
        if delivered or terminal_without_delivery or int(job["attempts"]) >= 4:
            _finish_job(
                job,
                lease_token,
                status="sent" if delivered else "skipped",
                error_code=error_code,
            )
        else:
            retry = now + timedelta(minutes=min(15, 2 ** int(job["attempts"])))
            _finish_job(
                job,
                lease_token,
                status="pending",
                available_at=retry.isoformat(),
                error_code=error_code,
            )
    except Exception:
        _finish_job(
            job,
            lease_token,
            status="pending",
            available_at=(now + timedelta(minutes=5)).isoformat(),
            error_code="worker_error",
        )
    return True


def main():
    while True:
        if not process_one():
            time.sleep(2)


if __name__ == "__main__":
    main()

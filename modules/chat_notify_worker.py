"""Deliver generic chat notifications without exposing message content."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import service_account
from pywebpush import WebPushException, webpush
from urllib.request import Request, urlopen

from .chat import DB_PATH


VAPID_PRIVATE = Path(os.environ.get("DAVID_PI_VAPID_PRIVATE_KEY_FILE", "/run/secrets/chat-vapid-private.pem"))
VAPID_SUBJECT = os.environ.get("DAVID_PI_VAPID_SUBJECT", "https://localhost")
FCM_CREDENTIAL = Path(os.environ.get("DAVID_PI_FCM_CREDENTIAL_FILE", "/run/secrets/firebase-service-account.json"))
GENERIC_PAYLOAD = {"title": "David-Pi", "body": "New David-Pi message"}


def _connection():
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def _web(subscription, conversation_id):
    if not VAPID_PRIVATE.is_file():
        raise RuntimeError("web_push_not_configured")
    payload = GENERIC_PAYLOAD | {"url": f"/chat/{conversation_id}"}
    webpush(
        subscription_info={
            "endpoint": subscription["endpoint"],
            "keys": {"p256dh": subscription["p256dh"], "auth": subscription["auth"]},
        },
        data=json.dumps(payload, separators=(",", ":")),
        vapid_private_key=str(VAPID_PRIVATE),
        vapid_claims={"sub": VAPID_SUBJECT},
        timeout=10,
    )


def _fcm(subscription, conversation_id):
    if not FCM_CREDENTIAL.is_file():
        raise RuntimeError("fcm_not_configured")
    credentials = service_account.Credentials.from_service_account_file(
        FCM_CREDENTIAL, scopes=["https://www.googleapis.com/auth/firebase.messaging"]
    )
    credentials.refresh(GoogleRequest())
    project = credentials.project_id
    body = {
        "message": {
            "token": subscription["device_token"],
            # Data-only delivery lets the app create the same generic notification
            # in foreground and background and route the tap to the right chat.
            "data": {"url": f"/chat/{conversation_id}"},
            "android": {"priority": "high", "notification": {"channel_id": "david_pi_chat"}},
        }
    }
    request = Request(
        f"https://fcm.googleapis.com/v1/projects/{project}/messages:send",
        data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {credentials.token}", "Content-Type": "application/json"},
    )
    with urlopen(request, timeout=10) as response:
        if response.status >= 300:
            raise RuntimeError("fcm_rejected")


def process_one():
    now = datetime.now(timezone.utc)
    with _connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        job = connection.execute(
            "SELECT * FROM notification_jobs WHERE status='pending' AND available_at<=? ORDER BY id LIMIT 1",
            (now.isoformat(),),
        ).fetchone()
        if not job:
            connection.commit()
            return False
        connection.execute("UPDATE notification_jobs SET status='working',attempts=attempts+1 WHERE id=?", (job["id"],))
        connection.commit()
    error_code = None
    delivered = 0
    try:
        with _connection() as connection:
            subscriptions = connection.execute(
                "SELECT * FROM push_subscriptions WHERE owner_id=?", (job["recipient_id"],)
            ).fetchall()
        if not subscriptions:
            error_code = "no_subscription"
        for subscription in subscriptions:
            try:
                if subscription["platform"] == "web":
                    _web(subscription, job["conversation_id"])
                elif subscription["platform"] == "android":
                    _fcm(subscription, job["conversation_id"])
                else:
                    continue
                delivered += 1
            except (WebPushException, OSError, RuntimeError, ValueError):
                error_code = "provider_unavailable"
        with _connection() as connection:
            if delivered or error_code == "no_subscription" or job["attempts"] >= 4:
                connection.execute(
                    "UPDATE notification_jobs SET status=?,last_error_code=? WHERE id=?",
                    ("sent" if delivered else "skipped", error_code, job["id"]),
                )
            else:
                retry = now + timedelta(minutes=min(15, 2 ** int(job["attempts"])))
                connection.execute(
                    "UPDATE notification_jobs SET status='pending',available_at=?,last_error_code=? WHERE id=?",
                    (retry.isoformat(), error_code, job["id"]),
                )
            connection.commit()
    except Exception:
        with _connection() as connection:
            connection.execute(
                "UPDATE notification_jobs SET status='pending',available_at=?,last_error_code='worker_error' WHERE id=?",
                ((now + timedelta(minutes=5)).isoformat(), job["id"]),
            )
            connection.commit()
    return True


def main():
    while True:
        if not process_one():
            time.sleep(2)


if __name__ == "__main__":
    main()

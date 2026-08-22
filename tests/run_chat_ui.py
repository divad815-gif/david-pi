"""Isolated browser-test server for the chat UI; never uses production data."""

import base64
import os
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEST_DATA = tempfile.TemporaryDirectory(prefix="david-pi-chat-ui-")
os.environ.update(
    PHOTO_DATA=TEST_DATA.name,
    DAVID_PI_DISABLE_METRICS="1",
    DAVID_PI_DISABLE_DEVICE_HOUSEKEEPING="1",
    DAVID_PI_PLATFORM_DATA=str(Path(TEST_DATA.name) / "platform"),
    DAVID_PI_FILES_DATA=str(Path(TEST_DATA.name) / "files"),
    DAVID_PI_CHAT_DATA=str(Path(TEST_DATA.name) / "chat"),
    DAVID_PI_CHAT_KEY_B64=base64.b64encode(b"david-pi-chat-test-key-32-bytes!").decode(),
)
sys.path.insert(0, str(ROOT))

from app import app  # noqa: E402
from modules.chat import DB_PATH, _encrypt  # noqa: E402
from modules.platform import connect, utcnow  # noqa: E402


app.config["TESTING"] = True
now = utcnow()
title_cipher, title_nonce = _encrypt(b"David and Diana")
body_cipher, body_nonce = _encrypt(b"Chat UI verification message")
with connect(DB_PATH) as connection:
    connection.executemany(
        "INSERT OR REPLACE INTO portal_users VALUES(?,?,?,?)",
        [("david@example.test", "David", now, now), ("diana@example.test", "Diana", now, now)],
    )
    connection.execute(
        "INSERT OR REPLACE INTO conversations VALUES(?,?,?,?,?,?,?,?)",
        ("ui-test", "direct", title_cipher, title_nonce, "david@example.test|diana@example.test", "david@example.test", now, now),
    )
    connection.executemany(
        "INSERT OR REPLACE INTO conversation_members VALUES(?,?,?)",
        [("ui-test", "david@example.test", now), ("ui-test", "diana@example.test", now)],
    )
    connection.executemany(
        "INSERT INTO messages(conversation_id,sender_id,sender_name,body_cipher,body_nonce,created_at) VALUES(?,?,?,?,?,?)",
        [
            (
                "ui-test",
                "diana@example.test" if index % 2 else "david@example.test",
                "Diana" if index % 2 else "David",
                body_cipher,
                body_nonce,
                now,
            )
            for index in range(80)
        ],
    )


class TestIdentityHeaders:
    def __init__(self, application):
        self.application = application

    def __call__(self, environ, start_response):
        environ["HTTP_X_TEST_TAILSCALE_LOGIN"] = "david@example.test"
        environ["HTTP_X_TEST_TAILSCALE_NAME"] = "David"
        return self.application(environ, start_response)


app.wsgi_app = TestIdentityHeaders(app.wsgi_app)
app.run(host="127.0.0.1", port=int(os.environ.get("DAVID_PI_CHAT_TEST_PORT", "5099")), debug=False, use_reloader=False)

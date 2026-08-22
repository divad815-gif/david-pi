import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, render_template, request

from .identity import current_device, require_profile
from .platform import PLATFORM_DATA, connect, migrate, utcnow


DB_PATH = PLATFORM_DATA / "notes.db"
notes_bp = Blueprint("notes", __name__)


def initialize_notes(connection):
    connection.execute(
        """CREATE TABLE IF NOT EXISTS notes (
            id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '',
            checklist_json TEXT NOT NULL DEFAULT '[]', note_type TEXT NOT NULL DEFAULT 'text',
            visibility TEXT NOT NULL DEFAULT 'shared', owner TEXT NOT NULL,
            tags_json TEXT NOT NULL DEFAULT '[]', pinned INTEGER NOT NULL DEFAULT 0,
            archived INTEGER NOT NULL DEFAULT 0, deleted_at TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1
        )"""
    )
    connection.execute("CREATE INDEX IF NOT EXISTS notes_updated_idx ON notes(updated_at DESC)")
    connection.execute("CREATE INDEX IF NOT EXISTS notes_owner_idx ON notes(owner, visibility)")
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(notes)")}
    if "owner_id" not in columns:
        try:
            connection.execute("ALTER TABLE notes ADD COLUMN owner_id TEXT")
        except sqlite3.OperationalError:
            if "owner_id" not in {
                row["name"] for row in connection.execute("PRAGMA table_info(notes)")
            }:
                raise
    if "owner_name" not in columns:
        try:
            connection.execute("ALTER TABLE notes ADD COLUMN owner_name TEXT")
        except sqlite3.OperationalError:
            if "owner_name" not in {
                row["name"] for row in connection.execute("PRAGMA table_info(notes)")
            }:
                raise
    connection.execute("CREATE INDEX IF NOT EXISTS notes_visibility_idx ON notes(visibility, owner_id)")


migrate(DB_PATH, initialize_notes)


def visible_clause(identity):
    if identity["owner_id"]:
        return "(visibility = 'shared' OR owner_id = ?)", [identity["owner_id"]]
    return "visibility = 'shared'", []


def clean_tags(value):
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        tag = " ".join(str(item).split()).strip()[:30]
        if tag and tag.lower() not in [existing.lower() for existing in result]:
            result.append(tag)
    return result[:12]


def clean_checklist(value):
    if not isinstance(value, list):
        return []
    result = []
    for item in value[:100]:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).replace("\x00", "").strip()[:500]
        if text:
            result.append({"id": str(item.get("id") or uuid.uuid4().hex), "text": text, "done": bool(item.get("done"))})
    return result


def note_json(row, identity=None):
    item = dict(row)
    # Notes created before the household author picker used the neutral
    # "home" owner. Keep them visible and present them as David's existing
    # notes rather than creating an unexplained third category.
    item["owner_display"] = item.get("owner_name") or (
        "Diana" if item.get("owner") == "diana" else "David"
    )
    item["is_mine"] = bool(
        identity and identity["owner_id"] and item.get("owner_id") == identity["owner_id"]
    )
    item["pinned"] = bool(item["pinned"])
    item["archived"] = bool(item["archived"])
    item["tags"] = json.loads(item.pop("tags_json") or "[]")
    item["checklist"] = json.loads(item.pop("checklist_json") or "[]")
    return item


def visible_note(connection, note_id, identity):
    clause, parameters = visible_clause(identity)
    return connection.execute(
        f"SELECT * FROM notes WHERE id = ? AND {clause}", (note_id, *parameters)
    ).fetchone()


@notes_bp.get("/notes")
@require_profile(api=False)
def notes_page():
    return render_template("notes.html")


@notes_bp.get("/api/notes")
@require_profile()
def list_notes():
    identity = current_device()
    view = request.args.get("view", "all")
    query = " ".join(request.args.get("q", "").split()).lower()[:120]
    if view in ("all", "shared"):
        conditions, parameters = ["visibility = 'shared'"], []
    elif view == "mine":
        conditions, parameters = ["owner_id = ?"], [identity["owner_id"] or ""]
    else:
        clause, parameters = visible_clause(identity)
        conditions = [clause]
    if view == "mine":
        pass
    elif view == "pinned":
        conditions.append("pinned = 1")
    elif view == "archived":
        conditions.append("archived = 1")
    elif view == "deleted":
        conditions.append("deleted_at IS NOT NULL")
    if view not in ("archived", "deleted"):
        conditions.extend(["archived = 0", "deleted_at IS NULL"])
    if query:
        conditions.append("(LOWER(title) LIKE ? OR LOWER(body) LIKE ? OR LOWER(tags_json) LIKE ?)")
        pattern = f"%{query}%"
        parameters.extend([pattern, pattern, pattern])
    with connect(DB_PATH) as connection:
        rows = connection.execute(
            f"SELECT * FROM notes WHERE {' AND '.join(conditions)} ORDER BY pinned DESC, updated_at DESC",
            parameters,
        ).fetchall()
    return jsonify(notes=[note_json(row, identity) for row in rows], current_user=identity["name"])


@notes_bp.post("/api/notes")
@require_profile()
def create_note():
    data = request.get_json(silent=True) or {}
    identity = current_device()
    if not identity["owner_id"]:
        return jsonify(error="Open David-Pi through its private Tailscale address."), 401
    visibility = str(data.get("visibility", "shared")).lower()
    if visibility not in ("shared", "private"):
        return jsonify(error="Choose Shared or Only me."), 400
    note_id = uuid.uuid4().hex
    now = utcnow()
    with connect(DB_PATH) as connection:
        connection.execute(
            """INSERT INTO notes
               (id, visibility, owner, owner_id, owner_name, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (note_id, visibility, "home", identity["owner_id"], identity["name"], now, now),
        )
        row = connection.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    return jsonify(note=note_json(row, identity)), 201


@notes_bp.get("/api/notes/<note_id>")
@require_profile()
def get_note(note_id):
    identity = current_device()
    with connect(DB_PATH) as connection:
        row = visible_note(connection, note_id, identity)
    if not row:
        return jsonify(error="Note not found."), 404
    return jsonify(note=note_json(row, identity))


@notes_bp.put("/api/notes/<note_id>")
@require_profile()
def save_note(note_id):
    data = request.get_json(silent=True) or {}
    identity = current_device()
    try:
        expected_version = int(data.get("version"))
    except (TypeError, ValueError):
        return jsonify(error="Reload this note before saving."), 400
    note_type = data.get("note_type", "text")
    visibility = str(data.get("visibility", "shared")).lower()
    if visibility not in ("shared", "private"):
        return jsonify(error="Choose Shared or Only me."), 400
    if note_type not in ("text", "checklist"):
        return jsonify(error="That note setting is not supported."), 400
    with connect(DB_PATH) as connection:
        row = visible_note(connection, note_id, identity)
        if not row:
            return jsonify(error="Note not found."), 404
        owner_id, owner_name = row["owner_id"], row["owner_name"]
        if visibility == "private":
            if not identity["owner_id"]:
                return jsonify(error="Open David-Pi through its private Tailscale address."), 401
            if owner_id and owner_id != identity["owner_id"]:
                return jsonify(error="Only the note owner can make this note private."), 403
            owner_id, owner_name = identity["owner_id"], identity["name"]
        result = connection.execute(
            """UPDATE notes SET title = ?, body = ?, checklist_json = ?, note_type = ?,
               visibility = ?, owner_id = ?, owner_name = ?, tags_json = ?, updated_at = ?, version = version + 1
               WHERE id = ? AND version = ?""",
            (
                str(data.get("title", "")).replace("\x00", "")[:200],
                str(data.get("body", "")).replace("\x00", "")[:100000],
                json.dumps(clean_checklist(data.get("checklist", []))),
                note_type, visibility, owner_id, owner_name, json.dumps(clean_tags(data.get("tags", []))),
                utcnow(), note_id, expected_version,
            ),
        )
        if not result.rowcount:
            latest = visible_note(connection, note_id, identity)
            return jsonify(error="This note changed on another device. Your text was not overwritten.", conflict=True,
                           latest=note_json(latest, identity)), 409
        saved = visible_note(connection, note_id, identity)
    return jsonify(ok=True, note=note_json(saved, identity))


@notes_bp.post("/api/notes/<note_id>/state")
@require_profile()
def note_state(note_id):
    data = request.get_json(silent=True) or {}
    action = data.get("action")
    identity = current_device()
    assignments = {
        "pin": ("pinned", 1), "unpin": ("pinned", 0),
        "archive": ("archived", 1), "unarchive": ("archived", 0),
        "trash": ("deleted_at", utcnow()), "restore": ("deleted_at", None),
    }
    if action not in assignments:
        return jsonify(error="Unknown note action."), 400
    column, value = assignments[action]
    with connect(DB_PATH) as connection:
        if not visible_note(connection, note_id, identity):
            return jsonify(error="Note not found."), 404
        connection.execute(f"UPDATE notes SET {column} = ?, updated_at = ?, version = version + 1 WHERE id = ?",
                           (value, utcnow(), note_id))
    return jsonify(ok=True)


@notes_bp.delete("/api/notes/<note_id>")
@require_profile()
def purge_note(note_id):
    identity = current_device()
    with connect(DB_PATH) as connection:
        row = visible_note(connection, note_id, identity)
        if not row or not row["deleted_at"]:
            return jsonify(error="Only notes in Recently Deleted can be removed forever."), 400
        connection.execute("DELETE FROM notes WHERE id = ?", (note_id,))
    return jsonify(ok=True)


def purge_expired_notes(days=30):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with connect(DB_PATH) as connection:
        connection.execute("DELETE FROM notes WHERE deleted_at IS NOT NULL AND deleted_at < ?", (cutoff,))


def init_notes(app):
    app.register_blueprint(notes_bp)

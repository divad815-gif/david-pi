import hashlib
import json
import sqlite3
import uuid

from flask import Blueprint, jsonify, render_template, request

from .content_ownership import (
    actor_for_identity,
    audit_mutation,
    authorize,
    row_is_visible,
)
from .identity import current_device, require_profile
from .platform import PLATFORM_DATA, connect, initialize_data_foundation, migrate, utcnow


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
    initialize_data_foundation(connection)


migrate(DB_PATH, initialize_notes)


def visible_clause(identity):
    actor = actor_for_identity(identity)
    if actor.principal_id and actor.role in {"admin", "household"}:
        return "(visibility = 'shared' OR owner_id = ?)", [actor.principal_id]
    return "0 = 1", []


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
    item["owner_display"] = item.get("owner_name") or "Legacy (unclaimed)"
    item["is_mine"] = bool(
        identity and identity["owner_id"] and item.get("owner_id") == identity["owner_id"]
    )
    item["can_edit"] = item["is_mine"]
    item["ownership_status"] = "owned" if item.get("owner_id") else "legacy_unclaimed"
    item["pinned"] = bool(item["pinned"])
    item["archived"] = bool(item["archived"])
    item["tags"] = json.loads(item.pop("tags_json") or "[]")
    item["checklist"] = json.loads(item.pop("checklist_json") or "[]")
    item.pop("owner", None)
    item.pop("owner_id", None)
    item.pop("owner_name", None)
    return item


def note_summary_json(row, identity=None):
    """Return the list-card shape without sending full saved note content."""
    item = note_json(row, identity)
    checklist = item.pop("checklist")
    body = item.pop("body")
    if item["note_type"] == "checklist":
        total = len(checklist)
        done = sum(bool(entry.get("done")) for entry in checklist)
        item["summary"] = f"{done} of {total} complete" if total else "Empty checklist"
        item["checklist_total"] = total
        item["checklist_done"] = done
    else:
        item["summary"] = " ".join(body.split())[:240] or "Empty note"
        item["checklist_total"] = 0
        item["checklist_done"] = 0
    return item


def visible_note(connection, note_id, identity):
    clause, parameters = visible_clause(identity)
    return connection.execute(
        f"SELECT * FROM notes WHERE id = ? AND {clause}", (note_id, *parameters)
    ).fetchone()


def _note_for_write(connection, note_id, actor):
    row = connection.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    if row is None or not row_is_visible(row, actor):
        return None, (jsonify(error="Note not found."), 404)
    return row, None


def _expected_version(data):
    try:
        version = int(data.get("version"))
    except (TypeError, ValueError):
        return None
    return version if version > 0 else None


@notes_bp.get("/notes")
@require_profile(api=False)
def notes_page():
    owner = current_device().get("owner_id") or ""
    return render_template("notes.html", notes_draft_scope=hashlib.sha256(
        ("notes-drafts:" + owner).encode("utf-8")
    ).hexdigest())


@notes_bp.get("/api/notes")
@require_profile()
def list_notes():
    identity = current_device()
    actor = actor_for_identity(identity)
    if not actor.principal_id or actor.role not in {"admin", "household"}:
        return jsonify(error="Open David-Pi through an approved private Tailscale account."), 403
    view = request.args.get("view", "all")
    query = " ".join(request.args.get("q", "").split()).lower()[:120]
    try:
        limit = int(request.args.get("limit", "40"))
        offset = int(request.args.get("offset", "0"))
        if not 1 <= limit <= 100 or not 0 <= offset <= 1000000:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify(error="Choose a valid notes page."), 400
    export = request.args.get("export") == "1"
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
        total = connection.execute(
            f"SELECT COUNT(*) FROM notes WHERE {' AND '.join(conditions)}", parameters
        ).fetchone()[0]
        rows = connection.execute(
            f"SELECT * FROM notes WHERE {' AND '.join(conditions)} ORDER BY pinned DESC, updated_at DESC, id DESC"
            + ("" if export else " LIMIT ? OFFSET ?"),
            parameters if export else [*parameters, limit, offset],
        ).fetchall()
    notes = [(note_json if export else note_summary_json)(row, identity) for row in rows]
    return jsonify(notes=notes, count=total, total=total, offset=offset,
                   next_offset=offset + len(notes), has_more=not export and offset + len(notes) < total,
                   current_user=identity["name"])


@notes_bp.post("/api/notes")
@require_profile()
def create_note():
    data = request.get_json(silent=True) or {}
    identity = current_device()
    actor = actor_for_identity(identity)
    if not actor.principal_id or actor.role not in {"admin", "household"}:
        return jsonify(error="Open David-Pi through an approved private Tailscale account."), 403
    visibility = str(data.get("visibility", "shared")).lower()
    if visibility not in ("shared", "private"):
        return jsonify(error="Choose Shared or Only me."), 400
    note_id = uuid.uuid4().hex
    now = utcnow()
    with connect(DB_PATH) as connection:
        connection.execute("BEGIN IMMEDIATE")
        decision = authorize("note.create", actor)
        if not decision.allowed:
            return jsonify(error="Open David-Pi through an approved private Tailscale account."), 403
        connection.execute(
            """INSERT INTO notes
               (id, visibility, owner, owner_id, owner_name, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (note_id, visibility, "home", actor.principal_id, identity["name"], now, now),
        )
        row = connection.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
        audit_mutation(
            connection, actor=actor, domain="note", object_id=note_id,
            action="create", before=None, after=row,
        )
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
    expected_version = _expected_version(data)
    if expected_version is None:
        return jsonify(error="Reload this note before saving."), 400
    note_type = data.get("note_type", "text")
    visibility = str(data.get("visibility", "shared")).lower()
    if visibility not in ("shared", "private"):
        return jsonify(error="Choose Shared or Only me."), 400
    if note_type not in ("text", "checklist"):
        return jsonify(error="That note setting is not supported."), 400
    actor = actor_for_identity(identity)
    with connect(DB_PATH) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row, error = _note_for_write(connection, note_id, actor)
        if error:
            return error
        decision = authorize("note.update", actor, row)
        if not decision.allowed:
            return jsonify(error="This shared note is read-only because you are not its owner."), 403
        if row["deleted_at"]:
            return jsonify(error="Restore this note before editing it."), 409
        if row["version"] != expected_version:
            return jsonify(
                error="This note changed on another device. Your text was not overwritten.",
                conflict=True,
                latest=note_json(row, identity),
            ), 409
        result = connection.execute(
            """UPDATE notes SET title = ?, body = ?, checklist_json = ?, note_type = ?,
               visibility = ?, tags_json = ?, updated_at = ?, version = version + 1
               WHERE id = ? AND owner_id = ? AND version = ? AND deleted_at IS NULL""",
            (
                str(data.get("title", "")).replace("\x00", "")[:200],
                str(data.get("body", "")).replace("\x00", "")[:100000],
                json.dumps(clean_checklist(data.get("checklist", []))),
                note_type, visibility, json.dumps(clean_tags(data.get("tags", []))),
                utcnow(), note_id, actor.principal_id, expected_version,
            ),
        )
        if not result.rowcount:
            latest = visible_note(connection, note_id, identity)
            return jsonify(error="This note changed on another device. Your text was not overwritten.", conflict=True,
                           latest=note_json(latest, identity)), 409
        saved = visible_note(connection, note_id, identity)
        audit_mutation(
            connection, actor=actor, domain="note", object_id=note_id,
            action="update", before=row, after=saved,
        )
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
    expected_version = _expected_version(data)
    if expected_version is None:
        return jsonify(error="Reload this note before changing it."), 400
    column, value = assignments[action]
    actor = actor_for_identity(identity)
    with connect(DB_PATH) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row, error = _note_for_write(connection, note_id, actor)
        if error:
            return error
        decision = authorize("note.state.update", actor, row, action=action)
        if not decision.allowed:
            return jsonify(error="This shared note is read-only because you are not its owner."), 403
        if row["version"] != expected_version:
            return jsonify(error="This note changed elsewhere. Reload it before continuing.", conflict=True), 409
        if action == "restore" and not row["deleted_at"]:
            return jsonify(error="This note is not in Recently Deleted."), 409
        if action != "restore" and row["deleted_at"]:
            return jsonify(error="Restore this note before changing it."), 409
        result = connection.execute(
            f"UPDATE notes SET {column} = ?, updated_at = ?, version = version + 1 "
            "WHERE id = ? AND owner_id = ? AND version = ?",
            (value, utcnow(), note_id, actor.principal_id, expected_version),
        )
        if not result.rowcount:
            return jsonify(error="This note changed elsewhere. Reload it before continuing.", conflict=True), 409
        saved = connection.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
        audit_mutation(
            connection, actor=actor, domain="note", object_id=note_id,
            action=action, before=row, after=saved,
        )
    return jsonify(ok=True, note=note_json(saved, identity))


@notes_bp.delete("/api/notes/<note_id>")
@require_profile()
def purge_note(note_id):
    data = request.get_json(silent=True) or {}
    expected_version = _expected_version(data)
    if expected_version is None:
        return jsonify(error="Reload this note before changing it."), 400
    identity = current_device()
    actor = actor_for_identity(identity)
    with connect(DB_PATH) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row, error = _note_for_write(connection, note_id, actor)
        if error:
            return error
        decision = authorize("note.purge", actor, row)
        if not decision.allowed:
            return jsonify(error="This shared note is read-only because you are not its owner."), 403
        if row["version"] != expected_version:
            return jsonify(error="This note changed elsewhere. Reload it before continuing.", conflict=True), 409
        if not row["deleted_at"]:
            return jsonify(error="Only notes in Recently Deleted can be removed forever."), 400
        if data.get("confirm") != "permanently delete":
            return jsonify(error="Permanent deletion requires confirmation."), 400
    return jsonify(
        error="Permanent deletion is paused until a protected backup newer than this deletion is independently verified.",
        retained=True,
    ), 503


def init_notes(app):
    app.register_blueprint(notes_bp)

import hashlib
import mimetypes
import os
import sqlite3
import subprocess
import threading
import uuid
import zipfile
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

from .identity import current_device, require_profile
from .platform import PLATFORM_DATA, connect, migrate, utcnow


files_bp = Blueprint("files", __name__)
DB_PATH = PLATFORM_DATA / "files.db"
STORAGE = Path(os.environ.get("DAVID_PI_FILES_DATA", Path(os.environ.get("PHOTO_DATA", "/data")) / "files"))
OBJECTS = STORAGE / "objects"
INCOMING = STORAGE / "incoming"
PDF_CACHE = STORAGE / "pdf-cache"
MAX_FILE_BYTES = int(os.environ.get("DAVID_PI_MAX_FILE_BYTES", 2 * 1024 * 1024 * 1024))
PDF_CACHE_BYTES = int(os.environ.get("DAVID_PI_PDF_CACHE_BYTES", 512 * 1024 * 1024))
TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".yaml", ".yml", ".log", ".ini"}
DOCUMENT_TEXT_EXTENSIONS = {".docx", ".odt"}
INLINE_TYPES = {"application/pdf"}
SAFE_INLINE_EXTENSIONS = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".gif": "image/gif",
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".wav": "audio/wav",
}

for directory in (STORAGE, OBJECTS, INCOMING, PDF_CACHE):
    directory.mkdir(parents=True, exist_ok=True)

pdf_render_lock = threading.BoundedSemaphore(1)
pdf_page_counts = {}
MAX_PDF_PAGES = 1000


@contextmanager
def global_pdf_render_lock():
    """Serialize Poppler work across Gunicorn workers on Linux."""
    with pdf_render_lock:
        lock_path = INCOMING / ".pdf-render.lock"
        with lock_path.open("a+b") as lock:
            try:
                import fcntl
            except ImportError:
                yield
                return
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def managed_object(name):
    root = OBJECTS.resolve()
    candidate = root / str(name)
    if candidate.is_symlink():
        raise ValueError("Managed files cannot be symbolic links.")
    resolved = candidate.resolve(strict=False)
    if resolved != root and root not in resolved.parents:
        raise ValueError("Managed file path escaped its storage root.")
    return resolved


def initialize_files(connection):
    connection.execute(
        """CREATE TABLE IF NOT EXISTS file_folders (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, parent_id TEXT REFERENCES file_folders(id),
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS stored_files (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, folder_id TEXT REFERENCES file_folders(id),
            stored_name TEXT NOT NULL UNIQUE, content_type TEXT NOT NULL, byte_size INTEGER NOT NULL,
            sha256 TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            uploaded_by TEXT NOT NULL, deleted_at TEXT
        )"""
    )
    connection.execute("CREATE INDEX IF NOT EXISTS stored_files_folder_idx ON stored_files(folder_id, deleted_at)")
    connection.execute("CREATE INDEX IF NOT EXISTS stored_files_name_idx ON stored_files(name)")
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(stored_files)")}
    for name, definition in (
        ("owner_id", "TEXT"),
        ("owner_name", "TEXT"),
        ("visibility", "TEXT NOT NULL DEFAULT 'shared'"),
    ):
        if name not in columns:
            try:
                connection.execute(f"ALTER TABLE stored_files ADD COLUMN {name} {definition}")
            except sqlite3.OperationalError:
                current = {row["name"] for row in connection.execute("PRAGMA table_info(stored_files)")}
                if name not in current:
                    raise
    connection.execute("CREATE INDEX IF NOT EXISTS stored_files_visibility_idx ON stored_files(visibility, owner_id)")


migrate(DB_PATH, initialize_files)


def clean_name(value, fallback="Untitled"):
    name = " ".join(str(value or "").replace("\x00", "").split()).strip(" .")
    return name[:240] or fallback


def valid_folder(connection, folder_id):
    if not folder_id:
        return None
    row = connection.execute("SELECT id FROM file_folders WHERE id = ?", (folder_id,)).fetchone()
    if not row:
        raise ValueError("That folder no longer exists.")
    return row["id"]


def unique_name(connection, name, folder_id, exclude_id=None):
    base = clean_name(name)
    stem, suffix = os.path.splitext(base)
    candidate = base
    counter = 2
    while connection.execute(
        "SELECT 1 FROM stored_files WHERE LOWER(name)=LOWER(?) AND folder_id IS ? AND deleted_at IS NULL AND id != ?",
        (candidate, folder_id, exclude_id or ""),
    ).fetchone():
        candidate = f"{stem} ({counter}){suffix}"
        counter += 1
    return candidate


def visible_sql(identity, alias=""):
    prefix = f"{alias}." if alias else ""
    if identity["owner_id"]:
        return f"({prefix}visibility='shared' OR {prefix}owner_id=?)", [identity["owner_id"]]
    return f"{prefix}visibility='shared'", []


def file_json(row, identity=None):
    item = dict(row)
    suffix = Path(item["name"]).suffix.lower()
    content_type = item["content_type"]
    if content_type == "application/pdf" or suffix == ".pdf":
        kind = "pdf"
    elif content_type.startswith("image/"):
        kind = "image"
    elif content_type.startswith("video/"):
        kind = "video"
    elif content_type.startswith("audio/"):
        kind = "audio"
    elif content_type.startswith("text/") or suffix in TEXT_EXTENSIONS:
        kind = "text"
    elif suffix in {".doc", ".docx", ".odt", ".rtf"}:
        kind = "document"
    elif suffix in {".xls", ".xlsx", ".ods"}:
        kind = "spreadsheet"
    elif suffix in {".ppt", ".pptx", ".odp"}:
        kind = "presentation"
    else:
        kind = "other"
    item["kind"] = kind
    item["viewable"] = kind in {"pdf", "image", "video", "audio", "text"} or suffix in DOCUMENT_TEXT_EXTENSIONS
    item["text_preview"] = kind == "text" or suffix in DOCUMENT_TEXT_EXTENSIONS
    item["content_url"] = f"/api/files/{item['id']}/content"
    item["download_url"] = f"/api/files/{item['id']}/content?download=1"
    item["owner_display"] = item.get("owner_name") or item.get("uploaded_by") or "Home"
    item["is_mine"] = bool(
        identity and identity["owner_id"] and item.get("owner_id") == identity["owner_id"]
    )
    item.pop("stored_name", None)
    item.pop("sha256", None)
    return item


def get_file(file_id, include_deleted=False):
    condition = "" if include_deleted else " AND deleted_at IS NULL"
    identity = current_device()
    visible, parameters = visible_sql(identity)
    with connect(DB_PATH) as connection:
        return connection.execute(
            f"SELECT * FROM stored_files WHERE id = ?{condition} AND {visible}",
            (file_id, *parameters),
        ).fetchone()


def pdf_page_count(row):
    cached = pdf_page_counts.get(row["id"])
    if cached:
        return cached
    result = subprocess.run(
        ["pdfinfo", str(OBJECTS / row["stored_name"])],
        check=True, capture_output=True, text=True, timeout=30,
    )
    for line in result.stdout.splitlines():
        if line.lower().startswith("pages:"):
            pages = int(line.split(":", 1)[1].strip())
            if pages > MAX_PDF_PAGES:
                raise ValueError("This PDF has too many pages to preview safely.")
            if pages > 0:
                pdf_page_counts[row["id"]] = pages
                return pages
    raise ValueError("The number of pages could not be determined.")


def trim_pdf_cache():
    entries = [path for path in PDF_CACHE.glob("*.jpg") if path.is_file()]
    total = sum(path.stat().st_size for path in entries)
    if total <= PDF_CACHE_BYTES:
        return
    target = int(PDF_CACHE_BYTES * 0.8)
    for path in sorted(entries, key=lambda item: item.stat().st_mtime):
        size = path.stat().st_size
        path.unlink(missing_ok=True)
        total -= size
        if total <= target:
            break


@files_bp.get("/files")
@require_profile(api=False)
def files_page():
    return render_template("files.html")


@files_bp.get("/api/files")
@require_profile()
def list_files():
    folder_id = request.args.get("folder") or None
    query = " ".join(request.args.get("q", "").split())[:120]
    deleted = request.args.get("view") == "deleted"
    kind = request.args.get("kind", "")
    identity = current_device()
    if request.args.get("owner") == "mine":
        scope, parameters = "owner_id=?", [identity["owner_id"] or ""]
    else:
        scope, parameters = "visibility='shared'", []
    conditions = ["deleted_at IS NOT NULL" if deleted else "deleted_at IS NULL", scope]
    if not deleted:
        conditions.append("folder_id IS ?")
        parameters.append(folder_id)
    if query:
        conditions.append("LOWER(name) LIKE ?")
        parameters.append(f"%{query.lower()}%")
    with connect(DB_PATH) as connection:
        if folder_id:
            valid_folder(connection, folder_id)
        rows = connection.execute(
            f"SELECT * FROM stored_files WHERE {' AND '.join(conditions)} ORDER BY updated_at DESC", parameters
        ).fetchall()
        folders = [] if deleted or query else connection.execute(
            "SELECT * FROM file_folders WHERE parent_id IS ? ORDER BY LOWER(name)", (folder_id,)
        ).fetchall()
        current = connection.execute("SELECT * FROM file_folders WHERE id = ?", (folder_id,)).fetchone() if folder_id else None
        breadcrumbs = []
        seen = set()
        while current and current["id"] not in seen:
            seen.add(current["id"])
            breadcrumbs.append({"id": current["id"], "name": current["name"]})
            current = connection.execute("SELECT * FROM file_folders WHERE id = ?", (current["parent_id"],)).fetchone()
    items = [file_json(row, identity) for row in rows]
    if kind:
        items = [item for item in items if item["kind"] == kind]
    return jsonify(
        files=items,
        folders=[dict(row) for row in folders],
        breadcrumbs=list(reversed(breadcrumbs)),
        folder_id=folder_id,
        deleted=deleted,
        current_user=identity["name"],
    )


@files_bp.post("/api/files/folders")
@require_profile()
def create_folder():
    data = request.get_json(silent=True) or {}
    name = clean_name(data.get("name"), "")
    if not name:
        return jsonify(error="Give the folder a name."), 400
    folder_id, now = uuid.uuid4().hex, utcnow()
    try:
        with connect(DB_PATH) as connection:
            parent_id = valid_folder(connection, data.get("parent_id"))
            duplicate = connection.execute(
                "SELECT 1 FROM file_folders WHERE LOWER(name)=LOWER(?) AND parent_id IS ?", (name, parent_id)
            ).fetchone()
            if duplicate:
                return jsonify(error="A folder with that name already exists here."), 409
            connection.execute("INSERT INTO file_folders VALUES (?, ?, ?, ?, ?)", (folder_id, name, parent_id, now, now))
    except ValueError as error:
        return jsonify(error=str(error)), 400
    return jsonify(folder={"id": folder_id, "name": name, "parent_id": parent_id}), 201


@files_bp.post("/api/files/upload")
@require_profile()
def upload_files():
    uploads = request.files.getlist("files")
    if not uploads:
        return jsonify(error="Choose at least one file."), 400
    folder_id = request.form.get("folder_id") or None
    visibility = request.form.get("visibility", "shared").lower()
    identity = current_device()
    if visibility not in ("shared", "private"):
        return jsonify(error="Choose Shared or Only me."), 400
    if visibility == "private" and not identity["owner_id"]:
        return jsonify(error="Open David-Pi through its private Tailscale address."), 401
    added, errors = [], []
    with connect(DB_PATH) as connection:
        try:
            folder_id = valid_folder(connection, folder_id)
        except ValueError as error:
            return jsonify(error=str(error)), 400
    for upload in uploads[:100]:
        display_name = clean_name(upload.filename, "Untitled file")
        file_id = uuid.uuid4().hex
        temp_path = INCOMING / f"{file_id}.part"
        digest, size = hashlib.sha256(), 0
        final_path = None
        try:
            with temp_path.open("wb") as target:
                while True:
                    chunk = upload.stream.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_FILE_BYTES:
                        raise ValueError("This file exceeds the 2 GB file limit.")
                    digest.update(chunk)
                    target.write(chunk)
            suffix = Path(secure_filename(display_name)).suffix.lower()[:16]
            stored_name = f"{file_id}{suffix}"
            final_path = managed_object(stored_name)
            with connect(DB_PATH) as connection:
                final_name = unique_name(connection, display_name, folder_id)
                now = utcnow()
                temp_path.replace(final_path)
                connection.execute(
                    """INSERT INTO stored_files
                       (id,name,folder_id,stored_name,content_type,byte_size,sha256,
                        created_at,updated_at,uploaded_by,deleted_at,owner_id,owner_name,visibility)
                       VALUES (?,?,?,?,?,?,?,?,?,?,NULL,?,?,?)""",
                    (
                        file_id, final_name, folder_id, stored_name,
                        upload.mimetype or mimetypes.guess_type(final_name)[0] or "application/octet-stream",
                        size, digest.hexdigest(), now, now, identity["name"],
                        identity["owner_id"], identity["name"], visibility,
                    ),
                )
            added.append(final_name)
        except (OSError, ValueError, sqlite3.Error):
            temp_path.unlink(missing_ok=True)
            if final_path is not None:
                final_path.unlink(missing_ok=True)
            errors.append({"name": display_name, "reason": "This file could not be stored safely."})
    return jsonify(added=added, errors=errors), 201 if added else 400


@files_bp.get("/api/files/<file_id>/content")
@require_profile()
def file_content(file_id):
    row = get_file(file_id)
    if not row:
        return "Not found", 404
    try:
        path = managed_object(row["stored_name"])
    except ValueError:
        return "Not found", 404
    if not path.is_file():
        return "Not found", 404
    suffix = Path(row["name"]).suffix.lower()
    safe_type = SAFE_INLINE_EXTENSIONS.get(suffix)
    download = request.args.get("download") == "1" or safe_type is None
    return send_file(
        path,
        mimetype=safe_type or "application/octet-stream",
        as_attachment=download,
        download_name=row["name"],
        conditional=True,
        max_age=0,
    )


@files_bp.get("/api/files/<file_id>/pdf")
@require_profile()
def pdf_details(file_id):
    row = get_file(file_id)
    if not row or file_json(row)["kind"] != "pdf":
        return jsonify(error="PDF not found."), 404
    try:
        return jsonify(pages=pdf_page_count(row))
    except (OSError, subprocess.SubprocessError, ValueError):
        return jsonify(error="This PDF could not be opened safely."), 422


@files_bp.get("/api/files/<file_id>/pdf/pages/<int:page>")
@require_profile()
def pdf_page(file_id, page):
    row = get_file(file_id)
    if not row or file_json(row)["kind"] != "pdf":
        return jsonify(error="PDF not found."), 404
    requested_width = min(2000, max(800, request.args.get("width", 1600, type=int)))
    # Keep cache cardinality bounded while serving a smaller, faster phone page.
    width = min((1200, 1600, 2000), key=lambda candidate: abs(candidate - requested_width))
    try:
        pages = pdf_page_count(row)
        if page < 1 or page > pages:
            return jsonify(error="That page does not exist."), 404
        cached = PDF_CACHE / f"{row['id']}-{page}-{width}.jpg"
        if not cached.is_file():
            with global_pdf_render_lock():
                if not cached.is_file():
                    prefix = INCOMING / f"pdf-{uuid.uuid4().hex}"
                    rendered = Path(f"{prefix}.jpg")
                    try:
                        subprocess.run(
                            [
                                "pdftoppm", "-f", str(page), "-l", str(page), "-singlefile",
                                "-jpeg", "-jpegopt", "quality=84,optimize=y",
                                "-scale-to-x", str(width), "-scale-to-y", "-1",
                                str(OBJECTS / row["stored_name"]), str(prefix),
                            ],
                            check=True, capture_output=True, timeout=120,
                        )
                        if not rendered.is_file() or rendered.stat().st_size == 0:
                            raise ValueError("The page renderer produced no image.")
                        rendered.replace(cached)
                        trim_pdf_cache()
                    finally:
                        rendered.unlink(missing_ok=True)
        response = send_file(cached, mimetype="image/jpeg", conditional=True, max_age=604800)
        response.headers["Cache-Control"] = "private, max-age=604800, immutable"
        return response
    except subprocess.TimeoutExpired:
        return jsonify(error="This page took too long to render. Try another page."), 504
    except (OSError, subprocess.SubprocessError, ValueError):
        return jsonify(error="This page could not be displayed safely."), 422


@files_bp.get("/api/files/<file_id>/text")
@require_profile()
def file_text(file_id):
    row = get_file(file_id)
    if not row or not file_json(row)["text_preview"]:
        return jsonify(error="This file cannot be read as text."), 400
    if row["byte_size"] > 25 * 1024 * 1024:
        return jsonify(error="This text file is too large to preview."), 413
    try:
        path = OBJECTS / row["stored_name"]
        suffix = Path(row["name"]).suffix.lower()
        if suffix in DOCUMENT_TEXT_EXTENSIONS:
            member = "word/document.xml" if suffix == ".docx" else "content.xml"
            with zipfile.ZipFile(path) as archive:
                info = archive.getinfo(member)
                if info.file_size > 8 * 1024 * 1024:
                    raise ValueError("This document is too complex to preview safely.")
                root = ET.fromstring(archive.read(info))
            paragraph_tags = ("}p", "}h")
            lines = []
            for element in root.iter():
                if element.tag.endswith(paragraph_tags):
                    line = " ".join(part.strip() for part in element.itertext() if part.strip())
                    if line:
                        lines.append(line)
            text = "\n\n".join(lines)
        else:
            text = path.read_text("utf-8", errors="replace")
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, ET.ParseError):
        return jsonify(error="The file could not be opened."), 404
    return jsonify(name=row["name"], text=text)


@files_bp.put("/api/files/<file_id>")
@require_profile()
def update_file(file_id):
    data = request.get_json(silent=True) or {}
    identity = current_device()
    visible, parameters = visible_sql(identity)
    with connect(DB_PATH) as connection:
        row = connection.execute(
            f"SELECT * FROM stored_files WHERE id = ? AND deleted_at IS NULL AND {visible}",
            (file_id, *parameters),
        ).fetchone()
        if not row:
            return jsonify(error="File not found."), 404
        try:
            folder_id = valid_folder(connection, data.get("folder_id")) if "folder_id" in data else row["folder_id"]
        except ValueError as error:
            return jsonify(error=str(error)), 400
        name = unique_name(connection, clean_name(data.get("name", row["name"])), folder_id, file_id)
        visibility = str(data.get("visibility", row["visibility"])).lower()
        if visibility not in ("shared", "private"):
            return jsonify(error="Choose Shared or Only me."), 400
        if visibility == "private":
            if not identity["owner_id"]:
                return jsonify(error="Open David-Pi through its private Tailscale address."), 401
            if row["owner_id"] and row["owner_id"] != identity["owner_id"]:
                return jsonify(error="Only the owner can make this file private."), 403
        owner_id = row["owner_id"] or (identity["owner_id"] if visibility == "private" else None)
        owner_name = row["owner_name"] or (identity["name"] if owner_id else None)
        connection.execute(
            "UPDATE stored_files SET name=?,folder_id=?,visibility=?,owner_id=?,owner_name=?,updated_at=? WHERE id=?",
            (name, folder_id, visibility, owner_id, owner_name, utcnow(), file_id),
        )
    return jsonify(ok=True, name=name)


@files_bp.delete("/api/files/<file_id>")
@require_profile()
def trash_file(file_id):
    identity = current_device()
    visible, parameters = visible_sql(identity)
    with connect(DB_PATH) as connection:
        result = connection.execute(
            f"UPDATE stored_files SET deleted_at=?, updated_at=? WHERE id=? AND deleted_at IS NULL AND {visible}",
            (utcnow(), utcnow(), file_id, *parameters),
        )
    return (jsonify(ok=True), 200) if result.rowcount else (jsonify(error="File not found."), 404)


@files_bp.post("/api/files/<file_id>/restore")
@require_profile()
def restore_file(file_id):
    visible, parameters = visible_sql(current_device())
    with connect(DB_PATH) as connection:
        row = connection.execute(
            f"SELECT * FROM stored_files WHERE id=? AND deleted_at IS NOT NULL AND {visible}",
            (file_id, *parameters),
        ).fetchone()
        if not row:
            return jsonify(error="File not found."), 404
        name = unique_name(connection, row["name"], row["folder_id"], file_id)
        connection.execute(
            "UPDATE stored_files SET name=?, deleted_at=NULL, updated_at=? WHERE id=?", (name, utcnow(), file_id)
        )
    return jsonify(ok=True, name=name)


@files_bp.post("/api/files/<file_id>/purge")
@require_profile()
def purge_file(file_id):
    data = request.get_json(silent=True) or {}
    if data.get("confirm") != "permanently delete":
        return jsonify(error="Permanent deletion requires confirmation."), 400
    visible, parameters = visible_sql(current_device())
    with connect(DB_PATH) as connection:
        row = connection.execute(
            f"SELECT * FROM stored_files WHERE id=? AND deleted_at IS NOT NULL AND {visible}",
            (file_id, *parameters),
        ).fetchone()
        if not row:
            return jsonify(error="File not found."), 404
    try:
        path = managed_object(row["stored_name"])
    except ValueError:
        return jsonify(error="File storage record is invalid."), 409
    quarantine = STORAGE / "quarantine"
    quarantine.mkdir(exist_ok=True)
    held = quarantine / f"{file_id}-{uuid.uuid4().hex}"
    if path.is_file():
        os.replace(path, held)
    try:
        with connect(DB_PATH) as connection:
            current = connection.execute(
                f"SELECT 1 FROM stored_files WHERE id=? AND deleted_at IS NOT NULL AND {visible}",
                (file_id, *parameters),
            ).fetchone()
            if not current:
                raise ValueError("File state changed during deletion.")
            connection.execute("DELETE FROM stored_files WHERE id=?", (file_id,))
    except Exception:
        if held.exists():
            os.replace(held, path)
        raise
    held.unlink(missing_ok=True)
    for cached in PDF_CACHE.glob(f"{file_id}-*.jpg"):
        cached.unlink(missing_ok=True)
    pdf_page_counts.pop(file_id, None)
    return jsonify(ok=True)


def init_files(app):
    app.register_blueprint(files_bp)

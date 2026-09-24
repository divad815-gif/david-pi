import hashlib
import json
import logging
import mimetypes
import os
import re
import sqlite3
import stat
import subprocess
import threading
import uuid
import zipfile
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from pathlib import Path

from flask import Blueprint, current_app, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

from .content_ownership import (
    actor_for_identity,
    audit_mutation,
    authorize,
    row_is_visible,
)
from .identity import current_device, require_profile
from .content_policy import Actor
from . import file_jobs
from .platform import PLATFORM_DATA, connect, initialize_data_foundation, migrate, utcnow
from .secure_storage import (
    PinnedStorageRoot,
    StorageSafetyError,
    ensure_restricted_directory,
    identity,
    safe_component,
)


files_bp = Blueprint("files", __name__)
PRIVATE_REVALIDATE_SCOPE_HEADER = "X-David-Pi-Private-Revalidate-Scope"
DB_PATH = PLATFORM_DATA / "files.db"
STORAGE = Path(os.environ.get("DAVID_PI_FILES_DATA", Path(os.environ.get("PHOTO_DATA", "/data")) / "files"))
OBJECTS = STORAGE / "objects"
INCOMING = STORAGE / "incoming"
PDF_CACHE = STORAGE / "pdf-cache"
MAX_FILE_BYTES = int(os.environ.get("DAVID_PI_MAX_FILE_BYTES", 2 * 1024 * 1024 * 1024))
PDF_CACHE_BYTES = int(os.environ.get("DAVID_PI_PDF_CACHE_BYTES", 512 * 1024 * 1024))
TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".yaml", ".yml", ".log", ".ini"}
DOCUMENT_TEXT_EXTENSIONS = {".docx", ".odt"}
FILE_KINDS = frozenset({"pdf", "image", "video", "audio", "text", "document", "spreadsheet", "presentation", "other"})
INLINE_TYPES = {"application/pdf"}
SAFE_INLINE_EXTENSIONS = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".gif": "image/gif",
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".wav": "audio/wav",
}

for directory in (STORAGE, OBJECTS, INCOMING, PDF_CACHE):
    ensure_restricted_directory(directory)

OBJECT_STORAGE = PinnedStorageRoot(OBJECTS)
INCOMING_STORAGE = PinnedStorageRoot(INCOMING)
PDF_STORAGE = PinnedStorageRoot(PDF_CACHE)
LOGGER = logging.getLogger(__name__)

pdf_render_lock = threading.BoundedSemaphore(1)
pdf_page_counts = {}
MAX_PDF_PAGES = 1000


@contextmanager
def global_pdf_render_lock():
    """Serialize Poppler work across Gunicorn workers on Linux."""
    with pdf_render_lock:
        descriptor = -1
        try:
            try:
                descriptor, _metadata = INCOMING_STORAGE.create_regular(
                    ".pdf-render.lock", mode=0o600
                )
            except FileExistsError:
                descriptor, _metadata = INCOMING_STORAGE.open_regular(
                    ".pdf-render.lock", expected_size=0
                )
            try:
                import fcntl
            except ImportError:
                yield
                return
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            if descriptor >= 0:
                os.close(descriptor)


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
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            owner_id TEXT, owner_name TEXT, visibility TEXT NOT NULL DEFAULT 'shared',
            version INTEGER NOT NULL DEFAULT 1
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS stored_files (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, folder_id TEXT REFERENCES file_folders(id),
            stored_name TEXT NOT NULL UNIQUE, content_type TEXT NOT NULL, byte_size INTEGER NOT NULL,
            sha256 TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            uploaded_by TEXT NOT NULL, deleted_at TEXT,
            owner_id TEXT, owner_name TEXT, visibility TEXT NOT NULL DEFAULT 'shared',
            version INTEGER NOT NULL DEFAULT 1
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS file_upload_intents (
            id TEXT PRIMARY KEY,
            batch_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            owner_name TEXT NOT NULL,
            folder_id TEXT,
            display_name TEXT NOT NULL,
            stored_name TEXT NOT NULL UNIQUE,
            temp_name TEXT NOT NULL UNIQUE,
            content_type TEXT NOT NULL,
            byte_size INTEGER NOT NULL CHECK(byte_size >= 0),
            sha256 TEXT NOT NULL,
            visibility TEXT NOT NULL CHECK(visibility IN ('shared','private')),
            state TEXT NOT NULL CHECK(state IN ('prepared','committed')),
            created_at TEXT NOT NULL,
            committed_at TEXT
        )"""
    )
    connection.execute("CREATE INDEX IF NOT EXISTS stored_files_folder_idx ON stored_files(folder_id, deleted_at)")
    connection.execute("CREATE INDEX IF NOT EXISTS stored_files_name_idx ON stored_files(name)")
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(stored_files)")}
    for name, definition in (
        ("owner_id", "TEXT"),
        ("owner_name", "TEXT"),
        ("visibility", "TEXT NOT NULL DEFAULT 'shared'"),
        ("version", "INTEGER NOT NULL DEFAULT 1"),
    ):
        if name not in columns:
            try:
                connection.execute(f"ALTER TABLE stored_files ADD COLUMN {name} {definition}")
            except sqlite3.OperationalError:
                current = {row["name"] for row in connection.execute("PRAGMA table_info(stored_files)")}
                if name not in current:
                    raise
    folder_columns = {row["name"] for row in connection.execute("PRAGMA table_info(file_folders)")}
    for name, definition in (
        ("owner_id", "TEXT"),
        ("owner_name", "TEXT"),
        ("visibility", "TEXT NOT NULL DEFAULT 'shared'"),
        ("version", "INTEGER NOT NULL DEFAULT 1"),
    ):
        if name not in folder_columns:
            try:
                connection.execute(f"ALTER TABLE file_folders ADD COLUMN {name} {definition}")
            except sqlite3.OperationalError:
                current = {row["name"] for row in connection.execute("PRAGMA table_info(file_folders)")}
                if name not in current:
                    raise
    connection.execute("CREATE INDEX IF NOT EXISTS stored_files_visibility_idx ON stored_files(visibility, owner_id)")
    connection.execute("CREATE INDEX IF NOT EXISTS file_folders_visibility_idx ON file_folders(visibility, owner_id)")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS file_upload_intents_recovery_idx "
        "ON file_upload_intents(state,batch_id,created_at)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS file_upload_intents_namespace_idx "
        "ON file_upload_intents(folder_id,visibility,owner_id,display_name)"
    )
    initialize_data_foundation(connection)
    file_jobs.initialize(connection)


migrate(DB_PATH, initialize_files)


def clean_name(value, fallback="Untitled"):
    name = " ".join(str(value or "").replace("\x00", "").split()).strip(" .")
    return name[:240] or fallback


class FolderAccessError(ValueError):
    def __init__(self, message, status_code):
        super().__init__(message)
        self.status_code = status_code


def valid_folder(connection, folder_id, actor, *, owner_required=False):
    if not folder_id:
        return None
    row = connection.execute("SELECT * FROM file_folders WHERE id = ?", (folder_id,)).fetchone()
    if not row or not row_is_visible(row, actor):
        raise FolderAccessError("That folder no longer exists.", 404)
    if owner_required and row["owner_id"] != actor.principal_id:
        raise FolderAccessError("Only the folder owner can add or move items here.", 403)
    return row


def _namespace_query(table, folder_id, owner_id, visibility, exclude_id=None):
    parent_column = "parent_id" if table == "file_folders" else "folder_id"
    if visibility == "private":
        clause = f"{parent_column} IS ? AND visibility='private' AND owner_id=?"
        parameters = [folder_id, owner_id]
    else:
        # Shared and neutral legacy entries intentionally occupy the visible
        # household namespace.  Private names never participate in this query.
        clause = f"{parent_column} IS ? AND visibility='shared'"
        parameters = [folder_id]
    if exclude_id is not None:
        clause += " AND id != ?"
        parameters.append(exclude_id)
    return clause, parameters


def unique_name(connection, name, folder_id, owner_id, visibility, exclude_id=None):
    base = clean_name(name)
    stem, suffix = os.path.splitext(base)
    candidate = base
    counter = 2
    file_scope, file_parameters = _namespace_query(
        "stored_files", folder_id, owner_id, visibility, exclude_id
    )
    intent_scope, intent_parameters = _namespace_query(
        "file_upload_intents", folder_id, owner_id, visibility, exclude_id
    )
    while (
        connection.execute(
            f"SELECT 1 FROM stored_files WHERE LOWER(name)=LOWER(?) AND deleted_at IS NULL AND {file_scope}",
            (candidate, *file_parameters),
        ).fetchone()
        or connection.execute(
            f"SELECT 1 FROM file_upload_intents WHERE LOWER(display_name)=LOWER(?) "
            f"AND state='prepared' AND {intent_scope}",
            (candidate, *intent_parameters),
        ).fetchone()
    ):
        candidate = f"{stem} ({counter}){suffix}"
        counter += 1
    return candidate


def folder_name_exists(connection, name, parent_id, owner_id, visibility):
    scope, parameters = _namespace_query(
        "file_folders", parent_id, owner_id, visibility
    )
    return connection.execute(
        f"SELECT 1 FROM file_folders WHERE LOWER(name)=LOWER(?) AND {scope}",
        (name, *parameters),
    ).fetchone() is not None


def visible_sql(identity, alias=""):
    prefix = f"{alias}." if alias else ""
    actor = actor_for_identity(identity)
    if actor.principal_id and actor.role in {"admin", "household"}:
        return f"({prefix}visibility='shared' OR {prefix}owner_id=?)", [actor.principal_id]
    return "0 = 1", []


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
    if item.get("owner_id"):
        item["owner_display"] = item.get("owner_name") or item.get("uploaded_by") or "Owner"
    else:
        item["owner_display"] = "Legacy (unclaimed)"
    item["is_mine"] = bool(
        identity and identity["owner_id"] and item.get("owner_id") == identity["owner_id"]
    )
    item["can_edit"] = item["is_mine"]
    item["ownership_status"] = "owned" if item.get("owner_id") else "legacy_unclaimed"
    item.pop("stored_name", None)
    item.pop("sha256", None)
    item.pop("owner_id", None)
    item.pop("owner_name", None)
    item.pop("uploaded_by", None)
    return item


def file_kind_sql(alias=""):
    """Return the fixed SQL classifier used by ``file_json`` for indexed paging.

    The expression contains no request data; callers compare its result through
    a bound parameter. Keeping classification in the query means a kind filter
    no longer reads and serializes every file before discarding most of them.
    """
    prefix = f"{alias}." if alias else ""
    name = f"LOWER({prefix}name)"
    content_type = f"LOWER({prefix}content_type)"
    text_suffixes = " OR ".join(f"{name} LIKE '%{suffix}'" for suffix in sorted(TEXT_EXTENSIONS))
    return f"""CASE
        WHEN {content_type}='application/pdf' OR {name} LIKE '%.pdf' THEN 'pdf'
        WHEN {content_type} LIKE 'image/%' THEN 'image'
        WHEN {content_type} LIKE 'video/%' THEN 'video'
        WHEN {content_type} LIKE 'audio/%' THEN 'audio'
        WHEN {content_type} LIKE 'text/%' OR {text_suffixes} THEN 'text'
        WHEN {name} LIKE '%.doc' OR {name} LIKE '%.docx' OR {name} LIKE '%.odt' OR {name} LIKE '%.rtf' THEN 'document'
        WHEN {name} LIKE '%.xls' OR {name} LIKE '%.xlsx' OR {name} LIKE '%.ods' THEN 'spreadsheet'
        WHEN {name} LIKE '%.ppt' OR {name} LIKE '%.pptx' OR {name} LIKE '%.odp' THEN 'presentation'
        ELSE 'other' END"""


def folder_json(row, identity):
    item = dict(row)
    item["owner_display"] = item.get("owner_name") or "Legacy (unclaimed)"
    item["is_mine"] = bool(identity["owner_id"] and item.get("owner_id") == identity["owner_id"])
    item["can_add"] = item["is_mine"]
    item["ownership_status"] = "owned" if item.get("owner_id") else "legacy_unclaimed"
    item.pop("owner_id", None)
    item.pop("owner_name", None)
    return item


def _expected_version(data):
    try:
        version = int(data.get("version"))
    except (TypeError, ValueError):
        return None
    return version if version > 0 else None


def _file_for_write(connection, file_id, actor):
    row = connection.execute("SELECT * FROM stored_files WHERE id = ?", (file_id,)).fetchone()
    if row is None or not row_is_visible(row, actor):
        return None, (jsonify(error="File not found."), 404)
    return row, None


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
    descriptor = -1
    try:
        descriptor, _metadata = _open_managed_row(row)
        result = subprocess.run(
            ["pdfinfo", f"/proc/self/fd/{descriptor}"],
            check=True, capture_output=True, text=True, timeout=30,
            pass_fds=(descriptor,),
        )
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    for line in result.stdout.splitlines():
        if line.lower().startswith("pages:"):
            pages = int(line.split(":", 1)[1].strip())
            if pages > MAX_PDF_PAGES:
                raise ValueError("This PDF has too many pages to preview safely.")
            if pages > 0:
                if len(pdf_page_counts) >= 256:
                    pdf_page_counts.pop(next(iter(pdf_page_counts)))
                pdf_page_counts[row["id"]] = pages
                return pages
    raise ValueError("The number of pages could not be determined.")


def trim_pdf_cache():
    entries = []
    with PDF_STORAGE.directory_descriptor() as directory:
        for name in os.listdir(directory):
            if not name.endswith(".jpg"):
                continue
            try:
                safe_component(name)
                metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
            except (FileNotFoundError, OSError, StorageSafetyError):
                continue
            if stat.S_ISREG(metadata.st_mode):
                entries.append((name, metadata.st_size, metadata.st_mtime, identity(metadata)))
    total = sum(entry[1] for entry in entries)
    if total <= PDF_CACHE_BYTES:
        return
    target = int(PDF_CACHE_BYTES * 0.8)
    for name, size, _modified, entry_identity in sorted(entries, key=lambda item: item[2]):
        PDF_STORAGE.unlink_if_identity(name, entry_identity)
        total -= size
        if total <= target:
            break


def _open_pdf_cache(name):
    descriptor, metadata = PDF_STORAGE.open_regular(name)
    try:
        if metadata.st_size <= 4 or os.pread(descriptor, 3, 0) != b"\xff\xd8\xff":
            raise StorageSafetyError("Cached PDF page is not a JPEG")
        return descriptor, metadata
    except Exception:
        os.close(descriptor)
        raise


def _render_pdf_cache(row, page, width, cache_name):
    """Render and publish a page through pinned directory descriptors."""
    source_descriptor = rendered_descriptor = -1
    rendered_metadata = None
    prefix_name = safe_component(f"pdf-{uuid.uuid4().hex}")
    rendered_name = safe_component(f"{prefix_name}.jpg")
    try:
        source_descriptor, _metadata = _open_managed_row(row)
        with INCOMING_STORAGE.directory_descriptor() as incoming_directory:
            subprocess.run(
                [
                    "pdftoppm", "-f", str(page), "-l", str(page), "-singlefile",
                    "-jpeg", "-jpegopt", "quality=84,optimize=y",
                    # Bound both dimensions even for extremely tall pages.
                    "-scale-to", str(width),
                    f"/proc/self/fd/{source_descriptor}",
                    f"/proc/self/fd/{incoming_directory}/{prefix_name}",
                ],
                check=True,
                capture_output=True,
                timeout=120,
                pass_fds=(source_descriptor, incoming_directory),
            )
        rendered_descriptor, rendered_metadata = INCOMING_STORAGE.open_regular(rendered_name)
        if (
            rendered_metadata.st_size <= 4
            or rendered_metadata.st_size > 12 * 1024 * 1024
            or os.pread(rendered_descriptor, 3, 0) != b"\xff\xd8\xff"
        ):
            raise ValueError("The page renderer produced no valid image.")
        os.fchmod(rendered_descriptor, 0o600)
        try:
            PDF_STORAGE.link_descriptor(rendered_descriptor, cache_name)
        except FileExistsError:
            # A second worker may have published the same immutable derivative.
            pass
    finally:
        if source_descriptor >= 0:
            os.close(source_descriptor)
        if rendered_metadata is None:
            # Poppler may leave a partial derivative when it times out. Only
            # remove this render's generated regular file through its pinned
            # directory; never follow a replacement link or touch the source.
            try:
                rendered_descriptor, rendered_metadata = INCOMING_STORAGE.open_regular(rendered_name)
            except (OSError, StorageSafetyError):
                pass
        if rendered_descriptor >= 0:
            os.close(rendered_descriptor)
        if rendered_metadata is not None:
            try:
                INCOMING_STORAGE.unlink_if_identity(
                    rendered_name, identity(rendered_metadata)
                )
            except (OSError, StorageSafetyError):
                pass


@files_bp.get("/files")
@require_profile(api=False)
def files_page():
    return render_template("files.html", files_upload_scope=hashlib.sha256(
        ("files-uploads:" + (current_device().get("owner_id") or "")).encode("utf-8")
    ).hexdigest())


@files_bp.get("/api/files")
@require_profile()
def list_files():
    folder_id = request.args.get("folder") or None
    query = " ".join(request.args.get("q", "").split())[:120]
    deleted = request.args.get("view") == "deleted"
    kind = request.args.get("kind", "")
    if kind and kind not in FILE_KINDS:
        return jsonify(error="That file type is not available."), 400
    requested_limit = request.args.get("limit")
    if requested_limit is None:
        limit = 40
    elif not requested_limit.isdigit():
        return jsonify(error="The file page size is invalid."), 400
    else:
        limit = min(max(int(requested_limit), 1), 100)
    if request.args.get("export") == "1":
        limit = None
    raw_offset = request.args.get("offset", "0")
    if not raw_offset.isdigit():
        return jsonify(error="The file page position is invalid."), 400
    offset = max(int(raw_offset), 0)
    include_summary = request.args.get("summary", "1") != "0"
    identity = current_device()
    actor = actor_for_identity(identity)
    if not actor.principal_id or actor.role not in {"admin", "household"}:
        return jsonify(error="Open David-Pi through an approved private Tailscale account."), 403
    if request.args.get("owner") == "mine":
        scope, parameters = "owner_id=?", [identity["owner_id"] or ""]
        folder_scope, folder_parameters = "owner_id=?", [identity["owner_id"] or ""]
    else:
        scope, parameters = "visibility='shared'", []
        folder_scope, folder_parameters = "visibility='shared'", []
    conditions = ["deleted_at IS NOT NULL" if deleted else "deleted_at IS NULL", scope]
    if not deleted:
        conditions.append("folder_id IS ?")
        parameters.append(folder_id)
    if query:
        conditions.append("LOWER(name) LIKE ?")
        parameters.append(f"%{query.lower()}%")
    if kind:
        conditions.append(f"({file_kind_sql()}) = ?")
        parameters.append(kind)
    where = " AND ".join(conditions)
    with connect(DB_PATH) as connection:
        current_folder = None
        if folder_id:
            try:
                current_folder = valid_folder(connection, folder_id, actor)
            except FolderAccessError as error:
                return jsonify(error=str(error)), error.status_code
        pagination = " LIMIT ? OFFSET ?" if limit is not None else ""
        query_parameters = [*parameters, limit + 1, offset] if limit is not None else parameters
        rows = connection.execute(
            f"SELECT * FROM stored_files WHERE {where} "
            f"ORDER BY updated_at DESC, id DESC{pagination}",
            query_parameters,
        ).fetchall()
        total = connection.execute(
            f"SELECT COUNT(*) FROM stored_files WHERE {where}", parameters
        ).fetchone()[0] if include_summary else None
        folders = [] if deleted or query or kind or offset else connection.execute(
            f"SELECT * FROM file_folders WHERE parent_id IS ? AND {folder_scope} ORDER BY LOWER(name)",
            (folder_id, *folder_parameters),
        ).fetchall()
        current = current_folder
        breadcrumbs = []
        seen = set()
        try:
            while current:
                if current["id"] in seen:
                    return jsonify(error="This folder hierarchy is invalid."), 409
                seen.add(current["id"])
                breadcrumbs.append({"id": current["id"], "name": current["name"]})
                current = (
                    valid_folder(connection, current["parent_id"], actor)
                    if current["parent_id"] else None
                )
        except FolderAccessError:
            return jsonify(error="This folder hierarchy is not available."), 404
    has_more = limit is not None and len(rows) > limit
    if has_more:
        rows = rows[:limit]
    items = [file_json(row, identity) for row in rows]
    return jsonify(
        files=items,
        folders=[folder_json(row, identity) for row in folders],
        breadcrumbs=list(reversed(breadcrumbs)),
        folder_id=folder_id,
        deleted=deleted,
        current_user=identity["name"],
        can_add_here=current_folder is None or current_folder["owner_id"] == actor.principal_id,
        total=total,
        has_more=has_more,
        next_offset=offset + len(items) if has_more else None,
        offset=offset,
        limit=limit,
    )


@files_bp.post("/api/files/folders")
@require_profile()
def create_folder():
    data = request.get_json(silent=True) or {}
    name = clean_name(data.get("name"), "")
    if not name:
        return jsonify(error="Give the folder a name."), 400
    identity = current_device()
    actor = actor_for_identity(identity)
    if not actor.principal_id or actor.role not in {"admin", "household"}:
        return jsonify(error="Open David-Pi through an approved private Tailscale account."), 403
    visibility = str(data.get("visibility", "shared")).lower()
    if visibility not in {"shared", "private"}:
        return jsonify(error="Choose Shared or Only me."), 400
    folder_id, now = uuid.uuid4().hex, utcnow()
    try:
        with connect(DB_PATH) as connection:
            connection.execute("BEGIN IMMEDIATE")
            decision = authorize("file.folder.create", actor)
            if not decision.allowed:
                return jsonify(error="Open David-Pi through an approved private Tailscale account."), 403
            parent = valid_folder(connection, data.get("parent_id"), actor, owner_required=True)
            parent_id = parent["id"] if parent else None
            if parent is not None:
                visibility = parent["visibility"]
            if folder_name_exists(
                connection, name, parent_id, actor.principal_id, visibility
            ):
                return jsonify(error="A folder with that name already exists here."), 409
            connection.execute(
                """INSERT INTO file_folders
                   (id,name,parent_id,created_at,updated_at,owner_id,owner_name,visibility,version)
                   VALUES (?,?,?,?,?,?,?,?,1)""",
                (folder_id, name, parent_id, now, now, actor.principal_id, identity["name"], visibility),
            )
            row = connection.execute("SELECT * FROM file_folders WHERE id=?", (folder_id,)).fetchone()
            audit_mutation(
                connection, actor=actor, domain="file_folder", object_id=folder_id,
                action="create", before=None, after=row,
            )
    except FolderAccessError as error:
        return jsonify(error=str(error)), error.status_code
    return jsonify(folder=folder_json(row, identity)), 201


def _descriptor_sha256(descriptor):
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return digest.hexdigest()


def _open_managed_row(row):
    descriptor, metadata = OBJECT_STORAGE.open_regular(
        row["stored_name"], expected_size=row["byte_size"]
    )
    try:
        # The digest is a portable content identity: unlike st_dev/st_ino it
        # survives the existing rsync backup/restore workflow.  Hash the exact
        # descriptor that will be served so a post-open pathname swap cannot
        # redirect either verification or response bytes.
        if _descriptor_sha256(descriptor) != row["sha256"]:
            raise StorageSafetyError("Managed object digest changed")
        return descriptor, metadata
    except Exception:
        os.close(descriptor)
        raise


def _unlink_matching(root, name, byte_size, digest):
    descriptor = -1
    try:
        descriptor, metadata = root.open_regular(name, expected_size=byte_size)
        if _descriptor_sha256(descriptor) != digest:
            return False
        return root.unlink_if_identity(name, identity(metadata))
    except (FileNotFoundError, OSError, StorageSafetyError):
        return False
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _cleanup_staged(staged):
    for item in staged:
        staged_identity = item.get("identity")
        if staged_identity:
            try:
                INCOMING_STORAGE.unlink_if_identity(item["temp_name"], staged_identity)
            except (OSError, StorageSafetyError):
                pass


def _stage_upload(upload):
    file_id = uuid.uuid4().hex
    display_name = clean_name(upload.filename, "Untitled file")
    suffix = Path(secure_filename(display_name)).suffix.lower()[:16]
    if OBJECT_STORAGE.entry_stat(safe_component(f"{file_id}{suffix}"), allow_missing=True) is not None:
        raise FileExistsError("The generated managed object already exists")
    temp_name = f"{file_id}.part"
    descriptor = -1
    created_identity = None
    try:
        descriptor, metadata = INCOMING_STORAGE.create_regular(temp_name)
        created_identity = identity(metadata)
        digest, size = hashlib.sha256(), 0
        with os.fdopen(descriptor, "wb") as target:
            descriptor = -1
            while True:
                chunk = upload.stream.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_FILE_BYTES:
                    raise ValueError("This file exceeds the 2 GB file limit.")
                digest.update(chunk)
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        return {
            "id": file_id,
            "display_name": display_name,
            "temp_name": temp_name,
            "identity": created_identity,
            "size": size,
            "sha256": digest.hexdigest(),
            "mimetype": upload.mimetype,
        }
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        if created_identity:
            try:
                INCOMING_STORAGE.unlink_if_identity(temp_name, created_identity)
            except (OSError, StorageSafetyError):
                pass
        raise


def _prepare_upload_batch(staged, actor, owner_name, folder_id, visibility, batch_id=None):
    batch_id = batch_id or uuid.uuid4().hex
    prepared = []
    with connect(DB_PATH) as connection:
        connection.execute("BEGIN IMMEDIATE")
        decision = authorize("file.upload", actor)
        if not decision.allowed:
            raise ValueError("The verified actor is no longer authorized.")
        parent = valid_folder(connection, folder_id, actor, owner_required=True)
        folder_id = parent["id"] if parent else None
        if parent is not None:
            # Folder visibility defines the namespace and prevents a shared
            # direct URL from bypassing a private folder hierarchy.
            visibility = parent["visibility"]
        for item in staged:
            suffix = Path(secure_filename(item["display_name"])).suffix.lower()[:16]
            stored_name = safe_component(f"{item['id']}{suffix}")
            if (
                connection.execute(
                    "SELECT 1 FROM stored_files WHERE id=? OR stored_name=?",
                    (item["id"], stored_name),
                ).fetchone()
                or connection.execute(
                    "SELECT 1 FROM file_upload_intents WHERE id=? OR stored_name=? OR temp_name=?",
                    (item["id"], stored_name, item["temp_name"]),
                ).fetchone()
                or OBJECT_STORAGE.entry_stat(stored_name, allow_missing=True) is not None
            ):
                raise FileExistsError("The generated managed object already exists")
            final_name = unique_name(
                connection,
                item["display_name"],
                folder_id,
                actor.principal_id,
                visibility,
            )
            content_type = (
                item["mimetype"]
                or mimetypes.guess_type(final_name)[0]
                or "application/octet-stream"
            )
            now = utcnow()
            connection.execute(
                """INSERT INTO file_upload_intents
                   (id,batch_id,owner_id,owner_name,folder_id,display_name,stored_name,
                    temp_name,content_type,byte_size,sha256,visibility,state,created_at,committed_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'prepared',?,NULL)""",
                (
                    item["id"], batch_id, actor.principal_id, owner_name, folder_id,
                    final_name, stored_name, item["temp_name"], content_type,
                    item["size"], item["sha256"], visibility, now,
                ),
            )
            prepared.append(dict(connection.execute(
                "SELECT * FROM file_upload_intents WHERE id=?", (item["id"],)
            ).fetchone()))
    return batch_id, prepared


def _intent_rows(batch_id):
    with connect(DB_PATH) as connection:
        return [dict(row) for row in connection.execute(
            "SELECT * FROM file_upload_intents WHERE batch_id=? ORDER BY created_at,id",
            (batch_id,),
        ).fetchall()]


def _verify_intent_file(root, name, intent):
    descriptor, metadata = root.open_regular(name, expected_size=intent["byte_size"])
    try:
        if _descriptor_sha256(descriptor) != intent["sha256"]:
            raise StorageSafetyError("Managed upload digest changed")
        return descriptor, metadata
    except Exception:
        os.close(descriptor)
        raise


def _ensure_intent_published(intent):
    try:
        return _verify_intent_file(OBJECT_STORAGE, intent["stored_name"], intent)
    except FileNotFoundError:
        source_descriptor, source_metadata = _verify_intent_file(
            INCOMING_STORAGE, intent["temp_name"], intent
        )
        try:
            try:
                OBJECT_STORAGE.link_descriptor(source_descriptor, intent["stored_name"])
            except FileExistsError:
                # Another worker may be recovering the same durable intent.
                pass
            published_descriptor, published_metadata = _verify_intent_file(
                OBJECT_STORAGE, intent["stored_name"], intent
            )
            if identity(published_metadata) != identity(source_metadata):
                os.close(published_descriptor)
                raise StorageSafetyError("Published upload is not the staged inode")
            return published_descriptor, published_metadata
        finally:
            os.close(source_descriptor)


def _stored_row_matches_intent(row, intent):
    if row is None:
        return False
    matches = all(
        row[column] == value
        for column, value in (
            ("id", intent["id"]),
            ("name", intent["display_name"]),
            ("folder_id", intent["folder_id"]),
            ("stored_name", intent["stored_name"]),
            ("content_type", intent["content_type"]),
            ("byte_size", intent["byte_size"]),
            ("sha256", intent["sha256"]),
            ("owner_id", intent["owner_id"]),
            ("owner_name", intent["owner_name"]),
            ("visibility", intent["visibility"]),
        )
    )
    return matches


def _finalize_upload_batch(batch_id, verified):
    added = []
    with connect(DB_PATH) as connection:
        connection.execute("BEGIN IMMEDIATE")
        intents = connection.execute(
            "SELECT * FROM file_upload_intents WHERE batch_id=? ORDER BY created_at,id",
            (batch_id,),
        ).fetchall()
        if not intents:
            raise sqlite3.IntegrityError("Upload intent batch disappeared")
        for intent in intents:
            existing = connection.execute(
                "SELECT * FROM stored_files WHERE id=?", (intent["id"],)
            ).fetchone()
            if intent["state"] == "committed":
                if not _stored_row_matches_intent(existing, intent):
                    raise sqlite3.IntegrityError("Committed upload intent does not match its file")
                added.append(intent["display_name"])
                continue
            descriptor, metadata = verified[intent["id"]]
            if not OBJECT_STORAGE.matches_descriptor(intent["stored_name"], descriptor):
                raise StorageSafetyError("Published object changed before database commit")
            if existing is None:
                connection.execute(
                    """INSERT INTO stored_files
                       (id,name,folder_id,stored_name,content_type,byte_size,sha256,
                        created_at,updated_at,uploaded_by,deleted_at,owner_id,owner_name,visibility,
                        version)
                       VALUES (?,?,?,?,?,?,?,?,?,?,NULL,?,?,?,1)""",
                    (
                        intent["id"], intent["display_name"], intent["folder_id"],
                        intent["stored_name"], intent["content_type"], intent["byte_size"],
                        intent["sha256"], intent["created_at"], intent["created_at"],
                        intent["owner_name"], intent["owner_id"], intent["owner_name"],
                        intent["visibility"],
                    ),
                )
                existing = connection.execute(
                    "SELECT * FROM stored_files WHERE id=?", (intent["id"],)
                ).fetchone()
            elif not _stored_row_matches_intent(existing, intent):
                raise sqlite3.IntegrityError("Upload intent collided with another file")
            already_audited = connection.execute(
                "SELECT 1 FROM mutation_audit WHERE domain='file' AND object_id=? AND action='upload'",
                (intent["id"],),
            ).fetchone()
            if not already_audited:
                audit_mutation(
                    connection,
                    actor=Actor(intent["owner_id"], kind="human"),
                    domain="file",
                    object_id=intent["id"],
                    action="upload",
                    before=None,
                    after=existing,
                )
            now = utcnow()
            updated = connection.execute(
                """UPDATE file_upload_intents
                   SET state='committed',committed_at=?
                   WHERE id=? AND state='prepared'""",
                (now, intent["id"]),
            )
            if updated.rowcount != 1:
                raise sqlite3.IntegrityError("Upload intent changed during finalization")
            added.append(intent["display_name"])
    return added


def _cleanup_intent_sources(intents):
    for intent in intents:
        if intent["state"] != "committed":
            continue
        source_descriptor = object_descriptor = -1
        try:
            source_descriptor, source_metadata = _verify_intent_file(
                INCOMING_STORAGE, intent["temp_name"], intent
            )
            object_descriptor, object_metadata = _verify_intent_file(
                OBJECT_STORAGE, intent["stored_name"], intent
            )
            if (
                identity(source_metadata) == identity(object_metadata)
            ):
                INCOMING_STORAGE.unlink_if_identity(
                    intent["temp_name"], identity(source_metadata)
                )
        except (FileNotFoundError, OSError, StorageSafetyError):
            # Keeping an extra hard link is safer than deleting the last known
            # good copy.  A later recovery pass can retry without exposing it.
            pass
        finally:
            if source_descriptor >= 0:
                os.close(source_descriptor)
            if object_descriptor >= 0:
                os.close(object_descriptor)


def _recover_upload_batch(batch_id):
    intents = _intent_rows(batch_id)
    if not intents:
        raise sqlite3.IntegrityError("Upload intent batch is missing")
    verified = {}
    try:
        for intent in intents:
            if intent["state"] == "prepared":
                descriptor, metadata = _ensure_intent_published(intent)
                verified[intent["id"]] = (descriptor, metadata)
        added = _finalize_upload_batch(batch_id, verified)
    finally:
        for descriptor, _metadata in verified.values():
            os.close(descriptor)
    _cleanup_intent_sources(_intent_rows(batch_id))
    return added


def _batch_is_committed(batch_id):
    intents = _intent_rows(batch_id)
    if not intents or any(intent["state"] != "committed" for intent in intents):
        return False
    with connect(DB_PATH) as connection:
        return all(
            _stored_row_matches_intent(
                connection.execute("SELECT * FROM stored_files WHERE id=?", (intent["id"],)).fetchone(),
                intent,
            )
            for intent in intents
        )


def _cancel_prepared_batch(batch_id):
    intents = []
    with connect(DB_PATH) as connection:
        connection.execute("BEGIN IMMEDIATE")
        intents = [dict(row) for row in connection.execute(
            "SELECT * FROM file_upload_intents WHERE batch_id=? ORDER BY id", (batch_id,)
        ).fetchall()]
        if not intents or any(intent["state"] != "prepared" for intent in intents):
            return False
        if any(connection.execute(
            "SELECT 1 FROM stored_files WHERE id=?", (intent["id"],)
        ).fetchone() for intent in intents):
            return False
        connection.execute(
            "DELETE FROM file_upload_intents WHERE batch_id=? AND state='prepared'", (batch_id,)
        )
    for intent in intents:
        _unlink_matching(
            OBJECT_STORAGE, intent["stored_name"], intent["byte_size"], intent["sha256"]
        )
        _unlink_matching(
            INCOMING_STORAGE, intent["temp_name"], intent["byte_size"], intent["sha256"]
        )
    return True


def recover_upload_intents(limit=1000):
    """Idempotently finish durable upload batches left by terminated workers."""
    with connect(DB_PATH) as connection:
        batches = [row[0] for row in connection.execute(
            """SELECT batch_id FROM file_upload_intents WHERE state='prepared'
               GROUP BY batch_id ORDER BY MIN(created_at),batch_id LIMIT ?""",
            (min(max(int(limit), 1), 1000),),
        ).fetchall()]
        committed = [dict(row) for row in connection.execute(
            """SELECT * FROM file_upload_intents WHERE state='committed'
               ORDER BY committed_at,id LIMIT ?""",
            (min(max(int(limit), 1), 1000),),
        ).fetchall()]
    recovered = 0
    unresolved = 0
    for batch_id in batches:
        try:
            _recover_upload_batch(batch_id)
            recovered += 1
        except (OSError, ValueError, sqlite3.Error):
            unresolved += 1
            LOGGER.exception("A durable file upload batch remains queued for recovery")
    _cleanup_intent_sources(committed)
    return {"recovered_batches": recovered, "unresolved_batches": unresolved}


@files_bp.post("/api/files/upload")
@require_profile()
def upload_files():
    identity_record = current_device()
    actor = actor_for_identity(identity_record)
    if not actor.principal_id or actor.role not in {"admin", "household"}:
        return jsonify(error="Open David-Pi through an approved private Tailscale account."), 403
    if request.is_json:
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return jsonify(error="Choose an upload receipt to retry."), 400
        batch_id = str(data.get("upload_id", ""))[:64]
        status = file_jobs.retry_upload(batch_id, actor.principal_id)
        if not status:
            return jsonify(error="Upload not found."), 404
        return _upload_response(status)
    uploads = request.files.getlist("files")
    if not uploads:
        return jsonify(error="Choose at least one file."), 400
    if len(uploads) > 100:
        return jsonify(error="Upload no more than 100 files at once."), 400
    folder_id = request.form.get("folder_id") or None
    visibility = request.form.get("visibility", "shared").lower()
    if visibility not in ("shared", "private"):
        return jsonify(error="Choose Shared or Only me."), 400
    try:
        with connect(DB_PATH) as connection:
            valid_folder(connection, folder_id, actor, owner_required=True)
    except FolderAccessError as error:
        return jsonify(error=str(error)), error.status_code
    key = request.headers.get("Idempotency-Key") or uuid.uuid4().hex
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", key):
        return jsonify(error="Use a valid upload retry identifier."), 400
    batch_id = file_jobs.upload_id(actor.principal_id, key)
    staged = []
    try:
        for upload in uploads:
            staged.append(_stage_upload(upload))
    except (OSError, ValueError, StorageSafetyError):
        _cleanup_staged(staged)
        return jsonify(error="No files were stored because one upload could not be staged safely."), 400

    try:
        created = file_jobs.register_upload(batch_id, actor, identity_record["name"], staged, folder_id, visibility)
    except ValueError as error:
        _cleanup_staged(staged)
        return jsonify(error=str(error)), 409
    except (OSError, sqlite3.Error):
        # An uncertain commit must never discard a receipt's staged originals.
        try:
            status = file_jobs.upload_status(batch_id, actor.principal_id)
        except sqlite3.Error:
            return jsonify(upload_id=batch_id, state="checking", queued=True, added=[],
                           error="The upload outcome is being checked. Keep this receipt and check again.",
                           status_url=f"/api/files/uploads/{batch_id}"), 202
        if status:
            return _upload_response(status)
        _cleanup_staged(staged)
        return jsonify(upload_id=batch_id, state="not_received", queued=False, added=[],
                       error="Upload was not received. Retry with the same selected files.",
                       status_url=f"/api/files/uploads/{batch_id}"), 503
    if not created:
        _cleanup_staged(staged)
    else:
        # Small uploads retain their immediate completion response. Large batches
        # finish outside request threads and are tracked with the same receipt.
        if sum(item["size"] for item in staged) <= 16 * 1024 * 1024:
            try:
                file_jobs.process_upload_once(batch_id)
            except (OSError, ValueError, sqlite3.Error):
                LOGGER.warning("Files upload is awaiting background completion")
    return _upload_response(file_jobs.upload_status(batch_id, actor.principal_id))


def _upload_response(status):
    response = jsonify(**status, errors=[])
    response.status_code = 201 if status["state"] == "completed" else 202
    if response.status_code == 202:
        response.headers["Retry-After"] = "3"
    return response


@files_bp.get("/api/files/uploads/<batch_id>")
@require_profile()
def file_upload_status(batch_id):
    actor = actor_for_identity(current_device())
    if not actor.principal_id or actor.role not in {"admin", "household"}:
        return jsonify(error="Upload not found."), 404
    status = file_jobs.upload_status(batch_id, actor.principal_id)
    if status is None:
        return jsonify(error="This upload has not been received yet. Retry using the same selected files.", state="not_received"), 404
    return jsonify(status)


@files_bp.get("/api/files/uploads")
@require_profile()
def file_uploads():
    actor = actor_for_identity(current_device())
    if not actor.principal_id or actor.role not in {"admin", "household"}:
        return jsonify(error="Open Files through your approved account."), 403
    key = request.args.get("key")
    if key:
        status = file_jobs.upload_status(file_jobs.upload_id(actor.principal_id, key[:128]), actor.principal_id)
        return (jsonify(status), 200) if status else (jsonify(error="Upload not received yet.", state="not_received"), 404)
    with file_jobs.connection() as connection:
        rows = connection.execute("""SELECT id FROM file_upload_batches WHERE owner_id=?
            ORDER BY CASE WHEN state IN ('queued','processing') THEN 0 WHEN state='failed' THEN 1 ELSE 2 END,created_at DESC LIMIT 20""", (actor.principal_id,)).fetchall()
    return jsonify(uploads=[file_jobs.upload_status(row["id"], actor.principal_id) for row in rows])


@files_bp.get("/api/files/<file_id>/content")
@require_profile()
def file_content(file_id):
    row = get_file(file_id)
    if not row:
        return "Not found", 404
    descriptor = -1
    handle = None
    try:
        descriptor, _metadata = _open_managed_row(row)
        handle = os.fdopen(descriptor, "rb")
        descriptor = -1
    except (OSError, ValueError, StorageSafetyError):
        if descriptor >= 0:
            os.close(descriptor)
        return "Not found", 404
    suffix = Path(row["name"]).suffix.lower()
    safe_type = SAFE_INLINE_EXTENSIONS.get(suffix)
    download = request.args.get("download") == "1" or safe_type is None
    try:
        response = send_file(
            handle,
            mimetype=safe_type or "application/octet-stream",
            as_attachment=download,
            download_name=row["name"],
            conditional=True,
            max_age=0,
        )
        response.call_on_close(handle.close)
        return response
    except Exception:
        handle.close()
        raise


@files_bp.get("/api/files/<file_id>/pdf")
@require_profile()
def pdf_details(file_id):
    row = get_file(file_id)
    if not row or file_json(row)["kind"] != "pdf":
        return jsonify(error="PDF not found."), 404
    try:
        pages = file_jobs.cached_pages(row)
        if pages:
            return jsonify(pages=pages, state="ready")
        return _pdf_job_response(file_jobs.request_pdf(row, retry=request.args.get("retry") == "1"))
    except sqlite3.Error:
        return _pdf_job_response({"state": "busy", "error": "Preview queue is busy. Try again shortly."})
    except (OSError, subprocess.SubprocessError, ValueError):
        return jsonify(error="This PDF could not be opened safely."), 422


def _pdf_job_response(job):
    state = job["state"]
    response = jsonify(state=state, error=job.get("error", ""), retry_after=1)
    response.status_code = 422 if state == "failed" else 503 if state == "busy" else 202
    response.headers["Retry-After"] = "1"
    return response


@files_bp.get("/api/files/<file_id>/pdf/pages/<int:page>")
@require_profile()
def pdf_page(file_id, page):
    row = get_file(file_id)
    if not row or file_json(row)["kind"] != "pdf":
        return jsonify(error="PDF not found."), 404
    requested_width = min(2000, max(800, request.args.get("width", 1600, type=int)))
    # Keep cache cardinality bounded while serving a smaller, faster phone page.
    width = min((1200, 1600, 2000), key=lambda candidate: abs(candidate - requested_width))
    cache_descriptor = -1
    cache_handle = None
    try:
        pages = file_jobs.cached_pages(row)
        if page < 1 or page > MAX_PDF_PAGES or (pages and page > pages):
            return jsonify(error="That page does not exist."), 404
        cache_name = safe_component(f"{row['id']}-{page}-{width}.jpg")
        try:
            cache_descriptor, _metadata = _open_pdf_cache(cache_name)
        except FileNotFoundError:
            return _pdf_job_response(file_jobs.request_pdf(
                row, page, width, prefetch=request.args.get("prefetch") == "1", retry=request.args.get("retry") == "1"
            ))
        if request.args.get("status") == "1":
            return jsonify(state="ready", url=f"/api/files/{file_id}/pdf/pages/{page}?width={width}")
        cache_handle = os.fdopen(cache_descriptor, "rb")
        cache_descriptor = -1
        response = send_file(
            cache_handle, mimetype="image/jpeg", conditional=True, max_age=604800
        )
        response.call_on_close(cache_handle.close)
        cache_handle = None
        response.set_etag(
            hashlib.sha256(
                f"{row['sha256']}:{page}:{width}".encode("utf-8")
            ).hexdigest()
        )
        response.make_conditional(request)
        response.headers["Cache-Control"] = "private, no-cache, max-age=0, must-revalidate"
        if row["visibility"] == "shared":
            response.headers[PRIVATE_REVALIDATE_SCOPE_HEADER] = "shared"
        return response
    except subprocess.TimeoutExpired:
        return jsonify(error="This page took too long to render. Try another page."), 504
    except (OSError, subprocess.SubprocessError, ValueError):
        return jsonify(error="This page could not be displayed safely."), 422
    except sqlite3.Error:
        return _pdf_job_response({"state": "busy", "error": "Preview queue is busy. Try again shortly."})
    finally:
        if cache_descriptor >= 0:
            os.close(cache_descriptor)
        if cache_handle is not None:
            cache_handle.close()


@files_bp.get("/api/files/<file_id>/text")
@require_profile()
def file_text(file_id):
    row = get_file(file_id)
    if not row or not file_json(row)["text_preview"]:
        return jsonify(error="This file cannot be read as text."), 400
    if row["byte_size"] > 25 * 1024 * 1024:
        return jsonify(error="This text file is too large to preview."), 413
    descriptor = -1
    try:
        descriptor, _metadata = _open_managed_row(row)
        suffix = Path(row["name"]).suffix.lower()
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            if suffix in DOCUMENT_TEXT_EXTENSIONS:
                member = "word/document.xml" if suffix == ".docx" else "content.xml"
                with zipfile.ZipFile(handle) as archive:
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
                text = handle.read().decode("utf-8", errors="replace")
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, ET.ParseError):
        return jsonify(error="The file could not be opened."), 404
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return jsonify(name=row["name"], text=text)


@files_bp.put("/api/files/<file_id>")
@require_profile()
def update_file(file_id):
    data = request.get_json(silent=True) or {}
    expected_version = _expected_version(data)
    if expected_version is None:
        return jsonify(error="Reload this file before changing it."), 400
    identity = current_device()
    actor = actor_for_identity(identity)
    with connect(DB_PATH) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row, error = _file_for_write(connection, file_id, actor)
        if error:
            return error
        decision = authorize("file.update", actor, row)
        if not decision.allowed:
            return jsonify(error="This shared file is read-only because you are not its owner."), 403
        if row["deleted_at"]:
            return jsonify(error="Restore this file before changing it."), 409
        if row["version"] != expected_version:
            return jsonify(error="This file changed elsewhere. Reload it before continuing.", conflict=True), 409
        try:
            if "folder_id" in data:
                parent = valid_folder(connection, data.get("folder_id"), actor, owner_required=True)
                folder_id = parent["id"] if parent else None
            else:
                folder_id = row["folder_id"]
                parent = valid_folder(connection, folder_id, actor, owner_required=True)
        except FolderAccessError as error:
            return jsonify(error=str(error)), error.status_code
        visibility = str(data.get("visibility", row["visibility"])).lower()
        if visibility not in ("shared", "private"):
            return jsonify(error="Choose Shared or Only me."), 400
        if parent is not None:
            visibility = parent["visibility"]
        name = unique_name(
            connection,
            clean_name(data.get("name", row["name"])),
            folder_id,
            actor.principal_id,
            visibility,
            file_id,
        )
        result = connection.execute(
            """UPDATE stored_files
               SET name=?,folder_id=?,visibility=?,updated_at=?,version=version+1
               WHERE id=? AND owner_id=? AND version=? AND deleted_at IS NULL""",
            (name, folder_id, visibility, utcnow(), file_id, actor.principal_id, expected_version),
        )
        if not result.rowcount:
            return jsonify(error="This file changed elsewhere. Reload it before continuing.", conflict=True), 409
        saved = connection.execute("SELECT * FROM stored_files WHERE id=?", (file_id,)).fetchone()
        audit_mutation(
            connection, actor=actor, domain="file", object_id=file_id,
            action="update", before=row, after=saved,
        )
    return jsonify(ok=True, file=file_json(saved, identity), name=name)


@files_bp.delete("/api/files/<file_id>")
@require_profile()
def trash_file(file_id):
    data = request.get_json(silent=True) or {}
    expected_version = _expected_version(data)
    if expected_version is None:
        return jsonify(error="Reload this file before changing it."), 400
    identity = current_device()
    actor = actor_for_identity(identity)
    with connect(DB_PATH) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row, error = _file_for_write(connection, file_id, actor)
        if error:
            return error
        decision = authorize("file.trash", actor, row)
        if not decision.allowed:
            return jsonify(error="This shared file is read-only because you are not its owner."), 403
        if row["deleted_at"]:
            return jsonify(error="This file is already in Recently Deleted."), 409
        if row["version"] != expected_version:
            return jsonify(error="This file changed elsewhere. Reload it before continuing.", conflict=True), 409
        now = utcnow()
        result = connection.execute(
            """UPDATE stored_files SET deleted_at=?,updated_at=?,version=version+1
               WHERE id=? AND owner_id=? AND version=? AND deleted_at IS NULL""",
            (now, now, file_id, actor.principal_id, expected_version),
        )
        if not result.rowcount:
            return jsonify(error="This file changed elsewhere. Reload it before continuing.", conflict=True), 409
        saved = connection.execute("SELECT * FROM stored_files WHERE id=?", (file_id,)).fetchone()
        audit_mutation(
            connection, actor=actor, domain="file", object_id=file_id,
            action="trash", before=row, after=saved,
        )
    return jsonify(ok=True, file=file_json(saved, identity))


@files_bp.post("/api/files/<file_id>/restore")
@require_profile()
def restore_file(file_id):
    data = request.get_json(silent=True) or {}
    expected_version = _expected_version(data)
    if expected_version is None:
        return jsonify(error="Reload this file before changing it."), 400
    identity = current_device()
    actor = actor_for_identity(identity)
    with connect(DB_PATH) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row, error = _file_for_write(connection, file_id, actor)
        if error:
            return error
        decision = authorize("file.restore", actor, row)
        if not decision.allowed:
            return jsonify(error="This shared file is read-only because you are not its owner."), 403
        if not row["deleted_at"]:
            return jsonify(error="This file is not in Recently Deleted."), 409
        if row["version"] != expected_version:
            return jsonify(error="This file changed elsewhere. Reload it before continuing.", conflict=True), 409
        name = unique_name(
            connection,
            row["name"],
            row["folder_id"],
            actor.principal_id,
            row["visibility"],
            file_id,
        )
        result = connection.execute(
            """UPDATE stored_files SET name=?,deleted_at=NULL,updated_at=?,version=version+1
               WHERE id=? AND owner_id=? AND version=? AND deleted_at IS NOT NULL""",
            (name, utcnow(), file_id, actor.principal_id, expected_version),
        )
        if not result.rowcount:
            return jsonify(error="This file changed elsewhere. Reload it before continuing.", conflict=True), 409
        saved = connection.execute("SELECT * FROM stored_files WHERE id=?", (file_id,)).fetchone()
        audit_mutation(
            connection, actor=actor, domain="file", object_id=file_id,
            action="restore", before=row, after=saved,
        )
    return jsonify(ok=True, file=file_json(saved, identity), name=name)


@files_bp.post("/api/files/<file_id>/purge")
@require_profile()
def purge_file(file_id):
    data = request.get_json(silent=True) or {}
    expected_version = _expected_version(data)
    if expected_version is None:
        return jsonify(error="Reload this file before changing it."), 400
    if data.get("confirm") != "permanently delete":
        return jsonify(error="Permanent deletion requires confirmation."), 400
    identity = current_device()
    actor = actor_for_identity(identity)
    with connect(DB_PATH) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row, error = _file_for_write(connection, file_id, actor)
        if error:
            return error
        decision = authorize("file.purge", actor, row)
        if not decision.allowed:
            return jsonify(error="This shared file is read-only because you are not its owner."), 403
        if not row["deleted_at"]:
            return jsonify(error="Only files in Recently Deleted can be removed forever."), 400
        if row["version"] != expected_version:
            return jsonify(error="This file changed elsewhere. Reload it before continuing.", conflict=True), 409
    return jsonify(
        error="Permanent deletion is paused until a protected backup newer than this deletion is independently verified.",
        retained=True,
    ), 503


def init_files(app):
    @app.before_request
    def ensure_files_background_workers():
        # Start after fork, and never start threads in fixture/test app instances.
        if not current_app.testing and os.environ.get("DAVID_PI_FILES_BACKGROUND", "1") != "0":
            file_jobs.start_workers()
    app.register_blueprint(files_bp)

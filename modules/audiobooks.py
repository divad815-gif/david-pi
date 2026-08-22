"""Private DRM-free audiobook library for David-Pi."""
import hashlib, json, os, shutil, sqlite3, subprocess, uuid
from pathlib import Path
from flask import Blueprint, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename
from .identity import current_device, require_profile
from .platform import PLATFORM_DATA, connect, migrate, utcnow
from .audiobook_streaming import derivative_paths, enqueue, playback_status

bp = Blueprint("audiobooks", __name__)
DB_PATH = PLATFORM_DATA / "audiobooks.db"
ROOT = Path(os.environ.get("DAVID_PI_AUDIOBOOKS_DATA", Path(os.environ.get("PHOTO_DATA", "/data")) / "audiobooks"))
ORIGINALS, COVERS, INCOMING, TRASH, PLAYBACK = (ROOT / name for name in ("originals", "covers", "incoming", "trash", "playback"))
ALLOWED = {".mp3", ".m4b", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".wav"}
MIMES = {".mp3":"audio/mpeg", ".m4b":"audio/mp4", ".m4a":"audio/mp4", ".aac":"audio/aac", ".ogg":"audio/ogg", ".opus":"audio/ogg", ".flac":"audio/flac", ".wav":"audio/wav"}
MAX_BYTES = int(os.environ.get("DAVID_PI_MAX_AUDIOBOOK_BYTES", 4 * 1024**3))
RESERVE = int(os.environ.get("DAVID_PI_AUDIOBOOK_RESERVE", 2 * 1024**3))
for directory in (ROOT, ORIGINALS, COVERS, INCOMING, TRASH, PLAYBACK): directory.mkdir(parents=True, exist_ok=True)

def initialize(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS audiobooks(
      id TEXT PRIMARY KEY,title TEXT NOT NULL,author TEXT NOT NULL DEFAULT '',narrator TEXT NOT NULL DEFAULT '',series TEXT NOT NULL DEFAULT '',
      original_name TEXT NOT NULL,stored_name TEXT NOT NULL UNIQUE,cover_name TEXT,content_type TEXT NOT NULL,byte_size INTEGER NOT NULL,
      sha256 TEXT NOT NULL,duration_seconds REAL NOT NULL DEFAULT 0,chapters_json TEXT NOT NULL DEFAULT '[]',owner_id TEXT,owner_name TEXT,
      visibility TEXT NOT NULL DEFAULT 'shared',created_at TEXT NOT NULL,updated_at TEXT NOT NULL,deleted_at TEXT)""")
    connection.execute("CREATE INDEX IF NOT EXISTS audiobooks_library_idx ON audiobooks(deleted_at,updated_at DESC)")
    connection.execute("CREATE INDEX IF NOT EXISTS audiobooks_owner_idx ON audiobooks(visibility,owner_id)")
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS audiobooks_owner_hash_idx ON audiobooks(owner_id,sha256) WHERE deleted_at IS NULL")
    connection.execute("""CREATE TABLE IF NOT EXISTS audiobook_progress(
      book_id TEXT NOT NULL REFERENCES audiobooks(id) ON DELETE CASCADE,owner_id TEXT NOT NULL,position_seconds REAL NOT NULL DEFAULT 0,
      completed INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL,PRIMARY KEY(book_id,owner_id))""")
migrate(DB_PATH, initialize)

def clean(value, limit=180): return " ".join(str(value or "").replace("\x00", "").split())[:limit]
def visible(identity): return ("(visibility='shared' OR owner_id=?)", [identity["owner_id"]]) if identity["owner_id"] else ("visibility='shared'", [])
def managed(root, name):
    root = root.resolve(); candidate = root / str(name)
    if candidate.is_symlink(): raise ValueError("Symbolic links are not accepted.")
    resolved = candidate.resolve(strict=False)
    if resolved != root and root not in resolved.parents: raise ValueError("Audiobook path escaped storage.")
    return resolved

def probe(path):
    try:
        result = subprocess.run(["ffprobe","-v","error","-show_entries","format=duration:format_tags=title,artist,album_artist,composer,album","-show_chapters","-of","json",str(path)], capture_output=True,text=True,timeout=30,check=True)
        payload=json.loads(result.stdout or "{}"); tags=payload.get("format",{}).get("tags",{}) or {}; duration=float(payload.get("format",{}).get("duration") or 0)
        chapters=[]
        for chapter in (payload.get("chapters") or [])[:500]:
            start=float(chapter.get("start_time") or 0); end=float(chapter.get("end_time") or start)
            chapters.append({"title":clean((chapter.get("tags") or {}).get("title") or f"Chapter {len(chapters)+1}",120),"start":start,"end":end})
        return tags,duration,chapters
    except (OSError,subprocess.SubprocessError,json.JSONDecodeError,ValueError): return {},0.0,[]

def cover(source, destination):
    try:
        subprocess.run(["ffmpeg","-nostdin","-v","error","-i",str(source),"-map","0:v:0","-frames:v","1","-vf","scale=600:600:force_original_aspect_ratio=decrease","-y",str(destination)],capture_output=True,timeout=45,check=True)
        if destination.is_file() and destination.stat().st_size: return destination.name
    except (OSError,subprocess.SubprocessError): pass
    destination.unlink(missing_ok=True); return None

def find_book(book_id, include_deleted=False):
    identity=current_device(); clause,args=visible(identity); deleted="" if include_deleted else " AND deleted_at IS NULL"
    with connect(DB_PATH) as connection: return connection.execute(f"SELECT * FROM audiobooks WHERE id=?{deleted} AND {clause}",(book_id,*args)).fetchone()

def serialize(row, identity, progress=None):
    item=dict(row); item["chapters"]=json.loads(item.pop("chapters_json") or "[]"); item["is_mine"]=bool(identity["owner_id"] and item.get("owner_id")==identity["owner_id"])
    item["owner_display"]=item.get("owner_name") or "Home"; item["stream_url"]=f"/api/audiobooks/{item['id']}/stream"; item["download_url"]=f"/api/audiobooks/{item['id']}/download"; item["cover_url"]=f"/api/audiobooks/{item['id']}/cover" if item.get("cover_name") else None
    playback=playback_status(item["id"]); item["playback_state"]=playback["state"]; item["playback_mode"]=playback["mode"]
    item["hls_url"]=f"/api/audiobooks/{item['id']}/hls/index.m3u8" if playback["mode"]=="segmented" else None
    item["position_seconds"]=(progress or {}).get("position_seconds",0); item["completed"]=bool((progress or {}).get("completed",0)); item.pop("stored_name",None); item.pop("sha256",None); return item

@bp.get("/audiobooks")
@require_profile(api=False)
def page(): return render_template("audiobooks.html")

@bp.get("/api/audiobooks")
@require_profile()
def listing():
    identity=current_device(); query=clean(request.args.get("q"),120).casefold(); deleted=request.args.get("view")=="deleted"; mine=request.args.get("owner")=="mine"
    conditions=["deleted_at IS NOT NULL" if deleted else "deleted_at IS NULL"]; args=[]
    if mine: conditions.append("owner_id=?"); args.append(identity["owner_id"] or "")
    else: conditions.append("visibility='shared'")
    if query: conditions.append("(LOWER(title) LIKE ? OR LOWER(author) LIKE ? OR LOWER(series) LIKE ?)"); args.extend([f"%{query}%"]*3)
    with connect(DB_PATH) as connection:
        rows=connection.execute(f"SELECT * FROM audiobooks WHERE {' AND '.join(conditions)} ORDER BY LOWER(author),LOWER(series),LOWER(title)",args).fetchall()
        states={row["book_id"]:dict(row) for row in connection.execute("SELECT * FROM audiobook_progress WHERE owner_id=?",(identity["owner_id"] or "",)).fetchall()}
    return jsonify(books=[serialize(row,identity,states.get(row["id"])) for row in rows],deleted=deleted,current_user=identity["name"])

@bp.post("/api/audiobooks/upload")
@require_profile()
def upload():
    identity=current_device(); visibility=request.form.get("visibility","shared").lower(); uploads=request.files.getlist("books")[:20]
    if not identity["owner_id"]: return jsonify(error="Open David-Pi through private Tailscale HTTPS."),401
    if visibility not in {"shared","private"}: return jsonify(error="Choose Shared or Only me."),400
    if not uploads: return jsonify(error="Choose at least one audiobook."),400
    added=[]; duplicates=[]; errors=[]
    for item in uploads:
        original=clean(item.filename,240) or "Untitled audiobook"; suffix=Path(secure_filename(original)).suffix.lower()
        if suffix not in ALLOWED: errors.append({"name":original,"reason":"Use a DRM-free MP3, M4B, M4A, AAC, OGG, Opus, FLAC, or WAV file."}); continue
        book_id=uuid.uuid4().hex; temporary=managed(INCOMING,f"{book_id}.part"); final=managed(ORIGINALS,f"{book_id}{suffix}"); digest=hashlib.sha256(); size=0; extracted=None
        try:
            if shutil.disk_usage(ROOT).free < RESERVE: raise ValueError("David-Pi needs more free space before importing audiobooks.")
            with temporary.open("wb") as target:
                while chunk:=item.stream.read(1024**2):
                    size+=len(chunk)
                    if size>MAX_BYTES: raise ValueError("This audiobook exceeds the 4 GB file limit.")
                    digest.update(chunk); target.write(chunk)
            if not size: raise ValueError("The selected audiobook is empty.")
            sha=digest.hexdigest()
            with connect(DB_PATH) as connection: duplicate=connection.execute("SELECT title FROM audiobooks WHERE owner_id=? AND sha256=? AND deleted_at IS NULL",(identity["owner_id"],sha)).fetchone()
            if duplicate: duplicates.append(original); temporary.unlink(missing_ok=True); continue
            tags,duration,chapters=probe(temporary); title=clean(tags.get("title"),180) or Path(original).stem[:180]; author=clean(tags.get("album_artist") or tags.get("artist") or tags.get("composer"),160); series=clean(tags.get("album"),160)
            extracted=cover(temporary,managed(COVERS,f"{book_id}.jpg")); temporary.replace(final); now=utcnow()
            try:
                with connect(DB_PATH) as connection: connection.execute("""INSERT INTO audiobooks(id,title,author,narrator,series,original_name,stored_name,cover_name,content_type,byte_size,sha256,duration_seconds,chapters_json,owner_id,owner_name,visibility,created_at,updated_at,deleted_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)""",(book_id,title,author,"",series,original,final.name,extracted,MIMES[suffix],size,sha,duration,json.dumps(chapters),identity["owner_id"],identity["name"],visibility,now,now))
            except Exception: final.unlink(missing_ok=True); managed(COVERS,extracted).unlink(missing_ok=True) if extracted else None; raise
            enqueue(book_id, final.name, size)
            added.append(title)
        except (OSError,ValueError,sqlite3.Error) as error:
            temporary.unlink(missing_ok=True); errors.append({"name":original,"reason":str(error) if isinstance(error,ValueError) else "This audiobook could not be imported safely."})
    return jsonify(added=added,duplicates=duplicates,errors=errors),201 if added else (409 if duplicates and not errors else 422)

@bp.get("/api/audiobooks/<book_id>/stream")
def stream(book_id):
    row=find_book(book_id)
    if not row: return jsonify(error="Audiobook not found."),404
    optimized=managed(PLAYBACK,row["stored_name"])
    source=optimized if optimized.is_file() and optimized.stat().st_size else managed(ORIGINALS,row["stored_name"])
    return send_file(source,mimetype=row["content_type"],conditional=True)

@bp.get("/api/audiobooks/<book_id>/hls/index.m3u8")
def hls_playlist(book_id):
    if not find_book(book_id): return jsonify(error="Audiobook not found."),404
    paths=derivative_paths(book_id)
    if not paths: return jsonify(error="Segmented playback is not ready."),404
    response=send_file(paths[0],mimetype="application/vnd.apple.mpegurl",conditional=True)
    response.headers["Cache-Control"]="private, no-store"
    response.headers["Vary"]="Cookie"
    return response

@bp.get("/api/audiobooks/<book_id>/hls/index.m4s")
def hls_media(book_id):
    if not find_book(book_id): return jsonify(error="Audiobook not found."),404
    paths=derivative_paths(book_id)
    if not paths: return jsonify(error="Segmented playback is not ready."),404
    response=send_file(paths[1],mimetype="video/iso.segment",conditional=True,max_age=604800)
    response.headers["Cache-Control"]="private, max-age=604800, immutable"
    response.headers["Vary"]="Cookie"
    return response
@bp.get("/api/audiobooks/<book_id>/download")
def download(book_id):
    row=find_book(book_id)
    return send_file(managed(ORIGINALS,row["stored_name"]),mimetype="application/octet-stream",as_attachment=True,download_name=row["original_name"],conditional=True) if row else (jsonify(error="Audiobook not found."),404)
@bp.get("/api/audiobooks/<book_id>/cover")
def get_cover(book_id):
    row=find_book(book_id)
    return send_file(managed(COVERS,row["cover_name"]),mimetype="image/jpeg",conditional=True,max_age=604800) if row and row["cover_name"] else ("",404)

@bp.put("/api/audiobooks/<book_id>/progress")
def progress(book_id):
    identity=current_device(); row=find_book(book_id); payload=request.get_json(silent=True) or {}
    if not row or not identity["owner_id"]: return jsonify(error="Audiobook not found."),404
    try: position=max(0.0,min(float(payload.get("position_seconds",0)),max(float(row["duration_seconds"] or 0),0)+30))
    except (TypeError,ValueError): return jsonify(error="Playback position is invalid."),422
    completed=bool(payload.get("completed")); now=utcnow()
    with connect(DB_PATH) as connection: connection.execute("""INSERT INTO audiobook_progress(book_id,owner_id,position_seconds,completed,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(book_id,owner_id) DO UPDATE SET position_seconds=excluded.position_seconds,completed=excluded.completed,updated_at=excluded.updated_at""",(book_id,identity["owner_id"],position,int(completed),now))
    return jsonify(ok=True,position_seconds=position,completed=completed)

@bp.put("/api/audiobooks/<book_id>")
def update(book_id):
    identity=current_device(); row=find_book(book_id); payload=request.get_json(silent=True) or {}
    if not row or row["owner_id"]!=identity["owner_id"]: return jsonify(error="Only the person who added this audiobook can change it."),403
    visibility=payload.get("visibility",row["visibility"])
    if visibility not in {"shared","private"}: return jsonify(error="Choose Shared or Only me."),422
    with connect(DB_PATH) as connection: connection.execute("UPDATE audiobooks SET title=?,author=?,visibility=?,updated_at=? WHERE id=?",(clean(payload.get("title"),180) or row["title"],clean(payload.get("author"),160),visibility,utcnow(),book_id))
    return jsonify(ok=True)
@bp.delete("/api/audiobooks/<book_id>")
def trash(book_id):
    identity=current_device(); row=find_book(book_id)
    if not row or row["owner_id"]!=identity["owner_id"]: return jsonify(error="Only the person who added this audiobook can remove it."),403
    with connect(DB_PATH) as connection: connection.execute("UPDATE audiobooks SET deleted_at=?,updated_at=? WHERE id=?",(utcnow(),utcnow(),book_id))
    return jsonify(ok=True)
@bp.post("/api/audiobooks/<book_id>/restore")
def restore(book_id):
    identity=current_device(); row=find_book(book_id,True)
    if not row or row["owner_id"]!=identity["owner_id"]: return jsonify(error="Audiobook not found."),404
    with connect(DB_PATH) as connection: connection.execute("UPDATE audiobooks SET deleted_at=NULL,updated_at=? WHERE id=?",(utcnow(),book_id))
    return jsonify(ok=True)

def init_audiobooks(app):
    with connect(DB_PATH) as connection:
        rows=connection.execute("SELECT id,stored_name,byte_size FROM audiobooks WHERE deleted_at IS NULL").fetchall()
    for row in rows:
        enqueue(row["id"],row["stored_name"],row["byte_size"])
    app.register_blueprint(bp)

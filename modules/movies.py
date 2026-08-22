import json
import os
import random
import re
import sqlite3
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request, send_file
from PIL import Image, UnidentifiedImageError

from .identity import current_device, require_profile
from .platform import PLATFORM_DATA, connect, migrate, utcnow


DB_PATH = PLATFORM_DATA / "movies.db"
POSTER_CACHE = PLATFORM_DATA / "movie-posters"
POSTER_NAME = re.compile(r"^[A-Za-z0-9_-]{5,128}\.(?:jpe?g|png|webp)$", re.IGNORECASE)
POSTER_BYTES_MAX = 5 * 1024 * 1024
POSTER_PIXELS_MAX = 20_000_000
TMDB_TOKEN = os.environ.get("TMDB_API_READ_TOKEN", "").strip()
REGION = os.environ.get("MOVIE_REGION", "US").upper()
movies_bp = Blueprint("movies", __name__)
KNOWN_SERVICES = [
    (8, "Netflix"), (9, "Prime Video"), (15, "Hulu"), (337, "Disney+"),
    (1899, "Max"), (386, "Peacock"), (531, "Paramount+"), (350, "Apple TV+"),
]


def initialize_movies(connection):
    connection.execute(
        """CREATE TABLE IF NOT EXISTS movies (
            id TEXT PRIMARY KEY, tmdb_id INTEGER UNIQUE, title TEXT NOT NULL,
            release_year INTEGER, overview TEXT NOT NULL DEFAULT '', runtime INTEGER,
            genres_json TEXT NOT NULL DEFAULT '[]', poster_url TEXT,
            added_by TEXT NOT NULL, added_at TEXT NOT NULL, watched_at TEXT
        )"""
    )
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(movies)")}
    if "media_type" not in columns:
        connection.execute("ALTER TABLE movies ADD COLUMN media_type TEXT NOT NULL DEFAULT 'movie'")
    connection.execute("CREATE INDEX IF NOT EXISTS movies_media_type_added_idx ON movies(media_type, added_at DESC)")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS availability (
            movie_id TEXT NOT NULL REFERENCES movies(id) ON DELETE CASCADE,
            provider_id INTEGER NOT NULL, provider_name TEXT NOT NULL, kind TEXT NOT NULL,
            region TEXT NOT NULL, checked_at TEXT NOT NULL, newly_available INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(movie_id, provider_id, kind, region)
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS subscriptions (
            provider_id INTEGER PRIMARY KEY, name TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0
        )"""
    )
    connection.execute("CREATE TABLE IF NOT EXISTS movie_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.executemany("INSERT OR IGNORE INTO subscriptions VALUES (?, ?, 0)", KNOWN_SERVICES)


migrate(DB_PATH, initialize_movies)


def tmdb(path, parameters=None):
    if not TMDB_TOKEN:
        raise RuntimeError("Movie search needs a free TMDB API credential.")
    url = "https://api.themoviedb.org/3" + path
    if parameters:
        url += "?" + urllib.parse.urlencode(parameters)
    request_object = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {TMDB_TOKEN}", "Accept": "application/json", "User-Agent": "David-Pi/1"},
    )
    try:
        with urllib.request.urlopen(request_object, timeout=12) as response:
            return json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise RuntimeError("The movie service is unavailable right now.") from error


def movie_json(row, connection=None):
    item = dict(row)
    item["media_type"] = item.get("media_type") or "movie"
    if item.get("tmdb_id") is not None:
        item["tmdb_id"] = abs(int(item["tmdb_id"]))
    item["genres"] = json.loads(item.pop("genres_json") or "[]")
    item["watched"] = bool(item["watched_at"])
    item["poster_url"] = poster_proxy_url(item.get("poster_url"))
    if connection:
        availability = connection.execute(
            """SELECT a.provider_id, a.provider_name, a.kind, a.region, a.checked_at, a.newly_available,
               COALESCE(s.enabled, 0) subscribed FROM availability a
               LEFT JOIN subscriptions s ON s.provider_id = a.provider_id WHERE a.movie_id = ?
               ORDER BY subscribed DESC, kind, provider_name""", (item["id"],)
        ).fetchall()
        item["availability"] = [{**dict(value), "subscribed": bool(value["subscribed"]), "newly_available": bool(value["newly_available"])} for value in availability]
        item["available_now"] = any(value["subscribed"] and value["kind"] == "included" for value in item["availability"])
        item["last_checked"] = max((value["checked_at"] for value in item["availability"]), default=None)
    return item


@movies_bp.get("/movies")
@require_profile(api=False)
def movies_page():
    return render_template("movies.html", provider_mode="tmdb" if TMDB_TOKEN else "manual")


@movies_bp.get("/api/movies")
@require_profile()
def list_movies():
    query = " ".join(request.args.get("q", "").split()).lower()[:120]
    view = request.args.get("view", "all")
    media_type = requested_media_type()
    conditions, parameters = ["m.media_type = ?"], [media_type]
    if query:
        conditions.append("LOWER(m.title) LIKE ?")
        parameters.append(f"%{query}%")
    if view == "watched":
        conditions.append("m.watched_at IS NOT NULL")
    else:
        conditions.append("m.watched_at IS NULL")
    if view in ("david", "diana"):
        conditions.append("m.added_by = ?")
        parameters.append(view)
    if view == "available":
        conditions.append(
            "EXISTS (SELECT 1 FROM availability a JOIN subscriptions s ON s.provider_id = a.provider_id "
            "WHERE a.movie_id = m.id AND a.kind = 'included' AND s.enabled = 1)"
        )
    with connect(DB_PATH) as connection:
        rows = connection.execute(
            f"SELECT m.* FROM movies m {'WHERE ' + ' AND '.join(conditions) if conditions else ''} ORDER BY m.added_at DESC",
            parameters,
        ).fetchall()
        items = [movie_json(row, connection) for row in rows]
    return jsonify(movies=items, provider_mode="tmdb" if TMDB_TOKEN else "manual", region=REGION)


@movies_bp.get("/api/movies/search")
@require_profile()
def search_movies():
    query = " ".join(request.args.get("q", "").split())[:120]
    if len(query) < 2:
        return jsonify(results=[])
    if not TMDB_TOKEN:
        return jsonify(results=[], provider_mode="manual", message="Add this movie manually until TMDB is connected.")
    media_type = requested_media_type()
    data = tmdb(f"/search/{media_type}", {"query": query, "include_adult": "false", "language": "en-US", "page": 1})
    results = []
    for item in data.get("results", [])[:12]:
        release = item.get("release_date" if media_type == "movie" else "first_air_date") or ""
        results.append({
            "tmdb_id": item.get("id"),
            "media_type": media_type,
            "title": (item.get("title") or item.get("original_title")) if media_type == "movie" else (item.get("name") or item.get("original_name")),
            "release_year": int(release[:4]) if release[:4].isdigit() else None,
            "overview": item.get("overview") or "",
            "poster_url": poster_proxy_url(f"https://image.tmdb.org/t/p/w342{item['poster_path']}") if item.get("poster_path") else None,
        })
    return jsonify(results=results, provider_mode="tmdb")


@movies_bp.post("/api/movies")
@require_profile()
def add_movie():
    data = request.get_json(silent=True) or {}
    media_type = normalized_media_type(data.get("media_type"))
    title = " ".join(str(data.get("title", "")).split())[:200]
    if not title:
        return jsonify(error=f"Give the {'movie' if media_type == 'movie' else 'TV show'} a title."), 400
    tmdb_id = data.get("tmdb_id")
    detail = {}
    if tmdb_id and TMDB_TOKEN:
        detail = tmdb(f"/{media_type}/{int(tmdb_id)}", {"language": "en-US"})
    release = detail.get("release_date" if media_type == "movie" else "first_air_date") or ""
    year = detail.get("release_year") or data.get("release_year")
    if release[:4].isdigit():
        year = int(release[:4])
    genres = [item["name"] for item in detail.get("genres", []) if item.get("name")] or data.get("genres", [])
    poster_path = detail.get("poster_path")
    poster = (
        f"https://image.tmdb.org/t/p/w342{poster_path}"
        if poster_path
        else safe_http_url(data.get("poster_url"))
    )
    movie_id = uuid.uuid4().hex
    try:
        with connect(DB_PATH) as connection:
            if not tmdb_id:
                duplicate = connection.execute(
                    """SELECT 1 FROM movies
                       WHERE LOWER(title) = LOWER(?) AND COALESCE(release_year, -1) = COALESCE(?, -1)
                       AND media_type = ?
                       LIMIT 1""",
                    (title, int(year) if str(year).isdigit() else None, media_type),
                ).fetchone()
                if duplicate:
                    return jsonify(error="That movie is already on your watchlist."), 409
            connection.execute(
                """INSERT INTO movies
                   (id, tmdb_id, title, release_year, overview, runtime, genres_json, poster_url,
                    added_by, added_at, watched_at, media_type)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
                (movie_id, stored_tmdb_id(media_type, tmdb_id) if tmdb_id else None, title, int(year) if str(year).isdigit() else None,
                 str(detail.get("overview") or data.get("overview") or "")[:3000],
                 (detail.get("runtime") if media_type == "movie" else (detail.get("episode_run_time") or [None])[0]) or data.get("runtime"),
                 json.dumps(genres[:12]), poster, "home", utcnow(), media_type),
            )
            row = connection.execute("SELECT * FROM movies WHERE id = ?", (movie_id,)).fetchone()
    except sqlite3.IntegrityError:
        return jsonify(error="That movie is already on your watchlist."), 409
    return jsonify(movie=movie_json(row)), 201


@movies_bp.delete("/api/movies/<movie_id>")
@require_profile()
def remove_movie(movie_id):
    with connect(DB_PATH) as connection:
        result = connection.execute("DELETE FROM movies WHERE id = ?", (movie_id,))
    if not result.rowcount:
        return jsonify(error="Movie not found."), 404
    return jsonify(ok=True)


@movies_bp.post("/api/movies/<movie_id>/watched")
@require_profile()
def watched_movie(movie_id):
    data = request.get_json(silent=True) or {}
    with connect(DB_PATH) as connection:
        result = connection.execute("UPDATE movies SET watched_at = ? WHERE id = ?", (None if data.get("watched") is False else utcnow(), movie_id))
    if not result.rowcount:
        return jsonify(error="Movie not found."), 404
    return jsonify(ok=True)


def refresh_availability(movie_id, tmdb_id, media_type="movie"):
    providers = tmdb(f"/{media_type}/{abs(int(tmdb_id))}/watch/providers").get("results", {}).get(REGION, {})
    kinds = {"flatrate": "included", "free": "free", "ads": "free_with_ads", "rent": "rental", "buy": "purchase"}
    checked = utcnow()
    with connect(DB_PATH) as connection:
        old = {(row["provider_id"], row["kind"]) for row in connection.execute(
            "SELECT provider_id, kind FROM availability WHERE movie_id = ? AND region = ?", (movie_id, REGION)
        )}
        connection.execute("DELETE FROM availability WHERE movie_id = ? AND region = ?", (movie_id, REGION))
        for source, kind in kinds.items():
            for provider in providers.get(source, []):
                pair = (provider["provider_id"], kind)
                connection.execute(
                    "INSERT INTO availability VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (movie_id, provider["provider_id"], provider["provider_name"], kind, REGION, checked, int(pair not in old)),
                )
    return checked


@movies_bp.post("/api/movies/check")
@require_profile()
def check_movies():
    if not TMDB_TOKEN:
        return jsonify(error="Connect a free TMDB API credential before checking streaming availability.", needs_api_key=True), 503
    media_type = requested_media_type()
    now = time.time()
    with connect(DB_PATH) as connection:
        setting_key = f"last_check_epoch_{media_type}"
        previous = connection.execute("SELECT value FROM movie_settings WHERE key = ?", (setting_key,)).fetchone()
        if previous and now - float(previous["value"]) < 60:
            return jsonify(error="Availability was just checked. Wait a minute before trying again."), 429
        connection.execute("INSERT OR REPLACE INTO movie_settings VALUES (?, ?)", (setting_key, str(now)))
        rows = connection.execute(
            "SELECT id, tmdb_id, media_type FROM movies WHERE tmdb_id IS NOT NULL AND watched_at IS NULL AND media_type = ?",
            (media_type,),
        ).fetchall()
    checked, failed = 0, 0
    for row in rows:
        try:
            refresh_availability(row["id"], row["tmdb_id"], row["media_type"])
            checked += 1
        except RuntimeError:
            failed += 1
    with connect(DB_PATH) as connection:
        available = connection.execute(
            """SELECT COUNT(DISTINCT a.movie_id) FROM availability a JOIN subscriptions s ON s.provider_id = a.provider_id
               JOIN movies m ON m.id = a.movie_id WHERE a.kind = 'included' AND s.enabled = 1
               AND m.watched_at IS NULL AND m.media_type = ?""", (media_type,)
        ).fetchone()[0]
    return jsonify(ok=True, checked=checked, failed=failed, available=available, region=REGION)


@movies_bp.get("/api/movies/subscriptions")
@require_profile()
def subscriptions():
    with connect(DB_PATH) as connection:
        rows = connection.execute("SELECT * FROM subscriptions ORDER BY name").fetchall()
    return jsonify(subscriptions=[{**dict(row), "enabled": bool(row["enabled"])} for row in rows])


@movies_bp.put("/api/movies/subscriptions")
@require_profile()
def save_subscriptions():
    data = request.get_json(silent=True) or {}
    enabled = {int(value) for value in data.get("provider_ids", []) if str(value).isdigit()}
    with connect(DB_PATH) as connection:
        connection.execute("UPDATE subscriptions SET enabled = 0")
        if enabled:
            placeholders = ",".join("?" for _ in enabled)
            connection.execute(f"UPDATE subscriptions SET enabled = 1 WHERE provider_id IN ({placeholders})", tuple(enabled))
    return jsonify(ok=True)


@movies_bp.get("/api/movies/pick")
@require_profile()
def pick_movie():
    media_type = requested_media_type()
    with connect(DB_PATH) as connection:
        available = connection.execute(
            """SELECT DISTINCT m.* FROM movies m JOIN availability a ON a.movie_id = m.id
               JOIN subscriptions s ON s.provider_id = a.provider_id
               WHERE m.watched_at IS NULL AND a.kind = 'included' AND s.enabled = 1 AND m.media_type = ?""",
            (media_type,),
        ).fetchall()
        pool = available or connection.execute(
            "SELECT * FROM movies WHERE watched_at IS NULL AND media_type = ?", (media_type,)
        ).fetchall()
        if not pool:
            return jsonify(error=f"Add an unwatched {'movie' if media_type == 'movie' else 'TV show'} first."), 404
        chosen = random.choice(pool)
        return jsonify(movie=movie_json(chosen, connection), prioritized_available=bool(available))


def init_movies(app):
    app.register_blueprint(movies_bp)


def normalized_media_type(value):
    return "tv" if str(value).lower() == "tv" else "movie"


def requested_media_type():
    return normalized_media_type(request.args.get("type", "movie"))


def stored_tmdb_id(media_type, value):
    number = abs(int(value))
    return -number if media_type == "tv" else number


def safe_http_url(value):
    if not value:
        return None
    parsed = urllib.parse.urlsplit(str(value).strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        return None
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def poster_proxy_url(value):
    """Map only TMDB's fixed poster host/path to David-Pi's same-origin cache."""
    if not value:
        return None
    text = str(value).strip()
    if text.startswith("/api/movies/poster/"):
        name = text.rsplit("/", 1)[-1]
        return text if POSTER_NAME.fullmatch(name) else None
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme != "https" or parsed.hostname != "image.tmdb.org" or parsed.port not in (None, 443):
        return safe_http_url(text)
    prefix = "/t/p/w342/"
    if not parsed.path.startswith(prefix) or parsed.query or parsed.fragment:
        return None
    name = parsed.path[len(prefix):]
    if "/" in name or not POSTER_NAME.fullmatch(name):
        return None
    return f"/api/movies/poster/{name}"


def _poster_bytes_are_safe(data):
    try:
        from io import BytesIO
        with Image.open(BytesIO(data)) as image:
            width, height = image.size
            image_format = (image.format or "").upper()
            if width < 1 or height < 1 or width * height > POSTER_PIXELS_MAX:
                return False
            if image_format not in {"JPEG", "PNG", "WEBP"}:
                return False
            image.verify()
        return True
    except (UnidentifiedImageError, OSError, ValueError):
        return False


@movies_bp.get("/api/movies/poster/<filename>")
@require_profile()
def cached_movie_poster(filename):
    if not POSTER_NAME.fullmatch(filename):
        return jsonify(error="Poster not found."), 404
    POSTER_CACHE.mkdir(parents=True, exist_ok=True)
    cache_path = POSTER_CACHE / filename
    if not cache_path.is_file():
        remote_url = f"https://image.tmdb.org/t/p/w342/{filename}"
        request_object = urllib.request.Request(
            remote_url,
            headers={"Accept": "image/jpeg,image/png,image/webp", "User-Agent": "David-Pi/1"},
        )
        try:
            with urllib.request.urlopen(request_object, timeout=10) as response:
                content_type = str(response.headers.get("Content-Type", "")).split(";", 1)[0].lower()
                if content_type not in {"image/jpeg", "image/png", "image/webp"}:
                    raise ValueError("Unexpected poster type")
                data = response.read(POSTER_BYTES_MAX + 1)
            if len(data) > POSTER_BYTES_MAX or not _poster_bytes_are_safe(data):
                raise ValueError("Unsafe poster")
            descriptor, temporary_name = tempfile.mkstemp(prefix=".poster-", dir=POSTER_CACHE)
            try:
                with os.fdopen(descriptor, "wb") as temporary:
                    temporary.write(data)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                os.chmod(temporary_name, 0o640)
                os.replace(temporary_name, cache_path)
            finally:
                if os.path.exists(temporary_name):
                    os.unlink(temporary_name)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return jsonify(error="Poster unavailable."), 404
    response = send_file(cache_path, conditional=True)
    response.headers["Cache-Control"] = "private, max-age=2592000, immutable"
    return response

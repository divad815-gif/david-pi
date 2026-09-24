import hashlib
from io import BytesIO
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request, send_file
from PIL import Image, ImageOps, UnidentifiedImageError

from .household_content import (
    allowed_actor,
    audit_mutation,
    authorize,
    expected_version,
    mutable_sql,
    row_mutable_by,
    row_visible,
    validated_visibility,
    visible_sql,
)
from .identity import require_profile
from .installation import get_installation, module_mode
from .platform import PLATFORM_DATA, connect, migrate, utcnow
from .public_http import fetch_public


DB_PATH = PLATFORM_DATA / "movies.db"
POSTER_CACHE = PLATFORM_DATA / "movie-posters"
POSTER_NAME = re.compile(r"^[A-Za-z0-9_-]{5,128}\.(?:jpe?g|png|webp)$", re.IGNORECASE)
POSTER_BYTES_MAX = 5 * 1024 * 1024
POSTER_PIXELS_MAX = 20_000_000
TMDB_TOKEN = os.environ.get("TMDB_API_READ_TOKEN", "").strip()
INSTALLATION = get_installation()
REGION = INSTALLATION["country"] if INSTALLATION else os.environ.get("MOVIE_REGION", "US").upper()
if not re.fullmatch(r"[A-Z]{2}", REGION):
    raise ValueError("Movie country must be a two-letter code.")
if INSTALLATION and module_mode("movies") != "connected":
    TMDB_TOKEN = ""
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
    additions = {
        "owner_id": "TEXT",
        "owner_name": "TEXT",
        "visibility": "TEXT NOT NULL DEFAULT 'shared' CHECK(visibility IN ('shared','private'))",
        "version": "INTEGER NOT NULL DEFAULT 1 CHECK(version > 0)",
        "deleted_at": "TEXT",
        "deleted_by": "TEXT",
        "purge_after": "TEXT",
        # New writes use this nullable locator while the legacy globally-unique
        # tmdb_id column remains untouched for rollback compatibility.
        "catalog_tmdb_id": "INTEGER",
    }
    for name, definition in additions.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE movies ADD COLUMN {name} {definition}")
    connection.execute("CREATE INDEX IF NOT EXISTS movies_media_type_added_idx ON movies(media_type, added_at DESC)")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS movies_visibility_idx "
        "ON movies(deleted_at, visibility, owner_id, media_type)"
    )
    connection.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS movies_shared_catalog_active_idx
           ON movies(media_type, COALESCE(catalog_tmdb_id, ABS(tmdb_id)))
           WHERE deleted_at IS NULL AND visibility='shared'
             AND COALESCE(catalog_tmdb_id, ABS(tmdb_id)) IS NOT NULL"""
    )
    connection.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS movies_private_catalog_active_idx
           ON movies(owner_id, media_type,
                     COALESCE(catalog_tmdb_id, ABS(tmdb_id)))
           WHERE deleted_at IS NULL AND visibility='private' AND owner_id IS NOT NULL
             AND COALESCE(catalog_tmdb_id, ABS(tmdb_id)) IS NOT NULL"""
    )
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
    if not INSTALLATION:
        connection.executemany("INSERT OR IGNORE INTO subscriptions VALUES (?, ?, 0)", KNOWN_SERVICES)
    connection.execute("CREATE TABLE IF NOT EXISTS provider_catalog (country TEXT NOT NULL, media_type TEXT NOT NULL, payload TEXT NOT NULL, fetched_at REAL NOT NULL, PRIMARY KEY(country,media_type))")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS movie_watch_state (
            movie_id TEXT NOT NULL REFERENCES movies(id) ON DELETE CASCADE,
            owner_id TEXT NOT NULL,
            watched_at TEXT,
            version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
            updated_at TEXT NOT NULL,
            PRIMARY KEY(movie_id, owner_id)
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS movie_subscription_profiles (
            owner_id TEXT PRIMARY KEY,
            record_id TEXT NOT NULL UNIQUE,
            version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
            updated_at TEXT NOT NULL
        )"""
    )
    profile_columns = {
        row["name"] for row in connection.execute(
            "PRAGMA table_info(movie_subscription_profiles)"
        )
    }
    if "record_id" not in profile_columns:
        connection.execute("ALTER TABLE movie_subscription_profiles ADD COLUMN record_id TEXT")
    for row in connection.execute(
        "SELECT owner_id FROM movie_subscription_profiles WHERE record_id IS NULL"
    ).fetchall():
        connection.execute(
            "UPDATE movie_subscription_profiles SET record_id=? WHERE owner_id=?",
            (uuid.uuid4().hex, row["owner_id"]),
        )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS movie_subscription_profile_record_idx "
        "ON movie_subscription_profiles(record_id)"
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS movie_subscriptions (
            owner_id TEXT NOT NULL,
            provider_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(owner_id, provider_id)
        )"""
    )


migrate(DB_PATH, initialize_movies)


class MovieProviderError(RuntimeError):
    def __init__(self, message, code="provider_unavailable", status=503):
        super().__init__(message)
        self.code, self.status = code, status


@movies_bp.errorhandler(MovieProviderError)
def movie_provider_error(error):
    response = jsonify(error=str(error), code=error.code, local_content_available=True)
    response.status_code = error.status
    if error.code == "provider_rate_limit":
        response.headers["Retry-After"] = "60"
    return response


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
            raw = response.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise ValueError("Provider response exceeds its limit")
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("Provider response has an unexpected shape")
            return value
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise MovieProviderError("TMDB rejected the credential. An administrator can replace it in Settings; your saved watchlist still works.", "provider_credentials", 502) from None
        if error.code == 429:
            raise MovieProviderError("TMDB has temporarily limited requests. Try again later; your saved watchlist still works.", "provider_rate_limit", 429) from None
        raise MovieProviderError("TMDB is temporarily unavailable. Try again later or add this title manually.") from None
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        raise MovieProviderError("The movie service could not be reached or returned an invalid response. Try again later; your saved watchlist still works.") from error


def actor_or_error():
    actor, name = allowed_actor()
    if actor is None:
        return None, name, (jsonify(error="Use an approved Tailscale account."), 403)
    return actor, name, None


def movie_select(alias="m"):
    return f"""{alias}.*,
        COALESCE({alias}.catalog_tmdb_id, ABS({alias}.tmdb_id)) AS effective_tmdb_id,
        CASE WHEN watch.owner_id IS NOT NULL THEN watch.watched_at
             ELSE {alias}.watched_at END AS effective_watched_at,
        watch.version AS watch_version"""


def movie_json(row, connection=None, actor_id=None):
    item = dict(row)
    item["media_type"] = item.get("media_type") or "movie"
    effective_tmdb_id = item.pop("effective_tmdb_id", None)
    if effective_tmdb_id is None:
        effective_tmdb_id = item.get("catalog_tmdb_id")
    if effective_tmdb_id is None and item.get("tmdb_id") is not None:
        effective_tmdb_id = abs(int(item["tmdb_id"]))
    item["tmdb_id"] = effective_tmdb_id
    item.pop("catalog_tmdb_id", None)
    item["genres"] = json.loads(item.pop("genres_json") or "[]")
    item["watched_at"] = item.pop("effective_watched_at", item.get("watched_at"))
    item["watched"] = bool(item["watched_at"])
    item["state_version"] = item.pop("watch_version", None)
    item["can_edit"] = row_mutable_by(item, actor_id) and not item.get("deleted_at")
    item["can_restore"] = row_mutable_by(item, actor_id) and bool(item.get("deleted_at"))
    item["legacy_read_only"] = item.get("owner_id") is None and not row_mutable_by(
        item, actor_id
    )
    item["retained_until"] = item.get("purge_after")
    item.pop("deleted_by", None)
    item.pop("purge_after", None)
    item["poster_url"] = poster_proxy_url(item.get("poster_url"), item.get("id"))
    if connection:
        if actor_id:
            availability = connection.execute(
                """SELECT a.provider_id, a.provider_name, a.kind, a.region,
                          a.checked_at, a.newly_available,
                          COALESCE(personal.enabled, legacy.enabled, 0) subscribed
                   FROM availability a
                   LEFT JOIN subscriptions legacy ON legacy.provider_id=a.provider_id
                   LEFT JOIN movie_subscriptions personal
                     ON personal.provider_id=a.provider_id AND personal.owner_id=?
                   WHERE a.movie_id=? AND a.region=?
                   ORDER BY subscribed DESC, kind, provider_name""",
                (actor_id, item["id"], REGION),
            ).fetchall()
        else:
            availability = connection.execute(
                """SELECT a.provider_id, a.provider_name, a.kind, a.region,
                          a.checked_at, a.newly_available,
                          COALESCE(s.enabled, 0) subscribed FROM availability a
                   LEFT JOIN subscriptions s ON s.provider_id=a.provider_id
                   WHERE a.movie_id=? AND a.region=? ORDER BY subscribed DESC, kind, provider_name""",
                (item["id"], REGION),
            ).fetchall()
        item["availability"] = [{**dict(value), "subscribed": bool(value["subscribed"]), "newly_available": bool(value["newly_available"])} for value in availability]
        item["available_now"] = any(value["subscribed"] and value["kind"] == "included" for value in item["availability"])
        item["last_checked"] = max((value["checked_at"] for value in item["availability"]), default=None)
    return item


@movies_bp.get("/movies")
@require_profile(api=False)
def movies_page():
    _actor, _name, error = actor_or_error()
    if error:
        return "Open David-Pi through an approved private Tailscale account.", 403
    return render_template("movies.html", provider_mode="tmdb" if TMDB_TOKEN else "manual")


@movies_bp.get("/api/movies")
@require_profile()
def list_movies():
    actor, _name, error = actor_or_error()
    if error:
        return error
    query = " ".join(request.args.get("q", "").split()).lower()[:120]
    view = request.args.get("view", "all")
    if view not in {"all", "available", "watched", "mine", "trash"} and not view.startswith("owner:"):
        return jsonify(error="That watchlist view is not available."), 400
    media_type = requested_media_type()
    limit = request.args.get("limit", default=24, type=int)
    offset = request.args.get("offset", default=0, type=int)
    limit = max(1, min(limit if limit is not None else 24, 100))
    offset = max(0, offset if offset is not None else 0)
    if view == "trash":
        conditions = ["m.media_type = ?", "m.deleted_at IS NOT NULL", mutable_sql("m")]
    else:
        conditions = ["m.media_type = ?", "m.deleted_at IS NULL", visible_sql("m")]
    parameters = [media_type, actor.principal_id]
    if query:
        conditions.append("LOWER(m.title) LIKE ?")
        parameters.append(f"%{query}%")
    if view == "trash":
        pass
    elif view == "watched":
        conditions.append(
            "CASE WHEN watch.owner_id IS NOT NULL THEN watch.watched_at "
            "ELSE m.watched_at END IS NOT NULL"
        )
    else:
        conditions.append(
            "CASE WHEN watch.owner_id IS NOT NULL THEN watch.watched_at "
            "ELSE m.watched_at END IS NULL"
        )
    if view == "mine" or view.startswith("owner:"):
        owner = actor.principal_id if view == "mine" else view.removeprefix("owner:")
        configured = get_installation()
        if configured and owner not in {m["login"] for m in configured["members"]}:
            return jsonify(error="That household member is not available."), 400
        conditions.append("m.owner_id = ?")
        parameters.append(owner)
    if view == "available":
        conditions.append(
            "EXISTS (SELECT 1 FROM availability a "
            "LEFT JOIN subscriptions legacy ON legacy.provider_id=a.provider_id "
            "LEFT JOIN movie_subscriptions personal ON personal.provider_id=a.provider_id AND personal.owner_id=? "
            f"WHERE a.movie_id=m.id AND a.region='{REGION}' AND a.kind='included' AND COALESCE(personal.enabled,legacy.enabled,0)=1)"
        )
        parameters.append(actor.principal_id)
    where = "WHERE " + " AND ".join(conditions)
    available_expression = (
        "EXISTS (SELECT 1 FROM availability a "
        "LEFT JOIN subscriptions legacy ON legacy.provider_id=a.provider_id "
        "LEFT JOIN movie_subscriptions personal ON personal.provider_id=a.provider_id AND personal.owner_id=? "
        f"WHERE a.movie_id=m.id AND a.region='{REGION}' AND a.kind='included' AND COALESCE(personal.enabled,legacy.enabled,0)=1)"
    )
    with connect(DB_PATH) as connection:
        summary = connection.execute(
            f"SELECT COUNT(*) count, COALESCE(SUM({available_expression}), 0) available_count "
            f"FROM movies m LEFT JOIN movie_watch_state watch "
            f"ON watch.movie_id=m.id AND watch.owner_id=? {where}",
            [actor.principal_id, actor.principal_id, *parameters],
        ).fetchone()
        rows = connection.execute(
            f"""SELECT {movie_select()} FROM movies m
                LEFT JOIN movie_watch_state watch
                  ON watch.movie_id=m.id AND watch.owner_id=?
                {where} ORDER BY m.added_at DESC LIMIT ? OFFSET ?""",
            [actor.principal_id, *parameters, limit, offset],
        ).fetchall()
        items = [movie_json(row, connection, actor.principal_id) for row in rows]
    count = int(summary["count"])
    return jsonify(
        movies=items,
        count=count,
        available_count=int(summary["available_count"]),
        limit=limit,
        offset=offset,
        has_more=offset + len(items) < count,
        provider_mode="tmdb" if TMDB_TOKEN else "manual",
        region=REGION,
    )


@movies_bp.get("/api/movies/search")
@require_profile()
def search_movies():
    _actor, _name, error = actor_or_error()
    if error:
        return error
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
    actor, owner_name, error = actor_or_error()
    if error:
        return error
    if not authorize("movie.create", actor).allowed:
        return jsonify(error="This account cannot add watchlist titles."), 403
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify(error="Send one watchlist title at a time."), 400
    media_type = normalized_media_type(data.get("media_type"))
    title = " ".join(str(data.get("title", "")).split())[:200]
    if not title:
        return jsonify(error=f"Give the {'movie' if media_type == 'movie' else 'TV show'} a title."), 400
    try:
        tmdb_id = normalized_tmdb_id(data.get("tmdb_id"))
        visibility = validated_visibility(data)
    except ValueError as error:
        return jsonify(error=str(error)), 400
    detail = {}
    if tmdb_id is not None and TMDB_TOKEN:
        detail = tmdb(f"/{media_type}/{tmdb_id}", {"language": "en-US"})
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
            connection.execute("BEGIN IMMEDIATE")
            if tmdb_id is not None:
                duplicate = connection.execute(
                    f"""SELECT 1 FROM movies
                        WHERE media_type=?
                          AND COALESCE(catalog_tmdb_id, ABS(tmdb_id))=?
                          AND deleted_at IS NULL AND {visible_sql()}
                        LIMIT 1""",
                    (media_type, tmdb_id, actor.principal_id),
                ).fetchone()
                if duplicate:
                    return jsonify(error="That title is already on your watchlist."), 409
            else:
                duplicate = connection.execute(
                    """SELECT 1 FROM movies
                       WHERE LOWER(title) = LOWER(?) AND COALESCE(release_year, -1) = COALESCE(?, -1)
                       AND media_type = ? AND deleted_at IS NULL AND """ + visible_sql() +
                    """
                       LIMIT 1""",
                    (title, int(year) if str(year).isdigit() else None, media_type, actor.principal_id),
                ).fetchone()
                if duplicate:
                    return jsonify(error="That movie is already on your watchlist."), 409
            connection.execute(
                """INSERT INTO movies
                   (id, tmdb_id, catalog_tmdb_id, title, release_year, overview,
                    runtime, genres_json, poster_url,
                    added_by, added_at, watched_at, media_type, owner_id, owner_name,
                    visibility, version, deleted_at, deleted_by, purge_after)
                   VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, 1, NULL, NULL, NULL)""",
                (movie_id, tmdb_id, title, int(year) if str(year).isdigit() else None,
                 str(detail.get("overview") or data.get("overview") or "")[:3000],
                 (detail.get("runtime") if media_type == "movie" else (detail.get("episode_run_time") or [None])[0]) or data.get("runtime"),
                 json.dumps(genres[:12]), poster, owner_name, utcnow(), media_type,
                 actor.principal_id, owner_name, visibility),
            )
            row = connection.execute("SELECT * FROM movies WHERE id = ?", (movie_id,)).fetchone()
            audit_mutation(
                connection, actor, domain="movie", object_id=movie_id,
                action="create", before=None, after=row,
            )
    except sqlite3.IntegrityError:
        return jsonify(error="That movie is already on your watchlist."), 409
    return jsonify(movie=movie_json(row, actor_id=actor.principal_id)), 201


@movies_bp.delete("/api/movies/<movie_id>")
@require_profile()
def remove_movie(movie_id):
    actor, _name, error = actor_or_error()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    version = expected_version(data)
    if version is None:
        return jsonify(error="Refresh this title before removing it.", conflict=True), 409
    with connect(DB_PATH) as connection:
        connection.execute("BEGIN IMMEDIATE")
        before = connection.execute(
            f"SELECT * FROM movies WHERE id=? AND deleted_at IS NULL AND {visible_sql()}",
            (movie_id, actor.principal_id),
        ).fetchone()
        if not before:
            return jsonify(error="Movie not found."), 404
        decision = authorize(
            "movie.delete", actor, owner_id=before["owner_id"],
            visibility=before["visibility"],
        )
        if not decision.allowed:
            return jsonify(
                error="Only the person who added this title can remove it.",
                legacy_read_only=before["owner_id"] is None,
            ), 403
        if int(before["version"]) != version:
            return jsonify(error="This title changed on another screen.", conflict=True), 409
        now = utcnow()
        purge_after = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        changed = connection.execute(
            f"""UPDATE movies SET deleted_at=?,deleted_by=?,purge_after=?,version=version+1
               WHERE id=? AND {mutable_sql()} AND version=? AND deleted_at IS NULL""",
            (now, actor.principal_id, purge_after, movie_id, actor.principal_id, version),
        )
        if not changed.rowcount:
            return jsonify(error="This title changed on another screen.", conflict=True), 409
        after = connection.execute("SELECT * FROM movies WHERE id=?", (movie_id,)).fetchone()
        audit_mutation(
            connection, actor, domain="movie", object_id=movie_id,
            action="trash", before=before, after=after,
        )
    return jsonify(ok=True, version=after["version"], purge_after=after["purge_after"])


@movies_bp.post("/api/movies/<movie_id>/restore")
@require_profile()
def restore_movie(movie_id):
    actor, _name, error = actor_or_error()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    version = expected_version(data)
    if version is None:
        return jsonify(error="Refresh this title before restoring it.", conflict=True), 409
    with connect(DB_PATH) as connection:
        connection.execute("BEGIN IMMEDIATE")
        before = connection.execute("SELECT * FROM movies WHERE id=?", (movie_id,)).fetchone()
        if not before or not row_visible(before, actor):
            return jsonify(error="Movie not found."), 404
        decision = authorize(
            "movie.restore", actor, owner_id=before["owner_id"],
            visibility=before["visibility"],
        )
        if not decision.allowed:
            return jsonify(error="Only the owner can restore this title."), 403
        if not before["deleted_at"]:
            return jsonify(error="That title is already on the watchlist."), 409
        catalog_id = before["catalog_tmdb_id"]
        if catalog_id is None and before["tmdb_id"] is not None:
            catalog_id = abs(int(before["tmdb_id"]))
        if catalog_id is not None:
            duplicate = connection.execute(
                f"""SELECT 1 FROM movies
                    WHERE id != ? AND media_type=?
                      AND COALESCE(catalog_tmdb_id, ABS(tmdb_id))=?
                      AND deleted_at IS NULL AND {visible_sql()}
                    LIMIT 1""",
                (
                    movie_id, before["media_type"], catalog_id,
                    actor.principal_id,
                ),
            ).fetchone()
        else:
            duplicate = connection.execute(
                f"""SELECT 1 FROM movies
                    WHERE id != ? AND LOWER(title)=LOWER(?)
                      AND COALESCE(release_year, -1)=COALESCE(?, -1)
                      AND media_type=? AND deleted_at IS NULL AND {visible_sql()}
                    LIMIT 1""",
                (
                    movie_id, before["title"], before["release_year"],
                    before["media_type"], actor.principal_id,
                ),
            ).fetchone()
        if duplicate:
            return jsonify(
                error="That title is already on your watchlist.", duplicate=True
            ), 409
        changed = connection.execute(
            f"""UPDATE movies SET deleted_at=NULL,deleted_by=NULL,purge_after=NULL,
                      version=version+1
               WHERE id=? AND {mutable_sql()} AND version=? AND deleted_at IS NOT NULL""",
            (movie_id, actor.principal_id, version),
        )
        if not changed.rowcount:
            return jsonify(error="This title changed on another screen.", conflict=True), 409
        after = connection.execute("SELECT * FROM movies WHERE id=?", (movie_id,)).fetchone()
        audit_mutation(
            connection, actor, domain="movie", object_id=movie_id,
            action="restore", before=before, after=after,
        )
    return jsonify(ok=True, version=after["version"])


@movies_bp.post("/api/movies/<movie_id>/watched")
@require_profile()
def watched_movie(movie_id):
    actor, _name, error = actor_or_error()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    if not isinstance(data.get("watched"), bool):
        return jsonify(error="Choose whether this title is watched."), 400
    state_version = expected_version(data, field="state_version")
    with connect(DB_PATH) as connection:
        connection.execute("BEGIN IMMEDIATE")
        movie = connection.execute(
            f"SELECT * FROM movies WHERE id=? AND deleted_at IS NULL AND {visible_sql()}",
            (movie_id, actor.principal_id),
        ).fetchone()
        if not movie:
            return jsonify(error="Movie not found."), 404
        before = connection.execute(
            "SELECT * FROM movie_watch_state WHERE movie_id=? AND owner_id=?",
            (movie_id, actor.principal_id),
        ).fetchone()
        decision = authorize(
            "movie.watched_state.update", actor,
            personal_owner_id=before["owner_id"] if before else None,
            allow_personal_create=before is None,
        )
        if not decision.allowed:
            return jsonify(error="That watched state belongs to another person."), 403
        if before is not None and (state_version is None or int(before["version"]) != state_version):
            return jsonify(error="Your watched state changed on another screen.", conflict=True), 409
        if before is None and state_version not in (None, 0):
            return jsonify(error="Your watched state changed on another screen.", conflict=True), 409
        watched_at = None if data.get("watched") is False else utcnow()
        if before is None:
            connection.execute(
                """INSERT INTO movie_watch_state
                   (movie_id,owner_id,watched_at,version,updated_at) VALUES (?,?,?,1,?)""",
                (movie_id, actor.principal_id, watched_at, utcnow()),
            )
        else:
            connection.execute(
                """UPDATE movie_watch_state SET watched_at=?,version=version+1,updated_at=?
                   WHERE movie_id=? AND owner_id=? AND version=?""",
                (watched_at, utcnow(), movie_id, actor.principal_id, state_version),
            )
        after = connection.execute(
            "SELECT * FROM movie_watch_state WHERE movie_id=? AND owner_id=?",
            (movie_id, actor.principal_id),
        ).fetchone()
        audit_mutation(
            connection, actor, domain="movie_watched_state",
            object_id=f"{movie_id}:{actor.principal_id}", action="update",
            before=before, after=after, object_version=int(after["version"]),
        )
    return jsonify(ok=True, watched=bool(after["watched_at"]), state_version=after["version"])


def refresh_availability(movie_id, tmdb_id, media_type="movie", actor=None):
    providers = tmdb(f"/{media_type}/{abs(int(tmdb_id))}/watch/providers").get("results", {}).get(REGION, {})
    kinds = {"flatrate": "included", "free": "free", "ads": "free_with_ads", "rent": "rental", "buy": "purchase"}
    checked = utcnow()
    with connect(DB_PATH) as connection:
        connection.execute("BEGIN IMMEDIATE")
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
        if actor is not None:
            audit_mutation(
                connection, actor, domain="movie_availability", object_id=movie_id,
                action="refresh", before=None, after=None,
            )
    return checked


@movies_bp.post("/api/movies/check")
@require_profile()
def check_movies():
    actor, _name, error = actor_or_error()
    if error:
        return error
    if not authorize("movie.availability.refresh", actor).allowed:
        return jsonify(error="This account cannot refresh availability."), 403
    if not TMDB_TOKEN:
        return jsonify(error="Connect a free TMDB API credential before checking streaming availability.", needs_api_key=True), 503
    media_type = requested_media_type()
    now = time.time()
    with connect(DB_PATH) as connection:
        import hashlib
        principal_key = hashlib.sha256(actor.principal_id.encode("utf-8")).hexdigest()[:16]
        setting_key = f"last_check_epoch_{media_type}_{principal_key}"
        previous = connection.execute("SELECT value FROM movie_settings WHERE key = ?", (setting_key,)).fetchone()
        if previous and now - float(previous["value"]) < 60:
            return jsonify(error="Availability was just checked. Wait a minute before trying again."), 429
        connection.execute("INSERT OR REPLACE INTO movie_settings VALUES (?, ?)", (setting_key, str(now)))
        rows = connection.execute(
            f"""SELECT m.id,
                       COALESCE(m.catalog_tmdb_id, ABS(m.tmdb_id)) AS tmdb_id,
                       m.media_type FROM movies m
                LEFT JOIN movie_watch_state watch
                  ON watch.movie_id=m.id AND watch.owner_id=?
                WHERE COALESCE(m.catalog_tmdb_id, ABS(m.tmdb_id)) IS NOT NULL
                  AND CASE WHEN watch.owner_id IS NOT NULL THEN watch.watched_at
                           ELSE m.watched_at END IS NULL
                  AND m.media_type=? AND m.deleted_at IS NULL AND {visible_sql('m')}""",
            (actor.principal_id, media_type, actor.principal_id),
        ).fetchall()
    checked, failed = 0, 0
    for row in rows:
        try:
            refresh_availability(row["id"], row["tmdb_id"], row["media_type"], actor)
            checked += 1
        except RuntimeError:
            failed += 1
    with connect(DB_PATH) as connection:
        available = connection.execute(
            f"""SELECT COUNT(DISTINCT a.movie_id) FROM availability a
               JOIN movies m ON m.id=a.movie_id
               LEFT JOIN movie_watch_state watch
                 ON watch.movie_id=m.id AND watch.owner_id=?
               LEFT JOIN subscriptions legacy ON legacy.provider_id=a.provider_id
               LEFT JOIN movie_subscriptions personal
                 ON personal.provider_id=a.provider_id AND personal.owner_id=?
               WHERE a.kind='included' AND a.region='{REGION}'
                 AND COALESCE(personal.enabled,legacy.enabled,0)=1
                 AND CASE WHEN watch.owner_id IS NOT NULL THEN watch.watched_at
                          ELSE m.watched_at END IS NULL
                 AND m.media_type=? AND m.deleted_at IS NULL AND {visible_sql('m')}""",
            (actor.principal_id, actor.principal_id, media_type, actor.principal_id),
        ).fetchone()[0]
    return jsonify(ok=True, checked=checked, failed=failed, available=available, region=REGION)


def regional_services(media_type):
    if not TMDB_TOKEN:
        return None, False
    with connect(DB_PATH) as connection:
        cached = connection.execute("SELECT payload,fetched_at FROM provider_catalog WHERE country=? AND media_type=?", (REGION,media_type)).fetchone()
    if cached and time.time() - cached["fetched_at"] < 86400:
        return json.loads(cached["payload"]), False
    try:
        payload = tmdb(f"/watch/providers/{media_type}", {"watch_region": REGION, "language": "en"})
        services = [{"provider_id": int(item["provider_id"]), "name": str(item["provider_name"])[:200]} for item in payload.get("results", [])[:1000] if isinstance(item, dict) and isinstance(item.get("provider_id"), int) and isinstance(item.get("provider_name"), str)]
    except (RuntimeError, TypeError, ValueError, KeyError):
        if cached:
            return json.loads(cached["payload"]), True
        raise RuntimeError("Streaming services could not be loaded for this country. Your saved watchlist is still available.")
    with connect(DB_PATH) as connection:
        connection.execute("INSERT OR REPLACE INTO provider_catalog VALUES (?,?,?,?)", (REGION,media_type,json.dumps(services),time.time()))
        connection.executemany("INSERT INTO subscriptions(provider_id,name,enabled) VALUES (?,?,0) ON CONFLICT(provider_id) DO UPDATE SET name=excluded.name", [(item["provider_id"],item["name"]) for item in services])
    return services, False


@movies_bp.get("/api/movies/subscriptions")
@require_profile()
def subscriptions():
    actor, _name, error = actor_or_error()
    if error:
        return error
    try:
        catalogue, stale = regional_services(requested_media_type())
    except RuntimeError as error:
        return jsonify(error=str(error)), 503
    with connect(DB_PATH) as connection:
        rows = connection.execute(
            """SELECT legacy.provider_id,legacy.name,
                      COALESCE(personal.enabled,legacy.enabled,0) enabled
               FROM subscriptions legacy
               LEFT JOIN movie_subscriptions personal
                 ON personal.provider_id=legacy.provider_id AND personal.owner_id=?
               ORDER BY legacy.name""",
            (actor.principal_id,),
        ).fetchall()
        profile = connection.execute(
            "SELECT version FROM movie_subscription_profiles WHERE owner_id=?",
            (actor.principal_id,),
        ).fetchone()
    if catalogue is not None:
        provider_ids = {item["provider_id"] for item in catalogue}
        rows = [row for row in rows if row["provider_id"] in provider_ids]
    return jsonify(
        region=REGION, stale=stale, provider_mode="tmdb" if TMDB_TOKEN else "manual",
        subscriptions=[{**dict(row), "enabled": bool(row["enabled"])} for row in rows],
        version=profile["version"] if profile else None,
    )


@movies_bp.put("/api/movies/subscriptions")
@require_profile()
def save_subscriptions():
    actor, _name, error = actor_or_error()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    enabled = {int(value) for value in data.get("provider_ids", []) if str(value).isdigit()}
    version = expected_version(data)
    with connect(DB_PATH) as connection:
        connection.execute("BEGIN IMMEDIATE")
        before = connection.execute(
            "SELECT * FROM movie_subscription_profiles WHERE owner_id=?",
            (actor.principal_id,),
        ).fetchone()
        decision = authorize(
            "movie.subscription.update", actor,
            personal_owner_id=before["owner_id"] if before else None,
            allow_personal_create=before is None,
        )
        if not decision.allowed:
            return jsonify(error="Those subscriptions belong to another person."), 403
        if before is not None and (version is None or int(before["version"]) != version):
            return jsonify(error="Your services changed on another screen.", conflict=True), 409
        if before is None and version not in (None, 0):
            return jsonify(error="Your services changed on another screen.", conflict=True), 409
        now = utcnow()
        if before is None:
            connection.execute(
                """INSERT INTO movie_subscription_profiles
                   (owner_id,record_id,version,updated_at) VALUES (?,?,1,?)""",
                (actor.principal_id, uuid.uuid4().hex, now),
            )
        else:
            connection.execute(
                """UPDATE movie_subscription_profiles SET version=version+1,updated_at=?
                   WHERE owner_id=? AND version=?""",
                (now, actor.principal_id, version),
            )
        services = connection.execute("SELECT provider_id,name FROM subscriptions").fetchall()
        connection.executemany(
            """INSERT INTO movie_subscriptions(owner_id,provider_id,name,enabled)
               VALUES (?,?,?,?)
               ON CONFLICT(owner_id,provider_id) DO UPDATE SET
                 name=excluded.name,enabled=excluded.enabled""",
            [(actor.principal_id, row["provider_id"], row["name"], int(row["provider_id"] in enabled))
             for row in services],
        )
        after = connection.execute(
            "SELECT * FROM movie_subscription_profiles WHERE owner_id=?",
            (actor.principal_id,),
        ).fetchone()
        audit_mutation(
            connection, actor, domain="movie_subscription",
            object_id=after["record_id"], action="update", before=before, after=after,
        )
    return jsonify(ok=True, version=after["version"])


@movies_bp.get("/api/movies/pick")
@require_profile()
def pick_movie():
    actor, _name, error = actor_or_error()
    if error:
        return error
    media_type = requested_media_type()
    with connect(DB_PATH) as connection:
        available = connection.execute(
            f"""SELECT DISTINCT {movie_select()} FROM movies m
               JOIN availability a ON a.movie_id=m.id
               LEFT JOIN movie_watch_state watch
                 ON watch.movie_id=m.id AND watch.owner_id=?
               LEFT JOIN subscriptions legacy ON legacy.provider_id=a.provider_id
               LEFT JOIN movie_subscriptions personal
                 ON personal.provider_id=a.provider_id AND personal.owner_id=?
               WHERE CASE WHEN watch.owner_id IS NOT NULL THEN watch.watched_at
                          ELSE m.watched_at END IS NULL
                 AND a.kind='included' AND a.region='{REGION}' AND COALESCE(personal.enabled,legacy.enabled,0)=1
                 AND m.media_type=? AND m.deleted_at IS NULL AND {visible_sql('m')}""",
            (actor.principal_id, actor.principal_id, media_type, actor.principal_id),
        ).fetchall()
        pool = available or connection.execute(
            f"""SELECT {movie_select()} FROM movies m
                LEFT JOIN movie_watch_state watch
                  ON watch.movie_id=m.id AND watch.owner_id=?
                WHERE CASE WHEN watch.owner_id IS NOT NULL THEN watch.watched_at
                           ELSE m.watched_at END IS NULL
                  AND m.media_type=? AND m.deleted_at IS NULL AND {visible_sql('m')}""",
            (actor.principal_id, media_type, actor.principal_id),
        ).fetchall()
        if not pool:
            return jsonify(error=f"Add an unwatched {'movie' if media_type == 'movie' else 'TV show'} first."), 404
        chosen = random.choice(pool)
        return jsonify(movie=movie_json(chosen, connection, actor.principal_id), prioritized_available=bool(available))


def init_movies(app):
    app.register_blueprint(movies_bp)


def normalized_media_type(value):
    return "tv" if str(value).lower() == "tv" else "movie"


def requested_media_type():
    return normalized_media_type(request.args.get("type", "movie"))


def stored_tmdb_id(media_type, value):
    number = abs(int(value))
    return -number if media_type == "tv" else number


def normalized_tmdb_id(value):
    """Validate a TMDB locator without accepting booleans or lossy numbers."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("Choose a valid TMDB title.")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and value.strip().isdigit():
        number = int(value.strip())
    else:
        raise ValueError("Choose a valid TMDB title.")
    if number <= 0 or number > 9_223_372_036_854_775_807:
        raise ValueError("Choose a valid TMDB title.")
    return number


def safe_http_url(value):
    if not value:
        return None
    parsed = urllib.parse.urlsplit(str(value).strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        return None
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def poster_proxy_url(value, movie_id=None):
    """Map remote posters to authenticated, same-origin cache routes."""
    if not value:
        return None
    text = str(value).strip()
    if text.startswith("/api/movies/poster/"):
        name = text.rsplit("/", 1)[-1]
        return text if POSTER_NAME.fullmatch(name) else None
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme != "https" or parsed.hostname != "image.tmdb.org" or parsed.port not in (None, 443):
        return f"/api/movies/{movie_id}/poster" if movie_id and safe_http_url(text) else None
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
    except (
        UnidentifiedImageError,
        Image.DecompressionBombError,
        OSError,
        ValueError,
    ):
        return False


def _normalized_poster_bytes(data):
    if not _poster_bytes_are_safe(data):
        raise ValueError("Unsafe poster")
    with Image.open(BytesIO(data)) as image:
        safe_image = ImageOps.exif_transpose(image)
        safe_image.thumbnail((1200, 1800), Image.Resampling.LANCZOS)
        output = BytesIO()
        safe_image.convert("RGB").save(output, "JPEG", quality=86, optimize=True)
    result = output.getvalue()
    if len(result) > POSTER_BYTES_MAX:
        raise ValueError("Unsafe poster")
    return result


@movies_bp.get("/api/movies/<movie_id>/poster")
@require_profile()
def proxied_movie_poster(movie_id):
    actor, _name, error = actor_or_error()
    if error:
        return error
    with connect(DB_PATH) as connection:
        row = connection.execute(
            f"""SELECT poster_url FROM movies
                WHERE id=? AND deleted_at IS NULL AND {visible_sql()}""",
            (movie_id, actor.principal_id),
        ).fetchone()
    source_url = safe_http_url(row["poster_url"]) if row else None
    if not source_url:
        return jsonify(error="Poster not found."), 404
    cache_name = hashlib.sha256(
        f"{movie_id}\0{source_url}".encode("utf-8")
    ).hexdigest() + ".jpg"
    POSTER_CACHE.mkdir(parents=True, exist_ok=True)
    cache_path = POSTER_CACHE / cache_name
    if not cache_path.is_file():
        if get_installation() and module_mode("movies") != "connected":
            return jsonify(error="Online posters are disabled in manual mode."), 404
        try:
            source_bytes, _content_type, _final_url = fetch_public(
                source_url, image_only=True
            )
            data = _normalized_poster_bytes(source_bytes)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".poster-", dir=POSTER_CACHE
            )
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
        except (OSError, ValueError, urllib.error.URLError):
            return jsonify(error="Poster unavailable."), 404
    response = send_file(cache_path, conditional=True, mimetype="image/jpeg")
    response.headers["Cache-Control"] = "private, no-cache, must-revalidate"
    return response


@movies_bp.get("/api/movies/poster/<filename>")
@require_profile()
def cached_movie_poster(filename):
    _actor, _name, error = actor_or_error()
    if error:
        return error
    if not POSTER_NAME.fullmatch(filename):
        return jsonify(error="Poster not found."), 404
    POSTER_CACHE.mkdir(parents=True, exist_ok=True)
    cache_path = POSTER_CACHE / filename
    if not cache_path.is_file():
        if get_installation() and module_mode("movies") != "connected":
            return jsonify(error="Online posters are disabled in manual mode."), 404
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

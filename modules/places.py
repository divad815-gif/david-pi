"""Shared restaurant wish list, reviews, chooser, and margarita tracker."""

from __future__ import annotations

import json
import logging
import os
import random
import sqlite3
import stat
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request, send_file
from PIL import Image, ImageOps, UnidentifiedImageError

from .household_content import (
    allowed_actor,
    audit_mutation,
    authorize,
    expected_version,
)
from .identity import require_profile
from .platform import PLATFORM_DATA, connect, migrate, utcnow


DB_PATH = PLATFORM_DATA / "places.db"
PLACE_IMAGE_ROOT = PLATFORM_DATA / "places"
IMAGE_ROOT = PLACE_IMAGE_ROOT / "margaritas"
RESTAURANT_IMAGE_ROOT = PLACE_IMAGE_ROOT / "restaurants"
IMAGE_ROOT.mkdir(parents=True, exist_ok=True)
RESTAURANT_IMAGE_ROOT.mkdir(parents=True, exist_ok=True)
places_bp = Blueprint("places", __name__)
LOGGER = logging.getLogger(__name__)

CUISINES = (
    "American", "Barbecue", "Breakfast", "Burgers", "Cafe", "Chinese",
    "Dessert", "Indian", "Italian", "Japanese", "Korean", "Mediterranean",
    "Mexican", "Pizza", "Seafood", "Steakhouse", "Thai", "Vegetarian",
)
MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_IMAGE_PIXELS = 30_000_000
MAX_RESTAURANT_PHOTOS = 12
RESTAURANT_SHARED_TRIGGERS = {
    "restaurants_household_shared_insert": """
        CREATE TRIGGER restaurants_household_shared_insert
        BEFORE INSERT ON restaurants
        FOR EACH ROW
        WHEN NEW.visibility IS NULL OR NEW.visibility <> 'shared'
        BEGIN
            SELECT RAISE(ABORT, 'Date Night restaurants must remain household shared');
        END
    """,
    "restaurants_household_shared_update": """
        CREATE TRIGGER restaurants_household_shared_update
        BEFORE UPDATE OF visibility ON restaurants
        FOR EACH ROW
        WHEN NEW.visibility IS NULL OR NEW.visibility <> 'shared'
        BEGIN
            SELECT RAISE(ABORT, 'Date Night restaurants must remain household shared');
        END
    """,
}


def normalized_sql(value):
    return " ".join(str(value or "").strip().rstrip(";").split()).casefold()


def enforce_household_shared_restaurants(connection):
    """Normalize legacy visibility and install a validated, persistent invariant.

    Attribution, content, deletion state, and photo rows are deliberately
    untouched. A changed row's version advances once so stale pre-migration
    editors cannot overwrite the new shared state. Reinstalling the exact
    triggers inside the serialized migration transaction makes the invariant
    safe and idempotent across older deployments.
    """
    for trigger_name in RESTAURANT_SHARED_TRIGGERS:
        connection.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")
    connection.execute(
        "UPDATE restaurants SET visibility='shared', version=version+1 "
        "WHERE visibility IS NULL OR visibility <> 'shared'"
    )
    for statement in RESTAURANT_SHARED_TRIGGERS.values():
        connection.execute(statement)
    installed = {
        row["name"]: normalized_sql(row["sql"])
        for row in connection.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
            "AND name IN (?,?)",
            tuple(RESTAURANT_SHARED_TRIGGERS),
        ).fetchall()
    }
    expected = {
        name: normalized_sql(statement)
        for name, statement in RESTAURANT_SHARED_TRIGGERS.items()
    }
    if installed != expected:
        raise RuntimeError("Date Night household-sharing database invariant is invalid")


def initialize_places(connection):
    connection.execute(
        """CREATE TABLE IF NOT EXISTS restaurants (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            cuisines_json TEXT NOT NULL DEFAULT '[]',
            notes TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'want_to_go'
                CHECK(status IN ('want_to_go', 'reviewed')),
            rating REAL,
            review TEXT NOT NULL DEFAULT '',
            visited_at TEXT,
            added_by_id TEXT,
            added_by_name TEXT NOT NULL,
            reviewed_by_id TEXT,
            reviewed_by_name TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS restaurants_status_updated_idx "
        "ON restaurants(status, updated_at DESC)"
    )
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(restaurants)")}
    if "image_name" not in columns:
        connection.execute("ALTER TABLE restaurants ADD COLUMN image_name TEXT")
    additions = {
        "owner_id": "TEXT",
        "owner_name": "TEXT",
        "visibility": "TEXT NOT NULL DEFAULT 'shared' CHECK(visibility IN ('shared','private'))",
        "version": "INTEGER NOT NULL DEFAULT 1 CHECK(version > 0)",
        "deleted_at": "TEXT",
        "deleted_by": "TEXT",
        "purge_after": "TEXT",
    }
    for name, definition in additions.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE restaurants ADD COLUMN {name} {definition}")
    enforce_household_shared_restaurants(connection)
    connection.execute(
        "CREATE INDEX IF NOT EXISTS restaurants_visibility_idx "
        "ON restaurants(deleted_at,visibility,owner_id,status,updated_at)"
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS restaurant_photos (
            id TEXT PRIMARY KEY,
            restaurant_id TEXT NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
            image_name TEXT NOT NULL UNIQUE,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )"""
    )
    photo_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(restaurant_photos)")
    }
    photo_additions = {
        "version": "INTEGER NOT NULL DEFAULT 1 CHECK(version > 0)",
        "deleted_at": "TEXT",
        "deleted_by": "TEXT",
        "purge_after": "TEXT",
    }
    for name, definition in photo_additions.items():
        if name not in photo_columns:
            connection.execute(
                f"ALTER TABLE restaurant_photos ADD COLUMN {name} {definition}"
            )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS restaurant_photos_restaurant_idx "
        "ON restaurant_photos(restaurant_id, sort_order, created_at)"
    )
    # Preserve the original single-photo release while migrating to galleries.
    connection.execute(
        """INSERT OR IGNORE INTO restaurant_photos
           (id, restaurant_id, image_name, sort_order, created_at)
           SELECT 'legacy-' || id, id, image_name, 0, updated_at
           FROM restaurants WHERE image_name IS NOT NULL AND image_name != ''"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS cuisine_categories (
            normalized_name TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            created_at TEXT NOT NULL
        )"""
    )
    connection.executemany(
        """INSERT OR IGNORE INTO cuisine_categories
           (normalized_name, display_name, created_at) VALUES (?, ?, ?)""",
        [(name.casefold(), name, utcnow()) for name in CUISINES],
    )
    for row in connection.execute(
        "SELECT cuisines_json FROM restaurants"
    ).fetchall():
        try:
            existing = json.loads(row["cuisines_json"] or "[]")
        except (TypeError, json.JSONDecodeError):
            existing = []
        connection.executemany(
            """INSERT OR IGNORE INTO cuisine_categories
               (normalized_name, display_name, created_at) VALUES (?, ?, ?)""",
            [
                (name.casefold(), name, utcnow())
                for name in normalize_cuisines(existing)
            ],
        )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS margaritas (
            month INTEGER PRIMARY KEY CHECK(month BETWEEN 1 AND 12),
            name TEXT NOT NULL DEFAULT '',
            rating REAL,
            review TEXT NOT NULL DEFAULT '',
            image_name TEXT,
            updated_by_id TEXT,
            updated_by_name TEXT,
            updated_at TEXT,
            version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0)
        )"""
    )
    connection.executemany(
        "INSERT OR IGNORE INTO margaritas(month) VALUES (?)",
        [(month,) for month in range(1, 13)],
    )
    margarita_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(margaritas)")
    }
    if "version" not in margarita_columns:
        connection.execute(
            "ALTER TABLE margaritas ADD COLUMN version "
            "INTEGER NOT NULL DEFAULT 1 CHECK(version > 0)"
        )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS margarita_records (
            month INTEGER NOT NULL CHECK(month BETWEEN 1 AND 12),
            owner_id TEXT NOT NULL,
            record_id TEXT NOT NULL UNIQUE,
            owner_name TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            rating REAL,
            review TEXT NOT NULL DEFAULT '',
            image_name TEXT,
            version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
            updated_at TEXT NOT NULL,
            PRIMARY KEY(month, owner_id)
        )"""
    )
    record_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(margarita_records)")
    }
    if "record_id" not in record_columns:
        connection.execute("ALTER TABLE margarita_records ADD COLUMN record_id TEXT")
    for row in connection.execute(
        "SELECT month,owner_id FROM margarita_records WHERE record_id IS NULL"
    ).fetchall():
        connection.execute(
            "UPDATE margarita_records SET record_id=? WHERE month=? AND owner_id=?",
            (uuid.uuid4().hex, row["month"], row["owner_id"]),
        )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS margarita_record_id_idx "
        "ON margarita_records(record_id)"
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS margarita_image_history (
            id TEXT PRIMARY KEY,
            month INTEGER NOT NULL,
            owner_id TEXT NOT NULL,
            image_name TEXT NOT NULL,
            retained_at TEXT NOT NULL,
            purge_after TEXT NOT NULL
        )"""
    )
    promote_unambiguous_margarita_records(connection)
    # The old calendar has no trustworthy year. Keep it and every predecessor
    # verbatim; new calendars are additive, never inferred from edit timestamps.
    connection.execute("""CREATE TABLE IF NOT EXISTS margarita_years (
        year INTEGER NOT NULL CHECK(year BETWEEN 1900 AND 2200),
        month INTEGER NOT NULL CHECK(month BETWEEN 1 AND 12),
        name TEXT NOT NULL DEFAULT '', rating REAL, review TEXT NOT NULL DEFAULT '',
        image_name TEXT, updated_by_id TEXT, updated_by_name TEXT, updated_at TEXT,
        version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0), PRIMARY KEY(year,month))""")
    connection.execute("""CREATE TABLE IF NOT EXISTS margarita_revisions (
        id TEXT PRIMARY KEY, year INTEGER NOT NULL, month INTEGER NOT NULL,
        snapshot_json TEXT NOT NULL, image_name TEXT, retained_at TEXT NOT NULL)""")
    connection.execute("""CREATE TABLE IF NOT EXISTS margarita_calendar_copies (
        year INTEGER PRIMARY KEY, copied_at TEXT NOT NULL, copied_by TEXT NOT NULL)""")


def margarita_calendar(value):
    if value in (None, "", "legacy"):
        return None
    try:
        year = int(value)
    except (TypeError, ValueError):
        raise ValueError("Choose a calendar year from 1900 to 2200.") from None
    if str(year) != str(value) or not 1900 <= year <= 2200:
        raise ValueError("Choose a calendar year from 1900 to 2200.")
    return year


def margarita_calendar_rows(connection, year):
    if year is None:
        return connection.execute("SELECT * FROM margaritas ORDER BY month").fetchall()
    saved = {row["month"]: row for row in connection.execute(
        "SELECT * FROM margarita_years WHERE year=? ORDER BY month", (year,))}
    return [saved.get(month) or dict(year=year, month=month, name="", rating=None,
        review="", image_name=None, updated_by_id=None, updated_by_name=None,
        updated_at=None, version=1) for month in range(1, 13)]


def margarita_row_populated(row) -> bool:
    """Return whether a canonical or personal month contains saved content."""
    if row is None:
        return False
    return bool(
        clean_text(row["name"], 160)
        or row["rating"] is not None
        or clean_multiline(row["review"], 3000)
        or clean_text(row["image_name"], 255)
    )


def margarita_row_pristine(row) -> bool:
    """Return whether a canonical month has never held a shared decision."""
    if row is None or margarita_row_populated(row):
        return False
    keys = set(row.keys())
    return bool(
        int(row["version"] or 1) == 1
        and not clean_text(row["updated_by_id"] if "updated_by_id" in keys else None, 255)
        and not clean_text(row["updated_by_name"] if "updated_by_name" in keys else None, 255)
        and not clean_text(row["updated_at"] if "updated_at" in keys else None, 255)
    )


def populated_personal_margarita_records(connection, month):
    return [
        row
        for row in connection.execute(
            """SELECT * FROM margarita_records
               WHERE month=? ORDER BY owner_id, record_id""",
            (month,),
        ).fetchall()
        if margarita_row_populated(row)
    ]


def promote_unambiguous_margarita_records(connection):
    """Copy one clear personal predecessor into a blank shared calendar month.

    Personal rows are immutable migration sources. Ambiguity is deliberately
    left for a household member to resolve in the shared editor.
    """
    canonical_rows = connection.execute(
        "SELECT * FROM margaritas ORDER BY month"
    ).fetchall()
    for canonical in canonical_rows:
        if not margarita_row_pristine(canonical):
            continue
        records = populated_personal_margarita_records(connection, canonical["month"])
        if len(records) == 1:
            source = records[0]
            connection.execute(
                """UPDATE margaritas
                   SET name=?,rating=?,review=?,image_name=?,updated_by_id=?,
                       updated_by_name=?,updated_at=?,version=?
                   WHERE month=?
                     AND TRIM(COALESCE(name,''))=''
                     AND rating IS NULL
                     AND TRIM(COALESCE(review,''))=''
                     AND TRIM(COALESCE(image_name,''))=''
                     AND TRIM(COALESCE(updated_by_id,''))=''
                     AND TRIM(COALESCE(updated_by_name,''))=''
                     AND TRIM(COALESCE(updated_at,''))=''
                     AND version=1""",
                (
                    source["name"], source["rating"], source["review"],
                    source["image_name"], source["owner_id"], source["owner_name"],
                    source["updated_at"],
                    max(int(canonical["version"] or 1), int(source["version"] or 1)),
                    canonical["month"],
                ),
            )
        elif len(records) > 1:
            LOGGER.warning(
                "Margarita calendar migration requires household review: "
                "month=%d populated_personal_records=%d; canonical row preserved",
                canonical["month"], len(records),
            )


def margarita_migration_conflicts(connection) -> set[int]:
    """Return blank canonical months with ambiguous populated predecessors."""
    conflicts = set()
    for canonical in connection.execute(
        "SELECT * FROM margaritas ORDER BY month"
    ).fetchall():
        if margarita_row_pristine(canonical) and len(
            populated_personal_margarita_records(connection, canonical["month"])
        ) > 1:
            conflicts.add(int(canonical["month"]))
    return conflicts


def clean_text(value, maximum):
    return " ".join(str(value or "").split())[:maximum]


def clean_multiline(value, maximum):
    return str(value or "").replace("\x00", "").strip()[:maximum]


def normalize_cuisines(value):
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        name = clean_text(item, 40)
        if name and name.casefold() not in {existing.casefold() for existing in result}:
            result.append(name)
        if len(result) == 12:
            break
    return result


def normalize_rating(value, required=False):
    if value in (None, "") and not required:
        return None
    try:
        rating = float(value)
    except (TypeError, ValueError):
        raise ValueError("Choose a rating from 1 to 5 stars.")
    if rating < 1 or rating > 5 or abs(rating * 2 - round(rating * 2)) > 0.001:
        raise ValueError("Ratings use half-star steps from 1 to 5.")
    return rating


migrate(DB_PATH, initialize_places)


def actor_or_error():
    actor, name = allowed_actor()
    if actor is None:
        return None, name, (jsonify(error="Use an approved Tailscale account."), 403)
    return actor, name, None


def photo_json(row):
    return {
        "id": row["id"],
        "url": f"/api/places/restaurants/{row['restaurant_id']}/photos/{row['id']}",
    }


def restaurant_json(row, connection=None, actor_id=None):
    item = dict(row)
    item["cuisines"] = json.loads(item.pop("cuisines_json") or "[]")
    if connection is None:
        with connect(DB_PATH) as opened:
            return restaurant_json(row, opened, actor_id)
    photos = [] if item.get("deleted_at") else connection.execute(
        """SELECT id, restaurant_id, image_name FROM restaurant_photos
           WHERE restaurant_id=? AND deleted_at IS NULL
           ORDER BY sort_order, created_at, id""",
        (item["id"],),
    ).fetchall()
    item["photos"] = [photo_json(photo) for photo in photos]
    item["has_image"] = bool(photos)
    item["image_url"] = item["photos"][0]["url"] if photos else None
    household_member = bool(actor_id)
    item["can_edit"] = household_member and not item.get("deleted_at")
    item["can_restore"] = household_member and bool(item.get("deleted_at"))
    item["can_review"] = household_member and not item.get("deleted_at")
    item["legacy_read_only"] = False
    item["retained_until"] = item.get("purge_after")
    item.pop("image_name", None)
    item.pop("deleted_by", None)
    item.pop("purge_after", None)
    return item


def restaurant_input():
    if request.is_json:
        return request.get_json(silent=True) or {}, [], []
    cuisines = request.form.getlist("cuisines")
    uploads = [item for item in request.files.getlist("images") if item and item.filename]
    legacy_upload = request.files.get("image")
    if legacy_upload and legacy_upload.filename:
        uploads.append(legacy_upload)
    data = {
        "name": request.form.get("name"),
        "notes": request.form.get("notes"),
        "cuisines": cuisines,
        "version": request.form.get("version"),
    }
    if "visibility" in request.form:
        data["visibility"] = request.form.get("visibility")
    return data, uploads, request.form.getlist("remove_photo_ids")


def save_restaurant_photos(uploads):
    if len(uploads) > MAX_RESTAURANT_PHOTOS:
        raise ValueError(f"Choose no more than {MAX_RESTAURANT_PHOTOS} photos at once.")
    names = []
    try:
        for upload in uploads:
            name = save_place_image(upload, RESTAURANT_IMAGE_ROOT)
            if name:
                names.append(name)
    except Exception:
        for name in names:
            unlink_managed_image(RESTAURANT_IMAGE_ROOT, name)
        raise
    return names


def add_photo_rows(connection, restaurant_id, names):
    existing = connection.execute(
        """SELECT COUNT(*) AS total FROM restaurant_photos
           WHERE restaurant_id=? AND deleted_at IS NULL""",
        (restaurant_id,),
    ).fetchone()["total"]
    if existing + len(names) > MAX_RESTAURANT_PHOTOS:
        raise ValueError(f"A restaurant can have up to {MAX_RESTAURANT_PHOTOS} photos.")
    now = utcnow()
    connection.executemany(
        """INSERT INTO restaurant_photos
           (id, restaurant_id, image_name, sort_order, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        [(uuid.uuid4().hex, restaurant_id, name, existing + index, now)
         for index, name in enumerate(names)],
    )


def discard_unreferenced_restaurant_images(names):
    """Delete only staging artifacts proven unreferenced after a failed request."""
    if not names:
        return
    with connect(DB_PATH) as connection:
        placeholders = ",".join("?" for _ in names)
        referenced = {
            row["image_name"] for row in connection.execute(
                f"SELECT image_name FROM restaurant_photos WHERE image_name IN ({placeholders})",
                names,
            ).fetchall()
        }
    for name in names:
        if name not in referenced:
            unlink_managed_image(RESTAURANT_IMAGE_ROOT, name)


def unlink_managed_image(root, name):
    """Remove only a generated direct child through a pinned real directory."""
    if not name or Path(name).name != name:
        return False
    directory_fd = None
    try:
        directory_fd = os.open(
            root,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
        )
        os.unlink(name, dir_fd=directory_fd)
        os.fsync(directory_fd)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False
    finally:
        if directory_fd is not None:
            os.close(directory_fd)


def open_managed_image(root, name):
    """Open an image through pinned directory/file descriptors without symlinks."""
    if not name or Path(name).name != name:
        return None
    directory_fd = None
    file_fd = None
    try:
        directory_fd = os.open(
            root,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
        )
        file_fd = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
        details = os.fstat(file_fd)
        if not stat.S_ISREG(details.st_mode):
            os.close(file_fd)
            return None
        return os.fdopen(file_fd, "rb")
    except OSError:
        if file_fd is not None:
            try:
                os.close(file_fd)
            except OSError:
                pass
        return None
    finally:
        if directory_fd is not None:
            os.close(directory_fd)


def available_cuisines(connection=None):
    if connection is None:
        with connect(DB_PATH) as opened:
            return available_cuisines(opened)
    catalog = {name.casefold(): name for name in CUISINES}
    for row in connection.execute(
        "SELECT normalized_name,display_name FROM cuisine_categories"
    ).fetchall():
        catalog.setdefault(row["normalized_name"], row["display_name"])
    return sorted(catalog.values(), key=str.casefold)


def remember_cuisines(connection, cuisines):
    connection.executemany(
        """INSERT OR IGNORE INTO cuisine_categories
           (normalized_name, display_name, created_at) VALUES (?, ?, ?)""",
        [(name.casefold(), name, utcnow()) for name in cuisines],
    )


@places_bp.get("/places")
@require_profile(api=False)
def places_page():
    actor, _name, error = actor_or_error()
    if error:
        return "Open David-Pi through an approved private Tailscale account.", 403
    return render_template(
        "places.html", cuisines=available_cuisines()
    )


@places_bp.get("/api/places/restaurants")
@require_profile()
def list_restaurants():
    actor, _name, error = actor_or_error()
    if error:
        return error
    view = request.args.get("view", "want_to_go")
    sort = request.args.get("sort", "recent")
    query = clean_text(request.args.get("q"), 120).casefold()
    cuisine = clean_text(request.args.get("cuisine"), 40).casefold()
    if view not in {"want_to_go", "reviewed", "all", "trash"}:
        return jsonify(error="That restaurant view is not available."), 400
    order = {
        "recent": "COALESCE(visited_at, updated_at) DESC, updated_at DESC",
        "rating": "rating DESC, COALESCE(visited_at, updated_at) DESC",
        "name": "name COLLATE NOCASE ASC",
    }.get(sort)
    if not order:
        return jsonify(error="That sorting option is not available."), 400
    if view == "trash":
        conditions, parameters = ["deleted_at IS NOT NULL"], []
    else:
        conditions, parameters = ["deleted_at IS NULL"], []
    if view not in {"all", "trash"}:
        conditions.append("status = ?")
        parameters.append(view)
    with connect(DB_PATH) as connection:
        rows = connection.execute(
            f"SELECT * FROM restaurants "
            f"{'WHERE ' + ' AND '.join(conditions) if conditions else ''} "
            f"ORDER BY {order}",
            parameters,
        ).fetchall()
        items = [restaurant_json(row, connection, actor.principal_id) for row in rows]
    if query:
        items = [
            item for item in items
            if query in item["name"].casefold()
            or query in item["notes"].casefold()
            or query in item["review"].casefold()
            or any(query in value.casefold() for value in item["cuisines"])
        ]
    if cuisine:
        items = [
            item for item in items
            if any(cuisine == value.casefold() for value in item["cuisines"])
        ]
    return jsonify(
        restaurants=items,
        cuisines=available_cuisines(),
        count=len(items),
    )


@places_bp.post("/api/places/restaurants")
@require_profile()
def add_restaurant():
    actor, owner_name, error = actor_or_error()
    if error:
        return error
    if not authorize("place.restaurant.create", actor).allowed:
        return jsonify(error="This account cannot add restaurants."), 403
    data, uploads, _ = restaurant_input()
    name = clean_text(data.get("name"), 160)
    if not name:
        return jsonify(error="Give the restaurant a name."), 400
    cuisines = normalize_cuisines(data.get("cuisines"))
    restaurant_id = uuid.uuid4().hex
    now = utcnow()
    # Date Night is a single household module. Cached clients may still send a
    # former private value; it is intentionally ignored and saved as shared.
    visibility = "shared"
    image_names = []
    try:
        image_names = save_restaurant_photos(uploads)
        with connect(DB_PATH) as connection:
            connection.execute("BEGIN IMMEDIATE")
            duplicate = connection.execute(
                """SELECT 1 FROM restaurants WHERE LOWER(name)=LOWER(?)
                    AND deleted_at IS NULL LIMIT 1""",
                (name,),
            ).fetchone()
            if duplicate:
                for image_name in image_names:
                    unlink_managed_image(RESTAURANT_IMAGE_ROOT, image_name)
                return jsonify(error="That restaurant is already on the list."), 409
            connection.execute(
                """INSERT INTO restaurants
                   (id, name, cuisines_json, notes, status, added_by_id,
                    added_by_name, created_at, updated_at, owner_id, owner_name,
                    visibility, version, deleted_at, deleted_by, purge_after)
                   VALUES (?, ?, ?, ?, 'want_to_go', ?, ?, ?, ?, ?, ?, ?, 1, NULL, NULL, NULL)""",
                (
                    restaurant_id, name, json.dumps(cuisines),
                    clean_multiline(data.get("notes"), 1500),
                    actor.principal_id, owner_name, now, now,
                    actor.principal_id, owner_name, visibility,
                ),
            )
            remember_cuisines(connection, cuisines)
            add_photo_rows(connection, restaurant_id, image_names)
            row = connection.execute("SELECT * FROM restaurants WHERE id = ?", (restaurant_id,)).fetchone()
            audit_mutation(
                connection, actor, domain="place_restaurant", object_id=restaurant_id,
                action="create", before=None, after=row,
            )
    except ValueError as error:
        discard_unreferenced_restaurant_images(image_names)
        return jsonify(error=str(error)), 400
    except sqlite3.IntegrityError:
        discard_unreferenced_restaurant_images(image_names)
        return jsonify(error="That restaurant could not be added."), 409
    except Exception:
        discard_unreferenced_restaurant_images(image_names)
        raise
    return jsonify(restaurant=restaurant_json(row, actor_id=actor.principal_id)), 201


@places_bp.put("/api/places/restaurants/<restaurant_id>")
@require_profile()
def update_restaurant(restaurant_id):
    actor, _owner_name, error = actor_or_error()
    if error:
        return error
    data, uploads, remove_photo_ids = restaurant_input()
    version = expected_version(data)
    if version is None:
        return jsonify(error="Refresh this restaurant before saving it.", conflict=True), 409
    name = clean_text(data.get("name"), 160)
    if not name:
        return jsonify(error="Give the restaurant a name."), 400
    cuisines = normalize_cuisines(data.get("cuisines"))
    with connect(DB_PATH) as connection:
        preliminary = connection.execute(
            "SELECT * FROM restaurants WHERE id=? AND deleted_at IS NULL",
            (restaurant_id,),
        ).fetchone()
    if not preliminary:
        return jsonify(error="Restaurant not found."), 404
    decision = authorize("place.restaurant.update", actor)
    if not decision.allowed:
        return jsonify(
            error="This account cannot change the household restaurant list.",
        ), 403
    if int(preliminary["version"]) != version:
        return jsonify(error="This restaurant changed on another screen.", conflict=True), 409
    visibility = "shared"
    try:
        new_images = save_restaurant_photos(uploads)
    except ValueError as error:
        return jsonify(error=str(error)), 400
    try:
        with connect(DB_PATH) as connection:
            connection.execute("BEGIN IMMEDIATE")
            before = connection.execute(
                "SELECT * FROM restaurants WHERE id=? AND deleted_at IS NULL",
                (restaurant_id,),
            ).fetchone()
            if not before:
                for image_name in new_images:
                    unlink_managed_image(RESTAURANT_IMAGE_ROOT, image_name)
                return jsonify(error="Restaurant not found."), 404
            decision = authorize("place.restaurant.update", actor)
            if not decision.allowed:
                for image_name in new_images:
                    unlink_managed_image(RESTAURANT_IMAGE_ROOT, image_name)
                return jsonify(error="This account cannot change the household restaurant list."), 403
            if int(before["version"]) != version:
                for image_name in new_images:
                    unlink_managed_image(RESTAURANT_IMAGE_ROOT, image_name)
                return jsonify(error="This restaurant changed on another screen.", conflict=True), 409
            valid_remove_ids = [clean_text(value, 64) for value in remove_photo_ids if clean_text(value, 64)]
            if valid_remove_ids:
                placeholders = ",".join("?" for _ in valid_remove_ids)
                now = utcnow()
                purge_after = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
                connection.execute(
                    f"""UPDATE restaurant_photos
                        SET deleted_at=?,deleted_by=?,purge_after=?,version=version+1
                        WHERE restaurant_id=? AND id IN ({placeholders})
                          AND deleted_at IS NULL""",
                    [now, actor.principal_id, purge_after, restaurant_id, *valid_remove_ids],
                )
            add_photo_rows(connection, restaurant_id, new_images)
            remember_cuisines(connection, cuisines)
            changed = connection.execute(
                """UPDATE restaurants SET name=?, cuisines_json=?, notes=?, visibility=?,
                          updated_at=?,version=version+1
                   WHERE id=? AND version=? AND deleted_at IS NULL""",
                (
                    name, json.dumps(cuisines),
                    clean_multiline(data.get("notes"), 1500),
                    visibility,
                    utcnow(), restaurant_id, version,
                ),
            )
            row = connection.execute(
                "SELECT * FROM restaurants WHERE id = ?", (restaurant_id,)
            ).fetchone()
            if not changed.rowcount:
                raise sqlite3.IntegrityError("stale restaurant version")
            audit_mutation(
                connection, actor, domain="place_restaurant", object_id=restaurant_id,
                action="update", before=before, after=row,
            )
    except (sqlite3.Error, ValueError) as error:
        discard_unreferenced_restaurant_images(new_images)
        if isinstance(error, ValueError):
            return jsonify(error=str(error)), 400
        return jsonify(error="That restaurant could not be updated."), 409
    except Exception:
        discard_unreferenced_restaurant_images(new_images)
        raise
    return jsonify(restaurant=restaurant_json(row, actor_id=actor.principal_id))


@places_bp.get("/api/places/restaurants/<restaurant_id>/image")
@require_profile()
def restaurant_image(restaurant_id):
    actor, _name, error = actor_or_error()
    if error:
        return error
    with connect(DB_PATH) as connection:
        row = connection.execute(
            """SELECT restaurant.image_name FROM restaurants restaurant
                JOIN restaurant_photos photo
                  ON photo.restaurant_id=restaurant.id
                 AND photo.image_name=restaurant.image_name
                 AND photo.deleted_at IS NULL
                WHERE restaurant.id=? AND restaurant.deleted_at IS NULL""",
            (restaurant_id,),
        ).fetchone()
    if not row or not row["image_name"]:
        return jsonify(error="Restaurant photo not found."), 404
    opened = open_managed_image(RESTAURANT_IMAGE_ROOT, row["image_name"])
    if opened is None:
        return jsonify(error="Restaurant photo not found."), 404
    return send_file(
        opened, download_name="restaurant.jpg", mimetype="image/jpeg",
        conditional=True, max_age=604800,
    )


@places_bp.get("/api/places/restaurants/<restaurant_id>/photos/<photo_id>")
@require_profile()
def restaurant_photo(restaurant_id, photo_id):
    actor, _name, error = actor_or_error()
    if error:
        return error
    with connect(DB_PATH) as connection:
        row = connection.execute(
            """SELECT photo.image_name FROM restaurant_photos photo
                JOIN restaurants restaurant ON restaurant.id=photo.restaurant_id
                WHERE photo.id=? AND photo.restaurant_id=?
                  AND photo.deleted_at IS NULL AND restaurant.deleted_at IS NULL""",
            (photo_id, restaurant_id),
        ).fetchone()
    if not row:
        return jsonify(error="Restaurant photo not found."), 404
    opened = open_managed_image(RESTAURANT_IMAGE_ROOT, row["image_name"])
    if opened is None:
        return jsonify(error="Restaurant photo not found."), 404
    return send_file(
        opened, download_name="restaurant.jpg", mimetype="image/jpeg",
        conditional=True, max_age=604800,
    )


@places_bp.post("/api/places/restaurants/<restaurant_id>/review")
@require_profile()
def review_restaurant(restaurant_id):
    actor, reviewer_name, error = actor_or_error()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    try:
        rating = normalize_rating(data.get("rating"), required=True)
    except ValueError as error:
        return jsonify(error=str(error)), 400
    visited_at = clean_text(data.get("visited_at"), 10) or date.today().isoformat()
    try:
        date.fromisoformat(visited_at)
    except ValueError:
        return jsonify(error="Choose a valid visit date."), 400
    version = expected_version(data)
    if version is None:
        return jsonify(error="Refresh this restaurant before saving the review.", conflict=True), 409
    with connect(DB_PATH) as connection:
        connection.execute("BEGIN IMMEDIATE")
        before = connection.execute(
            "SELECT * FROM restaurants WHERE id=? AND deleted_at IS NULL",
            (restaurant_id,),
        ).fetchone()
        if not before:
            return jsonify(error="Restaurant not found."), 404
        decision = authorize("place.review.update", actor)
        if not decision.allowed:
            return jsonify(
                error="This account cannot change the household restaurant reviews.",
            ), 403
        if int(before["version"]) != version:
            return jsonify(error="This restaurant changed on another screen.", conflict=True), 409
        changed = connection.execute(
            """UPDATE restaurants
               SET status='reviewed', rating=?, review=?, visited_at=?,
                   reviewed_by_id=?, reviewed_by_name=?, updated_at=?,version=version+1
               WHERE id=? AND version=? AND deleted_at IS NULL""",
            (
                rating, clean_multiline(data.get("review"), 5000), visited_at,
                actor.principal_id, reviewer_name, utcnow(), restaurant_id, version,
            ),
        )
        row = connection.execute(
            "SELECT * FROM restaurants WHERE id = ?", (restaurant_id,)
        ).fetchone()
        if changed.rowcount:
            audit_mutation(
                connection, actor, domain="place_review", object_id=restaurant_id,
                action="update", before=before, after=row,
            )
    if not changed.rowcount:
        return jsonify(error="Restaurant not found."), 404
    return jsonify(restaurant=restaurant_json(row, actor_id=actor.principal_id))


@places_bp.delete("/api/places/restaurants/<restaurant_id>")
@require_profile()
def delete_restaurant(restaurant_id):
    actor, _name, error = actor_or_error()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    version = expected_version(data)
    if version is None:
        return jsonify(error="Refresh this restaurant before removing it.", conflict=True), 409
    with connect(DB_PATH) as connection:
        connection.execute("BEGIN IMMEDIATE")
        before = connection.execute(
            "SELECT * FROM restaurants WHERE id=? AND deleted_at IS NULL",
            (restaurant_id,),
        ).fetchone()
        if not before:
            return jsonify(error="Restaurant not found."), 404
        decision = authorize("place.restaurant.delete", actor)
        if not decision.allowed:
            return jsonify(
                error="This account cannot change the household restaurant list.",
            ), 403
        if int(before["version"]) != version:
            return jsonify(error="This restaurant changed on another screen.", conflict=True), 409
        now = utcnow()
        purge_after = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        changed = connection.execute(
            """UPDATE restaurants SET deleted_at=?,deleted_by=?,purge_after=?,
                      version=version+1,updated_at=?
               WHERE id=? AND version=? AND deleted_at IS NULL""",
            (now, actor.principal_id, purge_after, now,
             restaurant_id, version),
        )
        if not changed.rowcount:
            return jsonify(error="This restaurant changed on another screen.", conflict=True), 409
        after = connection.execute("SELECT * FROM restaurants WHERE id=?", (restaurant_id,)).fetchone()
        audit_mutation(
            connection, actor, domain="place_restaurant", object_id=restaurant_id,
            action="trash", before=before, after=after,
        )
    return jsonify(ok=True, version=after["version"], purge_after=after["purge_after"])


@places_bp.post("/api/places/restaurants/<restaurant_id>/restore")
@require_profile()
def restore_restaurant(restaurant_id):
    actor, _name, error = actor_or_error()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    version = expected_version(data)
    if version is None:
        return jsonify(error="Refresh this restaurant before restoring it.", conflict=True), 409
    with connect(DB_PATH) as connection:
        connection.execute("BEGIN IMMEDIATE")
        before = connection.execute("SELECT * FROM restaurants WHERE id=?", (restaurant_id,)).fetchone()
        if not before:
            return jsonify(error="Restaurant not found."), 404
        decision = authorize("place.restaurant.restore", actor)
        if not decision.allowed:
            return jsonify(error="This account cannot change the household restaurant list."), 403
        if not before["deleted_at"]:
            return jsonify(error="That restaurant is already on the list."), 409
        changed = connection.execute(
            """UPDATE restaurants SET deleted_at=NULL,deleted_by=NULL,purge_after=NULL,
                      version=version+1,updated_at=?
               WHERE id=? AND version=? AND deleted_at IS NOT NULL""",
            (utcnow(), restaurant_id, version),
        )
        if not changed.rowcount:
            return jsonify(error="This restaurant changed on another screen.", conflict=True), 409
        after = connection.execute("SELECT * FROM restaurants WHERE id=?", (restaurant_id,)).fetchone()
        audit_mutation(
            connection, actor, domain="place_restaurant", object_id=restaurant_id,
            action="restore", before=before, after=after,
        )
    return jsonify(ok=True, version=after["version"])


@places_bp.post("/api/places/choose")
@require_profile()
def choose_restaurant():
    actor, _name, error = actor_or_error()
    if error:
        return error
    if not authorize("place.choice.create", actor).allowed:
        return jsonify(error="This account cannot use the restaurant chooser."), 403
    data = request.get_json(silent=True) or {}
    pool = data.get("pool", "all")
    if pool not in {"all", "unvisited", "highly_rated"}:
        return jsonify(error="Choose a valid restaurant pool."), 400
    cuisines = {item.casefold() for item in normalize_cuisines(data.get("cuisines"))}
    conditions, parameters = ["deleted_at IS NULL"], []
    if pool == "unvisited":
        conditions.append("status='want_to_go'")
    elif pool == "highly_rated":
        conditions.extend(("status='reviewed'", "rating >= 4"))
    with connect(DB_PATH) as connection:
        rows = connection.execute(
            "SELECT * FROM restaurants"
            + (" WHERE " + " AND ".join(conditions) if conditions else ""),
            parameters,
        ).fetchall()
    choices = [restaurant_json(row, actor_id=actor.principal_id) for row in rows]
    if cuisines:
        choices = [
            item for item in choices
            if cuisines.intersection(value.casefold() for value in item["cuisines"])
        ]
    if not choices:
        return jsonify(
            error="No restaurants match those choices yet. Try a wider pool."
        ), 404
    return jsonify(restaurant=random.SystemRandom().choice(choices), pool_size=len(choices))


@places_bp.get("/api/places/margaritas")
@require_profile()
def list_margaritas():
    _actor, _name, error = actor_or_error()
    if error:
        return error
    try:
        year = margarita_calendar(request.args.get("year"))
    except ValueError as error:
        return jsonify(error=str(error)), 422
    with connect(DB_PATH) as connection:
        rows = margarita_calendar_rows(connection, year)
        conflicts = margarita_migration_conflicts(connection) if year is None else set()
        years = [row[0] for row in connection.execute("SELECT DISTINCT year FROM margarita_years ORDER BY year DESC")]
        copies = [row[0] for row in connection.execute("SELECT year FROM margarita_calendar_copies ORDER BY year")]
    return jsonify(
        calendar="legacy" if year is None else str(year), years=years, legacy_copied_to=copies,
        calendar_label="Existing calendar — year unconfirmed" if year is None else str(year),
        margaritas=[
            {
                **dict(row),
                "calendar": "legacy" if year is None else str(year),
                # Preserve the pre-shared response keys as attribution aliases.
                "owner_id": row["updated_by_id"],
                "owner_name": row["updated_by_name"],
                "month_name": MONTHS[row["month"] - 1],
                "has_image": bool(row["image_name"]),
                "migration_conflict": row["month"] in conflicts,
                "migration_status": (
                    "review_required" if row["month"] in conflicts else None
                ),
                "image_url": (
                    f"/api/places/margaritas/{row['month']}/image" + (f"?year={year}" if year is not None else "")
                    if row["image_name"] else None
                ),
            }
            for row in rows
        ]
    )


@places_bp.post("/api/places/margaritas/assign-year")
@require_profile()
def assign_margarita_year():
    actor, _name, error = actor_or_error()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    try:
        year = margarita_calendar(data.get("year"))
        if year is None:
            raise ValueError("Choose the year these margaritas belong to.")
    except ValueError as error:
        return jsonify(error=str(error)), 422
    if not authorize("place.margarita.update", actor).allowed:
        return jsonify(error="This account cannot edit the shared margarita calendar."), 403
    with connect(DB_PATH) as connection:
        connection.execute("BEGIN IMMEDIATE")
        if margarita_migration_conflicts(connection):
            return jsonify(error="Review the conflicting original months before assigning a year."), 409
        rows = margarita_calendar_rows(connection, None)
        versions = data.get("versions")
        if not isinstance(versions, dict) or any(versions.get(str(row["month"])) != row["version"] for row in rows):
            return jsonify(error="The original calendar changed. Reload it before assigning a year."), 409
        if connection.execute("SELECT 1 FROM margarita_years WHERE year=?", (year,)).fetchone():
            return jsonify(error="That year already has saved entries. Nothing was replaced."), 409
        for row in rows:
            connection.execute("""INSERT INTO margarita_years
                (year,month,name,rating,review,image_name,updated_by_id,updated_by_name,updated_at,version)
                VALUES(?,?,?,?,?,?,?,?,?,?)""", (year, row["month"], row["name"], row["rating"], row["review"],
                row["image_name"], row["updated_by_id"], row["updated_by_name"], row["updated_at"], row["version"]))
        connection.execute("INSERT INTO margarita_calendar_copies VALUES(?,?,?)", (year, utcnow(), actor.principal_id))
        audit_mutation(connection, actor, domain="place_margarita_record", object_id=str(year),
            action="update", before=None, after={"year": year, "copied_from": "legacy", "original_preserved": True})
    return jsonify(ok=True, year=year, original_preserved=True)


def save_place_image(upload, root):
    if not upload or not upload.filename:
        return None
    upload.stream.seek(0, 2)
    byte_size = upload.stream.tell()
    upload.stream.seek(0)
    if byte_size > MAX_IMAGE_BYTES:
        raise ValueError("That image is larger than 12 MB.")
    try:
        with Image.open(upload.stream) as image:
            image.load()
            if image.width * image.height > MAX_IMAGE_PIXELS:
                raise ValueError("That image has too many pixels.")
            safe = ImageOps.exif_transpose(image).convert("RGB")
            safe.thumbnail((1800, 1800), Image.Resampling.LANCZOS)
            name = f"{uuid.uuid4().hex}.jpg"
            temporary = f".{name}.part"
            directory_fd = os.open(
                root,
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
            )
            file_fd = None
            try:
                file_fd = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=directory_fd,
                )
                with os.fdopen(file_fd, "wb") as destination:
                    file_fd = None
                    safe.save(destination, "JPEG", quality=88, optimize=True)
                    destination.flush()
                    os.fsync(destination.fileno())
                os.rename(
                    temporary, name,
                    src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
                )
                os.fsync(directory_fd)
            except Exception:
                if file_fd is not None:
                    os.close(file_fd)
                try:
                    os.unlink(temporary, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
                raise
            finally:
                os.close(directory_fd)
            return name
    except (UnidentifiedImageError, OSError) as error:
        raise ValueError("Choose a valid photo.") from error


def save_margarita_image(upload):
    return save_place_image(upload, IMAGE_ROOT)


def margarita_image_referenced(connection, image_name):
    if not image_name:
        return False
    for table in ("margaritas", "margarita_records", "margarita_image_history", "margarita_years", "margarita_revisions"):
        if connection.execute(
            f"SELECT 1 FROM {table} WHERE image_name=? LIMIT 1", (image_name,)
        ).fetchone():
            return True
    return False


def discard_unreferenced_margarita_image(image_name):
    """Delete only a failed request's new, provably unreferenced image."""
    if not image_name:
        return
    with connect(DB_PATH) as connection:
        referenced = margarita_image_referenced(connection, image_name)
    if not referenced:
        unlink_managed_image(IMAGE_ROOT, image_name)


@places_bp.put("/api/places/margaritas/<int:month>")
@require_profile()
def update_margarita(month):
    actor, editor_name, error = actor_or_error()
    if error:
        return error
    if month not in range(1, 13):
        return jsonify(error="Choose a month from January through December."), 404
    try:
        year = margarita_calendar(request.args.get("year"))
        rating = normalize_rating(request.form.get("rating"), required=False)
        new_image = save_margarita_image(request.files.get("image"))
    except ValueError as error:
        return jsonify(error=str(error)), 400
    remove_image = request.form.get("remove_image") == "true"
    version = expected_version(request.form)
    try:
        with connect(DB_PATH) as connection:
            connection.execute("BEGIN IMMEDIATE")
            table = "margaritas" if year is None else "margarita_years"
            suffix = "" if year is None else " AND year=?"
            key = (month,) if year is None else (month, year)
            before = connection.execute(
                f"SELECT * FROM {table} WHERE month=?{suffix}", key,
            ).fetchone()
            if before is None and year is not None:
                before = margarita_calendar_rows(connection, year)[month - 1]
            decision = authorize("place.margarita.update", actor)
            if not decision.allowed:
                if new_image:
                    unlink_managed_image(IMAGE_ROOT, new_image)
                return jsonify(error="This account cannot edit the shared margarita calendar."), 403
            if before is None:
                if new_image:
                    unlink_managed_image(IMAGE_ROOT, new_image)
                return jsonify(error="This month changed on another screen.", conflict=True), 409
            if (
                year is None and margarita_row_pristine(before)
                and len(populated_personal_margarita_records(connection, month)) > 1
            ):
                if new_image:
                    unlink_managed_image(IMAGE_ROOT, new_image)
                return jsonify(
                    error=(
                        "This month has multiple older saved versions and needs "
                        "household review before it can be edited."
                    ),
                    conflict=True,
                    review_required=True,
                ), 409
            if version is None or int(before["version"]) != version:
                if new_image:
                    unlink_managed_image(IMAGE_ROOT, new_image)
                return jsonify(
                    error="This month changed on another screen.",
                    conflict=True,
                    current_version=int(before["version"]),
                ), 409
            old_image = before["image_name"]
            if year is not None:
                connection.execute("INSERT OR IGNORE INTO margarita_years(year,month) VALUES(?,?)", (year, month))
            image_name = new_image if new_image else (None if remove_image else old_image)
            now = utcnow()
            connection.execute("INSERT INTO margarita_revisions VALUES(?,?,?,?,?,?)", (
                uuid.uuid4().hex, year or 0, month, json.dumps(dict(before), sort_keys=True), old_image, now))
            if old_image and old_image != image_name:
                connection.execute(
                    """INSERT INTO margarita_image_history
                       (id,month,owner_id,image_name,retained_at,purge_after)
                       VALUES (?,?,?,?,?,?)""",
                    (
                        uuid.uuid4().hex, month, actor.principal_id, old_image, now,
                        (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
                    ),
                )
            updated = connection.execute(
                f"""UPDATE {table}
                   SET name=?,rating=?,review=?,image_name=?,updated_by_id=?,
                       updated_by_name=?,updated_at=?,version=version+1
                   WHERE month=? AND version=?{suffix}""",
                (
                    clean_text(request.form.get("name"), 160), rating,
                    clean_multiline(request.form.get("review"), 3000), image_name,
                    actor.principal_id, editor_name, now, month, version, *((year,) if year is not None else ()),
                ),
            )
            if updated.rowcount != 1:
                raise sqlite3.IntegrityError("margarita version changed")
            after = connection.execute(
                f"SELECT * FROM {table} WHERE month=?{suffix}", key,
            ).fetchone()
            audit_mutation(
                connection, actor, domain="place_margarita_record",
                object_id=str(month) if year is None else f"{year}:{month}", action="update",
                before=before, after=after,
            )
    except sqlite3.Error:
        discard_unreferenced_margarita_image(new_image)
        return jsonify(error="That margarita month could not be saved."), 409
    except Exception:
        discard_unreferenced_margarita_image(new_image)
        raise
    return jsonify(ok=True, version=after["version"])


@places_bp.get("/api/places/margaritas/<int:month>/image")
@require_profile()
def margarita_image(month):
    _actor, _name, error = actor_or_error()
    if error:
        return error
    try:
        year = margarita_calendar(request.args.get("year"))
    except ValueError as error:
        return jsonify(error=str(error)), 422
    with connect(DB_PATH) as connection:
        table = "margaritas" if year is None else "margarita_years"
        suffix = "" if year is None else " AND year=?"
        row = connection.execute(
            f"SELECT image_name FROM {table} WHERE month=?{suffix}", (month,) if year is None else (month, year),
        ).fetchone()
    if not row or not row["image_name"]:
        return jsonify(error="Margarita photo not found."), 404
    opened = open_managed_image(IMAGE_ROOT, row["image_name"])
    if opened is None:
        return jsonify(error="Margarita photo not found."), 404
    return send_file(
        opened, download_name="margarita.jpg", mimetype="image/jpeg", conditional=True,
    )


def init_places(app):
    app.register_blueprint(places_bp)

"""Shared restaurant wish list, reviews, chooser, and margarita tracker."""

from __future__ import annotations

import json
import random
import sqlite3
import uuid
from datetime import date
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request, send_file
from PIL import Image, ImageOps, UnidentifiedImageError

from .identity import current_device, require_profile
from .platform import PLATFORM_DATA, connect, migrate, utcnow


DB_PATH = PLATFORM_DATA / "places.db"
PLACE_IMAGE_ROOT = PLATFORM_DATA / "places"
IMAGE_ROOT = PLACE_IMAGE_ROOT / "margaritas"
RESTAURANT_IMAGE_ROOT = PLACE_IMAGE_ROOT / "restaurants"
IMAGE_ROOT.mkdir(parents=True, exist_ok=True)
RESTAURANT_IMAGE_ROOT.mkdir(parents=True, exist_ok=True)
places_bp = Blueprint("places", __name__)

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
    connection.execute(
        """CREATE TABLE IF NOT EXISTS restaurant_photos (
            id TEXT PRIMARY KEY,
            restaurant_id TEXT NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
            image_name TEXT NOT NULL UNIQUE,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )"""
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
    for row in connection.execute("SELECT cuisines_json FROM restaurants").fetchall():
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
            updated_at TEXT
        )"""
    )
    connection.executemany(
        "INSERT OR IGNORE INTO margaritas(month) VALUES (?)",
        [(month,) for month in range(1, 13)],
    )


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


def photo_json(row):
    return {
        "id": row["id"],
        "url": f"/api/places/restaurants/{row['restaurant_id']}/photos/{row['id']}",
    }


def restaurant_json(row, connection=None):
    item = dict(row)
    item["cuisines"] = json.loads(item.pop("cuisines_json") or "[]")
    if connection is None:
        with connect(DB_PATH) as opened:
            return restaurant_json(row, opened)
    photos = connection.execute(
        """SELECT id, restaurant_id, image_name FROM restaurant_photos
           WHERE restaurant_id=? ORDER BY sort_order, created_at, id""",
        (item["id"],),
    ).fetchall()
    item["photos"] = [photo_json(photo) for photo in photos]
    item["has_image"] = bool(photos)
    item["image_url"] = item["photos"][0]["url"] if photos else None
    item.pop("image_name", None)
    return item


def restaurant_input():
    if request.is_json:
        return request.get_json(silent=True) or {}, [], []
    cuisines = request.form.getlist("cuisines")
    uploads = [item for item in request.files.getlist("images") if item and item.filename]
    legacy_upload = request.files.get("image")
    if legacy_upload and legacy_upload.filename:
        uploads.append(legacy_upload)
    return {
        "name": request.form.get("name"),
        "notes": request.form.get("notes"),
        "cuisines": cuisines,
    }, uploads, request.form.getlist("remove_photo_ids")


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
            (RESTAURANT_IMAGE_ROOT / name).unlink(missing_ok=True)
        raise
    return names


def add_photo_rows(connection, restaurant_id, names):
    existing = connection.execute(
        "SELECT COUNT(*) AS total FROM restaurant_photos WHERE restaurant_id=?",
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


def available_cuisines(connection=None):
    if connection is None:
        with connect(DB_PATH) as opened:
            return available_cuisines(opened)
    return [
        row["display_name"]
        for row in connection.execute(
            """SELECT display_name FROM cuisine_categories
               ORDER BY display_name COLLATE NOCASE"""
        ).fetchall()
    ]


def remember_cuisines(connection, cuisines):
    connection.executemany(
        """INSERT OR IGNORE INTO cuisine_categories
           (normalized_name, display_name, created_at) VALUES (?, ?, ?)""",
        [(name.casefold(), name, utcnow()) for name in cuisines],
    )


@places_bp.get("/places")
@require_profile(api=False)
def places_page():
    return render_template("places.html", cuisines=available_cuisines())


@places_bp.get("/api/places/restaurants")
@require_profile()
def list_restaurants():
    view = request.args.get("view", "want_to_go")
    sort = request.args.get("sort", "recent")
    query = clean_text(request.args.get("q"), 120).casefold()
    cuisine = clean_text(request.args.get("cuisine"), 40).casefold()
    if view not in {"want_to_go", "reviewed", "all"}:
        return jsonify(error="That restaurant view is not available."), 400
    order = {
        "recent": "COALESCE(visited_at, updated_at) DESC, updated_at DESC",
        "rating": "rating DESC, COALESCE(visited_at, updated_at) DESC",
        "name": "name COLLATE NOCASE ASC",
    }.get(sort)
    if not order:
        return jsonify(error="That sorting option is not available."), 400
    conditions, parameters = [], []
    if view != "all":
        conditions.append("status = ?")
        parameters.append(view)
    with connect(DB_PATH) as connection:
        rows = connection.execute(
            f"SELECT * FROM restaurants "
            f"{'WHERE ' + ' AND '.join(conditions) if conditions else ''} "
            f"ORDER BY {order}",
            parameters,
        ).fetchall()
        items = [restaurant_json(row, connection) for row in rows]
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
    return jsonify(restaurants=items, cuisines=available_cuisines())


@places_bp.post("/api/places/restaurants")
@require_profile()
def add_restaurant():
    data, uploads, _ = restaurant_input()
    name = clean_text(data.get("name"), 160)
    if not name:
        return jsonify(error="Give the restaurant a name."), 400
    cuisines = normalize_cuisines(data.get("cuisines"))
    person = current_device()
    restaurant_id = uuid.uuid4().hex
    now = utcnow()
    image_names = []
    try:
        image_names = save_restaurant_photos(uploads)
        with connect(DB_PATH) as connection:
            duplicate = connection.execute(
                "SELECT 1 FROM restaurants WHERE LOWER(name) = LOWER(?) LIMIT 1", (name,)
            ).fetchone()
            if duplicate:
                for image_name in image_names:
                    (RESTAURANT_IMAGE_ROOT / image_name).unlink(missing_ok=True)
                return jsonify(error="That restaurant is already on the list."), 409
            connection.execute(
                """INSERT INTO restaurants
                   (id, name, cuisines_json, notes, status, added_by_id,
                    added_by_name, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 'want_to_go', ?, ?, ?, ?)""",
                (
                    restaurant_id, name, json.dumps(cuisines),
                    clean_multiline(data.get("notes"), 1500),
                    person["owner_id"], person["name"], now, now,
                ),
            )
            remember_cuisines(connection, cuisines)
            add_photo_rows(connection, restaurant_id, image_names)
            row = connection.execute("SELECT * FROM restaurants WHERE id = ?", (restaurant_id,)).fetchone()
    except ValueError as error:
        for image_name in image_names:
            (RESTAURANT_IMAGE_ROOT / image_name).unlink(missing_ok=True)
        return jsonify(error=str(error)), 400
    except sqlite3.IntegrityError:
        for image_name in image_names:
            (RESTAURANT_IMAGE_ROOT / image_name).unlink(missing_ok=True)
        return jsonify(error="That restaurant could not be added."), 409
    return jsonify(restaurant=restaurant_json(row)), 201


@places_bp.put("/api/places/restaurants/<restaurant_id>")
@require_profile()
def update_restaurant(restaurant_id):
    data, uploads, remove_photo_ids = restaurant_input()
    name = clean_text(data.get("name"), 160)
    if not name:
        return jsonify(error="Give the restaurant a name."), 400
    cuisines = normalize_cuisines(data.get("cuisines"))
    try:
        new_images = save_restaurant_photos(uploads)
    except ValueError as error:
        return jsonify(error=str(error)), 400
    removed_names = []
    try:
        with connect(DB_PATH) as connection:
            old = connection.execute("SELECT id FROM restaurants WHERE id=?", (restaurant_id,)).fetchone()
            if not old:
                for name in new_images: (RESTAURANT_IMAGE_ROOT / name).unlink(missing_ok=True)
                return jsonify(error="Restaurant not found."), 404
            valid_remove_ids = [clean_text(value, 64) for value in remove_photo_ids if clean_text(value, 64)]
            if valid_remove_ids:
                placeholders = ",".join("?" for _ in valid_remove_ids)
                removed_names = [record["image_name"] for record in connection.execute(
                    f"SELECT image_name FROM restaurant_photos WHERE restaurant_id=? AND id IN ({placeholders})",
                    [restaurant_id, *valid_remove_ids],
                ).fetchall()]
                connection.execute(
                    f"DELETE FROM restaurant_photos WHERE restaurant_id=? AND id IN ({placeholders})",
                    [restaurant_id, *valid_remove_ids],
                )
            add_photo_rows(connection, restaurant_id, new_images)
            remember_cuisines(connection, cuisines)
            changed = connection.execute(
                """UPDATE restaurants SET name=?, cuisines_json=?, notes=?, updated_at=?
                   WHERE id=?""",
                (
                    name, json.dumps(cuisines),
                    clean_multiline(data.get("notes"), 1500),
                    utcnow(), restaurant_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM restaurants WHERE id = ?", (restaurant_id,)
            ).fetchone()
    except (sqlite3.Error, ValueError) as error:
        for name in new_images: (RESTAURANT_IMAGE_ROOT / name).unlink(missing_ok=True)
        if isinstance(error, ValueError):
            return jsonify(error=str(error)), 400
        return jsonify(error="That restaurant could not be updated."), 409
    if not changed.rowcount:
        return jsonify(error="Restaurant not found."), 404
    for name in removed_names:
        (RESTAURANT_IMAGE_ROOT / Path(name).name).unlink(missing_ok=True)
    return jsonify(restaurant=restaurant_json(row))


@places_bp.get("/api/places/restaurants/<restaurant_id>/image")
@require_profile()
def restaurant_image(restaurant_id):
    with connect(DB_PATH) as connection:
        row = connection.execute(
            "SELECT image_name FROM restaurants WHERE id=?", (restaurant_id,)
        ).fetchone()
    if not row or not row["image_name"]:
        return jsonify(error="Restaurant photo not found."), 404
    path = RESTAURANT_IMAGE_ROOT / Path(row["image_name"]).name
    if not path.is_file() or path.is_symlink():
        return jsonify(error="Restaurant photo not found."), 404
    return send_file(path, mimetype="image/jpeg", conditional=True, max_age=604800)


@places_bp.get("/api/places/restaurants/<restaurant_id>/photos/<photo_id>")
@require_profile()
def restaurant_photo(restaurant_id, photo_id):
    with connect(DB_PATH) as connection:
        row = connection.execute(
            "SELECT image_name FROM restaurant_photos WHERE id=? AND restaurant_id=?",
            (photo_id, restaurant_id),
        ).fetchone()
    if not row:
        return jsonify(error="Restaurant photo not found."), 404
    path = RESTAURANT_IMAGE_ROOT / Path(row["image_name"]).name
    if not path.is_file() or path.is_symlink():
        return jsonify(error="Restaurant photo not found."), 404
    return send_file(path, mimetype="image/jpeg", conditional=True, max_age=604800)


@places_bp.post("/api/places/restaurants/<restaurant_id>/review")
@require_profile()
def review_restaurant(restaurant_id):
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
    person = current_device()
    with connect(DB_PATH) as connection:
        changed = connection.execute(
            """UPDATE restaurants
               SET status='reviewed', rating=?, review=?, visited_at=?,
                   reviewed_by_id=?, reviewed_by_name=?, updated_at=?
               WHERE id=?""",
            (
                rating, clean_multiline(data.get("review"), 5000), visited_at,
                person["owner_id"], person["name"], utcnow(), restaurant_id,
            ),
        )
        row = connection.execute(
            "SELECT * FROM restaurants WHERE id = ?", (restaurant_id,)
        ).fetchone()
    if not changed.rowcount:
        return jsonify(error="Restaurant not found."), 404
    return jsonify(restaurant=restaurant_json(row))


@places_bp.delete("/api/places/restaurants/<restaurant_id>")
@require_profile()
def delete_restaurant(restaurant_id):
    with connect(DB_PATH) as connection:
        rows = connection.execute(
            "SELECT image_name FROM restaurant_photos WHERE restaurant_id=?", (restaurant_id,)
        ).fetchall()
        changed = connection.execute(
            "DELETE FROM restaurants WHERE id = ?", (restaurant_id,)
        )
    if not changed.rowcount:
        return jsonify(error="Restaurant not found."), 404
    for row in rows:
        (RESTAURANT_IMAGE_ROOT / Path(row["image_name"]).name).unlink(missing_ok=True)
    return jsonify(ok=True)


@places_bp.post("/api/places/choose")
@require_profile()
def choose_restaurant():
    data = request.get_json(silent=True) or {}
    pool = data.get("pool", "all")
    if pool not in {"all", "unvisited", "highly_rated"}:
        return jsonify(error="Choose a valid restaurant pool."), 400
    cuisines = {item.casefold() for item in normalize_cuisines(data.get("cuisines"))}
    conditions, parameters = [], []
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
    choices = [restaurant_json(row) for row in rows]
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
    with connect(DB_PATH) as connection:
        rows = connection.execute("SELECT * FROM margaritas ORDER BY month").fetchall()
    return jsonify(
        margaritas=[
            {
                **dict(row),
                "month_name": MONTHS[row["month"] - 1],
                "has_image": bool(row["image_name"]),
                "image_url": (
                    f"/api/places/margaritas/{row['month']}/image"
                    if row["image_name"] else None
                ),
            }
            for row in rows
        ]
    )


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
            temporary = root / f".{name}.part"
            safe.save(temporary, "JPEG", quality=88, optimize=True)
            temporary.replace(root / name)
            return name
    except (UnidentifiedImageError, OSError) as error:
        raise ValueError("Choose a valid photo.") from error


def save_margarita_image(upload):
    return save_place_image(upload, IMAGE_ROOT)


@places_bp.put("/api/places/margaritas/<int:month>")
@require_profile()
def update_margarita(month):
    if month not in range(1, 13):
        return jsonify(error="Choose a month from January through December."), 404
    try:
        rating = normalize_rating(request.form.get("rating"), required=False)
        new_image = save_margarita_image(request.files.get("image"))
    except ValueError as error:
        return jsonify(error=str(error)), 400
    person = current_device()
    remove_image = request.form.get("remove_image") == "true"
    old_image = None
    with connect(DB_PATH) as connection:
        old = connection.execute(
            "SELECT image_name FROM margaritas WHERE month=?", (month,)
        ).fetchone()
        old_image = old["image_name"] if old else None
        image_name = new_image if new_image else (None if remove_image else old_image)
        connection.execute(
            """UPDATE margaritas SET name=?, rating=?, review=?, image_name=?,
               updated_by_id=?, updated_by_name=?, updated_at=? WHERE month=?""",
            (
                clean_text(request.form.get("name"), 160), rating,
                clean_multiline(request.form.get("review"), 3000), image_name,
                person["owner_id"], person["name"], utcnow(), month,
            ),
        )
    if old_image and (new_image or remove_image):
        (IMAGE_ROOT / old_image).unlink(missing_ok=True)
    return jsonify(ok=True)


@places_bp.get("/api/places/margaritas/<int:month>/image")
@require_profile()
def margarita_image(month):
    with connect(DB_PATH) as connection:
        row = connection.execute(
            "SELECT image_name FROM margaritas WHERE month=?", (month,)
        ).fetchone()
    if not row or not row["image_name"]:
        return jsonify(error="Margarita photo not found."), 404
    path = IMAGE_ROOT / Path(row["image_name"]).name
    if not path.is_file() or path.is_symlink():
        return jsonify(error="Margarita photo not found."), 404
    return send_file(path, mimetype="image/jpeg", conditional=True)


def init_places(app):
    app.register_blueprint(places_bp)

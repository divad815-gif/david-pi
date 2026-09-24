import hashlib
import html
import http.client
import ipaddress
from io import BytesIO
import json
import os
import random
import re
import socket
import ssl
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import uuid
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

from flask import Blueprint, Response, jsonify, render_template, request
from PIL import Image, ImageOps, UnidentifiedImageError

from .identity import require_profile
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
from .platform import PLATFORM_DATA, connect, migrate, utcnow
from . import recipe_quality
from . import recipe_planner


DB_PATH = PLATFORM_DATA / "recipes.db"
IMAGE_CACHE_PATH = PLATFORM_DATA / "recipe-image-cache.db"
recipes_bp = Blueprint("recipes", __name__)
PRIVATE_REVALIDATE_SCOPE_HEADER = "X-David-Pi-Private-Revalidate-Scope"
MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_RECIPE_IMAGE_BYTES = 3 * 1024 * 1024
MAX_RECIPE_IMAGE_PIXELS = 25_000_000
MEALDB_KEY = os.environ.get("THEMEALDB_API_KEY", "").strip()
MEALDB_BASE = "https://www.themealdb.com/api/json/v1/"
MEALDB_IMAGE_PREFIX = "/images/media/meals/"
MEALDB_IMAGE_NAME = re.compile(
    r"^[A-Za-z0-9_-]{1,128}\.(?:jpe?g|png|webp)$", re.IGNORECASE
)
RECIPE_SECTIONS = ("breakfast", "main", "dessert")
RECIPE_SECTION_ALIASES = {
    "breakfast": "breakfast",
    "lunch": "main",
    "dinner": "main",
    "lunch_dinner": "main",
    "main": "main",
    "dessert": "dessert",
    "desserts": "dessert",
}
def normalize_recipe_section(value, default="main"):
    return RECIPE_SECTION_ALIASES.get(str(value or "").strip().lower(), default)


def initialize_recipes(connection):
    recipe_planner.initialize(connection)
    connection.execute(
        """CREATE TABLE IF NOT EXISTS recipes (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
            meal_type TEXT NOT NULL DEFAULT 'main', tags_json TEXT NOT NULL DEFAULT '[]',
            total_minutes INTEGER, servings TEXT, ingredients_json TEXT NOT NULL DEFAULT '[]',
            instructions_json TEXT NOT NULL DEFAULT '[]', source_name TEXT, source_url TEXT UNIQUE,
            image_url TEXT, content_hash TEXT NOT NULL, favorite INTEGER NOT NULL DEFAULT 0,
            created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            imported_at TEXT, last_viewed_at TEXT, last_made_at TEXT, deleted_at TEXT
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS recipe_recommendations (
            recipe_id TEXT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
            recommended_at TEXT NOT NULL
        )"""
    )
    connection.execute("CREATE INDEX IF NOT EXISTS recipes_meal_idx ON recipes(meal_type, deleted_at)")
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(recipes)")}
    additions = {
        "owner_id": "TEXT",
        "owner_name": "TEXT",
        "visibility": "TEXT NOT NULL DEFAULT 'shared' CHECK(visibility IN ('shared','private'))",
        "version": "INTEGER NOT NULL DEFAULT 1 CHECK(version > 0)",
        "deleted_by": "TEXT",
        "purge_after": "TEXT",
        # New writes use this nullable locator.  The legacy globally-unique
        # source_url column stays untouched for rollback compatibility.
        "catalog_source_url": "TEXT",
    }
    for name, definition in additions.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE recipes ADD COLUMN {name} {definition}")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS recipes_visibility_idx "
        "ON recipes(deleted_at, visibility, owner_id, meal_type)"
    )
    connection.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS recipes_shared_catalog_active_idx
           ON recipes(COALESCE(catalog_source_url, source_url))
           WHERE deleted_at IS NULL AND visibility='shared'
             AND COALESCE(catalog_source_url, source_url) IS NOT NULL"""
    )
    connection.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS recipes_private_catalog_active_idx
           ON recipes(owner_id, COALESCE(catalog_source_url, source_url))
           WHERE deleted_at IS NULL AND visibility='private' AND owner_id IS NOT NULL
             AND COALESCE(catalog_source_url, source_url) IS NOT NULL"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS recipe_activity (
            recipe_id TEXT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
            owner_id TEXT NOT NULL,
            favorite INTEGER NOT NULL DEFAULT 0,
            last_viewed_at TEXT,
            last_made_at TEXT,
            version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
            updated_at TEXT NOT NULL,
            PRIMARY KEY(recipe_id, owner_id)
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS recipe_quality_proposals (
            id TEXT PRIMARY KEY,
            recipe_id TEXT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
            field_name TEXT NOT NULL CHECK(field_name = 'meal_type'),
            current_value_json TEXT NOT NULL,
            proposed_value_json TEXT NOT NULL,
            reason_code TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            analyzer_version INTEGER NOT NULL CHECK(analyzer_version > 0),
            recipe_version INTEGER NOT NULL CHECK(recipe_version > 0),
            owner_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending','accepted','rejected','stale')),
            created_at TEXT NOT NULL,
            reviewed_at TEXT,
            reviewed_by TEXT,
            version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
            UNIQUE(recipe_id, field_name, recipe_version, proposed_value_json)
        )"""
    )
    connection.execute(
        """CREATE INDEX IF NOT EXISTS recipe_quality_owner_status_idx
           ON recipe_quality_proposals(owner_id, status, created_at DESC)"""
    )
    # Existing category values are protected user data. Category migrations must
    # be previewed, backed up, and applied explicitly; app startup is read-only.


migrate(DB_PATH, initialize_recipes)


def initialize_recipe_image_cache(connection):
    connection.execute(
        """CREATE TABLE IF NOT EXISTS recipe_image_derivatives (
            recipe_id TEXT PRIMARY KEY,
            source_url TEXT NOT NULL,
            fetched_url TEXT NOT NULL,
            source_sha256 TEXT NOT NULL,
            derivative_sha256 TEXT NOT NULL,
            content_type TEXT NOT NULL CHECK(content_type = 'image/jpeg'),
            width INTEGER NOT NULL CHECK(width > 0),
            height INTEGER NOT NULL CHECK(height > 0),
            image_bytes BLOB NOT NULL,
            attribution_url TEXT,
            generated_at TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0)
        )"""
    )


migrate(IMAGE_CACHE_PATH, initialize_recipe_image_cache)


def plain(value, limit=10000):
    text = html.unescape(re.sub(r"<[^>]+>", "", str(value or "")))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def clean_lines(value, limit=200):
    if isinstance(value, str):
        value = value.splitlines()
    if not isinstance(value, list):
        return []
    lines = []
    for item in value[:limit]:
        if isinstance(item, dict):
            item = item.get("text") or item.get("name")
        cleaned = plain(item, 1000)
        if cleaned:
            lines.append(cleaned)
    return lines


def clean_tags(value):
    if isinstance(value, str):
        value = value.split(",")
    return list(dict.fromkeys(plain(item, 30) for item in (value or []) if plain(item, 30)))[:16]


def recipe_fingerprint(title, ingredients, instructions):
    return hashlib.sha256(
        json.dumps([title.lower(), ingredients, instructions], sort_keys=True).encode()
    ).hexdigest()


def recipe_json(row, actor_id=None):
    item = dict(row)
    effective_source = item.pop("effective_source_url", None)
    if effective_source is None:
        effective_source = item.get("catalog_source_url") or item.get("source_url")
    item["source_url"] = effective_source
    item.pop("catalog_source_url", None)
    item["tags"] = json.loads(item.pop("tags_json") or "[]")
    item["ingredients"] = json.loads(item.pop("ingredients_json") or "[]")
    item["instructions"] = json.loads(item.pop("instructions_json") or "[]")
    item["favorite"] = bool(item.pop("effective_favorite", item["favorite"]))
    item["last_viewed_at"] = item.pop("effective_last_viewed_at", item.get("last_viewed_at"))
    item["last_made_at"] = item.pop("effective_last_made_at", item.get("last_made_at"))
    item["state_version"] = item.pop("activity_version", None)
    item["can_edit"] = row_mutable_by(item, actor_id) and not item.get("deleted_at")
    item["can_restore"] = row_mutable_by(item, actor_id) and bool(item.get("deleted_at"))
    item["legacy_read_only"] = item.get("owner_id") is None and not row_mutable_by(
        item, actor_id
    )
    item["retained_until"] = item.get("purge_after")
    item["image"] = f"/api/recipes/{item['id']}/image" if item["image_url"] else None
    item.pop("content_hash", None)
    item.pop("deleted_by", None)
    item.pop("purge_after", None)
    return item


def recipe_summary_json(row, actor_id=None):
    """Return only fields needed by a library card, not the full cooking payload."""
    item = dict(row)
    return {
        "id": item["id"],
        "title": item["title"],
        "description": item["description"],
        "meal_type": item["meal_type"],
        "tags": json.loads(item["tags_json"] or "[]"),
        "total_minutes": item["total_minutes"],
        "favorite": bool(item.get("effective_favorite", item["favorite"])),
        "image": f"/api/recipes/{item['id']}/image" if item["image_url"] else None,
        "owner_name": item.get("owner_name"),
        "visibility": item.get("visibility") or "shared",
        "version": int(item.get("version") or 1),
        "state_version": item.get("activity_version"),
        "can_edit": row_mutable_by(item, actor_id) and not item.get("deleted_at"),
        "can_restore": row_mutable_by(item, actor_id) and bool(item.get("deleted_at")),
        "legacy_read_only": item.get("owner_id") is None and not row_mutable_by(
            item, actor_id
        ),
        "deleted_at": item.get("deleted_at"),
        "retained_until": item.get("purge_after"),
    }


def recipe_select(alias="r"):
    return f"""{alias}.*,
        COALESCE({alias}.catalog_source_url, {alias}.source_url) AS effective_source_url,
        COALESCE(activity.favorite, {alias}.favorite) AS effective_favorite,
        COALESCE(activity.last_viewed_at, {alias}.last_viewed_at) AS effective_last_viewed_at,
        COALESCE(activity.last_made_at, {alias}.last_made_at) AS effective_last_made_at,
        activity.version AS activity_version"""


def actor_or_error():
    actor, name = allowed_actor()
    if actor is None:
        return None, name, (jsonify(error="Use an approved Tailscale account."), 403)
    return actor, name, None


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def recipe_quality_rows(connection, owner_id):
    """Return only one owner's recipes in the analyzer's fixed projection."""
    return connection.execute(
        """SELECT id,title,description,meal_type,tags_json,total_minutes,servings,
                  ingredients_json,instructions_json,source_name,
                  COALESCE(catalog_source_url, source_url) AS source_url,
                  image_url,content_hash,deleted_at
           FROM recipes WHERE owner_id=? ORDER BY id""",
        (owner_id,),
    ).fetchall()


def quality_proposal_json(row):
    item = dict(row)
    return {
        "id": item["id"],
        "recipe_id": item["recipe_id"],
        "recipe_title": item.get("recipe_title") or "Recipe",
        "field": item["field_name"],
        "current": json.loads(item["current_value_json"]),
        "proposed": json.loads(item["proposed_value_json"]),
        "reason": item["reason_code"],
        "evidence": json.loads(item["evidence_json"]),
        "status": item["status"],
        "recipe_version": int(item["recipe_version"]),
        "version": int(item["version"]),
        "created_at": item["created_at"],
        "reviewed_at": item["reviewed_at"],
    }


def personal_activity(connection, recipe_id, actor_id):
    return connection.execute(
        "SELECT * FROM recipe_activity WHERE recipe_id=? AND owner_id=?",
        (recipe_id, actor_id),
    ).fetchone()


def set_personal_activity(connection, recipe_id, actor, *, action, favorite=None,
                          viewed=False, made=False, version=None):
    before = personal_activity(connection, recipe_id, actor.principal_id)
    if before is not None and (
        version is None or int(before["version"]) != version
    ):
        return None, "conflict"
    if before is None and version not in (None, 0):
        return None, "conflict"
    now = utcnow()
    if before is None:
        legacy = connection.execute(
            "SELECT favorite FROM recipes WHERE id=?", (recipe_id,)
        ).fetchone()
        initial_favorite = (
            int(bool(favorite))
            if favorite is not None
            else int(bool(legacy["favorite"])) if legacy else 0
        )
        connection.execute(
            """INSERT INTO recipe_activity
               (recipe_id,owner_id,favorite,last_viewed_at,last_made_at,version,updated_at)
               VALUES (?,?,?,?,?,1,?)""",
            (
                recipe_id,
                actor.principal_id,
                initial_favorite,
                now if viewed else None,
                now if made else None,
                now,
            ),
        )
    else:
        connection.execute(
            """UPDATE recipe_activity
               SET favorite=?, last_viewed_at=?, last_made_at=?,
                   version=version+1, updated_at=?
               WHERE recipe_id=? AND owner_id=? AND version=?""",
            (
                int(bool(favorite)) if favorite is not None else before["favorite"],
                now if viewed else before["last_viewed_at"],
                now if made else before["last_made_at"],
                now,
                recipe_id,
                actor.principal_id,
                int(before["version"]),
            ),
        )
    after = personal_activity(connection, recipe_id, actor.principal_id)
    audit_mutation(
        connection,
        actor,
        domain="recipe_activity",
        object_id=f"{recipe_id}:{actor.principal_id}",
        action=action,
        before=before,
        after=after,
        object_version=int(after["version"]),
    )
    return after, None


def resolved_public_url(value):
    parsed = urllib.parse.urlsplit(value.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Use a normal public http or https recipe URL.")
    if parsed.port and parsed.port not in (80, 443):
        raise ValueError("That URL uses an unsupported port.")
    try:
        addresses = {result[4][0] for result in socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)}
    except socket.gaierror as error:
        raise ValueError("That website could not be found.") from error
    validated = []
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%")[0])
        if not ip.is_global or ip.is_multicast or ip.is_unspecified:
            raise ValueError("Private or local network addresses cannot be imported.")
        validated.append(str(ip))
    safe = urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc.lower(), parsed.path or "/", parsed.query, "")
    )
    return safe, tuple(sorted(validated))


def public_url(value):
    return resolved_public_url(value)[0]


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, hostname, address, port, timeout):
        super().__init__(hostname, port=port, timeout=timeout, context=ssl.create_default_context())
        self._pinned_address = address

    def connect(self):
        self.sock = socket.create_connection(
            (self._pinned_address, self.port), self.timeout, self.source_address
        )
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


def fetch_chain(url, accept, limit, timeout=12):
    current = url
    for redirect_count in range(5):
        safe, addresses = resolved_public_url(current)
        parsed = urllib.parse.urlsplit(safe)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        address = addresses[0]
        connection = (
            PinnedHTTPSConnection(parsed.hostname, address, port, timeout)
            if parsed.scheme == "https"
            else http.client.HTTPConnection(address, port=port, timeout=timeout)
        )
        target = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        headers = {
            "Host": parsed.hostname,
            "User-Agent": "David-Pi/1 (+private recipe organizer)",
            "Accept": accept,
            "Accept-Encoding": "identity",
            "Connection": "close",
        }
        try:
            connection.request("GET", target, headers=headers)
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                response.read(1024)
                if not location:
                    raise ValueError("The website returned an invalid redirect.")
                if redirect_count == 4:
                    raise ValueError("Too many redirects.")
                current = urllib.parse.urljoin(safe, location)
                continue
            if response.status < 200 or response.status >= 300:
                raise urllib.error.HTTPError(
                    safe, response.status, response.reason, response.headers, None
                )
            if response.getheader("Content-Encoding", "identity").lower() not in ("", "identity"):
                raise ValueError("Compressed remote responses are not accepted.")
            data = response.read(limit + 1)
            if len(data) > limit:
                raise ValueError("That page or image is too large to import safely.")
            return data, response.getheader("Content-Type", "").split(";", 1)[0].lower(), safe
        finally:
            connection.close()
    raise ValueError("Too many redirects.")


def fetch_public(url, image_only=False):
    safe = public_url(url)
    parsed = urllib.parse.urlsplit(safe)
    if not image_only:
        robots_url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "/robots.txt", "", ""))
        try:
            robots_data, _, _ = fetch_chain(
                public_url(robots_url), "text/plain", 256 * 1024, timeout=8
            )
            robot = urllib.robotparser.RobotFileParser()
            robot.set_url(robots_url)
            robot.parse(robots_data.decode("utf-8", errors="replace").splitlines())
            if not robot.can_fetch("David-Pi/1", safe):
                raise ValueError("That website does not allow recipe importing.")
        except (urllib.error.URLError, OSError):
            pass
    data, content_type, final_url = fetch_chain(
        safe,
        "image/png,image/jpeg,image/webp" if image_only else "text/html,application/xhtml+xml",
        3 * 1024 * 1024 if image_only else MAX_PAGE_BYTES,
    )
    allowed = (
        content_type in ("image/png", "image/jpeg", "image/webp")
        if image_only
        else content_type in ("text/html", "application/xhtml+xml")
    )
    if not allowed:
        raise ValueError("That URL did not return the expected content.")
    return data, content_type, final_url


class RecipeDataParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_json = False
        self.buffer = []
        self.blocks = []
        self.title = ""
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "script" and attributes.get("type", "").lower() == "application/ld+json":
            self.in_json, self.buffer = True, []
        if tag == "title":
            self.in_title = True

    def handle_endtag(self, tag):
        if tag == "script" and self.in_json:
            self.blocks.append("".join(self.buffer))
            self.in_json = False
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.in_json:
            self.buffer.append(data)
        if self.in_title:
            self.title += data


def find_recipe(value):
    if isinstance(value, list):
        for item in value:
            found = find_recipe(item)
            if found:
                return found
    if isinstance(value, dict):
        kind = value.get("@type")
        if kind == "Recipe" or isinstance(kind, list) and "Recipe" in kind:
            return value
        for key in ("@graph", "mainEntity", "itemListElement"):
            found = find_recipe(value.get(key))
            if found:
                return found
    return None


def duration_minutes(value):
    match = re.fullmatch(r"P(?:\d+D)?T(?:(\d+)H)?(?:(\d+)M)?", str(value or ""))
    return int(match.group(1) or 0) * 60 + int(match.group(2) or 0) if match else None


def parse_recipe_page(data, final_url):
    parser = RecipeDataParser()
    parser.feed(data.decode("utf-8", errors="replace"))
    structured = None
    for block in parser.blocks:
        try:
            structured = find_recipe(json.loads(block))
        except json.JSONDecodeError:
            continue
        if structured:
            break
    if not structured:
        raise ValueError("No structured recipe was found. You can still enter it manually.")
    instructions = structured.get("recipeInstructions", [])
    if isinstance(instructions, str):
        instructions = instructions.splitlines()
    instruction_lines = []
    for step in instructions:
        if isinstance(step, dict) and step.get("@type") == "HowToSection":
            instruction_lines.extend(clean_lines(step.get("itemListElement", [])))
        else:
            instruction_lines.extend(clean_lines([step]))
    image = structured.get("image")
    if isinstance(image, list):
        image = image[0] if image else None
    if isinstance(image, dict):
        image = image.get("url")
    host = urllib.parse.urlsplit(final_url).hostname or ""
    return {
        "title": plain(structured.get("name") or parser.title, 200),
        "description": plain(structured.get("description"), 2000),
        "total_minutes": (
            duration_minutes(structured.get("totalTime"))
            or (
                (duration_minutes(structured.get("prepTime")) or 0)
                + (duration_minutes(structured.get("cookTime")) or 0)
            )
            or None
        ),
        "servings": plain(structured.get("recipeYield"), 100),
        "ingredients": clean_lines(structured.get("recipeIngredient", [])),
        "instructions": instruction_lines,
        "tags": clean_tags(structured.get("keywords", "").split(",")),
        "source_name": host.removeprefix("www."),
        "source_url": final_url,
        "image_url": public_url(image) if image else None,
    }


def mealdb_request(endpoint, parameters=None):
    if not MEALDB_KEY or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", MEALDB_KEY):
        raise RuntimeError("Connect a recipe provider in Settings, or keep using your local recipes.")
    url = MEALDB_BASE + MEALDB_KEY + "/" + endpoint
    if parameters:
        url += "?" + urllib.parse.urlencode(parameters)
    request_object = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "David-Pi/1"},
    )
    try:
        with urllib.request.urlopen(request_object, timeout=12) as response:
            data = response.read(1024 * 1024 + 1)
            if len(data) > 1024 * 1024:
                raise ValueError("The recipe service returned too much data.")
            return json.loads(data)
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise RuntimeError("The recipe provider rejected its credential. An administrator can replace it in Settings; local recipes still work.") from None
        if error.code == 429:
            raise RuntimeError("The recipe provider has temporarily limited requests. Try again later; local recipes still work.") from None
        raise RuntimeError("The recipe provider is temporarily unavailable. Try again later; local recipes still work.") from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise RuntimeError("The recipe discovery service is unavailable right now.") from error


def mealdb_image_proxy(value):
    """Return a same-origin URL for a canonical TheMealDB thumbnail."""
    if not value:
        return None
    try:
        parsed = urllib.parse.urlsplit(str(value).strip())
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or parsed.hostname != "www.themealdb.com"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith(MEALDB_IMAGE_PREFIX)
    ):
        return None
    filename = parsed.path[len(MEALDB_IMAGE_PREFIX):]
    if "/" in filename or not MEALDB_IMAGE_NAME.fullmatch(filename):
        return None
    return f"/api/recipes/discover/image/{filename}"


def mealdb_summary(meal):
    return {
        "mealdb_id": plain(meal.get("idMeal"), 30),
        "title": plain(meal.get("strMeal"), 200),
        "category": plain(meal.get("strCategory"), 60),
        "area": plain(meal.get("strArea"), 60),
        "image_url": mealdb_image_proxy(meal.get("strMealThumb")),
    }


def mealdb_recipe(meal, meal_type):
    ingredients = []
    for index in range(1, 21):
        ingredient = plain(meal.get(f"strIngredient{index}"), 200)
        measure = plain(meal.get(f"strMeasure{index}"), 100)
        if ingredient:
            ingredients.append(" ".join(part for part in (measure, ingredient) if part))
    instructions = [
        plain(step, 2000)
        for step in re.split(r"(?:\r?\n)+|(?<=\.)\s+(?=[A-Z])", meal.get("strInstructions") or "")
        if plain(step, 2000)
    ]
    tags = [meal.get("strCategory"), meal.get("strArea")]
    tags.extend((meal.get("strTags") or "").split(","))
    meal_id = plain(meal.get("idMeal"), 30)
    return {
        "title": meal.get("strMeal"),
        "description": " · ".join(value for value in (plain(meal.get("strArea"), 60), plain(meal.get("strCategory"), 60)) if value),
        "meal_type": meal_type,
        "tags": tags,
        "ingredients": ingredients,
        "instructions": instructions,
        "source_name": "TheMealDB",
        "source_url": public_url(meal["strSource"]) if meal.get("strSource") else f"https://www.themealdb.com/meal/{meal_id}",
        "image_url": public_url(meal["strMealThumb"]) if meal.get("strMealThumb") else None,
    }


def save_recipe(data, actor, owner_name, imported=False, route_id="recipe.create"):
    title = plain(data.get("title"), 200)
    if not title:
        raise ValueError("Give the recipe a name.")
    meal = normalize_recipe_section(data.get("meal_type"))
    source = public_url(data["source_url"]) if data.get("source_url") else None
    ingredients = clean_lines(data.get("ingredients", []))
    instructions = clean_lines(data.get("instructions", []))
    fingerprint = recipe_fingerprint(title, ingredients, instructions)
    recipe_id, now = uuid.uuid4().hex, utcnow()
    visibility = validated_visibility(data)
    if not authorize(route_id, actor).allowed:
        raise PermissionError("This account cannot add recipes.")
    try:
        with connect(DB_PATH) as connection:
            connection.execute("BEGIN IMMEDIATE")
            duplicate = connection.execute(
                f"""SELECT 1 FROM recipes
                    WHERE (content_hash = ? OR
                           (? IS NOT NULL AND
                            COALESCE(catalog_source_url, source_url) = ?))
                      AND deleted_at IS NULL AND {visible_sql()}
                    LIMIT 1""",
                (fingerprint, source, source, actor.principal_id),
            ).fetchone()
            if duplicate:
                raise FileExistsError("That recipe is already in your library.")
            connection.execute(
                """INSERT INTO recipes
                   (id,title,description,meal_type,tags_json,total_minutes,servings,
                    ingredients_json,instructions_json,source_name,source_url,
                    catalog_source_url,image_url,
                    content_hash,favorite,created_by,created_at,updated_at,imported_at,
                    last_viewed_at,last_made_at,deleted_at,owner_id,owner_name,visibility,
                    version,deleted_by,purge_after)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,NULL,NULL,NULL,?,?,?,1,NULL,NULL)""",
                (
                    recipe_id, title, plain(data.get("description"), 2000), meal,
                    json.dumps(clean_tags(data.get("tags", []))),
                    int(data["total_minutes"]) if str(data.get("total_minutes", "")).isdigit() else None,
                    plain(data.get("servings"), 100), json.dumps(ingredients), json.dumps(instructions),
                    plain(data.get("source_name"), 200) or None, None, source,
                    data.get("image_url"),
                    fingerprint, owner_name, now, now, now if imported else None,
                    actor.principal_id, owner_name, visibility,
                ),
            )
            row = connection.execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
            audit_mutation(
                connection, actor, domain="recipe", object_id=recipe_id,
                action="create", before=None, after=row,
            )
            if data.get("favorite") is True:
                set_personal_activity(
                    connection, recipe_id, actor, action="favorite", favorite=True,
                )
            row = connection.execute(
                f"""SELECT {recipe_select()} FROM recipes r
                    LEFT JOIN recipe_activity activity
                      ON activity.recipe_id=r.id AND activity.owner_id=?
                    WHERE r.id=?""",
                (actor.principal_id, recipe_id),
            ).fetchone()
    except sqlite3.IntegrityError:
        raise FileExistsError("That recipe has already been imported.")
    return recipe_json(row, actor.principal_id)


@recipes_bp.get("/recipes")
@require_profile(api=False)
def recipes_page():
    _actor, _name, error = actor_or_error()
    if error:
        return "Open David-Pi through an approved private Tailscale account.", 403
    return render_template("recipes.html")


@recipes_bp.get("/recipes/weekly-plan")
@require_profile(api=False)
def recipe_weekly_plan_page():
    _actor, _name, error = actor_or_error()
    if error:
        return "Open David-Pi through an approved private Tailscale account.", 403
    return render_template("recipe_weekly_plan.html")


@recipes_bp.get("/api/recipes")
@require_profile()
def list_recipes():
    actor, _name, error = actor_or_error()
    if error:
        return error
    query = " ".join(request.args.get("q", "").split()).lower()[:120]
    meal = request.args.get("meal", "")
    view = request.args.get("view", "active")
    if view not in {"active", "trash"}:
        return jsonify(error="That recipe view is not available."), 400
    if view == "trash":
        conditions = ["r.deleted_at IS NOT NULL", mutable_sql("r")]
    else:
        conditions = ["r.deleted_at IS NULL", visible_sql("r")]
    parameters = [actor.principal_id]
    if query:
        conditions.append("(LOWER(r.title) LIKE ? OR LOWER(r.tags_json) LIKE ? OR LOWER(r.ingredients_json) LIKE ?)")
        pattern = f"%{query}%"
        parameters.extend([pattern, pattern, pattern])
    meal = normalize_recipe_section(meal, default="")
    if meal in RECIPE_SECTIONS:
        conditions.append("r.meal_type = ?")
        parameters.append(meal)
    requested_limit = request.args.get("limit", "30")
    if not str(requested_limit).isdigit() or len(str(requested_limit)) > 6:
        return jsonify(error="The recipe page size is invalid."), 400
    limit = min(max(int(requested_limit), 1), 100)
    offset = max(int(request.args.get("offset", 0)), 0) if str(request.args.get("offset", "0")).isdigit() else 0
    include_summary = request.args.get("summary", "1") != "0"
    where = " AND ".join(conditions)
    with connect(DB_PATH) as connection:
        total = connection.execute(f"SELECT COUNT(*) FROM recipes r WHERE {where}", parameters).fetchone()[0] if include_summary else None
        facet_row = connection.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN r.total_minutes IS NOT NULL THEN 1 ELSE 0 END) AS timed,
                      SUM(CASE WHEN r.servings IS NOT NULL AND TRIM(r.servings) != '' THEN 1 ELSE 0 END) AS with_servings,
                      SUM(CASE WHEN r.meal_type = 'breakfast' THEN 1 ELSE 0 END) AS breakfast,
                      SUM(CASE WHEN r.meal_type = 'main' THEN 1 ELSE 0 END) AS main,
                      SUM(CASE WHEN r.meal_type = 'dessert' THEN 1 ELSE 0 END) AS dessert
               FROM recipes r WHERE """ + (
                   "r.deleted_at IS NOT NULL AND " + mutable_sql("r")
                   if view == "trash"
                   else "r.deleted_at IS NULL AND " + visible_sql("r")
               ),
            (actor.principal_id,),
        ).fetchone() if include_summary else None
        section_count = sum(int(facet_row[name] or 0) for name in RECIPE_SECTIONS) if facet_row else 0
        pagination = " LIMIT ? OFFSET ?" if limit is not None else ""
        query_parameters = [*parameters, limit + 1, offset] if limit is not None else parameters
        rows = connection.execute(
            f"""SELECT r.id, r.title, r.description, r.meal_type, r.tags_json,
                       r.total_minutes, r.favorite, r.image_url, r.owner_id,
                       r.owner_name, r.visibility, r.version, r.deleted_at,
                       r.purge_after,
                       COALESCE(activity.favorite, r.favorite) AS effective_favorite,
                       activity.version AS activity_version
                FROM recipes r
                LEFT JOIN recipe_activity activity
                  ON activity.recipe_id=r.id AND activity.owner_id=?
                WHERE {where}
                ORDER BY effective_favorite DESC, r.updated_at DESC, r.id DESC{pagination}""",
            [actor.principal_id, *query_parameters],
        ).fetchall()
    has_more = limit is not None and len(rows) > limit
    if has_more:
        rows = rows[:limit]
    return jsonify(
        recipes=[recipe_summary_json(row, actor.principal_id) for row in rows],
        total=total,
        has_more=has_more,
        facets={
            "timed": int(facet_row["timed"] or 0),
            "with_servings": int(facet_row["with_servings"] or 0),
            "sections": {
                **{name: int(facet_row[name] or 0) for name in RECIPE_SECTIONS},
                "other": int(facet_row["total"] or 0) - section_count,
            },
            "total": int(facet_row["total"] or 0),
        } if facet_row else None,
    )


@recipes_bp.get("/api/recipes/quality-review")
@require_profile()
def list_recipe_quality_review():
    actor, _name, error = actor_or_error()
    if error:
        return error
    status = request.args.get("status", "pending")
    summary_only = request.args.get("summary", "0") == "1"
    if status not in {"pending", "accepted", "rejected", "stale", "all"}:
        return jsonify(error="That review state is not available."), 400
    status_sql = "" if status == "all" else "AND q.status=?"
    parameters = (
        (actor.principal_id,)
        if status == "all"
        else (actor.principal_id, status)
    )
    with connect(DB_PATH) as connection:
        rows = [] if summary_only else connection.execute(
            f"""SELECT q.*, r.title AS recipe_title
                FROM recipe_quality_proposals q
                JOIN recipes r ON r.id=q.recipe_id
                WHERE q.owner_id=? AND r.owner_id=q.owner_id {status_sql}
                ORDER BY CASE q.status WHEN 'pending' THEN 0 ELSE 1 END,
                         q.created_at DESC, q.id""",
            parameters,
        ).fetchall()
        count_rows = connection.execute(
            """SELECT status, COUNT(*) AS total
               FROM recipe_quality_proposals
               WHERE owner_id=? GROUP BY status""",
            (actor.principal_id,),
        ).fetchall()
    counts = {name: 0 for name in ("pending", "accepted", "rejected", "stale")}
    counts.update({row["status"]: int(row["total"]) for row in count_rows})
    return jsonify(
        proposals=[quality_proposal_json(row) for row in rows],
        counts=counts,
        safety={
            "automatic_recipe_changes": False,
            "reviewer": "recipe_owner",
        },
    )


@recipes_bp.post("/api/recipes/quality-review/refresh")
@require_profile()
def refresh_recipe_quality_review():
    actor, _name, error = actor_or_error()
    if error:
        return error
    if not authorize("recipe.quality.refresh", actor).allowed:
        return jsonify(error="This account cannot review recipe quality."), 403

    created = 0
    stale = 0
    with connect(DB_PATH) as connection:
        connection.execute("BEGIN IMMEDIATE")
        stale_candidates = connection.execute(
            """SELECT q.*, r.version AS current_recipe_version,
                      r.meal_type AS current_recipe_value,
                      r.deleted_at AS recipe_deleted_at,
                      r.owner_id AS current_recipe_owner
               FROM recipe_quality_proposals q
               LEFT JOIN recipes r ON r.id=q.recipe_id
               WHERE q.owner_id=? AND q.status='pending'""",
            (actor.principal_id,),
        ).fetchall()
        for before in stale_candidates:
            current_json = canonical_json(before["current_recipe_value"])
            still_current = (
                before["current_recipe_owner"] == actor.principal_id
                and before["recipe_deleted_at"] is None
                and int(before["current_recipe_version"] or 0)
                == int(before["recipe_version"])
                and before["current_value_json"] == current_json
            )
            if still_current:
                continue
            connection.execute(
                """UPDATE recipe_quality_proposals
                   SET status='stale', reviewed_at=?, version=version+1
                   WHERE id=? AND version=? AND status='pending'""",
                (utcnow(), before["id"], before["version"]),
            )
            after = connection.execute(
                "SELECT * FROM recipe_quality_proposals WHERE id=?", (before["id"],)
            ).fetchone()
            audit_mutation(
                connection, actor, domain="recipe_quality_proposal",
                object_id=before["id"], action="stale", before=before, after=after,
            )
            stale += 1

        report = recipe_quality.analyze_recipes(
            recipe_quality_rows(connection, actor.principal_id),
            include_titles=False,
            max_findings=1000,
        )
        for finding in report["taxonomy"]["likely_misclassified"]["records"]:
            recipe = connection.execute(
                """SELECT id,owner_id,meal_type,version,deleted_at
                   FROM recipes WHERE id=? AND owner_id=?""",
                (finding["recipe_id"], actor.principal_id),
            ).fetchone()
            if not recipe or recipe["deleted_at"] is not None:
                continue
            proposal_id = uuid.uuid4().hex
            evidence = {
                "confidence": finding["confidence"],
                "title_keywords": finding["evidence"]["title_keywords"],
                "tag_keywords": finding["evidence"]["tag_keywords"],
            }
            result = connection.execute(
                """INSERT OR IGNORE INTO recipe_quality_proposals
                   (id,recipe_id,field_name,current_value_json,proposed_value_json,
                    reason_code,evidence_json,analyzer_version,recipe_version,
                    owner_id,status,created_at,reviewed_at,reviewed_by,version)
                   VALUES (?,?,'meal_type',?,?,'category_hint',?,?,?,?,
                           'pending',?,NULL,NULL,1)""",
                (
                    proposal_id,
                    recipe["id"],
                    canonical_json(recipe["meal_type"]),
                    canonical_json(finding["review_category"]),
                    canonical_json(evidence),
                    recipe_quality.ANALYZER_VERSION,
                    int(recipe["version"]),
                    actor.principal_id,
                    utcnow(),
                ),
            )
            if result.rowcount:
                after = connection.execute(
                    "SELECT * FROM recipe_quality_proposals WHERE id=?", (proposal_id,)
                ).fetchone()
                audit_mutation(
                    connection, actor, domain="recipe_quality_proposal",
                    object_id=proposal_id, action="create", before=None, after=after,
                )
                created += 1
        pending = connection.execute(
            """SELECT COUNT(*) FROM recipe_quality_proposals
               WHERE owner_id=? AND status='pending'""",
            (actor.principal_id,),
        ).fetchone()[0]
    return jsonify(
        created=created,
        stale=stale,
        pending=int(pending),
        recipes_changed=0,
    )


@recipes_bp.put("/api/recipes/quality-review/<proposal_id>")
@require_profile()
def review_recipe_quality_proposal(proposal_id):
    actor, _name, error = actor_or_error()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    action = data.get("action")
    if action not in {"accept", "reject"}:
        return jsonify(error="Choose accept or reject."), 400
    version = expected_version(data)
    if version is None:
        return jsonify(error="Refresh the review before saving it.", conflict=True), 409

    with connect(DB_PATH) as connection:
        connection.execute("BEGIN IMMEDIATE")
        before = connection.execute(
            "SELECT * FROM recipe_quality_proposals WHERE id=?", (proposal_id,)
        ).fetchone()
        if not before:
            return jsonify(error="Review suggestion not found."), 404
        recipe_before = connection.execute(
            "SELECT * FROM recipes WHERE id=?", (before["recipe_id"],)
        ).fetchone()
        decision = authorize(
            "recipe.quality.review", actor,
            owner_id=recipe_before["owner_id"] if recipe_before else None,
        )
        if (
            not decision.allowed
            or before["owner_id"] != actor.principal_id
            or not recipe_before
            or recipe_before["owner_id"] != before["owner_id"]
        ):
            return jsonify(error="Only the recipe owner can review this suggestion."), 403
        if before["status"] != "pending" or int(before["version"]) != version:
            return jsonify(error="This suggestion was already reviewed.", conflict=True), 409

        current_json = canonical_json(recipe_before["meal_type"])
        binding_valid = (
            recipe_before["deleted_at"] is None
            and int(recipe_before["version"]) == int(before["recipe_version"])
            and before["field_name"] == "meal_type"
            and before["current_value_json"] == current_json
        )
        if not binding_valid:
            connection.execute(
                """UPDATE recipe_quality_proposals
                   SET status='stale', reviewed_at=?, reviewed_by=?, version=version+1
                   WHERE id=? AND version=? AND status='pending'""",
                (utcnow(), actor.principal_id, proposal_id, version),
            )
            after = connection.execute(
                "SELECT * FROM recipe_quality_proposals WHERE id=?", (proposal_id,)
            ).fetchone()
            audit_mutation(
                connection, actor, domain="recipe_quality_proposal",
                object_id=proposal_id, action="stale", before=before, after=after,
            )
            return jsonify(
                error="The recipe changed after this suggestion was created.",
                conflict=True,
                status="stale",
            ), 409

        reviewed_at = utcnow()
        if action == "reject":
            connection.execute(
                """UPDATE recipe_quality_proposals
                   SET status='rejected', reviewed_at=?, reviewed_by=?, version=version+1
                   WHERE id=? AND version=? AND status='pending'""",
                (reviewed_at, actor.principal_id, proposal_id, version),
            )
            after = connection.execute(
                "SELECT * FROM recipe_quality_proposals WHERE id=?", (proposal_id,)
            ).fetchone()
            audit_mutation(
                connection, actor, domain="recipe_quality_proposal",
                object_id=proposal_id, action="reject", before=before, after=after,
            )
            return jsonify(ok=True, status="rejected")

        proposed = json.loads(before["proposed_value_json"])
        if proposed not in RECIPE_SECTIONS:
            return jsonify(error="That suggested section is not valid."), 409
        changed = connection.execute(
            """UPDATE recipes
               SET meal_type=?, updated_at=?, version=version+1
               WHERE id=? AND owner_id=? AND version=? AND deleted_at IS NULL
                 AND meal_type=?""",
            (
                proposed,
                reviewed_at,
                recipe_before["id"],
                actor.principal_id,
                recipe_before["version"],
                recipe_before["meal_type"],
            ),
        )
        if changed.rowcount != 1:
            raise sqlite3.IntegrityError("recipe quality proposal binding changed")
        recipe_after = connection.execute(
            "SELECT * FROM recipes WHERE id=?", (recipe_before["id"],)
        ).fetchone()
        connection.execute(
            """UPDATE recipe_quality_proposals
               SET status='accepted', reviewed_at=?, reviewed_by=?, version=version+1
               WHERE id=? AND version=? AND status='pending'""",
            (reviewed_at, actor.principal_id, proposal_id, version),
        )
        after = connection.execute(
            "SELECT * FROM recipe_quality_proposals WHERE id=?", (proposal_id,)
        ).fetchone()
        audit_mutation(
            connection, actor, domain="recipe", object_id=recipe_before["id"],
            action="quality_accept", before=recipe_before, after=recipe_after,
        )
        audit_mutation(
            connection, actor, domain="recipe_quality_proposal",
            object_id=proposal_id, action="accept", before=before, after=after,
        )
    return jsonify(
        ok=True,
        status="accepted",
        recipe_id=recipe_after["id"],
        meal_type=recipe_after["meal_type"],
        recipe_version=int(recipe_after["version"]),
    )


@recipes_bp.get("/api/recipes/discover")
@require_profile()
def discover_recipes():
    _actor, _name, error = actor_or_error()
    if error:
        return error
    query = " ".join(request.args.get("q", "").split())[:100]
    if len(query) < 2:
        return jsonify(results=[])
    try:
        meals = mealdb_request("search.php", {"s": query}).get("meals") or []
    except (RuntimeError, ValueError) as error:
        return jsonify(error=str(error)), 503
    return jsonify(
        results=[mealdb_summary(meal) for meal in meals[:20]],
        source="TheMealDB",
    )


@recipes_bp.get("/api/recipes/discover/random")
@require_profile()
def random_discovery():
    _actor, _name, error = actor_or_error()
    if error:
        return error
    try:
        meals = mealdb_request("random.php").get("meals") or []
    except (RuntimeError, ValueError) as error:
        return jsonify(error=str(error)), 503
    return jsonify(
        results=[mealdb_summary(meal) for meal in meals[:1]],
        source="TheMealDB",
    )


@recipes_bp.get("/api/recipes/discover/image/<filename>")
@require_profile()
def discovery_recipe_image(filename):
    """Fetch and normalize a discovery thumbnail without exposing remote images."""
    _actor, _name, error = actor_or_error()
    if error:
        return error
    if not MEALDB_IMAGE_NAME.fullmatch(filename):
        return jsonify(error="Recipe image not found."), 404
    source_url = f"https://www.themealdb.com{MEALDB_IMAGE_PREFIX}{filename}"
    try:
        source_bytes, _, _ = fetch_public(source_url, image_only=True)
        data, _width, _height = render_recipe_image_derivative(source_bytes)
    except (
        OSError,
        ValueError,
        urllib.error.URLError,
        Image.DecompressionBombError,
    ):
        return jsonify(error="Recipe image unavailable."), 404
    digest = hashlib.sha256(data).hexdigest()
    return recipe_image_response(data, digest)


@recipes_bp.post("/api/recipes/import-mealdb")
@require_profile()
def import_mealdb_recipe():
    actor, owner_name, error = actor_or_error()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    meal_id = plain(data.get("mealdb_id"), 30)
    if not meal_id.isdigit():
        return jsonify(error="Choose a recipe from the discovery results."), 400
    meal_type = normalize_recipe_section(data.get("meal_type"), default="")
    if meal_type not in RECIPE_SECTIONS:
        return jsonify(error="Choose breakfast, lunch & dinner, or desserts."), 400
    try:
        meals = mealdb_request("lookup.php", {"i": meal_id}).get("meals") or []
        if not meals:
            return jsonify(error="That recipe is no longer available."), 404
        item = save_recipe(
            mealdb_recipe(meals[0], meal_type), actor, owner_name,
            imported=True, route_id="recipe.mealdb_import",
        )
    except FileExistsError as error:
        return jsonify(error=str(error), duplicate=True), 409
    except (RuntimeError, ValueError) as error:
        return jsonify(error=str(error)), 503
    return jsonify(recipe=item), 201


@recipes_bp.post("/api/recipes")
@require_profile()
def create_recipe():
    actor, owner_name, error = actor_or_error()
    if error:
        return error
    try:
        item = save_recipe(request.get_json(silent=True) or {}, actor, owner_name)
    except FileExistsError as error:
        return jsonify(error=str(error), duplicate=True), 409
    except ValueError as error:
        return jsonify(error=str(error)), 400
    return jsonify(recipe=item), 201


@recipes_bp.post("/api/recipes/import")
@require_profile()
def import_recipe():
    actor, owner_name, error = actor_or_error()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    try:
        page, _, final_url = fetch_public(str(data.get("url", "")))
        parsed = parse_recipe_page(page, final_url)
        parsed["meal_type"] = normalize_recipe_section(data.get("meal_type"))
        item = save_recipe(
            parsed, actor, owner_name, imported=True, route_id="recipe.url_import",
        )
        return jsonify(recipe=item), 201
    except FileExistsError as error:
        return jsonify(error=str(error), duplicate=True), 409
    except (ValueError, urllib.error.URLError, TimeoutError, OSError) as error:
        return jsonify(error=str(error) or "This recipe could not be imported."), 400


@recipes_bp.get("/api/recipes/<recipe_id>")
@require_profile()
def get_recipe(recipe_id):
    actor, _name, error = actor_or_error()
    if error:
        return error
    with connect(DB_PATH) as connection:
        row = connection.execute(
            f"""SELECT {recipe_select()} FROM recipes r
                LEFT JOIN recipe_activity activity
                  ON activity.recipe_id=r.id AND activity.owner_id=?
                WHERE r.id=? AND r.deleted_at IS NULL AND {visible_sql('r')}""",
            (actor.principal_id, recipe_id, actor.principal_id),
        ).fetchone()
    if not row:
        return jsonify(error="Recipe not found."), 404
    return jsonify(recipe=recipe_json(row, actor.principal_id))


@recipes_bp.post("/api/recipes/<recipe_id>/viewed")
@require_profile()
def mark_viewed(recipe_id):
    actor, _name, error = actor_or_error()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    state_version = expected_version(data, field="state_version")
    with connect(DB_PATH) as connection:
        connection.execute("BEGIN IMMEDIATE")
        recipe = connection.execute(
            f"SELECT * FROM recipes WHERE id=? AND deleted_at IS NULL AND {visible_sql()}",
            (recipe_id, actor.principal_id),
        ).fetchone()
        if not recipe:
            return jsonify(error="Recipe not found."), 404
        before = personal_activity(connection, recipe_id, actor.principal_id)
        decision = authorize(
            "recipe.viewed.update", actor,
            personal_owner_id=before["owner_id"] if before else None,
            allow_personal_create=before is None,
        )
        if not decision.allowed:
            return jsonify(error="That activity belongs to another person."), 403
        after, conflict = set_personal_activity(
            connection, recipe_id, actor, action="viewed", viewed=True,
            version=state_version,
        )
        if conflict:
            return jsonify(error="Your recipe activity changed on another screen.", conflict=True), 409
    return jsonify(ok=True, state_version=after["version"])


@recipes_bp.post("/api/recipes/<recipe_id>/favorite")
@require_profile()
def favorite_recipe(recipe_id):
    actor, _name, error = actor_or_error()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    if not isinstance(data.get("favorite"), bool):
        return jsonify(error="Choose whether this recipe is a favorite."), 400
    state_version = expected_version(data, field="state_version")
    with connect(DB_PATH) as connection:
        connection.execute("BEGIN IMMEDIATE")
        recipe = connection.execute(
            f"SELECT * FROM recipes WHERE id=? AND deleted_at IS NULL AND {visible_sql()}",
            (recipe_id, actor.principal_id),
        ).fetchone()
        if not recipe:
            return jsonify(error="Recipe not found."), 404
        before = personal_activity(connection, recipe_id, actor.principal_id)
        decision = authorize(
            "recipe.favorite.update", actor,
            personal_owner_id=before["owner_id"] if before else None,
            allow_personal_create=before is None,
        )
        if not decision.allowed:
            return jsonify(error="That favorite belongs to another person."), 403
        after, conflict = set_personal_activity(
            connection, recipe_id, actor, action="favorite",
            favorite=bool(data.get("favorite")), version=state_version,
        )
        if conflict:
            return jsonify(error="This recipe changed on another screen.", conflict=True), 409
    return jsonify(ok=True, favorite=bool(after["favorite"]), state_version=after["version"])


@recipes_bp.put("/api/recipes/<recipe_id>")
@require_profile()
def update_recipe(recipe_id):
    actor, _name, error = actor_or_error()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    if "favorite" in data and not isinstance(data.get("favorite"), bool):
        return jsonify(error="Choose whether this recipe is a favorite."), 400
    version = expected_version(data)
    if version is None:
        return jsonify(error="Refresh this recipe before saving it.", conflict=True), 409
    title = plain(data.get("title"), 200)
    if not title:
        return jsonify(error="Give the recipe a name."), 400
    meal = normalize_recipe_section(data.get("meal_type"), default="")
    if meal not in RECIPE_SECTIONS:
        return jsonify(error="Choose breakfast, lunch & dinner, or desserts."), 400
    ingredients = clean_lines(data.get("ingredients", []))
    instructions = clean_lines(data.get("instructions", []))
    fingerprint = recipe_fingerprint(title, ingredients, instructions)
    with connect(DB_PATH) as connection:
        connection.execute("BEGIN IMMEDIATE")
        before = connection.execute(
            f"SELECT * FROM recipes WHERE id=? AND deleted_at IS NULL AND {visible_sql()}",
            (recipe_id, actor.principal_id),
        ).fetchone()
        if not before:
            return jsonify(error="Recipe not found."), 404
        decision = authorize(
            "recipe.update", actor, owner_id=before["owner_id"],
            visibility=before["visibility"],
        )
        if not decision.allowed:
            return jsonify(
                error="Only the person who added this recipe can change it.",
                legacy_read_only=before["owner_id"] is None,
            ), 403
        if int(before["version"]) != version:
            return jsonify(error="This recipe changed on another screen.", conflict=True), 409
        try:
            visibility = validated_visibility(
                data, default=before["visibility"] or "shared"
            )
        except ValueError as error:
            return jsonify(error=str(error)), 400
        if not str(before["owner_id"] or "").strip() and visibility != "shared":
            return jsonify(error="Legacy shared recipes must remain shared."), 400
        activity_before = None
        state_version = None
        if "favorite" in data:
            activity_before = personal_activity(connection, recipe_id, actor.principal_id)
            state_version = expected_version(data, field="state_version")
            activity_decision = authorize(
                "recipe.favorite.update", actor,
                personal_owner_id=activity_before["owner_id"] if activity_before else None,
                allow_personal_create=activity_before is None,
            )
            if not activity_decision.allowed:
                return jsonify(error="That favorite belongs to another person."), 403
            if activity_before is not None and (
                state_version is None or int(activity_before["version"]) != state_version
            ):
                return jsonify(
                    error="Your favorite changed on another screen.", conflict=True
                ), 409
            if activity_before is None and state_version is not None:
                return jsonify(
                    error="Your favorite changed on another screen.", conflict=True
                ), 409
        source = before["catalog_source_url"] or before["source_url"]
        duplicate = connection.execute(
            f"""SELECT 1 FROM recipes
               WHERE (content_hash = ? OR
                      (? IS NOT NULL AND
                       COALESCE(catalog_source_url, source_url) = ?))
                 AND id != ? AND deleted_at IS NULL AND {visible_sql()}
               LIMIT 1""",
            (fingerprint, source, source, recipe_id, actor.principal_id),
        ).fetchone()
        if duplicate:
            return jsonify(error="That recipe is already in your library.", duplicate=True), 409
        result = connection.execute(
            f"""UPDATE recipes SET title=?, description=?, meal_type=?, tags_json=?, total_minutes=?, servings=?,
               ingredients_json=?, instructions_json=?, content_hash=?, visibility=?, updated_at=?, version=version+1
               WHERE id=? AND deleted_at IS NULL AND {mutable_sql()} AND version=?""",
            (title, plain(data.get("description"), 2000), meal, json.dumps(clean_tags(data.get("tags", []))),
             int(data["total_minutes"]) if str(data.get("total_minutes", "")).isdigit() else None,
             plain(data.get("servings"), 100), json.dumps(ingredients), json.dumps(instructions), fingerprint,
             visibility,
             utcnow(), recipe_id, actor.principal_id, version),
        )
        row = connection.execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
        if result.rowcount:
            audit_mutation(
                connection, actor, domain="recipe", object_id=recipe_id,
                action="update", before=before, after=row,
            )
            if "favorite" in data:
                _activity, conflict = set_personal_activity(
                    connection, recipe_id, actor, action="favorite",
                    favorite=bool(data.get("favorite")), version=state_version,
                )
                if conflict:
                    raise sqlite3.IntegrityError("stale recipe activity version")
            row = connection.execute(
                f"""SELECT {recipe_select()} FROM recipes r
                    LEFT JOIN recipe_activity activity
                      ON activity.recipe_id=r.id AND activity.owner_id=?
                    WHERE r.id=?""",
                (actor.principal_id, recipe_id),
            ).fetchone()
    if not result.rowcount:
        return jsonify(error="Recipe not found."), 404
    return jsonify(ok=True, recipe=recipe_json(row, actor.principal_id))


@recipes_bp.post("/api/recipes/<recipe_id>/made")
@require_profile()
def mark_made(recipe_id):
    actor, _name, error = actor_or_error()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    state_version = expected_version(data, field="state_version")
    with connect(DB_PATH) as connection:
        connection.execute("BEGIN IMMEDIATE")
        recipe = connection.execute(
            f"SELECT * FROM recipes WHERE id=? AND deleted_at IS NULL AND {visible_sql()}",
            (recipe_id, actor.principal_id),
        ).fetchone()
        if not recipe:
            return jsonify(error="Recipe not found."), 404
        before = personal_activity(connection, recipe_id, actor.principal_id)
        decision = authorize(
            "recipe.made.update", actor,
            personal_owner_id=before["owner_id"] if before else None,
            allow_personal_create=before is None,
        )
        if not decision.allowed:
            return jsonify(error="That activity belongs to another person."), 403
        after, conflict = set_personal_activity(
            connection, recipe_id, actor, action="made", made=True,
            version=state_version,
        )
        if conflict:
            return jsonify(error="Your recipe activity changed on another screen.", conflict=True), 409
    return jsonify(ok=True, state_version=after["version"])


@recipes_bp.delete("/api/recipes/<recipe_id>")
@require_profile()
def trash_recipe(recipe_id):
    actor, _name, error = actor_or_error()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    version = expected_version(data)
    if version is None:
        return jsonify(error="Refresh this recipe before removing it.", conflict=True), 409
    with connect(DB_PATH) as connection:
        connection.execute("BEGIN IMMEDIATE")
        before = connection.execute(
            f"SELECT * FROM recipes WHERE id=? AND deleted_at IS NULL AND {visible_sql()}",
            (recipe_id, actor.principal_id),
        ).fetchone()
        if not before:
            return jsonify(error="Recipe not found."), 404
        decision = authorize(
            "recipe.trash", actor, owner_id=before["owner_id"],
            visibility=before["visibility"],
        )
        if not decision.allowed:
            return jsonify(
                error="Only the person who added this recipe can remove it.",
                legacy_read_only=before["owner_id"] is None,
            ), 403
        if int(before["version"]) != version:
            return jsonify(error="This recipe changed on another screen.", conflict=True), 409
        deleted_at = utcnow()
        purge_after = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        changed = connection.execute(
            f"""UPDATE recipes SET deleted_at=?, deleted_by=?, purge_after=?,
                      version=version+1, updated_at=?
               WHERE id=? AND {mutable_sql()} AND version=? AND deleted_at IS NULL""",
            (deleted_at, actor.principal_id, purge_after, deleted_at,
             recipe_id, actor.principal_id, version),
        )
        if not changed.rowcount:
            return jsonify(error="This recipe changed on another screen.", conflict=True), 409
        after = connection.execute("SELECT * FROM recipes WHERE id=?", (recipe_id,)).fetchone()
        audit_mutation(
            connection, actor, domain="recipe", object_id=recipe_id,
            action="trash", before=before, after=after,
        )
    return jsonify(ok=True, version=after["version"], purge_after=after["purge_after"])


@recipes_bp.post("/api/recipes/<recipe_id>/restore")
@require_profile()
def restore_recipe(recipe_id):
    actor, _name, error = actor_or_error()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    version = expected_version(data)
    if version is None:
        return jsonify(error="Refresh this recipe before restoring it.", conflict=True), 409
    with connect(DB_PATH) as connection:
        connection.execute("BEGIN IMMEDIATE")
        before = connection.execute("SELECT * FROM recipes WHERE id=?", (recipe_id,)).fetchone()
        if not before or not row_visible(before, actor):
            return jsonify(error="Recipe not found."), 404
        decision = authorize(
            "recipe.restore", actor, owner_id=before["owner_id"],
            visibility=before["visibility"],
        )
        if not decision.allowed:
            return jsonify(error="Only the owner can restore this recipe."), 403
        if not before["deleted_at"]:
            return jsonify(error="That recipe is already in the library."), 409
        source = before["catalog_source_url"] or before["source_url"]
        duplicate = connection.execute(
            f"""SELECT 1 FROM recipes
                WHERE id != ? AND deleted_at IS NULL
                  AND (content_hash = ? OR
                       (? IS NOT NULL AND
                        COALESCE(catalog_source_url, source_url) = ?))
                  AND {visible_sql()}
                LIMIT 1""",
            (
                recipe_id, before["content_hash"], source, source,
                actor.principal_id,
            ),
        ).fetchone()
        if duplicate:
            return jsonify(
                error="That recipe is already in your library.", duplicate=True
            ), 409
        changed = connection.execute(
            f"""UPDATE recipes SET deleted_at=NULL, deleted_by=NULL, purge_after=NULL,
                      version=version+1, updated_at=?
               WHERE id=? AND {mutable_sql()} AND version=? AND deleted_at IS NOT NULL""",
            (utcnow(), recipe_id, actor.principal_id, version),
        )
        if not changed.rowcount:
            return jsonify(error="This recipe changed on another screen.", conflict=True), 409
        after = connection.execute("SELECT * FROM recipes WHERE id=?", (recipe_id,)).fetchone()
        audit_mutation(
            connection, actor, domain="recipe", object_id=recipe_id,
            action="restore", before=before, after=after,
        )
    return jsonify(ok=True, version=after["version"])


@recipes_bp.route("/api/recipes/recommend", methods=["GET", "POST"])
@require_profile()
def recommend_recipes():
    actor, _name, error = actor_or_error()
    if error:
        return error
    data = (request.get_json(silent=True) or {}) if request.method == "POST" else request.args
    if request.method == "POST" and not authorize(
        "recipe.recommendation.create", actor
    ).allowed:
        return jsonify(error="This account cannot request recipe recommendations."), 403
    meal = normalize_recipe_section(data.get("meal"), default="")
    if meal not in RECIPE_SECTIONS:
        return jsonify(error="Choose breakfast, lunch & dinner, or desserts."), 400
    quick = data.get("quick") in (True, 1, "1", "true")
    favorite = data.get("favorite") in (True, 1, "1", "true")
    ingredients = [
        plain(item, 80).lower()
        for item in re.split(r"[,\n]+", plain(data.get("ingredient"), 400))
        if plain(item, 80)
    ][:8]
    conditions = ["r.meal_type = ?", "r.deleted_at IS NULL", visible_sql("r")]
    parameters = [meal, actor.principal_id]
    if quick:
        conditions.append("r.total_minutes IS NOT NULL AND r.total_minutes <= 30")
    if favorite:
        conditions.append("COALESCE(activity.favorite, r.favorite) = 1")
    for ingredient in ingredients:
        conditions.append("LOWER(r.ingredients_json) LIKE ?")
        parameters.append(f"%{ingredient}%")
    with connect(DB_PATH) as connection:
        if request.method == "POST":
            connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            f"""SELECT {recipe_select()} FROM recipes r
                LEFT JOIN recipe_activity activity
                  ON activity.recipe_id=r.id AND activity.owner_id=?
                WHERE {' AND '.join(conditions)}""",
            [actor.principal_id, *parameters],
        ).fetchall()
        recent = {row[0] for row in connection.execute(
            "SELECT recipe_id FROM recipe_recommendations ORDER BY recommended_at DESC LIMIT 12"
        )}
        pool = [row for row in rows if row["id"] not in recent] or rows
        choices = random.sample(pool, min(3, len(pool))) if pool else []
        if request.method == "POST":
            request_id = uuid.uuid4().hex
            connection.executemany(
                "INSERT INTO recipe_recommendations VALUES (?, ?)",
                [(row["id"], utcnow()) for row in choices],
            )
            audit_mutation(
                connection, actor, domain="recipe_recommendation",
                object_id=request_id, action="create", before=None, after=None,
            )
    return jsonify(recipes=[recipe_json(row, actor.principal_id) for row in choices])


def render_recipe_image_derivative(source_bytes):
    with Image.open(BytesIO(source_bytes)) as image:
        if image.width * image.height > MAX_RECIPE_IMAGE_PIXELS:
            raise ValueError("The recipe image is too large.")
        image.load()
        safe_image = ImageOps.exif_transpose(image)
        safe_image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
        width, height = safe_image.size
        safe = BytesIO()
        safe_image.convert("RGB").save(safe, "JPEG", quality=86, optimize=True)
    derivative = safe.getvalue()
    if not derivative or len(derivative) > MAX_RECIPE_IMAGE_BYTES:
        raise ValueError("The optimized recipe image is too large.")
    return derivative, width, height


def cached_recipe_image(recipe_id, source_url):
    with connect(IMAGE_CACHE_PATH) as connection:
        row = connection.execute(
            """SELECT source_url,derivative_sha256,content_type,image_bytes
               FROM recipe_image_derivatives WHERE recipe_id=?""",
            (recipe_id,),
        ).fetchone()
    if not row or row["source_url"] != source_url:
        return None
    data = bytes(row["image_bytes"])
    if (
        row["content_type"] != "image/jpeg"
        or not data
        or len(data) > MAX_RECIPE_IMAGE_BYTES
        or hashlib.sha256(data).hexdigest() != row["derivative_sha256"]
    ):
        return None
    return data, row["derivative_sha256"]


def recipe_image_response(data, digest, *, shared=False):
    response = Response(data, mimetype="image/jpeg")
    response.set_etag(digest)
    response.headers["Cache-Control"] = "private, no-cache, max-age=0, must-revalidate"
    if shared:
        response.headers[PRIVATE_REVALIDATE_SCOPE_HEADER] = "shared"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response.make_conditional(request)


@recipes_bp.get("/api/recipes/<recipe_id>/image")
@require_profile()
def recipe_image(recipe_id):
    actor, _name, error = actor_or_error()
    if error:
        return error
    with connect(DB_PATH) as connection:
        row = connection.execute(
            f"""SELECT image_url,version,visibility,
                       COALESCE(catalog_source_url, source_url) AS attribution_url
                FROM recipes
                WHERE id=? AND deleted_at IS NULL AND {visible_sql()}""",
            (recipe_id, actor.principal_id),
        ).fetchone()
    if not row or not row["image_url"]:
        return "", 404
    try:
        # ``fetch_public`` performs the pinned-DNS URL validation on a miss.
        # A cache hit is tied to the exact previously fetched locator and never
        # makes a new network request.
        source_url = str(row["image_url"])
        cached = cached_recipe_image(recipe_id, source_url)
        if cached:
            return recipe_image_response(*cached, shared=row["visibility"] == "shared")
        from .installation import get_installation, module_mode
        if get_installation() and module_mode("recipes") != "connected":
            return "", 404
        source_bytes, _, fetched_url = fetch_public(source_url, image_only=True)
        data, width, height = render_recipe_image_derivative(source_bytes)
        derivative_sha256 = hashlib.sha256(data).hexdigest()
        source_sha256 = hashlib.sha256(source_bytes).hexdigest()
        with connect(DB_PATH) as connection:
            current = connection.execute(
                f"""SELECT image_url,version,visibility
                    FROM recipes WHERE id=? AND deleted_at IS NULL AND {visible_sql()}""",
                (recipe_id, actor.principal_id),
            ).fetchone()
        if (
            not current
            or int(current["version"]) != int(row["version"])
            or str(current["image_url"] or "") != source_url
        ):
            return "", 404
        with connect(IMAGE_CACHE_PATH) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO recipe_image_derivatives
                   (recipe_id,source_url,fetched_url,source_sha256,derivative_sha256,
                    content_type,width,height,image_bytes,attribution_url,generated_at,version)
                   VALUES (?,?,?,?,?,'image/jpeg',?,?,?,?,?,1)
                   ON CONFLICT(recipe_id) DO UPDATE SET
                     source_url=excluded.source_url,
                     fetched_url=excluded.fetched_url,
                     source_sha256=excluded.source_sha256,
                     derivative_sha256=excluded.derivative_sha256,
                     content_type=excluded.content_type,
                     width=excluded.width,
                     height=excluded.height,
                     image_bytes=excluded.image_bytes,
                     attribution_url=excluded.attribution_url,
                     generated_at=excluded.generated_at,
                     version=recipe_image_derivatives.version+1""",
                (
                    recipe_id,
                    source_url,
                    fetched_url,
                    source_sha256,
                    derivative_sha256,
                    width,
                    height,
                    data,
                    row["attribution_url"],
                    utcnow(),
                ),
            )
    except (
        ValueError,
        urllib.error.URLError,
        TimeoutError,
        OSError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
    ):
        return "", 404
    return recipe_image_response(data, derivative_sha256, shared=current["visibility"] == "shared")


def init_recipes(app):
    recipe_planner.register(recipes_bp, DB_PATH, actor_or_error, plain)
    app.register_blueprint(recipes_bp)

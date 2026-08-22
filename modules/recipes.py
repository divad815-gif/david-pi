import hashlib
import html
import http.client
import ipaddress
from io import BytesIO
import json
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
from datetime import datetime, timezone
from html.parser import HTMLParser

from flask import Blueprint, Response, jsonify, render_template, request
from PIL import Image, UnidentifiedImageError

from .identity import current_device, require_profile
from .platform import PLATFORM_DATA, connect, migrate, utcnow


DB_PATH = PLATFORM_DATA / "recipes.db"
recipes_bp = Blueprint("recipes", __name__)
MAX_PAGE_BYTES = 2 * 1024 * 1024
MEALDB_BASE = "https://www.themealdb.com/api/json/v1/1/"
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
BREAKFAST_TITLE_PATTERN = re.compile(
    r"\b(breakfast|brunch|pancakes?|waffles?|omelettes?|french toast|"
    r"eggs benedict|porridge|muesli|granola|hash browns?)\b",
    re.IGNORECASE,
)
DESSERT_TITLE_PATTERN = re.compile(
    r"\b(dessert|cake|cheesecake|cookies?|biscuits?|brownies?|pudding|"
    r"ice cream|gelato|sorbet|tiramisu|baklava|doughnuts?|donuts?|"
    r"cr.me br.l.e|cupcakes?|fudge|truffles?|tarts?|cobbler|"
    r"sweet bread)\b",
    re.IGNORECASE,
)


def normalize_recipe_section(value, default="main"):
    return RECIPE_SECTION_ALIASES.get(str(value or "").strip().lower(), default)


def classify_legacy_recipe(title, tags):
    """Map the old breakfast/lunch/dinner split into three useful shelves."""
    normalized_tags = {str(tag).strip().lower() for tag in (tags or []) if str(tag).strip()}
    title = str(title or "")
    if normalized_tags & {"breakfast", "brunch"} or BREAKFAST_TITLE_PATTERN.search(title):
        return "breakfast"
    if normalized_tags & {"dessert", "desert", "pudding", "cake", "sweet", "treat"} or DESSERT_TITLE_PATTERN.search(title):
        return "dessert"
    return "main"


def initialize_recipes(connection):
    connection.execute(
        """CREATE TABLE IF NOT EXISTS recipes (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
            meal_type TEXT NOT NULL DEFAULT 'dinner', tags_json TEXT NOT NULL DEFAULT '[]',
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
    legacy_rows = connection.execute(
        "SELECT id, title, tags_json FROM recipes WHERE meal_type IN ('lunch', 'dinner')"
    ).fetchall()
    for row in legacy_rows:
        try:
            tags = json.loads(row["tags_json"] or "[]")
        except (TypeError, json.JSONDecodeError):
            tags = []
        connection.execute(
            "UPDATE recipes SET meal_type = ? WHERE id = ?",
            (classify_legacy_recipe(row["title"], tags), row["id"]),
        )


migrate(DB_PATH, initialize_recipes)


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


def recipe_json(row):
    item = dict(row)
    item["tags"] = json.loads(item.pop("tags_json") or "[]")
    item["ingredients"] = json.loads(item.pop("ingredients_json") or "[]")
    item["instructions"] = json.loads(item.pop("instructions_json") or "[]")
    item["favorite"] = bool(item["favorite"])
    item["image"] = f"/api/recipes/{item['id']}/image" if item["image_url"] else None
    return item


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
        "total_minutes": duration_minutes(structured.get("totalTime")) or duration_minutes(structured.get("cookTime")),
        "servings": plain(structured.get("recipeYield"), 100),
        "ingredients": clean_lines(structured.get("recipeIngredient", [])),
        "instructions": instruction_lines,
        "tags": clean_tags(structured.get("keywords", "").split(",")),
        "source_name": host.removeprefix("www."),
        "source_url": final_url,
        "image_url": public_url(image) if image else None,
    }


def mealdb_request(endpoint, parameters=None):
    url = MEALDB_BASE + endpoint
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
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise RuntimeError("The recipe discovery service is unavailable right now.") from error


def mealdb_summary(meal):
    return {
        "mealdb_id": plain(meal.get("idMeal"), 30),
        "title": plain(meal.get("strMeal"), 200),
        "category": plain(meal.get("strCategory"), 60),
        "area": plain(meal.get("strArea"), 60),
        "image_url": public_url(meal["strMealThumb"]) if meal.get("strMealThumb") else None,
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


def save_recipe(data, imported=False):
    title = plain(data.get("title"), 200)
    if not title:
        raise ValueError("Give the recipe a name.")
    meal = normalize_recipe_section(data.get("meal_type"))
    source = public_url(data["source_url"]) if data.get("source_url") else None
    ingredients = clean_lines(data.get("ingredients", []))
    instructions = clean_lines(data.get("instructions", []))
    fingerprint = hashlib.sha256(json.dumps([title.lower(), ingredients, instructions], sort_keys=True).encode()).hexdigest()
    recipe_id, now = uuid.uuid4().hex, utcnow()
    try:
        with connect(DB_PATH) as connection:
            duplicate = connection.execute(
                "SELECT 1 FROM recipes WHERE content_hash = ? AND deleted_at IS NULL LIMIT 1",
                (fingerprint,),
            ).fetchone()
            if duplicate:
                raise FileExistsError("That recipe is already in your library.")
            connection.execute(
                """INSERT INTO recipes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, NULL, NULL, NULL)""",
                (recipe_id, title, plain(data.get("description"), 2000), meal, json.dumps(clean_tags(data.get("tags", []))),
                 int(data["total_minutes"]) if str(data.get("total_minutes", "")).isdigit() else None,
                 plain(data.get("servings"), 100), json.dumps(ingredients), json.dumps(instructions),
                 plain(data.get("source_name"), 200) or None, source, data.get("image_url"), fingerprint,
                 "home", now, now, now if imported else None),
            )
            row = connection.execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
    except sqlite3.IntegrityError:
        raise FileExistsError("That recipe has already been imported.")
    return recipe_json(row)


@recipes_bp.get("/recipes")
@require_profile(api=False)
def recipes_page():
    return render_template("recipes.html")


@recipes_bp.get("/api/recipes")
@require_profile()
def list_recipes():
    query = " ".join(request.args.get("q", "").split()).lower()[:120]
    meal = request.args.get("meal", "")
    conditions = ["deleted_at IS NULL"]
    parameters = []
    if query:
        conditions.append("(LOWER(title) LIKE ? OR LOWER(tags_json) LIKE ? OR LOWER(ingredients_json) LIKE ?)")
        pattern = f"%{query}%"
        parameters.extend([pattern, pattern, pattern])
    meal = normalize_recipe_section(meal, default="")
    if meal in RECIPE_SECTIONS:
        conditions.append("meal_type = ?")
        parameters.append(meal)
    with connect(DB_PATH) as connection:
        rows = connection.execute(
            f"SELECT * FROM recipes WHERE {' AND '.join(conditions)} ORDER BY favorite DESC, updated_at DESC", parameters
        ).fetchall()
    return jsonify(recipes=[recipe_json(row) for row in rows])


@recipes_bp.get("/api/recipes/discover")
@require_profile()
def discover_recipes():
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
    try:
        meals = mealdb_request("random.php").get("meals") or []
    except (RuntimeError, ValueError) as error:
        return jsonify(error=str(error)), 503
    return jsonify(
        results=[mealdb_summary(meal) for meal in meals[:1]],
        source="TheMealDB",
    )


@recipes_bp.post("/api/recipes/import-mealdb")
@require_profile()
def import_mealdb_recipe():
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
        item = save_recipe(mealdb_recipe(meals[0], meal_type), imported=True)
    except FileExistsError as error:
        return jsonify(error=str(error), duplicate=True), 409
    except (RuntimeError, ValueError) as error:
        return jsonify(error=str(error)), 503
    return jsonify(recipe=item), 201


@recipes_bp.post("/api/recipes")
@require_profile()
def create_recipe():
    try:
        item = save_recipe(request.get_json(silent=True) or {})
    except FileExistsError as error:
        return jsonify(error=str(error), duplicate=True), 409
    except ValueError as error:
        return jsonify(error=str(error)), 400
    return jsonify(recipe=item), 201


@recipes_bp.post("/api/recipes/import")
@require_profile()
def import_recipe():
    data = request.get_json(silent=True) or {}
    try:
        page, _, final_url = fetch_public(str(data.get("url", "")))
        parsed = parse_recipe_page(page, final_url)
        parsed["meal_type"] = normalize_recipe_section(data.get("meal_type"))
        item = save_recipe(parsed, imported=True)
        return jsonify(recipe=item), 201
    except FileExistsError as error:
        return jsonify(error=str(error), duplicate=True), 409
    except (ValueError, urllib.error.URLError, TimeoutError, OSError) as error:
        return jsonify(error=str(error) or "This recipe could not be imported."), 400


@recipes_bp.get("/api/recipes/<recipe_id>")
@require_profile()
def get_recipe(recipe_id):
    with connect(DB_PATH) as connection:
        row = connection.execute("SELECT * FROM recipes WHERE id = ? AND deleted_at IS NULL", (recipe_id,)).fetchone()
    if not row:
        return jsonify(error="Recipe not found."), 404
    return jsonify(recipe=recipe_json(row))


@recipes_bp.post("/api/recipes/<recipe_id>/viewed")
@require_profile()
def mark_viewed(recipe_id):
    with connect(DB_PATH) as connection:
        result = connection.execute(
            "UPDATE recipes SET last_viewed_at = ? WHERE id = ? AND deleted_at IS NULL",
            (utcnow(), recipe_id),
        )
    return (jsonify(ok=True), 200) if result.rowcount else (jsonify(error="Recipe not found."), 404)


@recipes_bp.put("/api/recipes/<recipe_id>")
@require_profile()
def update_recipe(recipe_id):
    data = request.get_json(silent=True) or {}
    title = plain(data.get("title"), 200)
    if not title:
        return jsonify(error="Give the recipe a name."), 400
    meal = normalize_recipe_section(data.get("meal_type"), default="")
    if meal not in RECIPE_SECTIONS:
        return jsonify(error="Choose breakfast, lunch & dinner, or desserts."), 400
    with connect(DB_PATH) as connection:
        result = connection.execute(
            """UPDATE recipes SET title=?, description=?, meal_type=?, tags_json=?, total_minutes=?, servings=?,
               ingredients_json=?, instructions_json=?, favorite=?, updated_at=? WHERE id=? AND deleted_at IS NULL""",
            (title, plain(data.get("description"), 2000), meal, json.dumps(clean_tags(data.get("tags", []))),
             int(data["total_minutes"]) if str(data.get("total_minutes", "")).isdigit() else None,
             plain(data.get("servings"), 100), json.dumps(clean_lines(data.get("ingredients", []))),
             json.dumps(clean_lines(data.get("instructions", []))), int(bool(data.get("favorite"))), utcnow(), recipe_id),
        )
        row = connection.execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
    if not result.rowcount:
        return jsonify(error="Recipe not found."), 404
    return jsonify(ok=True, recipe=recipe_json(row))


@recipes_bp.post("/api/recipes/<recipe_id>/made")
@require_profile()
def mark_made(recipe_id):
    with connect(DB_PATH) as connection:
        result = connection.execute("UPDATE recipes SET last_made_at = ? WHERE id = ? AND deleted_at IS NULL", (utcnow(), recipe_id))
    if not result.rowcount:
        return jsonify(error="Recipe not found."), 404
    return jsonify(ok=True)


@recipes_bp.delete("/api/recipes/<recipe_id>")
@require_profile()
def trash_recipe(recipe_id):
    with connect(DB_PATH) as connection:
        result = connection.execute("UPDATE recipes SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL", (utcnow(), recipe_id))
    if not result.rowcount:
        return jsonify(error="Recipe not found."), 404
    return jsonify(ok=True)


@recipes_bp.route("/api/recipes/recommend", methods=["GET", "POST"])
@require_profile()
def recommend_recipes():
    data = (request.get_json(silent=True) or {}) if request.method == "POST" else request.args
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
    conditions = ["meal_type = ?", "deleted_at IS NULL"]
    parameters = [meal]
    if quick:
        conditions.append("total_minutes IS NOT NULL AND total_minutes <= 30")
    if favorite:
        conditions.append("favorite = 1")
    for ingredient in ingredients:
        conditions.append("LOWER(ingredients_json) LIKE ?")
        parameters.append(f"%{ingredient}%")
    with connect(DB_PATH) as connection:
        rows = connection.execute(f"SELECT * FROM recipes WHERE {' AND '.join(conditions)}", parameters).fetchall()
        recent = {row[0] for row in connection.execute(
            "SELECT recipe_id FROM recipe_recommendations ORDER BY recommended_at DESC LIMIT 12"
        )}
        pool = [row for row in rows if row["id"] not in recent] or rows
        choices = random.sample(pool, min(3, len(pool))) if pool else []
        if request.method == "POST":
            connection.executemany("INSERT INTO recipe_recommendations VALUES (?, ?)", [(row["id"], utcnow()) for row in choices])
    return jsonify(recipes=[recipe_json(row) for row in choices])


@recipes_bp.get("/api/recipes/<recipe_id>/image")
@require_profile()
def recipe_image(recipe_id):
    with connect(DB_PATH) as connection:
        row = connection.execute("SELECT image_url FROM recipes WHERE id = ? AND deleted_at IS NULL", (recipe_id,)).fetchone()
    if not row or not row["image_url"]:
        return "", 404
    try:
        data, _, _ = fetch_public(row["image_url"], image_only=True)
        with Image.open(BytesIO(data)) as image:
            if image.width * image.height > 25_000_000:
                raise ValueError("The recipe image is too large.")
            image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
            safe = BytesIO()
            image.convert("RGB").save(safe, "JPEG", quality=86, optimize=True)
        data = safe.getvalue()
    except (ValueError, urllib.error.URLError, TimeoutError, OSError, UnidentifiedImageError):
        return "", 404
    return Response(data, mimetype="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


def init_recipes(app):
    app.register_blueprint(recipes_bp)

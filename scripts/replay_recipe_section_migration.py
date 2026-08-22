import json
import re
import sqlite3
import sys


BREAKFAST = re.compile(
    r"\b(breakfast|brunch|pancakes?|waffles?|omelettes?|french toast|"
    r"eggs benedict|porridge|muesli|granola|hash browns?)\b",
    re.IGNORECASE,
)
DESSERT = re.compile(
    r"\b(dessert|cake|cheesecake|cookies?|biscuits?|brownies?|pudding|"
    r"ice cream|gelato|sorbet|tiramisu|baklava|doughnuts?|donuts?|"
    r"cr.me br.l.e|cupcakes?|fudge|truffles?|tarts?|cobbler|sweet bread)\b",
    re.IGNORECASE,
)


def classify(title, tags):
    normalized = {str(tag).strip().lower() for tag in tags if str(tag).strip()}
    if normalized & {"breakfast", "brunch"} or BREAKFAST.search(title or ""):
        return "breakfast"
    if normalized & {"dessert", "desert", "pudding", "cake", "sweet", "treat"} or DESSERT.search(title or ""):
        return "dessert"
    return "main"


live_path, original_backup_path, safety_backup_path = sys.argv[1:4]
live = sqlite3.connect(live_path)
original = sqlite3.connect(f"file:{original_backup_path}?mode=ro", uri=True)
with sqlite3.connect(safety_backup_path) as safety:
    live.backup(safety)

legacy = original.execute(
    "SELECT id, title, tags_json FROM recipes WHERE meal_type IN ('lunch', 'dinner')"
).fetchall()
changes = {"breakfast": 0, "main": 0, "dessert": 0}
live.execute("BEGIN IMMEDIATE")
try:
    for recipe_id, title, tags_json in legacy:
        try:
            tags = json.loads(tags_json or "[]")
        except (TypeError, json.JSONDecodeError):
            tags = []
        section = classify(title, tags)
        live.execute("UPDATE recipes SET meal_type=? WHERE id=?", (section, recipe_id))
        changes[section] += 1
    live.commit()
except Exception:
    live.rollback()
    raise
print("replayed=" + str(len(legacy)))
for section in ("breakfast", "main", "dessert"):
    print(f"{section}={changes[section]}")

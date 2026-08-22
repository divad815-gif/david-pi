import sqlite3
import sys


connection = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
rows = connection.execute(
    "SELECT meal_type, COUNT(*) FROM recipes "
    "WHERE deleted_at IS NULL GROUP BY meal_type ORDER BY meal_type"
).fetchall()
total = connection.execute(
    "SELECT COUNT(*) FROM recipes WHERE deleted_at IS NULL"
).fetchone()[0]
for section, count in rows:
    print(f"{section}={count}")
print(f"total={total}")
for title, tags in connection.execute(
    "SELECT title, tags_json FROM recipes WHERE deleted_at IS NULL "
    "AND meal_type = 'dessert' AND lower(title) LIKE '%pie%' ORDER BY title"
):
    print(f"pie_candidate={title!r} tags={tags}")

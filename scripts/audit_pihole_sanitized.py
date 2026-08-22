import json
import os
import sqlite3
import sys
from datetime import datetime, timezone


root, summary_path = sys.argv[1:3]
db_path = os.path.join(root, "gravity.db")
print(f"gravity_db_present={os.path.isfile(db_path)}")
if os.path.isfile(db_path):
    print(f"gravity_db_updated={datetime.fromtimestamp(os.path.getmtime(db_path), timezone.utc).isoformat()}")
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "gravity" in tables:
        print(f"blocked_domains={connection.execute('SELECT COUNT(*) FROM gravity').fetchone()[0]}")
    if "adlist" in tables:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(adlist)")}
        print(f"adlists_total={connection.execute('SELECT COUNT(*) FROM adlist').fetchone()[0]}")
        if "enabled" in columns:
            print(f"adlists_enabled={connection.execute('SELECT COUNT(*) FROM adlist WHERE enabled=1').fetchone()[0]}")
        selected = [column for column in ("id", "enabled", "comment") if column in columns]
        if selected:
            for row in connection.execute(f"SELECT {','.join(selected)} FROM adlist ORDER BY id"):
                values = dict(zip(selected, row))
                comment = str(values.get("comment") or "unlabeled").replace("\n", " ")[:120]
                print(f"adlist_{values.get('id', 'unknown')}_enabled={values.get('enabled', 'unknown')} comment={comment}")
    if "domainlist" in tables:
        for kind, count in connection.execute("SELECT type, COUNT(*) FROM domainlist GROUP BY type ORDER BY type"):
            print(f"domainlist_type_{kind}={count}")

print(f"summary_present={os.path.isfile(summary_path)}")
if os.path.isfile(summary_path):
    with open(summary_path, encoding="utf-8") as handle:
        data = json.load(handle)
    allowed = {
        "generated_at", "updated_at", "state", "status", "summary",
        "total_queries", "queries_total", "blocked_queries", "queries_blocked",
        "blocked_percent", "percentage_blocked", "age_minutes", "collector",
    }
    safe = {key: value for key, value in data.items() if key in allowed and isinstance(value, (str, int, float, bool, type(None)))}
    print("summary_safe=" + json.dumps(safe, sort_keys=True, separators=(",", ":")))

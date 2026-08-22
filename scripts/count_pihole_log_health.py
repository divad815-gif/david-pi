import sys


counts = {"error": 0, "warning": 0, "restart": 0, "database": 0}
for line in sys.stdin:
    lowered = line.lower()
    if "error" in lowered or "fatal" in lowered:
        counts["error"] += 1
    if "warn" in lowered:
        counts["warning"] += 1
    if "restart" in lowered or "received signal" in lowered:
        counts["restart"] += 1
    if "database" in lowered and ("error" in lowered or "locked" in lowered or "corrupt" in lowered):
        counts["database"] += 1
for key, value in counts.items():
    print(f"log_{key}_lines={value}")

"""Suite-wide process defaults that must be set before application imports."""

from __future__ import annotations

import os


# Application integration fixtures use synthetic identities that are deliberately
# absent from the production allowlist.  Keep that test-only choice explicit now
# that a missing production setting fails closed to enforcement.
os.environ.setdefault("DAVID_PI_ACCESS_MODE", "off")

os.environ.setdefault("DAVID_PI_PUBLIC_URL", "https://server.example-tail.ts.net")
os.environ.setdefault("THEMEALDB_API_KEY", "fixture-key")

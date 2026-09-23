"""Deterministic, read-only quality audit for the David-Pi recipe library.

This module intentionally does not import :mod:`modules.recipes`: importing that
module runs schema initialization.  The command below opens an existing database
with SQLite ``mode=ro`` and ``query_only`` and writes a JSON report to stdout.
It never changes categories or any other saved recipe field.
"""

import argparse
from collections import Counter, defaultdict
from difflib import SequenceMatcher
import hashlib
import json
from pathlib import Path
import re
import sqlite3


ANALYZER_VERSION = 1
REPORT_SCHEMA = "david-pi.recipe-quality/v1"
CANONICAL_CATEGORIES = ("breakfast", "main", "dessert")
REQUIRED_COLUMNS = {
    "id", "title", "description", "meal_type", "tags_json",
    "total_minutes", "servings", "ingredients_json", "instructions_json",
    "source_name", "source_url", "image_url", "content_hash", "deleted_at",
}
HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
WORD = re.compile(r"[a-z0-9]+")
TITLE_STOP_WORDS = {
    "and", "easy", "for", "from", "number", "quick", "recipe", "the",
    "with",
}

# Deliberately conservative. Ambiguous terms such as pie and tart are omitted.
# These hints create review findings only; they are never migration rules.
CATEGORY_HINTS = {
    "breakfast": {
        "breakfast", "pancake", "pancakes", "waffle", "waffles", "omelet",
        "omelette", "porridge", "granola", "muesli", "frittata",
    },
    "dessert": {
        "dessert", "cake", "cheesecake", "cookie", "cookies", "brownie",
        "brownies", "pudding", "tiramisu", "cupcake", "cupcakes", "donut",
        "donuts", "doughnut", "doughnuts", "gelato", "sorbet",
    },
    "main": {
        "main", "dinner", "lunch", "entree", "chicken", "beef", "pork",
        "lamb", "salmon", "fish", "shrimp", "prawn", "prawns", "soup",
        "stew", "curry", "pasta", "carbonara", "burger", "pizza", "dal",
        "prosciutto", "sausage", "ugali", "stamppot",
    },
}


class RecipeAuditError(RuntimeError):
    """Raised when a database cannot be safely audited."""


def _canonical_json(value):
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )


def _sha256(value):
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _tokens(value):
    return WORD.findall(str(value or "").casefold())


def _normalized_title(value):
    return " ".join(_tokens(value))


def _meaningful_title_tokens(value):
    return {
        token for token in _tokens(value)
        if len(token) >= 3 and not token.isdigit() and token not in TITLE_STOP_WORDS
    }


def _decode_list(value):
    try:
        decoded = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, list) else None


def _recipe_fingerprint(title, ingredients, instructions):
    """Match the canonical fingerprint currently used by modules.recipes."""
    encoded = json.dumps(
        [str(title or "").lower(), ingredients, instructions], sort_keys=True
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def connect_read_only(path):
    database = Path(path).resolve()
    connection = sqlite3.connect(
        f"file:{database}?mode=ro", uri=True, timeout=10
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _validate_schema(connection):
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'recipes'"
    ).fetchone()
    if not table:
        raise RecipeAuditError("recipes table is missing")
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(recipes)")
    }
    missing = sorted(REQUIRED_COLUMNS - columns)
    if missing:
        raise RecipeAuditError(
            "recipes table is missing required columns: " + ", ".join(missing)
        )
    return columns


def load_recipes(connection):
    """Load only the fields required by the audit, in deterministic order."""
    columns = _validate_schema(connection)
    fields = sorted(REQUIRED_COLUMNS)
    if "catalog_source_url" in columns:
        fields.remove("source_url")
        fields.append(
            "COALESCE(catalog_source_url, source_url) AS source_url"
        )
    selected = ", ".join(fields)
    return connection.execute(
        f"SELECT {selected} FROM recipes ORDER BY id"  # nosec: fixed allowlist
    ).fetchall()


def _display(row, include_titles):
    item = {"recipe_id": str(row["id"])}
    if include_titles:
        item["title"] = str(row["title"] or "")
    return item


def _limited(records, total, max_findings):
    return {
        "count": total,
        "shown": min(total, max_findings),
        "truncated": total > max_findings,
        "records": records[:max_findings],
    }


def _category_review(row, tags):
    title_tokens = set(_tokens(row["title"]))
    tag_tokens = set()
    for tag in tags or []:
        tag_tokens.update(_tokens(tag))
    scores = {}
    evidence = {}
    for category, hints in CATEGORY_HINTS.items():
        title_hits = sorted(title_tokens & hints)
        tag_hits = sorted(tag_tokens & hints)
        scores[category] = 2 * len(title_hits) + len(tag_hits)
        evidence[category] = {
            "title_keywords": title_hits,
            "tag_keywords": tag_hits,
        }
    best_score = max(scores.values())
    winners = [key for key, score in scores.items() if score == best_score]
    current = str(row["meal_type"] or "").strip().casefold()
    if best_score < 2 or len(winners) != 1 or winners[0] == current:
        return None
    predicted = winners[0]
    return {
        "current_category": current or None,
        "review_category": predicted,
        "confidence": "high" if best_score >= 4 else "medium",
        "evidence": evidence[predicted],
    }


def _metadata_issues(row, tags, ingredients, instructions):
    issues = []
    description = str(row["description"] or "").strip()
    if not description:
        issues.append("missing_description")
    elif len(description) < 20:
        issues.append("weak_description")
    if tags is None:
        issues.append("invalid_tags_json")
    elif not tags:
        issues.append("missing_tags")
    if row["total_minutes"] is None:
        issues.append("missing_total_minutes")
    elif not isinstance(row["total_minutes"], int) or row["total_minutes"] <= 0:
        issues.append("invalid_total_minutes")
    if not str(row["servings"] or "").strip():
        issues.append("missing_servings")
    if ingredients is None:
        issues.append("invalid_ingredients_json")
    elif not ingredients:
        issues.append("missing_ingredients")
    elif len(ingredients) < 2:
        issues.append("weak_ingredients")
    if instructions is None:
        issues.append("invalid_instructions_json")
    elif not instructions:
        issues.append("missing_instructions")
    if not str(row["source_name"] or "").strip():
        issues.append("missing_source_name")
    if not str(row["source_url"] or "").strip():
        issues.append("missing_source_url")
    if not str(row["image_url"] or "").strip():
        issues.append("missing_image")
    content_hash = str(row["content_hash"] or "")
    if not HEX_SHA256.fullmatch(content_hash):
        issues.append("invalid_content_hash")
    elif ingredients is not None and instructions is not None:
        if content_hash != _recipe_fingerprint(
            row["title"], ingredients, instructions
        ):
            issues.append("stale_content_hash")
    if len(_tokens(row["title"])) == 0:
        issues.append("missing_title_words")
    return sorted(issues)


def _duplicate_group(records, include_titles):
    return {
        "recipe_ids": [str(row["id"]) for row in records],
        **(
            {"titles": [str(row["title"] or "") for row in records]}
            if include_titles else {}
        ),
    }


def _duplicate_groups(grouped, include_titles):
    groups = []
    for key in sorted(grouped):
        records = grouped[key]
        if len(records) > 1:
            groups.append(_duplicate_group(records, include_titles))
    return groups


def _near_title_pairs(active, include_titles, max_findings):
    prepared = [
        (
            row,
            _normalized_title(row["title"]),
            _meaningful_title_tokens(row["title"]),
        )
        for row in active
    ]
    postings = defaultdict(list)
    for index, (_, _, title_tokens) in enumerate(prepared):
        for token in title_tokens:
            postings[token].append(index)
    candidates = set()
    for token in sorted(postings):
        indexes = postings[token]
        for left_position, left_index in enumerate(indexes):
            for right_index in indexes[left_position + 1:]:
                candidates.add((left_index, right_index))

    count = 0
    shown = []
    for left_index, right_index in sorted(candidates):
        left, left_title, left_tokens = prepared[left_index]
        right, right_title, right_tokens = prepared[right_index]
        if not right_title or left_title == right_title:
            continue
        sequence = SequenceMatcher(None, left_title, right_title).ratio()
        union = left_tokens | right_tokens
        jaccard = len(left_tokens & right_tokens) / len(union) if union else 0
        if sequence < 0.88 and not (
            len(left_tokens) >= 2 and len(right_tokens) >= 2 and jaccard >= 0.8
        ):
            continue
        count += 1
        if len(shown) >= max_findings:
            continue
        item = {
            "recipe_ids": [str(left["id"]), str(right["id"])],
            "title_similarity": round(sequence, 4),
            "title_token_jaccard": round(jaccard, 4),
            "same_stored_content_hash": bool(
                left["content_hash"]
                and left["content_hash"] == right["content_hash"]
            ),
        }
        if include_titles:
            item["titles"] = [
                str(left["title"] or ""), str(right["title"] or "")
            ]
        shown.append(item)
    return count, shown


def analyze_recipes(rows, include_titles=True, max_findings=200):
    """Return a deterministic report without mutating ``rows`` or a database."""
    if max_findings < 1:
        raise ValueError("max_findings must be at least 1")
    rows = sorted(rows, key=lambda row: str(row["id"]))
    active = [row for row in rows if row["deleted_at"] is None]

    category_counts = Counter()
    invalid_categories = []
    category_reviews = []
    metadata_records = []
    metadata_counts = Counter()
    stored_hashes = defaultdict(list)
    computed_hashes = defaultdict(list)
    normalized_titles = defaultdict(list)

    for row in active:
        observed_category = str(row["meal_type"] or "")
        category = observed_category.strip().casefold()
        if observed_category in CANONICAL_CATEGORIES:
            category_counts[observed_category] += 1
        else:
            invalid_categories.append({
                **_display(row, include_titles),
                "observed_category": observed_category or None,
                "normalized_candidate": (
                    category if category in CANONICAL_CATEGORIES else None
                ),
            })

        tags = _decode_list(row["tags_json"])
        ingredients = _decode_list(row["ingredients_json"])
        instructions = _decode_list(row["instructions_json"])
        review = _category_review(row, tags)
        if review:
            category_reviews.append({**_display(row, include_titles), **review})

        issues = _metadata_issues(row, tags, ingredients, instructions)
        if issues:
            metadata_counts.update(issues)
            metadata_records.append({**_display(row, include_titles), "issues": issues})

        stored_hash = str(row["content_hash"] or "")
        if stored_hash:
            stored_hashes[stored_hash].append(row)
        if ingredients is not None and instructions is not None:
            computed_hashes[
                _recipe_fingerprint(row["title"], ingredients, instructions)
            ].append(row)
        title_key = _normalized_title(row["title"])
        if title_key:
            normalized_titles[title_key].append(row)

    total_active = len(active)
    category_payload = {
        category: {
            "count": category_counts[category],
            "share": round(category_counts[category] / total_active, 6)
            if total_active else 0,
        }
        for category in CANONICAL_CATEGORIES
    }
    imbalance_flags = []
    if total_active:
        for category in CANONICAL_CATEGORIES:
            share = category_counts[category] / total_active
            if category_counts[category] == 0:
                imbalance_flags.append({"category": category, "reason": "empty"})
            elif share <= 0.05:
                imbalance_flags.append({
                    "category": category, "reason": "underrepresented"
                })
            if share >= 0.70:
                imbalance_flags.append({"category": category, "reason": "dominant"})

    stored_groups = _duplicate_groups(stored_hashes, include_titles)
    computed_groups = _duplicate_groups(computed_hashes, include_titles)
    title_groups = _duplicate_groups(normalized_titles, include_titles)
    near_pair_count, near_pairs = _near_title_pairs(
        active, include_titles, max_findings
    )
    input_projection = [
        {key: row[key] for key in sorted(REQUIRED_COLUMNS)} for row in rows
    ]

    report = {
        "schema": REPORT_SCHEMA,
        "analyzer_version": ANALYZER_VERSION,
        "scope": {
            "total": len(rows),
            "active": total_active,
            "deleted": len(rows) - total_active,
            "analysis_input_sha256": _sha256(input_projection),
            "titles_included": bool(include_titles),
            "max_findings_per_section": max_findings,
        },
        "taxonomy": {
            "categories": category_payload,
            "imbalance_flags": imbalance_flags,
            "invalid_or_uncategorized": _limited(
                invalid_categories, len(invalid_categories), max_findings
            ),
            "likely_misclassified": _limited(
                category_reviews, len(category_reviews), max_findings
            ),
        },
        "duplicates": {
            "stored_content_hash_groups": _limited(
                stored_groups, len(stored_groups), max_findings
            ),
            "computed_content_groups": _limited(
                computed_groups, len(computed_groups), max_findings
            ),
            "normalized_title_groups": _limited(
                title_groups, len(title_groups), max_findings
            ),
            "near_title_pairs": _limited(
                near_pairs, near_pair_count, max_findings
            ),
        },
        "metadata": {
            "issue_counts": dict(sorted(metadata_counts.items())),
            "recipes_with_issues": _limited(
                metadata_records, len(metadata_records), max_findings
            ),
        },
        "safety": {
            "mode": "read_only",
            "changes_applied": 0,
            "body_fields_emitted": False,
            "source_urls_emitted": False,
            "classification_is_review_only": True,
        },
    }
    report["report_sha256"] = _sha256(report)
    return report


def audit_database(path, include_titles=True, max_findings=200):
    with connect_read_only(path) as connection:
        return analyze_recipes(
            load_recipes(connection),
            include_titles=include_titles,
            max_findings=max_findings,
        )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="Path to an existing recipes.db")
    parser.add_argument(
        "--redact-titles", action="store_true",
        help="Emit recipe IDs without titles (body fields and URLs are never emitted)",
    )
    parser.add_argument("--max-findings", type=int, default=200)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = audit_database(
            args.db,
            include_titles=not args.redact_titles,
            max_findings=args.max_findings,
        )
    except (OSError, sqlite3.Error, RecipeAuditError, ValueError) as error:
        print(_canonical_json({"status": "error", "error": str(error)}))
        return 1
    if args.pretty:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(_canonical_json(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

import ast
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from modules.access_control import ADMIN_ROUTES, IDENTITY_ROLES, request_class
from modules.content_policy import (
    KNOWN_AUDIT_CLASSES,
    KNOWN_AUTHORIZATIONS,
    KNOWN_CSRF_CLASSES,
    KNOWN_EFFECTS,
    KNOWN_LEGACY_HANDLING,
    KNOWN_RESOURCES,
    compile_route_policy,
    validate_runtime_routes,
)


ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "config" / "route-policy.json"
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def load_policy():
    return json.loads(POLICY_PATH.read_text(encoding="utf-8"))


def source_mutations():
    """Return every state-changing Flask decorator declared in source."""
    found = set()
    sources = [ROOT / "app.py", *sorted((ROOT / "modules").glob("*.py"))]
    for source in sources:
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not (
                    isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    and decorator.args
                    and isinstance(decorator.args[0], ast.Constant)
                    and isinstance(decorator.args[0].value, str)
                ):
                    continue
                shortcut = decorator.func.attr.upper()
                if shortcut in UNSAFE_METHODS:
                    methods = {shortcut}
                elif shortcut == "ROUTE":
                    methods = set()
                    for keyword in decorator.keywords:
                        if keyword.arg == "methods" and isinstance(
                            keyword.value, (ast.List, ast.Tuple)
                        ):
                            methods.update(
                                str(item.value).upper()
                                for item in keyword.value.elts
                                if isinstance(item, ast.Constant)
                            )
                else:
                    continue
                for method in methods & UNSAFE_METHODS:
                    found.add((method, decorator.args[0].value))
    return found


def concrete_path(rule):
    value = re.sub(r"<int:[^>]+>", "1", rule)
    return re.sub(r"<(?:(?:path):)?[^>]+>", "example", value)


def runtime_mutations(safe_methods):
    """Load Flask in isolation and return every live unsafe URL-map method."""
    script = """
import json
import app
safe = set(json.loads(%r))
routes = sorted(
    (method, rule.rule)
    for rule in app.app.url_map.iter_rules()
    for method in set(rule.methods or ()) - safe
)
print('__DAVID_PI_ROUTES__' + json.dumps(routes))
""" % json.dumps(sorted(safe_methods))
    with tempfile.TemporaryDirectory() as directory:
        environment = os.environ.copy()
        environment.update(
            {
                "PHOTO_DATA": directory,
                "DAVID_PI_PLATFORM_DATA": str(Path(directory) / "platform"),
                "DAVID_PI_FILES_DATA": str(Path(directory) / "files"),
                "DAVID_PI_CHAT_DATA": str(Path(directory) / "chat"),
                "DAVID_PI_DISABLE_METRICS": "1",
                "DAVID_PI_DISABLE_DEVICE_HOUSEKEEPING": "1",
                "PIHOLE_SUMMARY": str(Path(directory) / "pihole-summary.json"),
                "DAVID_PI_CHAT_KEY_B64": base64.b64encode(
                    b"route-policy-test-key-32-bytes!!"
                ).decode("ascii"),
            }
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    marker = "__DAVID_PI_ROUTES__"
    line = next(
        value for value in reversed(result.stdout.splitlines()) if value.startswith(marker)
    )
    return {tuple(item) for item in json.loads(line[len(marker):])}


class RoutePolicyInventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.policy = load_policy()
        cls.compiled = compile_route_policy(cls.policy)

    def test_policy_is_versioned_and_declares_all_principal_classes(self):
        self.assertEqual(self.policy["schema_version"], 2)
        self.assertEqual(self.policy["enforcement"]["status"], "foundation_only")
        self.assertIs(self.policy["enforcement"]["content_admin_override"], False)
        self.assertFalse(self.compiled.content_admin_override)
        self.assertEqual(self.policy["default_access"], "portal")
        self.assertEqual(
            set(self.policy["safe_methods"]), {"GET", "HEAD", "OPTIONS"}
        )
        self.assertEqual(
            self.policy["principals"]["david"],
            {
                "kind": "human",
                "login": "david@example.test",
                "role": "admin",
            },
        )
        self.assertEqual(
            self.policy["principals"]["diana"],
            {
                "kind": "human",
                "login": "diana@example.test",
                "role": "household",
            },
        )
        self.assertEqual(dict(IDENTITY_ROLES), {})
        self.assertEqual(
            set(self.policy["principals"]),
            {"missing", "unknown", "david", "diana", "device"},
        )
        for access_class in self.policy["access_classes"].values():
            self.assertLessEqual(
                set(access_class["allowed_principals"]),
                set(self.policy["principals"]),
            )

    def test_every_source_mutation_has_one_explicit_policy_entry(self):
        declared = []
        required = {
            "id",
            "methods",
            "route",
            "access",
            "effect",
            "resource",
            "csrf",
            "audit",
            "legacy_handling",
        }
        ids = set()
        for entry in self.policy["mutations"]:
            self.assertLessEqual(required, set(entry), entry)
            self.assertNotIn(entry["id"], ids)
            ids.add(entry["id"])
            self.assertIn(entry["access"], self.policy["access_classes"])
            self.assertIn(entry["effect"], KNOWN_EFFECTS)
            self.assertIn(entry["resource"], KNOWN_RESOURCES)
            self.assertIn(entry["csrf"], KNOWN_CSRF_CLASSES)
            self.assertIn(entry["audit"], KNOWN_AUDIT_CLASSES)
            self.assertIn(entry["legacy_handling"], KNOWN_LEGACY_HANDLING)
            authorization_names = (
                set(entry["authorization_by_action"].values())
                if "authorization_by_action" in entry
                else {entry["authorization"]}
            )
            self.assertLessEqual(authorization_names, KNOWN_AUTHORIZATIONS)
            self.assertNotEqual(
                "authorization" in entry,
                "authorization_by_action" in entry,
                entry,
            )
            if entry["effect"] == "destructive":
                self.assertIn("retention", entry)
                self.assertFalse(
                    authorization_names & {"shared_collaboration", "visible_read_only"},
                    entry,
                )
            for method in entry["methods"]:
                self.assertIn(method, UNSAFE_METHODS)
                declared.append((method, entry["route"]))
        self.assertEqual(len(declared), len(set(declared)), "duplicate policy entry")
        self.assertEqual(set(declared), source_mutations())

    def test_runtime_url_map_has_no_dynamic_or_uninspectable_mutation_gaps(self):
        declared = {
            (method, entry["route"])
            for entry in self.policy["mutations"]
            for method in entry["methods"]
        }
        self.assertEqual(
            declared,
            runtime_mutations(set(self.policy["safe_methods"])),
        )
        validate_runtime_routes(self.compiled, declared)

    def test_declared_access_matches_central_fail_closed_classifier(self):
        admin = {
            (method, entry["route"])
            for entry in self.policy["admin_routes"]
            for method in entry["methods"]
        }
        self.assertEqual(admin, ADMIN_ROUTES)

        for entry in self.policy["mutations"]:
            for method in entry["methods"]:
                classified = request_class(concrete_path(entry["route"]), method)
                if entry["access"] == "device_bearer":
                    self.assertEqual(classified, "device_bearer", entry)
                else:
                    self.assertEqual(classified, "portal", entry)
                if entry["access"] == "admin":
                    self.assertIn((method, entry["route"]), ADMIN_ROUTES)

    def test_read_only_device_inventory_matches_classifier(self):
        for entry in self.policy["device_bearer_routes"]:
            for method in entry["methods"]:
                self.assertEqual(
                    request_class(concrete_path(entry["route"]), method),
                    "device_bearer",
                    entry,
                )

    def test_public_inventory_matches_classifier(self):
        for entry in self.policy["public_routes"]:
            for method in entry["methods"]:
                self.assertEqual(
                    request_class(concrete_path(entry["route"]), method),
                    "public",
                    entry,
                )

    def test_policy_role_matrix_is_least_privilege(self):
        matrix = {
            name: set(value["allowed_principals"])
            for name, value in self.policy["access_classes"].items()
        }
        self.assertEqual(matrix["portal"], {"david", "diana"})
        self.assertEqual(matrix["admin"], {"david"})
        self.assertEqual(matrix["device_bearer"], {"device"})
        self.assertNotIn("missing", matrix["portal"])
        self.assertNotIn("unknown", matrix["portal"])


if __name__ == "__main__":
    unittest.main()

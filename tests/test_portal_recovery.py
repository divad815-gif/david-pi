import importlib.util
import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "verify_portal_recovery.py"
spec = importlib.util.spec_from_file_location("verify_portal_recovery", SCRIPT)
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


def container(restarts=0, healthy=True, image="sha256:known"):
    return {
        "Image": image,
        "RestartCount": restarts,
        "Config": {
            "Labels": {"com.docker.compose.service": "photo-portal"},
            "Healthcheck": {"Test": ["CMD", "python", "healthcheck"]},
        },
        "HostConfig": {
            "RestartPolicy": {"Name": "unless-stopped"},
            "PortBindings": {
                "8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8090"}]
            },
        },
        "State": {
            "Running": healthy,
            "Health": {"Status": "healthy" if healthy else "starting"},
        },
    }


class PortalRecoveryTests(unittest.TestCase):
    def test_source_compose_enables_unless_stopped_for_portal(self):
        compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        portal = compose.split("  chat-notifier:", 1)[0]
        self.assertIn("restart: unless-stopped", portal)
        self.assertNotIn('restart: "no"', portal)
        self.assertIn('"127.0.0.1:8090:8000"', portal)
        self.assertIn("healthcheck:", portal)

    def test_inspection_requires_restart_health_and_loopback_binding(self):
        self.assertEqual(recovery.verify_configuration(container()), [])
        wrong = container()
        wrong["HostConfig"]["RestartPolicy"]["Name"] = "no"
        wrong["HostConfig"]["PortBindings"]["8000/tcp"][0]["HostIp"] = "0.0.0.0"
        errors = recovery.verify_configuration(wrong)
        self.assertIn("restart policy is not unless-stopped", errors)
        self.assertIn(
            "portal port is not restricted to 127.0.0.1:8090", errors
        )

    def test_failure_exercise_requires_explicit_window_acknowledgement(self):
        called = []
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(PermissionError):
                recovery.exercise_recovery(runner=lambda args: called.append(args))
        self.assertEqual(called, [])

    def test_failure_exercise_is_fixed_target_and_accepts_bounded_recovery(self):
        commands = []
        inspections = iter([container(restarts=3), container(restarts=4)])

        def runner(arguments):
            commands.append(arguments)
            return subprocess.CompletedProcess(arguments, 0, stdout="", stderr="")

        def inspector(_runner):
            return next(inspections)

        clock = iter([0, 1])
        with patch.dict(
            os.environ, {recovery.EXERCISE_ACK: "YES"}, clear=False
        ):
            result = recovery.exercise_recovery(
                runner=runner,
                inspector=inspector,
                monotonic=lambda: next(clock),
                sleeper=lambda seconds: None,
            )
        self.assertEqual(result["RestartCount"], 4)
        self.assertEqual(
            commands,
            [[
                "docker",
                "exec",
                "family-photo-portal",
                "python",
                "-c",
                "import os; os.kill(1, 9)",
            ]],
        )


if __name__ == "__main__":
    unittest.main()

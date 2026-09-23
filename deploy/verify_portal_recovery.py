#!/usr/bin/env python3
"""Inspect portal recovery controls; optionally exercise them in a window.

The default mode is read-only.  ``--exercise`` terminates PID 1 inside only the
fixed ``family-photo-portal`` container and waits at most 90 seconds for
Docker's restart policy and healthcheck to recover it.  It does not use
``docker stop`` or ``docker kill`` because Docker treats those as an operator
stop and suppresses restart policy.  The exercise is intentionally locked
behind an explicit environment acknowledgement and must never be run outside
an approved maintenance window.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time


CONTAINER = "family-photo-portal"
EXERCISE_ACK = "DAVID_PI_ALLOW_PORTAL_RECOVERY_EXERCISE"
RECOVERY_DEADLINE_SECONDS = 90


def command(arguments):
    return subprocess.run(
        arguments,
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )


def inspect_container(runner=command):
    result = runner(["docker", "inspect", CONTAINER])
    payload = json.loads(result.stdout)
    if len(payload) != 1:
        raise RuntimeError("expected one fixed portal container")
    return payload[0]


def verify_configuration(container):
    errors = []
    labels = container.get("Config", {}).get("Labels") or {}
    if labels.get("com.docker.compose.service") != "photo-portal":
        errors.append("container is not the expected Compose service")
    if (
        container.get("HostConfig", {})
        .get("RestartPolicy", {})
        .get("Name")
        != "unless-stopped"
    ):
        errors.append("restart policy is not unless-stopped")
    if not container.get("Config", {}).get("Healthcheck", {}).get("Test"):
        errors.append("container healthcheck is missing")
    bindings = (
        container.get("HostConfig", {})
        .get("PortBindings", {})
        .get("8000/tcp", [])
    )
    if bindings != [{"HostIp": "127.0.0.1", "HostPort": "8090"}]:
        errors.append("portal port is not restricted to 127.0.0.1:8090")
    return errors


def recovered(container, image_id, initial_restart_count):
    state = container.get("State", {})
    return (
        container.get("Image") == image_id
        and state.get("Running") is True
        and state.get("Health", {}).get("Status") == "healthy"
        and int(container.get("RestartCount", 0)) > initial_restart_count
    )


def exercise_recovery(runner=command, inspector=inspect_container, monotonic=time.monotonic, sleeper=time.sleep):
    if os.environ.get(EXERCISE_ACK) != "YES":
        raise PermissionError(
            f"set {EXERCISE_ACK}=YES only inside an approved maintenance window"
        )
    before = inspector(runner)
    errors = verify_configuration(before)
    if errors:
        raise RuntimeError("; ".join(errors))
    if before.get("State", {}).get("Health", {}).get("Status") != "healthy":
        raise RuntimeError("portal must be healthy before a recovery exercise")
    image_id = before.get("Image")
    restart_count = int(before.get("RestartCount", 0))
    try:
        runner(
            [
                "docker",
                "exec",
                CONTAINER,
                "python",
                "-c",
                "import os; os.kill(1, 9)",
            ]
        )
    except subprocess.CalledProcessError:
        # The exec channel can disappear with PID 1; recovery state below is
        # authoritative and remains bounded by the deadline.
        pass
    deadline = monotonic() + RECOVERY_DEADLINE_SECONDS
    while monotonic() < deadline:
        sleeper(2)
        current = inspector(runner)
        if recovered(current, image_id, restart_count):
            return current
    # A best-effort start is the bounded rollback for a failed exercise.  It
    # does not change configuration or image and targets only the fixed name.
    try:
        runner(["docker", "start", CONTAINER])
    except Exception:
        pass
    raise TimeoutError("portal did not recover within 90 seconds")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exercise", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        container = (
            exercise_recovery() if arguments.exercise else inspect_container()
        )
        errors = verify_configuration(container)
        if errors:
            raise RuntimeError("; ".join(errors))
    except Exception as error:
        print(f"portal_recovery=failed reason={error}", file=sys.stderr)
        return 1
    print(
        "portal_recovery=pass mode="
        + ("exercise recovery_seconds_lte=90" if arguments.exercise else "inspect")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

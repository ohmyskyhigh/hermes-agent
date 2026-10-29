"""Separate current process lifecycle from recovery of a previous process."""
from __future__ import annotations

import json
from pathlib import Path

_RECOVERY_TAGS = {"gateway.previous_unclean_exit", "gateway.state_db_integrity"}


def lifecycle_probe_result(path: Path, running_pid: int | None) -> tuple[bool, str]:
    """Check the latest lifecycle boundary against the independently live PID."""
    with path.open("rb") as stream:
        stream.seek(0, 2)
        offset = max(0, stream.tell() - 65536)
        stream.seek(offset)
        if offset:
            stream.readline()  # The bounded read may start partway through a record.
        lines = stream.read().decode("utf-8", errors="replace").splitlines()
    recovery = []
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            return False, f"Lifecycle line not JSON: {line[:120]}"
        if not isinstance(event, dict):
            return False, "Lifecycle record is not an object"
        if event.get("tag") in _RECOVERY_TAGS:
            recovery.append(event)
            continue
        tag = event.get("tag", "?")
        message = (
            f"Last lifecycle event: tag={tag} pid={event.get('pid', '?')} "
            f"ts={event.get('ts', '?')}"
        )
        healthy = tag == "gateway.start" and running_pid is not None and event.get("pid") == running_pid
        integrity_ok, note = _recovery_result(recovery)
        return healthy and integrity_ok, message + note
    return False, f"No lifecycle boundary found in exit-diag log tail: {path}"


def _recovery_result(events: list[dict]) -> tuple[bool, str]:
    if not events:
        return True, ""
    # Events are newest first. The deferred result supersedes the startup record.
    verdict = next((event["state_db_integrity"] for event in events if "state_db_integrity" in event), "unknown")
    previous = next((event.get("prior_pid", "?") for event in events if event.get("tag") == "gateway.previous_unclean_exit"), "?")
    display = "pending" if verdict == "deferred" else verdict
    note = f"; previous exit recovery: prior_pid={previous}, database integrity={display}"
    return verdict in ("ok", "absent", "deferred"), note

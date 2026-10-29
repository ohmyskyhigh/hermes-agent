"""Run the externally pinned Fork regression manifest on the exact candidate."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys


def summarize(output: str, returncode: int) -> dict:
    match = re.search(r"=== Summary: (\d+) files, (\d+) tests passed, (\d+) failed([^\n]*)", output)
    if match is None:
        raise ValueError("canonical runner did not emit a completed summary")
    files, passed, failed = map(int, match.group(1, 2, 3))
    skipped = re.search(r"(\d+) skipped", match[4])
    if returncode or not files or not passed or failed or "CRASHED" in match[4]:
        raise ValueError(f"regression did not pass: exit={returncode}, files={files}, passed={passed}, failed={failed}")
    return {"files": files, "passed": passed, "failed": failed,
            "skipped": int(skipped[1]) if skipped else 0}


def selected_files(policy: dict, platform: str, root: Path) -> list[str]:
    files = policy["common_test_files"] + policy["platform_test_files"][platform]
    if not files or len(files) != len(set(files)):
        raise ValueError("required test list is empty or duplicated")
    for name in files:
        path = root / name
        if not name.startswith("tests/") or not path.resolve().is_relative_to(root) or not path.is_file():
            raise ValueError(f"required test file unavailable: {name}")
    return files


def verify_checkout(root: Path, expected_commit: str) -> str:
    def git(*args):
        return subprocess.check_output(
            ["git", "-C", str(root), *args], text=True, encoding="utf-8", timeout=30).strip()

    if git("rev-parse", "HEAD") != expected_commit or git("status", "--porcelain=v1"):
        raise ValueError("candidate SHA changed or checkout is dirty")
    return git("rev-parse", "HEAD^{tree}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=("linux", "windows", "macos"), required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--policy-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--bash", required=True, help="Canonical runner shell supplied by the invoking host")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    tree = verify_checkout(root, args.expected_commit)
    policy_bytes = (root / "scripts/ci/fork_required_tests.json").read_bytes()
    if hashlib.sha256(policy_bytes).hexdigest() != args.policy_sha256:
        raise ValueError("test manifest differs from the external maintenance policy")
    policy = json.loads(policy_bytes)
    files = selected_files(policy, args.platform, root)
    args.output.mkdir(parents=True, exist_ok=True)
    file_list = args.output / "required-files.txt"
    file_list.write_text("\n".join(files) + "\n", encoding="utf-8")
    command = [args.bash, "scripts/run_tests.sh", "--files-from", str(file_list.resolve()),
               "-j", str(args.jobs), "--file-retries", "0", "--file-timeout", "600"]
    log_path = args.output / "tests.log"
    with log_path.open("w", encoding="utf-8") as log:
        result = subprocess.run(command, cwd=root, stdout=log, stderr=subprocess.STDOUT,
                                check=False, timeout=4800)
    output = log_path.read_text(encoding="utf-8-sig", errors="replace")
    print(output)
    summary = summarize(output, result.returncode)
    if summary["files"] != len(files):
        raise ValueError("canonical runner did not execute the complete required file selection")
    if verify_checkout(root, args.expected_commit) != tree:
        raise ValueError("candidate tree changed during validation")
    evidence = {"commit": args.expected_commit, "tree": tree,
                "policy_sha256": args.policy_sha256, "platform": args.platform,
                "required_files": files, "summary": summary,
                "skip_policy": policy["skip_policy"], "full_repository_suite": False}
    (args.output / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

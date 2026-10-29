"""Require native assertions, complete reports, and the unchanged pinned tree."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.ci.fork_validation import verify_checkout


def summarize_report(report: dict, required: dict[str, list[str]]) -> dict:
    if report.get("success") is not True or report.get("numFailedTests", 0):
        raise ValueError("native JavaScript report did not succeed")
    suites = report.get("testResults", [])
    assertions = [case for suite in suites for case in suite.get("assertionResults", [])]
    if not assertions or any(case["status"] not in ("passed", "pending", "skipped") for case in assertions):
        raise ValueError("native JavaScript report is empty or contains failed assertions")
    for name, titles in required.items():
        matching = [suite for suite in suites if suite["name"].replace("\\", "/").endswith("/" + name)]
        if len(matching) != 1 or matching[0].get("status") != "passed":
            raise ValueError(f"required native file did not pass: {name}")
        cases = matching[0].get("assertionResults", [])
        for title in titles:
            hits = [case for case in cases if title in case["fullName"]]
            if not hits or any(case["status"] != "passed" for case in hits):
                raise ValueError(f"required native assertion missing or skipped: {name}: {title}")
    passed = sum(case["status"] == "passed" for case in assertions)
    if not passed:
        raise ValueError("native JavaScript report executed no passing assertions")
    return {"files": len(suites), "passed": passed,
            "skipped": [case["fullName"] for case in assertions if case["status"] != "passed"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=("windows", "macos"), required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--policy-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--initialize", action="store_true")
    args = parser.parse_args()
    tree = verify_checkout(ROOT, args.expected_commit)
    raw = (ROOT / "scripts/ci/fork_required_tests.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != args.policy_sha256:
        raise ValueError("native test manifest differs from external maintenance policy")
    policy = json.loads(raw)["native_javascript"][args.platform]
    args.output.mkdir(parents=True, exist_ok=True)
    evidence_path = args.output / "evidence.json"
    if args.initialize:
        evidence = {"commit": args.expected_commit, "tree": tree, "platform": args.platform,
                    "policy_sha256": args.policy_sha256, "complete": False}
        for suite, required in policy.items():
            (args.output / f"{suite}-files.txt").write_text("\n".join(required) + "\n", encoding="utf-8")
    else:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8-sig"))
        if evidence["tree"] != tree or evidence["commit"] != args.expected_commit:
            raise ValueError("native candidate changed during validation")
        evidence["summaries"] = {
            suite: summarize_report(json.loads((args.output / f"{suite}.json").read_text(encoding="utf-8-sig")), required)
            for suite, required in policy.items()
        }
        evidence["required_assertions"] = policy
        evidence["complete"] = True
    evidence_path.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

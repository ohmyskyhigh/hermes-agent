"""A green process or empty suite must never satisfy the Fork regression gate."""
from pathlib import Path
import subprocess

import pytest

from scripts.ci.fork_validation import selected_files, summarize, verify_checkout


@pytest.mark.parametrize("output,code", [
    ("", 0),
    ("=== Summary: 1 files, 0 tests passed, 0 failed (100% complete)", 0),
    ("=== Summary: 1 files, 3 tests passed, 1 failed (100% complete)", 1),
    ("=== Summary: 1 files, 3 tests passed, 0 failed, 1 file CRASHED", 1),
    ("=== Summary: 1 files, 3 tests passed, 0 failed", 1),
])
def test_only_completed_success_with_executed_tests_passes(output, code):
    with pytest.raises(ValueError):
        summarize(output, code)


def test_skips_remain_explicit_evidence_for_cross_host_review():
    result = summarize("=== Summary: 2 files, 12 tests passed, 0 failed, 1 skipped (100% complete)", 0)
    assert result == {"files": 2, "passed": 12, "failed": 0, "skipped": 1}


@pytest.mark.parametrize("names", [[], ["tests/missing.py"], ["../outside.py"]])
def test_missing_or_empty_required_selection_refuses(names, tmp_path: Path):
    policy = {"common_test_files": names, "platform_test_files": {"windows": []}}
    with pytest.raises(ValueError):
        selected_files(policy, "windows", tmp_path)


@pytest.fixture
def candidate(tmp_path):
    def git(*args):
        return subprocess.check_output(
            ["git", "-C", str(tmp_path), *args], text=True, encoding="utf-8", timeout=30).strip()

    git("init", "-q")
    git("config", "user.name", "Fork validation test")
    git("config", "user.email", "test@example.invalid")
    (tmp_path / "source.txt").write_text("original", encoding="utf-8")
    git("add", "source.txt")
    git("commit", "-qm", "candidate")
    return tmp_path, git("rev-parse", "HEAD"), git("rev-parse", "HEAD^{tree}")


def test_clean_checkout_preserves_commit_and_tree(candidate):
    root, sha, tree = candidate
    assert verify_checkout(root, sha) == tree


@pytest.mark.parametrize("change", ["head", "tracked", "untracked"])
def test_candidate_or_content_drift_refuses(candidate, change):
    root, sha, _tree = candidate
    if change == "head":
        sha = "0" * 40
    else:
        path = root / ("source.txt" if change == "tracked" else "unexpected.txt")
        path.write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="SHA changed or checkout is dirty"):
        verify_checkout(root, sha)

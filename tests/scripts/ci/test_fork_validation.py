"""A green process or empty suite must never satisfy the Fork regression gate."""
from pathlib import Path

import pytest

from scripts.ci.fork_validation import selected_files, summarize


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

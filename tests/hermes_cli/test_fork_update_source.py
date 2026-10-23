"""Origin-only deployment: official advances cannot bypass the reviewed fork.

These cover the existing source-check and sync paths plus the documented Git preflight.
The general updater can stash/reset divergence; deployments must pass preflight first.
"""
import os
from pathlib import Path
import subprocess

import pytest

from hermes_cli.update_cmd_check import fetch_compare_branch
from hermes_cli.update_cmd_git import _assess_parked_branch_switch, _sync_with_upstream_if_needed
from hermes_constants import get_hermes_home


def git(root, *args, check=True):
    return subprocess.run(["git", *args], cwd=root, check=check, capture_output=True, text=True,
                          encoding="utf-8", timeout=30)


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    for key, value in {
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    }.items():
        monkeypatch.setenv(key, value)
    official = tmp_path / "official"
    official.mkdir()
    git(official, "init", "-q", "-b", "main")
    (official / "content").write_text("base", encoding="utf-8")
    git(official, "add", ".")
    git(official, "commit", "-qm", "base")
    fork = tmp_path / "fork.git"
    git(tmp_path, "clone", "-q", "--bare", str(official), str(fork))
    local = tmp_path / "runtime"
    git(tmp_path, "clone", "-q", str(fork), str(local))
    (get_hermes_home() / ".skip_upstream_prompt").touch()
    return official, fork, local


def test_official_advance_is_invisible_until_published_in_fork(deployment):
    official, fork, local = deployment
    old = git(local, "rev-parse", "HEAD").stdout.strip()
    (official / "content").write_text("official advance", encoding="utf-8")
    git(official, "commit", "-qam", "official advance")
    new = git(official, "rev-parse", "HEAD").stdout.strip()
    result, ref = fetch_compare_branch(["git"], local, "main", [])
    assert result.returncode == 0 and ref == "origin/main"
    assert git(local, "rev-parse", ref).stdout.strip() == old
    assert _sync_with_upstream_if_needed(["git"], local, assume_yes=True) is False
    assert git(local, "remote").stdout.strip() == "origin"
    assert git(fork, "rev-parse", "main").stdout.strip() == old
    git(official, "push", "-q", str(fork), "main")
    result, ref = fetch_compare_branch(["git"], local, "main", [])
    assert result.returncode == 0
    assert git(local, "merge-base", "--is-ancestor", "HEAD", ref).returncode == 0
    git(local, "merge", "--ff-only", ref)
    assert git(local, "rev-parse", "HEAD").stdout.strip() == new
    assert git(local, "remote").stdout.strip() == "origin"


def test_unreachable_fork_and_unsafe_checkout_preserve_old_head(deployment):
    official, fork, local = deployment
    old = git(local, "rev-parse", "HEAD").stdout.strip()
    git(local, "remote", "set-url", "origin", str(fork.parent / "missing.git"))
    result, ref = fetch_compare_branch(["git"], local, "main", [])
    assert result.returncode != 0 and ref == "origin/main"
    assert git(local, "rev-parse", "HEAD").stdout.strip() == old
    assert git(local, "remote").stdout.strip() == "origin"
    git(local, "remote", "set-url", "origin", str(fork))
    git(local, "checkout", "-qb", "local-work")
    (local / "content").write_text("local edits", encoding="utf-8")
    assert _assess_parked_branch_switch(["git"], local, "local-work", "main") == (False, "dirty")
    git(local, "commit", "-qam", "local work")
    local_head = git(local, "rev-parse", "HEAD").stdout.strip()
    (official / "content").write_text("reviewed update", encoding="utf-8")
    git(official, "commit", "-qam", "reviewed update")
    git(official, "push", "-q", str(fork), "main")
    result, ref = fetch_compare_branch(["git"], local, "main", [])
    assert result.returncode == 0
    assert git(local, "merge-base", "--is-ancestor", "HEAD", ref, check=False).returncode == 1
    assert git(local, "rev-parse", "HEAD").stdout.strip() == local_head

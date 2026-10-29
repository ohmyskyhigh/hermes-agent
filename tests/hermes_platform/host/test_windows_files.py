"""Native file metadata and exclusive-create contracts on Windows."""
import os

import pytest

from hermes_platform.host.windows_files import create_new_file, file_change_time_ns
from tests.waiting import wait_until

pytestmark = pytest.mark.platforms("windows")


def test_exclusive_create_preserves_an_existing_entry(tmp_path):
    path = tmp_path / "output"
    with os.fdopen(create_new_file(path), "wb") as stream:
        stream.write(b"first\n")
    with pytest.raises(OSError):
        create_new_file(path)
    assert path.read_bytes() == b"first\n"


def test_change_time_detects_a_same_size_rewrite_with_restored_mtime(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_bytes(b"model: a\n")
    initial = file_change_time_ns(path)
    assert initial is not None
    before = path.stat()
    probe = tmp_path / "clock"

    def clock_advanced():
        probe.touch()
        return (file_change_time_ns(probe) or 0) > initial

    wait_until(clock_advanced, what="Windows file change clock to advance")
    path.write_bytes(b"model: b\n")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert path.stat().st_mtime_ns == before.st_mtime_ns
    assert file_change_time_ns(path) > initial
    assert file_change_time_ns(tmp_path / "missing") is None

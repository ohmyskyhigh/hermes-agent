"""A failed ZIP member must preserve complete recovery points."""
import os
import zipfile

from tests.hermes_cli.test_backup import (
    _advance_backup_clock, _fail_zip_write_after,
)


class TestFailedZipMemberRecovery:
    def test_automatic_backup_omits_crc_valid_partial_member(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_bytes(b"model: test\n")
        (hermes_home / "flaky.bin").write_bytes(os.urandom(300_000))
        archive = tmp_path / "automatic.zip"
        _fail_zip_write_after(monkeypatch, "flaky.bin")

        from hermes_cli.backup import _write_full_zip_backup

        assert _write_full_zip_backup(archive, hermes_home) is None
        assert not archive.exists()
        salvage = tmp_path / "automatic.incomplete.zip"
        # A streaming reader scans local headers, so the dropped member's bytes must be gone too.
        assert b"flaky.bin" not in salvage.read_bytes()
        with zipfile.ZipFile(salvage) as zf:
            assert "flaky.bin" not in zf.namelist()
            assert zf.read("config.yaml") == b"model: test\n"
            assert zf.testzip() is None

    def test_incomplete_pre_update_backup_does_not_rotate_last_complete(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_bytes(b"model: test\n")

        from hermes_cli.backup import create_pre_update_backup

        good = create_pre_update_backup(hermes_home=hermes_home, keep=2)
        assert good is not None and good.exists()

        real_write = zipfile.ZipFile.write
        (hermes_home / "flaky.bin").write_bytes(os.urandom(300_000))
        _fail_zip_write_after(monkeypatch, "flaky.bin")
        for _ in range(2):
            _advance_backup_clock()
            assert create_pre_update_backup(hermes_home=hermes_home, keep=2) is None

        assert good.exists(), "an incomplete generation rotated out the last complete backup"
        backup_dir = hermes_home / "backups"
        salvages = list(backup_dir.glob("pre-update-*.incomplete.zip"))
        assert len(salvages) == 1, "repeated incomplete runs must not pile up"
        with zipfile.ZipFile(salvages[0]) as zf:
            assert "flaky.bin" not in zf.namelist()
            assert zf.read("config.yaml") == b"model: test\n"
            assert zf.testzip() is None

        # Salvage archives must not count toward retention on the next complete run.
        monkeypatch.setattr(zipfile.ZipFile, "write", real_write)
        (hermes_home / "flaky.bin").unlink()
        _advance_backup_clock()
        newest = create_pre_update_backup(hermes_home=hermes_home, keep=2)
        assert newest is not None and newest.exists()
        assert good.exists(), "a complete run pruned complete backups in favour of salvage"
        assert len(list(backup_dir.glob("pre-update-*.incomplete.zip"))) == 1


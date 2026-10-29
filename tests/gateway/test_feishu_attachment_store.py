"""Profile-local inbound attachment ledger contracts (no network or SDK)."""
import hashlib
import importlib
import importlib.util
import json
import sqlite3
from pathlib import Path
import re

import pytest


def test_duplicate_pending_does_not_destroy_completed_result(tmp_path):
    store = store_class()(tmp_path)
    final = {"state": "failed", "error_kind": "permission", "retryable": False}
    attachment_id = store.save(ref(state="failed"), final)
    assert store.save(ref()) == attachment_id
    record = store.get(attachment_id, "cli_test", "oc_test")
    assert record["ref"]["state"] == "failed"
    assert record["result"] == final


@pytest.mark.parametrize("field", ["app_id", "chat_id", "thread_id"])
def test_scope_is_exact_for_ids_and_lists(tmp_path, field):
    store = store_class()(tmp_path)
    attachment_id = store.save(ref())
    scope = {"app_id": "cli_test", "chat_id": "oc_test", "thread_id": ""}
    scope[field] = "another"
    assert store.get(attachment_id, **scope) is None
    assert store.list(**scope) == []
    assert store.save(ref(**{field: "another"})) != attachment_id
    assert store_class()(tmp_path / "another_profile").get(
        attachment_id, "cli_test", "oc_test") is None


def test_list_is_latest_first_and_bounded(tmp_path):
    store = store_class()(tmp_path)
    ids = [store.save(ref(message_id=f"om_{n}")) for n in range(25)]
    assert [r["attachment_id"] for r in store.list("cli_test", "oc_test", limit=1000)] == ids[-20:][::-1]
    assert len(store.list("cli_test", "oc_test")) == 10


@pytest.mark.parametrize("field", ["app_secret", "tenant_access_token", "path"])
def test_invalid_reference_is_rejected_before_persistence(tmp_path, field):
    store = store_class()(tmp_path)
    with pytest.raises(ValueError):
        store.save(ref(**{field: "DO_NOT_PERSIST"}))
    assert store.list("cli_test", "oc_test") == []


def test_result_rejects_credentials_before_persistence(tmp_path):
    store = store_class()(tmp_path)
    with pytest.raises(ValueError):
        store.save(ref(), {"state": "failed", "app_secret": "DO_NOT_PERSIST"})
    assert store.list("cli_test", "oc_test") == []


@pytest.mark.parametrize("result", [
    {"state": "failed", "error_kind": "DO_NOT_PERSIST"},
    {"state": "failed", "error_code": "DO_NOT_PERSIST"},
    {"state": "ready", "size_bytes": True},
    {"state": "ready", "size_bytes": -1},
    {"state": "ready", "sha256": "not-a-hash"},
    {"state": "ready", "mime_type": "DO_NOT_PERSIST"},
    {"state": "ready", "path": {"app_secret": "DO_NOT_PERSIST"}},
    {"state": "other"},
])
def test_malformed_result_metadata_never_reaches_ledger(tmp_path, result):
    store = store_class()(tmp_path)
    with pytest.raises(ValueError):
        store.save(ref(), result)
    assert store.list("cli_test", "oc_test") == []


def test_failed_result_cannot_expose_a_cached_path(tmp_path):
    store = store_class()(tmp_path)
    attachment_id = store.save(ref(state="failed"), {"state": "failed", "path": str(tmp_path / "outside"),
                                                     "error_kind": "network", "retryable": True})
    assert store.get(attachment_id, "cli_test", "oc_test")["result"]["path"] == ""


@pytest.mark.parametrize("field,value", [("file_key", "file_other"), ("app_id", "cli_other"),
                                        ("chat_id", "oc_other"), ("thread_id", "omt_other"),
                                        ("message_id", "om_other"), ("app_secret", "DO_NOT_EXPOSE")])
def test_stored_ref_is_revalidated_before_it_can_route_recovery(tmp_path, field, value):
    store = store_class()(tmp_path)
    attachment_id = store.save(ref())
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE attachments SET ref_json=?", (json.dumps(ref(**{field: value})),))
    assert store.get(attachment_id, "cli_test", "oc_test") is None
    assert store.list("cli_test", "oc_test") == []


def test_stored_result_is_revalidated_on_read(tmp_path):
    store = store_class()(tmp_path)
    attachment_id = store.save(ref())
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE attachments SET result_json=?", (json.dumps({"state": "failed", "error_kind": "DO_NOT_EXPOSE"}),))
    assert store.get(attachment_id, "cli_test", "oc_test") is None


@pytest.mark.parametrize("kind,code,retryable", [
    ("invalid_input", "", False), ("busy", "queue_timeout", False),
    ("network", "tls", False), ("network", "connection", True), ("network", "timeout", True),
    ("filesystem", "", False), ("internal", "", False), ("timeout", "deadline", False),
    ("authentication", "", False), ("redirect", "302", False), ("http", "403", False),
    ("http", "503", True), ("size_limit", "", False), ("protocol", "content_encoding", False),
    ("protocol", "length_mismatch", False), ("media_mismatch", "image_cover", False),
    ("api", "99991663", False),
])
def test_real_downloader_failures_survive_store_round_trip(tmp_path, kind, code, retryable):
    from plugins.platforms.feishu.inbound_resources import _DownloadFailure, _failed
    result = _failed(_DownloadFailure(kind, code, retryable), 1)
    store = store_class()(tmp_path)
    attachment_id = store.save(ref(state="failed", error_kind=kind), result)
    assert store.get(attachment_id, "cli_test", "oc_test")["result"] == result


@pytest.mark.parametrize("kind,code", [
    ("protocol", "range_mismatch"), ("protocol", "etag_mismatch"),
    ("authentication", "auth_response_too_large"),
    ("authentication", "auth_response_invalid"),
    ("authentication", "unsupported_app_type"),
])
def test_fixed_auth_and_range_errors_survive_get_and_list(tmp_path, kind, code):
    from plugins.platforms.feishu.inbound_resources import _DownloadFailure, _failed

    result = _failed(_DownloadFailure(kind, code, False), 1)
    assert set(result) == {"state", "path", "mime_type", "size_bytes", "sha256",
                           "attempts", "error_kind", "error_code", "retryable"}
    store = store_class()(tmp_path)
    reference = ref(state="failed", error_kind=kind)
    attachment_id = store.save(reference, result)
    reopened = store_class()(tmp_path)
    expected = {"attachment_id": attachment_id, "ref": reference, "result": result}
    assert reopened.get(attachment_id, "cli_test", "oc_test") == expected
    assert reopened.list("cli_test", "oc_test") == [expected]


@pytest.mark.parametrize("code", [
    "auth_response_invalid:DO_NOT_PERSIST", "range_mismatch_DO_NOT_PERSIST",
    "unsupported_app_type=DO_NOT_PERSIST", "arbitrary_error", "1234567890123",
])
def test_error_vocabulary_does_not_accept_arbitrary_labels(tmp_path, code):
    from plugins.platforms.feishu.inbound_resources import _DownloadFailure, _failed

    store = store_class()(tmp_path)
    with pytest.raises(ValueError, match="invalid error code"):
        store.save(ref(state="failed"), _failed(_DownloadFailure("authentication", code), 1))
    assert store.list("cli_test", "oc_test") == []


def ready(path):
    data = path.read_bytes()
    return {"state": "ready", "path": str(path), "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(), "mime_type": "video/mp4",
            "retryable": False}


@pytest.mark.parametrize("damage", ["missing", "size", "hash", "outside", "directory", "relative"])
def test_reads_hide_invalid_cache_paths(tmp_path, damage):
    store = store_class()(tmp_path)
    path = tmp_path / "cache" / "clip.mp4"
    path.write_bytes(b"sample")
    result = ready(path)
    attachment_id = store.save(ref(state="ready"), result)
    assert store.get(attachment_id, "cli_test", "oc_test")["result"]["path"] == str(path)
    def directory():
        path.unlink()
        path.mkdir()

    def outside():
        external = tmp_path / "outside.mp4"
        external.write_bytes(b"sample")
        store.save(ref(state="ready"), ready(external))

    def relative():
        store.save(ref(state="ready"), {**result, "path": "clip.mp4"})

    damage_cache = {
        "missing": path.unlink, "size": lambda: path.write_bytes(b"longer sample"),
        "hash": lambda: path.write_bytes(b"TAMPER"), "directory": directory,
        "outside": outside, "relative": relative,
    }
    damage_cache[damage]()
    record = store.get(attachment_id, "cli_test", "oc_test")
    assert record["result"]["state"] == "failed"
    assert record["result"]["path"] == ""
    assert record["result"]["error_kind"] == "cache_invalid"
    assert record["result"]["retryable"] is True
    assert store.list("cli_test", "oc_test")[0] == record


def make_link(link, target, directory=False):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError:
        if directory:
            _winapi = pytest.importorskip("_winapi")
            _winapi.CreateJunction(str(target), str(link))
        else:
            pytest.skip("Host does not allow file symlink creation")


@pytest.mark.parametrize("component", ["file", "parent"])
def test_symlink_cache_entries_are_not_trusted(tmp_path, component):
    store = store_class()(tmp_path)
    folder = tmp_path / "cache" / "original"
    folder.mkdir()
    path = folder / "clip.mp4"
    path.write_bytes(b"sample")
    if component == "file":
        link = tmp_path / "cache" / "link.mp4"
        make_link(link, path)
    else:
        directory = tmp_path / "cache" / "linked"
        make_link(directory, folder, directory=True)
        link = directory / "clip.mp4"
    attachment_id = store.save(ref(state="ready"), ready(link))
    assert store.get(attachment_id, "cli_test", "oc_test")["result"]["path"] == ""


def test_symlink_profile_cache_is_rejected(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    make_link(home / "cache", outside, directory=True)
    with pytest.raises(ValueError, match="cache"):
        store_class()(home)
    assert not (outside / "feishu-attachments.sqlite3").exists()


@pytest.mark.parametrize("component", ["cache", "feishu-attachments.sqlite3", "feishu-attachments.sqlite3-journal"])
def test_ledger_never_opens_reparse_targets_after_initialization(tmp_path, monkeypatch, component):
    from types import SimpleNamespace
    store = store_class()(tmp_path)
    attachment_id = store.save(ref())
    target = store.cache_dir if component == "cache" else store.cache_dir / component
    if not target.exists():
        target.touch()
    original = Path.lstat
    def lstat(path):
        info = original(path)
        return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400) if path == target else info
    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(ValueError, match="cache"):
        store.get(attachment_id, "cli_test", "oc_test")


def ref(**changes):
    return {"v": 1, "platform": "feishu", "app_id": "cli_test", "message_id": "om_first",
            "file_key": "file_test", "chat_id": "oc_test", "thread_id": "", "sender_id": "ou_test",
            "resource_type": "file", "media_hint": "video", "file_name": "clip.mp4",
            "state": "pending", **changes}


def store_class():
    name = "plugins.platforms.feishu.attachment_store"
    assert importlib.util.find_spec(name) is not None, "attachment ledger is not implemented"
    return importlib.import_module(name).AttachmentStore


def test_pending_ref_survives_reopen_with_stable_id(tmp_path):
    store = store_class()(tmp_path)
    attachment_id = store.save(ref())
    assert re.fullmatch(r"[0-9a-f]{64}", attachment_id)
    reopened = store_class()(tmp_path)
    assert reopened.save(ref()) == attachment_id
    record = reopened.get(attachment_id, "cli_test", "oc_test")
    assert record == {"attachment_id": attachment_id, "ref": ref(), "result": {"state": "pending"}}
    assert reopened.list("cli_test", "oc_test") == [record]
    assert (tmp_path / "cache" / "feishu-attachments.sqlite3").is_file()

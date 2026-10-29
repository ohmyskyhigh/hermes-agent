"""Incoming resource receipts survive failures, mixing and batching."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def make_adapter(monkeypatch, tmp_path):
    from gateway.config import PlatformConfig
    from plugins.platforms.feishu.adapter import FeishuAdapter
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = FeishuAdapter(PlatformConfig(extra={"app_id": "cli_test"}))
    adapter._client = object()
    adapter._fetch_message_resource = AsyncMock(return_value=SimpleNamespace(success=lambda: False, code=999, msg="failed"))
    adapter._dispatch_inbound_event = AsyncMock()
    adapter.get_chat_info = AsyncMock(return_value={"name": "test", "type": "group"})
    adapter._resolve_sender_profile = AsyncMock(return_value={"user_id": "ou_test", "user_name": "test", "user_id_alt": None})
    return adapter


@pytest.mark.parametrize("configured,expected", [
    (1, 1), (128 * 1024 * 1024, 128 * 1024 * 1024),
    (512 * 1024 * 1024, 512 * 1024 * 1024),
    (1024 * 1024 * 1024, 512 * 1024 * 1024),
    (0, 512 * 1024 * 1024), (-1, 512 * 1024 * 1024),
])
def test_resource_limit_matches_recovery_ceiling(monkeypatch, configured, expected):
    from plugins.platforms.feishu.inbound_attachments import resource_limit
    monkeypatch.setattr("gateway.platforms.base.get_inbound_media_max_bytes", lambda: configured)
    assert resource_limit() == expected


def post_message():
    return SimpleNamespace(message_type="post", message_id="om_test", chat_id="oc_test", thread_id="omt_test",
        parent_id=None, root_id=None, mentions=[], content=json.dumps({"content": [[
        {"tag": "media", "file_key": "file_test", "image_key": "img_cover"}]]}))


def captioned_files(*items):
    message = post_message()
    message.content = json.dumps({"content": [[
        {"tag": "text", "text": "Please read both attachments."},
        *({"tag": "file", "file_key": key, "file_name": name} for key, name in items),
    ]]})
    return message


def failed_download():
    return {"state": "failed", "path": "", "size_bytes": 0,
            "error_kind": "http", "retryable": True}


@pytest.mark.asyncio
async def test_display_colon_preserves_caption_and_all_pending_receipts(monkeypatch, tmp_path):
    from plugins.platforms.feishu.attachment_store import AttachmentStore
    adapter = make_adapter(monkeypatch, tmp_path)
    message = captioned_files(("file_one", "valid.pdf"), ("file_two", "meeting 10:30.pdf"))
    calls = []

    async def download(**kwargs):
        entries = AttachmentStore(tmp_path).list(app_id="cli_test", chat_id="oc_test", thread_id="omt_test")
        assert {entry["ref"]["file_key"] for entry in entries} == {"file_one", "file_two"}
        if not calls:
            assert all(entry["ref"]["state"] == "pending" for entry in entries)
        calls.append(kwargs)
        return failed_download()

    monkeypatch.setattr(adapter, "_download_attachment_result", download)
    refs = []
    text, _, paths, *_ = await adapter._extract_message_content(message, attachment_refs=refs, sender_id="ou_test")
    assert "Please read both attachments." in text and not paths
    assert [ref["file_name"] for ref in refs] == ["valid.pdf", "meeting 10:30.pdf"]
    assert [call["file_key"] for call in calls] == ["file_one", "file_two"]
    assert calls[1]["filename"] == "meeting 10:30.pdf"


@pytest.mark.asyncio
@pytest.mark.parametrize("filename", [
    "../escape.pdf", "C:\\private\\escape.pdf", "C:escape.pdf", "/escape.pdf",
    "bad\x00name.pdf", "bad\nname.pdf", "x" * 256, "\ud800",
])
async def test_invalid_filename_keeps_recoverable_identity_without_path(monkeypatch, tmp_path, filename):
    from plugins.platforms.feishu.attachment_store import AttachmentStore
    adapter = make_adapter(monkeypatch, tmp_path)
    message = captioned_files(("file_one", "valid.pdf"), ("file_two", filename))
    calls = []

    async def download(**kwargs):
        entries = AttachmentStore(tmp_path).list(app_id="cli_test", chat_id="oc_test", thread_id="omt_test")
        by_key = {entry["ref"]["file_key"]: entry for entry in entries}
        assert set(by_key) == {"file_one", "file_two"}
        assert by_key["file_two"]["ref"]["file_name"] == ""
        if not calls:
            assert all(entry["ref"]["state"] == "pending" for entry in entries)
        calls.append(kwargs)
        return failed_download()

    monkeypatch.setattr(adapter, "_download_attachment_result", download)
    refs = []
    text, _, paths, *_ = await adapter._extract_message_content(message, attachment_refs=refs, sender_id="ou_test")
    assert "Please read both attachments." in text and paths == []
    assert "Attachment 2: unsupported filename metadata omitted" in text
    assert [ref["file_key"] for ref in refs] == ["file_one", "file_two"]
    assert [ref["file_name"] for ref in refs] == ["valid.pdf", ""]
    assert [call["filename"] for call in calls] == ["valid.pdf", ""]
    store = AttachmentStore(tmp_path)
    recovered = store.get(store._id(refs[1]), app_id="cli_test", chat_id="oc_test", thread_id="omt_test")
    assert recovered is not None
    assert recovered["ref"] == refs[1] and recovered["result"]["retryable"]
    assert not list(tmp_path.rglob("*.pdf"))


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_key", ["file/../../secret", "file:secret", "file\x00bad", "f" * 513])
async def test_malformed_resource_identifier_skips_only_that_item(monkeypatch, tmp_path, bad_key):
    from plugins.platforms.feishu.attachment_store import AttachmentStore
    adapter = make_adapter(monkeypatch, tmp_path)
    message = captioned_files(("file_one", "valid.pdf"), (bad_key, "../bad.pdf"),
                              ("file_three", "meeting 10:30.pdf"))
    adapter._download_attachment_result = AsyncMock(return_value=failed_download())
    refs = []
    text, _, _, *_ = await adapter._extract_message_content(message, attachment_refs=refs, sender_id="ou_test")
    assert "Please read both attachments." in text
    assert "Attachment 2: rejected invalid resource metadata; not downloaded" in text
    assert [ref["file_key"] for ref in refs] == ["file_one", "file_three"]
    assert [call.kwargs["file_key"] for call in adapter._download_attachment_result.await_args_list] == ["file_one", "file_three"]
    assert len(AttachmentStore(tmp_path).list(app_id="cli_test", chat_id="oc_test", thread_id="omt_test")) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["file", "image", "post-file", "post-image", "post-files"])
@pytest.mark.parametrize("bad_key", [" file_two ", " ", "", 123, 0, False, None, [], {}])
async def test_parser_preserves_invalid_keys_for_ingress_rejection(monkeypatch, tmp_path, shape, bad_key):
    from plugins.platforms.feishu.adapter import normalize_feishu_message
    from plugins.platforms.feishu.attachment_store import AttachmentStore
    adapter = make_adapter(monkeypatch, tmp_path)
    message = post_message()
    image = shape.endswith("image")
    key_field = "image_key" if image else "file_key"
    invalid = {key_field: bad_key, "file_name": "bad.pdf", "text": "Keep this caption."}
    expected_keys = []
    if shape.startswith("post"):
        valid_key = "img_one" if image else "file_one"
        valid = {"tag": "img" if image else "file", key_field: valid_key, "file_name": "good.pdf"}
        body: dict = {"content": [[{"tag": "text", "text": "Keep this caption."}, valid]]}
        if shape == "post-files":
            body["files"] = [invalid]
        else:
            body["content"][0].append({**invalid, "tag": "img" if image else "file"})
        expected_keys.append(valid_key)
    else:
        message.message_type = shape
        body = invalid
    message.content = json.dumps(body)
    adapter._download_attachment_result = AsyncMock(return_value=failed_download())
    refs = []
    text, _, paths, *_ = await adapter._extract_message_content(message, attachment_refs=refs, sender_id="ou_test")
    index = 2 if expected_keys else 1
    assert f"Attachment {index}: rejected invalid resource metadata; not downloaded" in text
    assert paths == []
    assert [ref["file_key"] for ref in refs] == expected_keys
    assert [call.kwargs["file_key"] for call in adapter._download_attachment_result.await_args_list] == expected_keys
    entries = AttachmentStore(tmp_path).list(app_id="cli_test", chat_id="oc_test", thread_id="omt_test")
    assert [entry["ref"]["file_key"] for entry in entries] == expected_keys
    if expected_keys or image:
        assert "Keep this caption." in text
    if expected_keys and not image:
        assert refs[0]["file_name"] == "good.pdf"
    normalized = normalize_feishu_message(message_type=message.message_type, raw_content=message.content)
    parsed_key = normalized.image_keys[-1] if image else normalized.media_refs[-1].file_key
    assert parsed_key == bad_key and type(parsed_key) is type(bad_key)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["file", "post"])
@pytest.mark.parametrize("filename", ["\nreport.pdf", "report.pdf\t", 123, 0, False, None, [], {}, " report.pdf "])
async def test_parser_defers_filename_validation_to_ingress(monkeypatch, tmp_path, kind, filename):
    from plugins.platforms.feishu.adapter import normalize_feishu_message
    from plugins.platforms.feishu.attachment_store import AttachmentStore
    adapter = make_adapter(monkeypatch, tmp_path)
    message = captioned_files(("file_one", "good.pdf"), ("file_two", filename))
    if kind == "file":
        message.message_type = kind
        message.content = json.dumps({"file_key": "file_two", "file_name": filename})
    normalized = normalize_feishu_message(message_type=message.message_type, raw_content=message.content)
    parsed_name = normalized.media_refs[-1].file_name
    assert parsed_name == filename and type(parsed_name) is type(filename)
    if not isinstance(filename, str):
        rendered = normalized.text_content if kind == "post" else normalized.metadata["placeholder_text"]
        assert rendered.endswith("[Attachment]")
    elif filename == "\nreport.pdf":
        rendered = normalized.text_content if kind == "post" else normalized.metadata["placeholder_text"]
        assert rendered.endswith("[Attachment: report.pdf]")
    valid_name = filename == " report.pdf "
    expected_name = filename if valid_name else ""
    expected_keys = ["file_one", "file_two"] if kind == "post" else ["file_two"]
    calls = []

    async def download(**kwargs):
        entries = AttachmentStore(tmp_path).list(app_id="cli_test", chat_id="oc_test", thread_id="omt_test")
        by_key = {entry["ref"]["file_key"]: entry["ref"] for entry in entries}
        assert set(by_key) == set(expected_keys)
        assert by_key["file_two"]["file_name"] == expected_name
        if not calls:
            assert all(ref["state"] == "pending" for ref in by_key.values())
        calls.append(kwargs)
        return failed_download()

    monkeypatch.setattr(adapter, "_download_attachment_result", download)
    refs = []
    text, _, paths, *_ = await adapter._extract_message_content(message, attachment_refs=refs, sender_id="ou_test")
    assert paths == []
    assert [ref["file_key"] for ref in refs] == expected_keys
    assert [call["file_key"] for call in calls] == expected_keys
    assert refs[-1]["file_name"] == calls[-1]["filename"] == expected_name
    index = 2 if kind == "post" else 1
    notice = f"Attachment {index}: unsupported filename metadata omitted; resource identity retained."
    assert (notice in text) is not valid_name
    if kind == "post":
        assert "Please read both attachments." in text and refs[0]["file_name"] == "good.pdf"
    store = AttachmentStore(tmp_path)
    stored = store.get(store._id(refs[-1]), app_id="cli_test", chat_id="oc_test", thread_id="omt_test")
    assert stored is not None and stored["ref"] == refs[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"file_key": " file_two"}, {"file_key": "file_two "}, {"file_key": 123},
    {"file_key": ""}, {"media_hint": "text"},
])
async def test_receipt_boundary_rejects_instead_of_repairing_metadata(monkeypatch, tmp_path, changes):
    from plugins.platforms.feishu.inbound_attachments import receive_attachments
    adapter = make_adapter(monkeypatch, tmp_path)
    adapter._download_attachment_result = AsyncMock(return_value=failed_download())
    valid = SimpleNamespace(file_key="file_one", file_name=" report.pdf ", media_hint="document")
    invalid = SimpleNamespace(file_key="file_two", file_name="../bad.pdf", media_hint="document")
    for field, value in changes.items():
        setattr(invalid, field, value)
    normalized = SimpleNamespace(image_keys=[], media_refs=[valid, invalid])
    refs = []
    _, _, note = await receive_attachments(adapter, post_message(), normalized, "ou_test", refs)
    assert len(refs) == 1 and refs[0]["file_key"] == "file_one"
    assert refs[0]["file_name"] == " report.pdf "
    assert "Attachment 2: rejected invalid resource metadata; not downloaded" in note
    assert "unsupported filename metadata omitted" not in note
    assert adapter._download_attachment_result.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("thread", [0, False, " omt_test", "omt_test "])
async def test_ingress_never_repairs_thread_identity(monkeypatch, tmp_path, thread):
    adapter = make_adapter(monkeypatch, tmp_path)
    message = captioned_files(("file_one", "valid.pdf"))
    message.thread_id = thread
    adapter._download_attachment_result = AsyncMock(return_value=failed_download())
    refs = []
    text, _, _, *_ = await adapter._extract_message_content(message, attachment_refs=refs, sender_id="ou_test")
    assert refs == []
    assert "Please read both attachments." in text
    assert "Attachment 1: rejected invalid resource metadata; not downloaded" in text
    adapter._download_attachment_result.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("filename,total,notice", [
    pytest.param("报" * 255, 64, "64-KiB", id="status-budget"),
    pytest.param("😀" * 255, 64, "64-KiB", id="pending-budget"),
    pytest.param("valid.pdf", 65, "64-attachment", id="count-budget"),
])
async def test_receipt_limits_keep_deterministic_prefix_before_network(monkeypatch, tmp_path, filename, total, notice):
    from gateway.attachment_refs import validate_attachment_refs
    from plugins.platforms.feishu.attachment_store import AttachmentStore
    adapter = make_adapter(monkeypatch, tmp_path)
    items = [(f"file_{index:02}", filename) for index in range(total)]
    message = captioned_files(*items)
    refs, calls = [], []

    async def download(**kwargs):
        if not calls:
            assert validate_attachment_refs(refs) == refs
            # Worst-case status metadata must fit too, not just the initial pending set.
            validate_attachment_refs([dict(ref, state="failed", error_kind="x" * 64) for ref in refs])
            store = AttachmentStore(tmp_path)
            for ref in refs:
                stored = store.get(store._id(ref), app_id="cli_test", chat_id="oc_test", thread_id="omt_test")
                assert stored is not None and stored["ref"] == ref and ref["state"] == "pending"
        calls.append(kwargs)
        return failed_download()

    monkeypatch.setattr(adapter, "_download_attachment_result", download)
    text, _, _, *_ = await adapter._extract_message_content(message, attachment_refs=refs, sender_id="ou_test")
    assert "Please read both attachments." in text and notice in text
    assert "not downloaded" in text
    assert 0 < len(refs) < total
    assert len(refs) <= 64 and len(calls) == len(refs)
    assert [ref["file_key"] for ref in refs] == [key for key, _ in items[:len(refs)]]
    assert all(ref["file_name"] == filename for ref in refs)  # Do not trim valid metadata to fit.
    assert validate_attachment_refs(refs) == refs


@pytest.mark.asyncio
async def test_failed_video_keeps_scoped_ref_and_recovery_notice(monkeypatch, tmp_path):
    adapter = make_adapter(monkeypatch, tmp_path)
    failed = {"state": "failed", "path": "", "mime_type": "", "size_bytes": 0,
              "sha256": "", "attempts": 3, "error_kind": "http", "error_code": "503", "retryable": True}
    monkeypatch.setattr(adapter, "_download_attachment_result", AsyncMock(return_value=failed), raising=False)
    message = post_message()
    await adapter._process_inbound_message(data=None, message=message,
        sender_id=SimpleNamespace(open_id="ou_test", user_id=None, union_id=None),
        chat_type="group", message_id=message.message_id)
    event = adapter._dispatch_inbound_event.await_args.args[0]
    assert event.media_urls == []
    refs = getattr(event, "attachment_refs", None)
    assert refs and len(refs) == 1, "A failed resource must not become a bare [Attachment]"
    assert refs[0]["file_key"] == "file_test"
    assert refs[0]["state"] == "failed"
    assert refs[0]["media_hint"] == "video"
    assert refs[0]["chat_id"] == "oc_test" and refs[0]["thread_id"] == "omt_test"
    assert "feishu_attachment" in event.text and "http" in event.text


def make_event(sender="ou_test", suffix="1"):
    from gateway.config import Platform
    from gateway.session import SessionSource
    from gateway.platforms.event import MessageEvent
    event = MessageEvent(text="attachment", message_id="om_" + suffix,
        source=SessionSource(platform=Platform.FEISHU, chat_id="oc_test", user_id=sender, thread_id="omt_test"))
    event.attachment_refs = [{"message_id": "om_" + suffix}]
    return event


def test_different_senders_never_merge_attachment_batches():
    from plugins.platforms.feishu.adapter import FeishuAdapter
    assert not FeishuAdapter._text_batch_is_compatible(make_event(), make_event("ou_other"))


def test_busy_text_merge_preserves_both_receipts():
    from gateway.platforms.base import merge_pending_message_event
    first, second = make_event(suffix="1"), make_event(suffix="2")
    pending = {"key": first}
    merge_pending_message_event(pending, "key", second, merge_text=True)
    assert [ref["message_id"] for ref in pending["key"].attachment_refs] == ["om_1", "om_2"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,body,mime,hint", [
    ("file", {"file_key": "file_test", "file_name": "report.pdf"}, "application/pdf", "document"),
    ("media", {"file_key": "file_test", "image_key": "img_cover"}, "video/mp4", "video"),
    ("post", {"content": [[{"tag": "media", "file_key": "file_test", "image_key": "img_cover"}]]}, "video/mp4", "video"),
])
@pytest.mark.parametrize("thread", ["", "omt_test"])
async def test_downloaded_files_reach_event_after_pending_receipt(monkeypatch, tmp_path, kind, body, mime, hint, thread):
    import hashlib
    from plugins.platforms.feishu.attachment_store import AttachmentStore
    adapter = make_adapter(monkeypatch, tmp_path)
    message = post_message()
    message.message_type, message.content, message.thread_id = kind, json.dumps(body), thread
    path = tmp_path / "cache" / "documents" / "test.bin"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"real bytes")
    calls = []
    async def download(**kw):
        entries = AttachmentStore(tmp_path).list(app_id="cli_test", chat_id="oc_test", thread_id=thread)
        assert len(entries) == 1 and entries[0]["ref"]["state"] == "pending"
        calls.append(kw)
        return {"state": "ready", "path": str(path), "mime_type": mime, "size_bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "attempts": 1,
                "error_kind": "", "error_code": "", "retryable": False}
    monkeypatch.setattr(adapter, "_download_attachment_result", download)
    await adapter._process_inbound_message(data=None, message=message,
        sender_id=SimpleNamespace(open_id="ou_test", user_id=None, union_id=None),
        chat_type="group" if thread else "p2p", message_id=message.message_id)
    event = adapter._dispatch_inbound_event.await_args.args[0]
    assert event.media_urls == [str(path)] and event.media_types == [mime]
    assert event.media_text_inlined == [False]
    assert event.attachment_refs[0]["state"] == "ready"
    assert calls[0]["resource_type"] == "file" and calls[0]["media_hint"] == hint
    assert len(calls) == 1  # Cover is never the video body.


@pytest.mark.asyncio
async def test_mixed_attachments_enforce_one_total_budget(monkeypatch, tmp_path):
    from plugins.platforms.feishu import inbound_attachments
    adapter = make_adapter(monkeypatch, tmp_path)
    monkeypatch.setattr(inbound_attachments, "resource_limit", lambda: 5)
    message = post_message()
    message.content = json.dumps({"content": [[{"tag": "file", "file_key": "file_one"},
                                               {"tag": "file", "file_key": "file_two"}]]})
    import hashlib
    path = tmp_path / "cache" / "file.bin"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(b"12345")
    result = {"state": "ready", "path": str(path), "mime_type": "application/octet-stream", "size_bytes": 5,
              "sha256": hashlib.sha256(b"12345").hexdigest(), "attempts": 1,
              "error_kind": "", "error_code": "", "retryable": False}
    adapter._download_attachment_result = AsyncMock(return_value=result)
    refs = []
    out = await adapter._extract_message_content(message, attachment_refs=refs, sender_id="ou_test")
    assert len(out[2]) == 1 and len(refs) == 2
    assert refs[1]["state"] == "failed" and refs[1]["error_kind"] == "too_large"
    assert adapter._download_attachment_result.await_count == 1

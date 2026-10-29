"""Regression coverage for Feishu media delivery in topic threads.

Feishu rejects media sent with CreateMessage(receive_id_type="thread_id") using
99992402. Topic media must use ReplyMessage(reply_in_thread=True) against a
real message anchor; if no anchor can be resolved, delivery must fail closed
rather than creating a new topic in the parent chat.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from types import SimpleNamespace
from typing import Any

import pytest

from gateway.config import Platform, PlatformConfig

from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource
from plugins.platforms.feishu.adapter import FeishuAdapter


class _FileAPI:
    def create(self, request):  # pragma: no cover - upload body is not under test
        return SimpleNamespace(success=lambda: True, data=SimpleNamespace(file_key="file_123"))


class _ImageAPI:
    def create(self, request):
        return SimpleNamespace(success=lambda: True, data=SimpleNamespace(image_key="img_123"))


class _MessageAPI:
    def __init__(self, tail: str = "om_tail"):
        self.tail = tail
        self.calls: list[tuple[str, Any]] = []
        self.list_calls = 0

    def list(self, request):
        self.list_calls += 1
        items = [SimpleNamespace(message_id=self.tail)] if self.tail else []
        return SimpleNamespace(success=lambda: True, data=SimpleNamespace(items=items))

    def reply(self, request):
        self.calls.append(("reply", request))
        return SimpleNamespace(success=lambda: True, data=SimpleNamespace(message_id="om_sent"))

    def create(self, request):
        self.calls.append(("create", request))
        return SimpleNamespace(success=lambda: True, data=SimpleNamespace(message_id="om_created"))


def _adapter(tail: str = "om_tail") -> tuple[FeishuAdapter, _MessageAPI]:
    adapter = FeishuAdapter(PlatformConfig())
    messages = _MessageAPI(tail=tail)
    adapter._client = SimpleNamespace(
        im=SimpleNamespace(v1=SimpleNamespace(file=_FileAPI(), image=_ImageAPI(), message=messages))
    )
    return adapter, messages


def _send_into_topic(adapter: FeishuAdapter):
    with tempfile.NamedTemporaryFile("wb", suffix=".pdf", delete=False) as tmp:
        tmp.write(b"%PDF-1.4 test")
        path = tmp.name
    try:
        return asyncio.run(
            adapter.send_document(
                chat_id="oc_chat",
                file_path=path,
                metadata={"thread_id": "omt_thread"},
            )
        )
    finally:
        os.unlink(path)


def test_topic_media_replies_to_thread_tail():
    adapter, messages = _adapter(tail="om_tail")
    result = _send_into_topic(adapter)

    assert result.success, result.error
    kinds = [kind for kind, _ in messages.calls]
    assert kinds == ["reply"], f"expected one reply to the thread tail, got {kinds}"
    body = messages.calls[0][1].request_body
    assert body.reply_in_thread is True


def test_topic_media_without_resolvable_tail_fails_closed():
    adapter, messages = _adapter(tail="")
    result = _send_into_topic(adapter)

    assert not result.success, "A missing topic anchor must not create a parent-chat message"
    assert "reply anchor" in result.error.lower()
    assert messages.calls == [], "Topic media must never spill into a new top-level topic"


@pytest.mark.parametrize("msg_type", ["image", "text", "post", "file"])
def test_raw_topic_send_without_anchor_fails_closed(msg_type):
    adapter, messages = _adapter(tail="")
    with pytest.raises(ValueError, match="reply anchor"):
        asyncio.run(adapter._send_raw_message(
            chat_id="oc_chat", msg_type=msg_type, payload="{}",
            reply_to=None, metadata={"thread_id": "omt_thread"},
        ))
    assert messages.calls == []


def test_non_topic_send_can_still_create_chat_message():
    adapter, messages = _adapter(tail="")
    asyncio.run(adapter._send_raw_message(
        chat_id="oc_chat", msg_type="text", payload='{"text":"ordinary group"}',
        reply_to=None, metadata=None,
    ))
    assert [kind for kind, _ in messages.calls] == ["create"]
    assert messages.calls[0][1].receive_id_type == "chat_id"


@pytest.mark.parametrize("caption", [None, "captioned image"])
def test_topic_image_without_anchor_fails_closed(tmp_path, caption):
    adapter, messages = _adapter(tail="")
    image = tmp_path / "fixture.png"
    image.write_bytes(b"fixture")
    result = asyncio.run(adapter.send_image_file(
        chat_id="oc_chat", image_path=str(image), caption=caption,
        metadata={"thread_id": "omt_thread"},
    ))
    assert not result.success
    assert "reply anchor" in result.error.lower()
    assert messages.calls == []


def test_withdrawn_topic_anchor_never_creates_parent_message(tmp_path):
    adapter, messages = _adapter(tail="")
    image = tmp_path / "fixture.png"
    image.write_bytes(b"fixture")

    def withdrawn(request):
        messages.calls.append(("reply", request))
        return SimpleNamespace(success=lambda: False, code=230011, msg="withdrawn")

    messages.reply = withdrawn
    result = asyncio.run(adapter.send_image_file(
        chat_id="oc_chat", image_path=str(image),
        metadata={"thread_id": "omt_thread", "reply_to_message_id": "om_root"},
    ))
    assert not result.success
    assert [kind for kind, _ in messages.calls] == ["reply"]


def test_late_campaign_reply_override_reaches_media_metadata(tmp_path):
    """Campaign wakes set their durable root anchor inside the handler, after
    processing metadata was first captured. Attachments must use the refreshed
    final metadata instead of querying the Feishu thread-list API."""
    adapter, messages = _adapter(tail="")
    curve = tmp_path / "curve.png"
    curve.write_bytes(b"fixture")
    captured: list[dict[str, Any]] = []
    real_send_image = adapter.send_image_file

    async def handler(event):
        event.reply_anchor_override = "om_root"
        return f"MEDIA:{curve}"

    async def send_image_file(**kwargs):
        captured.append(kwargs)
        result = await real_send_image(**kwargs)
        assert result.success, result.error
        return result

    adapter._message_handler = handler
    adapter.send_image_file = send_image_file
    event = MessageEvent(
        text="campaign wake",
        internal=True,
        source=SessionSource(
            platform=Platform.FEISHU,
            chat_id="oc_chat",
            thread_id="omt_thread",
            chat_type="group",
        ),
    )

    async def run():
        adapter._active_sessions["campaign"] = asyncio.Event()
        await adapter._process_message_background(event, "campaign")

    asyncio.run(run())

    assert len(captured) == 1
    assert captured[0]["metadata"]["thread_id"] == "omt_thread"
    assert captured[0]["metadata"]["reply_to_message_id"] == "om_root"
    assert messages.list_calls == 0
    assert [kind for kind, _ in messages.calls] == ["reply"]
    request = messages.calls[0][1]
    assert request.message_id == "om_root"
    assert request.request_body.reply_in_thread is True
    assert request.request_body.msg_type == "image"

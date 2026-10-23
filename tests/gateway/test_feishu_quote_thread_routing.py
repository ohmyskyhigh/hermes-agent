"""Quoted-message ancestry must not opt replies into native Feishu topics."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.feishu.adapter import FeishuAdapter


@pytest.fixture
def adapter():
    instance = FeishuAdapter(PlatformConfig())
    instance._fetch_message_text = AsyncMock(return_value="quoted task")
    instance._resolve_sender_profile = AsyncMock(return_value={
        "user_id": "ou_peer", "user_name": "Peer", "user_id_alt": None,
    })
    instance._dispatch_inbound_event = AsyncMock()
    instance._client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(
        message=SimpleNamespace(reply=lambda request: request),
    )))
    return instance


async def normalize_and_reply(adapter, *, relation, chat_kind="group"):
    adapter.get_chat_info = AsyncMock(return_value={"name": "Chat", "type": chat_kind})
    message = SimpleNamespace(
        chat_id="oc_chat", message_type="text", content='{"text":"done"}',
        mentions=[], **relation,
    )
    await adapter._process_inbound_message(
        data=None, message=message, sender_id=SimpleNamespace(open_id="ou_peer"),
        chat_type="group", message_id="om_completion", is_bot=True,
    )
    event = adapter._dispatch_inbound_event.call_args.args[0]
    request = await adapter._send_raw_message(
        chat_id=event.source.chat_id, msg_type="text", payload='{"text":"ack"}',
        reply_to=event.message_id, metadata={"thread_id": event.source.thread_id},
    )
    return event, request


@pytest.mark.asyncio
@pytest.mark.parametrize("thread_fields", [{}, {"thread_id": None}, {"thread_id": ""}])
@pytest.mark.parametrize("parent_id", [None, "om_parent"])
async def test_ordinary_quoted_group_reply_stays_inline(adapter, thread_fields, parent_id):
    relation = {"root_id": "om_root", "parent_id": parent_id, **thread_fields}
    event, request = await normalize_and_reply(adapter, relation=relation)

    assert request.request_body.reply_in_thread is False
    assert event.source.thread_id is None
    assert event.source.chat_type == "group"
    assert event.reply_to_message_id == (parent_id or relation["root_id"])
    assert event.reply_to_text == "quoted task"
    assert request.message_id == event.message_id


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_kind", ["group", "forum"])
async def test_real_topic_reply_preserves_native_thread(adapter, chat_kind):
    relation = {"thread_id": "omt_topic", "root_id": "om_root", "parent_id": "om_parent"}
    event, request = await normalize_and_reply(adapter, relation=relation, chat_kind=chat_kind)

    assert request.request_body.reply_in_thread is True
    assert event.source.thread_id == relation["thread_id"]
    assert event.source.chat_type == chat_kind
    assert event.reply_to_message_id == relation["parent_id"]
    assert event.reply_to_text == "quoted task"
    assert request.message_id == event.message_id

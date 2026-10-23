"""Explicit native handoffs must survive Markdown transport selection."""
import json
from types import SimpleNamespace

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.feishu.adapter import FeishuAdapter


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", [" plain reply", " **bold** reply"])
async def test_leading_native_handoff_uses_text(suffix):
    adapter = FeishuAdapter(PlatformConfig())
    adapter._client = object()
    sent = []

    async def transport(**kwargs):
        sent.append(kwargs)
        return SimpleNamespace(success=lambda: True, data=SimpleNamespace(message_id="om_result"))

    adapter._feishu_send_with_retry = transport
    mention = '<at user_id="ou_receiver">Receiver</at>'
    result = await adapter.send("oc_test", mention + suffix)
    assert result.success
    assert len(sent) == 1
    assert sent[0]["msg_type"] == "text"
    assert json.loads(sent[0]["payload"])["text"] == mention + suffix.replace("**", "")


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    "**Normal Markdown**",
    '```xml\n<at user_id="ou_receiver">Receiver</at>\n```',
    '<at user_id="invalid">Receiver</at> **not a native ID**',
    '<at user_id="ou_receiver">Receiver **incomplete tag**',
])
async def test_markdown_without_complete_native_prefix_stays_post(body):
    adapter = FeishuAdapter(PlatformConfig())
    adapter._client = object()
    sent = []

    async def transport(**kwargs):
        sent.append(kwargs)
        return SimpleNamespace(success=lambda: True, data=SimpleNamespace(message_id="om_result"))

    adapter._feishu_send_with_retry = transport
    result = await adapter.send("oc_test", body)
    assert result.success
    assert len(sent) == 1
    assert sent[0]["msg_type"] == "post"
    elements = [element for locale in json.loads(sent[0]["payload"]).values()
                for row in locale["content"] for element in row]
    assert all(element["tag"] == "md" for element in elements)
    assert body == "\n".join(element["text"] for element in elements)


@pytest.mark.asyncio
async def test_explicit_handoff_stops_after_failed_first_chunk():
    adapter = FeishuAdapter(PlatformConfig())
    adapter._client = object()
    adapter.MAX_MESSAGE_LENGTH = 100
    sent = []

    async def transport(**kwargs):
        sent.append(kwargs)
        return SimpleNamespace(success=lambda: len(sent) > 1, code=999,
                               msg="recipient rejected", data=SimpleNamespace(message_id="om_result"))

    adapter._feishu_send_with_retry = transport
    body = '<at user_id="ou_receiver">Receiver</at> **handoff**\n' + "body " * 50
    result = await adapter.send("oc_test", body, metadata={"mentions": []})
    assert not result.success
    assert len(sent) == 1

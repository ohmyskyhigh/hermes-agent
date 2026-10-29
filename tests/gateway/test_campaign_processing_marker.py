from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.feishu.adapter import FeishuAdapter
from tests.gateway.test_topic_campaign_ingress import message, runner_for


@pytest.mark.asyncio
async def test_collector_does_not_show_processing_until_turn_admitted(tmp_path, monkeypatch):
    adapter = FeishuAdapter(PlatformConfig(enabled=True, extra={
        "app_id": "fixture", "app_secret": "fixture"}))
    adapter.gateway_runner = runner_for(tmp_path)
    monkeypatch.setattr(adapter, "_reactions_enabled", lambda: True)
    monkeypatch.setattr(adapter, "_add_reaction", AsyncMock(return_value="reaction"))
    event = message()
    await adapter.on_processing_start(event)
    adapter._add_reaction.assert_not_awaited()
    event._campaign_turn_admitted = True
    await adapter.on_processing_start(event)
    await adapter.on_processing_start(event)
    adapter._add_reaction.assert_awaited_once_with("om_root", "Typing")
    internal = message()
    internal.internal = True
    internal.message_id = "synthetic-wake-id"
    internal._campaign_turn_admitted = True
    await adapter.on_processing_start(internal)
    adapter._add_reaction.assert_awaited_once_with("om_root", "Typing")


@pytest.mark.asyncio
async def test_unrelated_chat_keeps_existing_processing_marker(tmp_path, monkeypatch):
    adapter = FeishuAdapter(PlatformConfig(enabled=True, extra={
        "app_id": "fixture", "app_secret": "fixture"}))
    adapter.gateway_runner = runner_for(tmp_path)
    monkeypatch.setattr(adapter, "_reactions_enabled", lambda: True)
    monkeypatch.setattr(adapter, "_add_reaction", AsyncMock(return_value="reaction"))
    event = message()
    event.source.chat_id = "unrelated"
    await adapter.on_processing_start(event)
    adapter._add_reaction.assert_awaited_once()

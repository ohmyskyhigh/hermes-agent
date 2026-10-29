"""Exercise the actual authorized GatewayRunner pipeline, not just the Campaign service."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def runner_for(tmp_path):
    raw = dict(enabled=True, state_path=str(tmp_path / "campaigns.db"), groups=[dict(
        platform="feishu", chat_id="oc_command", profiles=["default"], collector_profile="default",
        default_owner="default", mentions={"default": {"ou_bot": "default"}})])
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(platforms={Platform.FEISHU: PlatformConfig(enabled=True)}, topic_campaigns=raw)
    adapter = SimpleNamespace(send=AsyncMock(), on_processing_start=AsyncMock())
    runner.adapters = {Platform.FEISHU: adapter}
    runner.pairing_store = MagicMock()
    runner.session_store = MagicMock()
    runner._is_user_authorized_for_source = lambda _: True
    runner._admit_bot_message_for_source = lambda _: True
    runner._delivery_adapter_for = lambda _: adapter
    runner._intake_adapter_for = lambda _: adapter
    runner._rescue_orphaned_overflow = lambda *_: None
    runner._enqueue_fifo = MagicMock()
    runner._handle_message_with_agent = AsyncMock(return_value="agent-ok")
    runner._run_post_turn_hooks = AsyncMock()
    runner._active_profile_name = lambda: "default"
    return runner


def message():
    return MessageEvent(text="produce the report", message_id="om_root", mentioned_user_ids=["ou_bot"],
                        topic_root_message_id="om_root", source=SessionSource(platform=Platform.FEISHU,
                        chat_id="oc_command", thread_id="omt_topic", chat_type="group", user_id="ou_user"))


@pytest.mark.asyncio
async def test_real_ingress_creates_before_agent_and_unrelated_chat_is_unchanged(tmp_path):
    runner = runner_for(tmp_path)
    event = message()
    assert await runner._handle_message(event) == "agent-ok"
    items = await runner._topic_campaign_service.db("list")
    assert len(items) == 1 and items[0]["owner_profile"] == "default"
    assert "Campaign supervisor" in event.text
    assert runner._handle_message_with_agent.await_count == 1
    runner.adapters[Platform.FEISHU].on_processing_start.assert_awaited_once_with(event)
    other = message()
    other.source.chat_id = "oc_unrelated"
    assert await runner._handle_message(other) == "agent-ok"
    assert other.text == "produce the report"
    assert len(await runner._topic_campaign_service.db("list")) == 1


@pytest.mark.asyncio
async def test_unauthorized_ingress_cannot_create_or_control_campaign(tmp_path):
    runner = runner_for(tmp_path)
    runner._is_user_authorized_for_source = lambda _: False
    assert await runner._handle_message(message()) is None
    assert not (tmp_path / "campaigns.db").exists()
    runner._handle_message_with_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_feishu_native_mention_hint_preserves_creation_prefix_and_controls(tmp_path):
    runner = runner_for(tmp_path)
    runner.config.topic_campaigns["groups"][0]["creation_prefix"] = "[Campaign验收]"
    event = message()
    event.text = "[Mentioned: Reviewer (open_id=ou_bot)]\n\n[Campaign验收] produce the report"
    assert await runner._handle_message(event) == "agent-ok"
    service = runner._topic_campaign_service
    item = (await service.db("list"))[0]
    assert item["goal"] == "[Campaign验收] produce the report"
    event = message()
    event.message_id = "om_pause"
    event.text = "[Mentioned: Reviewer (open_id=ou_bot)]\n\n暂停 Campaign"
    assert "已暂停" in await runner._handle_message(event)
    assert (await service.db("get", item["key"]))["status"] == "paused"


def test_policy_survives_gateway_config_roundtrip(tmp_path):
    runner = runner_for(tmp_path)
    restored = GatewayConfig.from_dict(runner.config.to_dict())
    assert restored.topic_campaigns == runner.config.topic_campaigns
    nested = GatewayConfig.from_dict({"gateway": {"topic_campaigns": runner.config.topic_campaigns}})
    assert nested.topic_campaigns == restored.topic_campaigns


@pytest.mark.parametrize("nested", [True, False])
def test_policy_loads_from_actual_yaml_file(tmp_path, monkeypatch, nested):
    import json
    from gateway import config

    raw = runner_for(tmp_path).config.topic_campaigns
    document = {"gateway": {"topic_campaigns": raw}} if nested else {"topic_campaigns": raw}
    (tmp_path / "config.yaml").write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(config, "get_hermes_home", lambda: tmp_path)
    assert config.load_gateway_config().topic_campaigns == raw


@pytest.mark.asyncio
async def test_pending_human_clarification_is_not_rewritten_as_a_campaign_task(tmp_path):
    runner = runner_for(tmp_path)
    runner._hm_pending_reply_intercepts = AsyncMock(return_value="clarification accepted")
    event = message()
    assert await runner._handle_message(event) == "clarification accepted"
    assert event.text == "produce the report"
    assert not (tmp_path / "campaigns.db").exists()


@pytest.mark.asyncio
async def test_real_feishu_admission_redelivery_and_timer_wake_keep_same_topic(tmp_path, monkeypatch):
    from gateway.platforms.base import SendResult
    from gateway.session_authority import initialize_session_authority
    from gateway.session_results import execution_result
    from hermes_state_runtime import list_session_admissions
    from plugins.platforms.feishu.adapter import FeishuAdapter
    from tests.waiting import async_wait_until

    monkeypatch.setenv("FEISHU_ALLOWED_USERS", "ou_user")
    raw = runner_for(tmp_path).config.topic_campaigns
    runner = GatewayRunner(GatewayConfig(topic_campaigns=raw, sessions_dir=tmp_path / "sessions"))
    adapter = FeishuAdapter(PlatformConfig(enabled=True, extra={"app_id": "fixture-app", "app_secret": "fixture"}))
    adapter.gateway_runner, runner.adapters = runner, {Platform.FEISHU: adapter}
    authority = await initialize_session_authority(runner, profile_id="default", instance_id="campaign-test")
    runner._wire_adapter_handlers(adapter)
    turns = []
    sent = []

    async def send(chat_id, content, **kwargs):
        assert kwargs.get("reply_to") == "om_root"
        sent.append((chat_id, content, kwargs))
        return SendResult(success=True, message_id="sent")

    async def turn(event, *_args, **_kwargs):
        turns.append((event.text, event.source.thread_id, event.sender_open_id))
        execution_result.get()["result"] = {"final_response": "verified reply", "messages": []}
        return "verified reply"

    monkeypatch.setattr(adapter, "send", send)
    monkeypatch.setattr(runner, "_handle_message_with_agent", turn)
    try:
        event = message()
        event.sender_open_id = "ou_user"
        await adapter.handle_message(event)
        await async_wait_until(lambda: bool(sent) and not adapter._background_tasks, timeout=20, what="campaign root reply")
        repeated = message()
        repeated.sender_open_id = "ou_user"
        await adapter.handle_message(repeated)
        service = runner._topic_campaign_service
        item = (await service.db("list"))[0]
        assert item["goal"] == "produce the report"
        await service.tick(item["created_at"] + 901)
        await async_wait_until(lambda: len(turns) == 2 and not adapter._background_tasks, timeout=20, what="campaign owner timer turn")
        assert all(text.startswith("[Campaign supervisor") and thread == "omt_topic" and sender == "ou_user"
                   for text, thread, sender in turns)
        assert all(chat == "oc_command" and kwargs["metadata"]["thread_id"] == "omt_topic"
                   for chat, _, kwargs in sent)
        entry = runner.session_store.lookup_by_session_key(runner._session_key_for_source(event.source))
        rows = list_session_admissions(authority.db, session_id=entry.session_id, pending_only=False)
        roots = [r for r in rows if r["request_id"] == "om_root"]
        assert len(roots) == 1 and roots[0]["payload"]["text"] == "produce the report"
    finally:
        authority.db.close()

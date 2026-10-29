"""Run the real timer and ingress bridge with SQLite and a fake transport (no live Feishu)."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource
from gateway.topic_campaign_config import CampaignPolicy
from gateway.topic_campaign_service import TopicCampaignService, observe_campaign_message, prepare_campaign_turn


@asynccontextmanager
async def scope(_home):
    yield


@pytest.fixture
def setup(tmp_path, monkeypatch):
    import gateway.run
    import gateway.wake
    import agent.estop
    monkeypatch.setattr(gateway.run, "_async_profile_runtime_scope", scope)
    monkeypatch.setattr(agent.estop, "check_paused", lambda *_: False)
    wake = AsyncMock()
    monkeypatch.setattr(gateway.wake, "deliver_wake", wake)
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)), on_processing_start=AsyncMock())
    raw = dict(enabled=True, state_path=str(tmp_path / "campaigns.db"),
               check_interval_seconds=60, review_interval_seconds=300, report_interval_seconds=900,
               groups=[dict(platform="feishu", chat_id="oc_command", profiles=["analyst", "joi", "writer"],
                            collector_profile="joi", default_owner="joi",
                            mentions={p: {"ou_analyst": "analyst", "ou_writer": "writer"}
                                      for p in ["analyst", "joi", "writer"]})])
    runner = SimpleNamespace(config=GatewayConfig(topic_campaigns=raw, multiplex_profiles=True), _active_profile_name=lambda: "analyst",
        _intake_adapter_for=lambda source: adapter,
        _delivery_adapter_for=lambda source: adapter, _session_key_for_source=lambda source: source.profile + ":" + source.thread_id,
        _resolve_profile_home_for_source=lambda source: tmp_path / source.profile,
        _is_session_running=lambda _: False, _external_drain_active=False)
    service = TopicCampaignService(runner, CampaignPolicy.parse(raw))
    runner._topic_campaign_service = service
    return service, runner, adapter, wake


def event(profile="analyst", *, thread="omt_task", mentions=None, text="Please investigate"):
    source = SessionSource(platform=Platform.FEISHU, chat_id="oc_command", chat_type="group",
                           thread_id=thread, profile=profile, user_id="ou_human", user_id_alt="on_human")
    return MessageEvent(text=text, message_type=MessageType.TEXT, source=source, message_id="om_first",
                        mentioned_user_ids=mentions if mentions is not None else ["ou_analyst"], topic_root_message_id="om_first")


@pytest.mark.asyncio
async def test_native_mentions_and_unmentioned_topics_have_one_owner(setup):
    service, runner, _, _ = setup
    observer = event("joi")
    assert (await service.intake(observer, observer.source))[0]
    target = event()
    assert not (await service.intake(target, target.source))[0]
    assert target.text == "Please investigate"
    await prepare_campaign_turn(runner, target, target.source)
    assert "owner Agent: analyst" in target.text
    assert len(await service.db("list")) == 1
    no_mention = event("joi", thread="omt_second", mentions=[])
    assert not (await service.intake(no_mention, no_mention.source))[0]
    assert (await service.db("get", "feishu:oc_command:omt_second"))["owner_profile"] == "joi"
    unrelated = event(thread=None)
    assert await service.intake(unrelated, unrelated.source) == (False, None)


@pytest.mark.asyncio
async def test_periodic_owner_review_runs_without_any_completion_and_survives_restart(setup):
    service, runner, adapter, wake = setup
    message = event()
    await service.intake(message, message.source)
    item = (await service.db("list"))[0]
    now = item["created_at"]
    await service.tick(now)
    assert wake.await_count == 1
    await service.tick(now + 60)
    assert wake.await_count == 1  # accepted wake is not a completed review, but is coalesced
    restored = TopicCampaignService(runner, service.policy)
    await restored.tick(now + 301)
    assert wake.await_count == 2
    await restored.tick(now + 901)
    assert adapter.send.await_count == 1
    assert adapter.send.call_args.args[0] == "oc_command"
    assert adapter.send.call_args.kwargs["metadata"] == {"thread_id": "omt_task"}
    assert adapter.send.call_args.kwargs["reply_to"] == "om_first"
    assert "尚未登记有效检查" in adapter.send.call_args.args[1]


@pytest.mark.asyncio
async def test_busy_owner_does_not_silence_report_or_spawn_duplicate(setup):
    service, runner, adapter, wake = setup
    message = event()
    await service.intake(message, message.source)
    runner._is_session_running = lambda _: True
    item = (await service.db("list"))[0]
    await service.tick(item["created_at"] + 901)
    wake.assert_not_awaited()
    assert "负责人当前仍在执行" in adapter.send.call_args.args[1]


@pytest.mark.asyncio
async def test_scheduled_report_uses_real_feishu_reply_request(setup):
    from gateway.config import PlatformConfig
    from plugins.platforms.feishu.adapter import FeishuAdapter

    service, runner, _, _ = setup
    adapter = FeishuAdapter(PlatformConfig())
    reply = SimpleNamespace(success=lambda: True, data=SimpleNamespace(message_id="om_report"))
    send = AsyncMock(return_value=reply)
    adapter._client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=SimpleNamespace(reply=send))))
    # Keep the real SDK request builder and send path; only replace the final network call.
    async def transport(fn, request):
        return await fn(request)
    adapter._run_blocking = AsyncMock(side_effect=transport)
    runner._delivery_adapter_for = lambda _: adapter
    runner._is_session_running = lambda _: True
    message = event()
    await service.intake(message, message.source)
    item = (await service.db("list"))[0]
    await service.tick(item["created_at"] + 901)
    request = send.call_args.args[0]
    assert request.message_id == "om_first"
    assert request.request_body.reply_in_thread is True
    assert (await service.db("get", item["key"]))["last_reported_at"] > 0


@pytest.mark.asyncio
async def test_failed_wake_still_reports_and_failed_report_stays_due(setup):
    service, _, adapter, wake = setup
    message = event()
    await service.intake(message, message.source)
    item = (await service.db("list"))[0]
    now = item["created_at"] + 901
    wake.side_effect = RuntimeError("queue refused")
    adapter.send.return_value = SimpleNamespace(success=False)
    await service.tick(now)
    assert adapter.send.await_count == 1
    assert (await service.db("get", item["key"]))["last_reported_at"] == 0
    adapter.send.return_value = SimpleNamespace(success=True)
    assert not await service.tick(now + 1)
    await service.tick(now + 61)
    assert "queue refused" in adapter.send.call_args.args[1]
    assert (await service.db("get", item["key"]))["last_reported_at"] == now + 61


@pytest.mark.asyncio
async def test_pause_rejects_already_queued_supervisor_turn(setup):
    service, runner, adapter, wake = setup
    message = event()
    await service.intake(message, message.source)
    pause = event(text="暂停 Campaign")
    pause.message_id = "om_pause"
    handled, reply = await service.intake(pause, pause.source)
    assert handled and "已暂停" in reply
    message.internal = True
    assert await observe_campaign_message(runner, message, message.source) == (True, None)
    await service.tick(10**11)
    wake.assert_not_awaited()
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_completion_report_is_retried_after_restart_and_stops_periodic_wakes(setup):
    service, runner, adapter, wake = setup
    message = event()
    await service.intake(message, message.source)
    item = (await service.db("list"))[0]
    now = item["created_at"] + 1
    await service.db("update", item["key"], "analyst",
                     {"done_when": "report verified", "evidence": "report artifact", "status": "completed"}, now)
    adapter.send.return_value = SimpleNamespace(success=False)
    await service.tick(now)
    adapter.send.return_value = SimpleNamespace(success=True)
    restored = TopicCampaignService(runner, service.policy)
    await restored.tick(now + 901)
    assert "验收证据：report artifact" in adapter.send.call_args.args[1]
    assert "已要求负责人检查" not in adapter.send.call_args.args[1]
    assert "调度异常" not in adapter.send.call_args.args[1]
    assert (await restored.db("get", item["key"]))["last_error"] == ""
    await restored.tick(now + 2000)
    wake.assert_not_awaited()
    assert adapter.send.await_count == 2


@pytest.mark.asyncio
async def test_missing_owner_route_reports_problem_without_impersonating_owner(setup):
    service, _, adapter, wake = setup
    collector = event("joi")
    await service.intake(collector, collector.source)
    item = (await service.db("list"))[0]
    await service.tick(item["created_at"] + 901)
    wake.assert_not_awaited()
    assert "负责人尚无可用话题路由" in adapter.send.call_args.args[1]
    assert adapter.send.call_args.kwargs["metadata"]["thread_id"] == "omt_task"


@pytest.mark.asyncio
async def test_canary_prefix_does_not_take_over_existing_topics(setup):
    service, _, _, _ = setup
    service.policy.groups[0]["creation_prefix"] = "[Campaign验收]"
    ordinary = event("joi")
    assert await service.intake(ordinary, ordinary.source) == (True, None)
    assert await service.db("list") == []
    ordinary = event()
    assert await service.intake(ordinary, ordinary.source) == (False, None)
    assert ordinary.text == "Please investigate"
    test = event(text="[Campaign验收] one task")
    await service.intake(test, test.source)
    followup = event(text="continue with the same task")
    followup.message_id = "om_follow"
    await service.intake(followup, followup.source)
    assert len(await service.db("list")) == 1
    assert len(await service.db("events", "feishu:oc_command:omt_task")) == 2


@pytest.mark.asyncio
async def test_collector_cannot_claim_another_gateways_owner_and_standalone_never_borrows_bot(setup):
    service, runner, adapter, _ = setup
    for profile in ["joi", "analyst"]:
        message = event(profile)
        await service.intake(message, message.source)
    item = (await service.db("list"))[0]
    runner._delivery_adapter_for = lambda source: adapter if source.profile == "joi" else None
    assert not service.eligible(item)
    runner.config.multiplex_profiles = False
    runner._delivery_adapter_for = lambda _: adapter
    assert service.route(item, "joi") == (None, None)
    assert service.eligible(item)


@pytest.mark.asyncio
async def test_kanban_heartbeat_detects_stall_and_completion_accelerates_owner_review(setup):
    import sqlite3
    service, _, _, wake = setup
    message = event()
    await service.intake(message, message.source)
    item = (await service.db("list"))[0]
    now = item["created_at"]
    path = service.policy.state_path.parent / "kanban.db"
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE tasks(id TEXT,status TEXT,last_heartbeat_at REAL,result TEXT)")
        conn.execute("INSERT INTO tasks VALUES ('task-1','running',?,'')", (now - 1000,))
        conn.commit()
        await service.db("work_update", item["key"], "analyst", "draft",
                         {"kind": "kanban", "task_id": "task-1", "profile": "writer"}, now)
        item = await service.db("get", item["key"])
        health = await service.health(item, now, message.source)
        assert health["workers"][0]["stale"] is True
        await service.tick(now)
        await service.db("update", item["key"], "analyst", {"next_action": "inspect worker"}, now + 1)
        conn.execute("UPDATE tasks SET status='done',result='draft evidence',last_heartbeat_at=?", (now - 1000,))
        conn.commit()
        await service.tick(now + 61)
        assert wake.await_count == 2
        assert 'draft evidence' in wake.call_args.kwargs["text"]
        assert (await service.health(item, now + 61, message.source))["workers"][0]["stale"] is False
        await service.db("work_update", item["key"], "analyst", "receipt",
                         {"status": "done", "summary": "verified artifact"}, now - 1000)
        item = await service.db("get", item["key"])
        assert not any(w["stale"] for w in (await service.health(item, now + 61, message.source))["workers"])
        from gateway.topic_campaign_prompt import report
        rendered = report(item, await service.health(item, now + 61, message.source), now + 61)
        assert "工作进度：" in rendered
        assert "待检查的执行者" not in rendered
        assert '"stale"' not in rendered
        assert "task-1" not in rendered
    finally:
        conn.close()

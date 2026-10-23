"""Reply policy contracts at the gateway's real delivery boundaries."""
from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock, call

import pytest

from gateway.config import Platform, StreamingConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource
from gateway.turn_context import TurnContext
from gateway.run_turn_runner import TurnRunner


def event(chat_type="group"):
    return MessageEvent(text="hello", source=SessionSource(
        platform=Platform.FEISHU, chat_id="oc_chat", chat_type=chat_type,
        user_id="user", user_name="User", profile="joi"), message_id="om_message",
        raw_message={"event": {"sender": {"sender_id": {"open_id": "ou_trusted"}}}})


def test_buffer_blocks_deltas_and_direct_commentary_fallback():
    ctx = TurnContext(source=event().source, _run_still_current=lambda: True,
        interim_assistant_messages_enabled=True, resolve_display_setting=lambda *a: True)
    ctx.reply_policy_buffer = True
    gateway = SimpleNamespace(config=SimpleNamespace(streaming=StreamingConfig()),
                              _adapter_for_source=lambda source: None)
    runner = TurnRunner(gateway, ctx)
    runner._send_status_text = Mock()
    ctx._status_adapter = object()
    tts = Mock()
    ctx.streaming_tts_consumer_holder[0] = tts
    consumer, delta, interim, enabled = runner._setup_stream_consumer("feishu")
    interim("private draft")
    assert consumer is None and delta is None and not enabled
    runner._send_status_text.assert_not_called()
    tts.on_delta.assert_not_called()
    ctx.reply_policy_buffer = False
    consumer, delta, interim, enabled = runner._setup_stream_consumer("feishu")
    assert delta is not None and enabled
    delta("DM delta")
    interim("DM commentary MEDIA:/tmp/preview.png")
    assert tts.on_delta.call_args_list == [
        call("DM delta"), call(None),
        call("DM commentary MEDIA:/tmp/preview.png"), call(None),
    ]
    runner._send_status_text.assert_called_once()


@pytest.mark.asyncio
async def test_prepare_cached_final_suppress_precedes_voice_and_media(monkeypatch):
    from hermes_cli import plugins
    from gateway.run import GatewayRunner
    from gateway.reply_policy import prepare_reply_policy
    calls = []
    def hook(name, **kw):
        calls.append(kw)
        return [None, {"invalid": True}, {"buffer": True} if kw["phase"] == "prepare"
                else {"action": "suppress"}, {"action": "send"}]
    monkeypatch.setattr(plugins, "invoke_hook", hook)
    ev = event()
    assert await prepare_reply_policy(ev)
    assert await prepare_reply_policy(ev)
    runner = object.__new__(GatewayRunner)
    runner._adapter_for_source = Mock(return_value=None)
    runner._should_send_voice_reply = Mock(return_value=True)
    runner._send_voice_reply = AsyncMock()
    runner._deliver_media_from_response = AsyncMock()
    result = {"completed": True, "messages": [{"role": "assistant", "content": "secret"}]}
    for _ in range(2):
        assert await runner._hmwa_deliver_turn_response(ev, ev.source,
            SimpleNamespace(session_id="s"), "s", 1, result, result["messages"],
            "secret MEDIA:/tmp/secret.png", "", False) is None
    runner._send_voice_reply.assert_not_called()
    runner._deliver_media_from_response.assert_not_called()
    assert result["messages"][0]["content"] == "secret"
    assert [c["phase"] for c in calls] == ["prepare", "final"]
    assert calls[0]["context"]["sender_open_id"] == "ou_trusted"
    assert calls[1]["context"]["text"] == "secret MEDIA:/tmp/secret.png"
    assert calls[1]["context"]["has_media"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_type, buffered", [("group", True), ("dm", False)])
async def test_real_agent_path_policy_controls_streams_and_tts(monkeypatch, tmp_path, chat_type, buffered):
    from hermes_cli import plugins
    from tests.gateway.test_streaming_tts_gateway_regression import _setup_monkeypatches, _make_runner
    _setup_monkeypatches(monkeypatch, tmp_path)
    from gateway.reply_policy import prepare_reply_policy
    monkeypatch.setattr(plugins, "invoke_hook", lambda name, **kw:
                        [{"buffer": True}] if kw["context"]["chat_type"] == "group" else [])
    ev = event(chat_type)
    runner = _make_runner()
    runner._adapter_for_source = lambda source: None
    seen = []
    original = TurnRunner._setup_stream_consumer
    def capture(self, platform):
        seen.append(getattr(self._ctx, "reply_policy_buffer", False))
        return original(self, platform)
    monkeypatch.setattr(TurnRunner, "_setup_stream_consumer", capture)
    start = Mock()
    monkeypatch.setattr(runner, "_run_agent_start_streaming_tts", start)
    result = await runner._run_agent(message="hello", context_prompt="", history=[],
        source=ev.source, session_id="session-1", session_key="policy-test",
        message_type="voice", reply_policy_event=ev)
    assert result["final_response"] == "Hello from the agent."
    assert seen == [buffered]
    assert start.call_count == (0 if buffered else 1)
    assert await prepare_reply_policy(ev) is buffered


from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.config import PlatformConfig


class RecordingAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(), Platform.FEISHU)
        self.sent = []
    async def connect(self, **kw):
        return True
    async def disconnect(self):
        pass
    async def get_chat_info(self, chat_id):
        return {}
    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append((content, dict(metadata or {})))
        return SendResult(success=True, message_id="sent")


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["useful answer", "MEDIA:/tmp/report.pdf"])
async def test_base_reads_final_mentions_after_handler_and_sends_text_before_media(monkeypatch, text, tmp_path):
    from hermes_cli import plugins
    from gateway.reply_policy import finalize_reply_policy
    adapter = RecordingAdapter()
    if text.startswith("MEDIA:"):
        attachment = tmp_path / "report.pdf"
        attachment.write_bytes(b"%PDF-1.4 test")
        text = f"MEDIA:{attachment.as_posix()}"
    ev = event()
    mentions = [{"id": "ou_trusted", "name": "User"}]
    monkeypatch.setattr(plugins, "invoke_hook", lambda *a, **kw: [{"action": "send", "mentions": mentions}])
    async def handler(event):
        await finalize_reply_policy(event, {"completed": True}, text)
        return text
    adapter._message_handler = handler
    adapter._start_typing_refresh = Mock(return_value=None)
    adapter._stop_typing_refresh = AsyncMock()
    adapter._wants_auto_tts = Mock(return_value=False)
    attachment_metadata = []
    async def attachments(event, extracted, metadata, **kw):
        assert adapter.sent
        attachment_metadata.append(metadata)
    adapter._deliver_attachments = attachments
    await adapter._process_message_background(ev, "policy-test")
    assert len(adapter.sent) == 1
    assert adapter.sent[0][0].strip()
    assert adapter.sent[0][1]["mentions"] == mentions
    assert all("mentions" not in (m or {}) for m in attachment_metadata)


@pytest.mark.asyncio
async def test_full_handler_persists_suppressed_assistant_before_policy(monkeypatch, tmp_path):
    from tests.gateway.test_gateway_silence_tokens import _runner
    from hermes_cli import plugins
    runner = _runner(monkeypatch, tmp_path)
    ev = event()
    transcript = []
    runner.session_store.append_to_transcript.side_effect = lambda sid, message, **kw: transcript.append(message)
    def hook(name, **kw):
        if name != "gateway_reply_policy":
            return []
        assert any(m.get("role") == "assistant" for m in transcript)
        return [{"action": "suppress"}]
    monkeypatch.setattr(plugins, "invoke_hook", hook)
    runner._run_agent = AsyncMock(return_value={
        "final_response": "private answer", "completed": True,
        "messages": [{"role": "user", "content": "hello"},
                     {"role": "assistant", "content": "private answer"}],
        "tools": [], "history_offset": 0, "api_calls": 1, "failed": False})
    runner._should_send_voice_reply = Mock(return_value=True)
    runner._send_voice_reply = AsyncMock()
    result = await runner._handle_message_with_agent(ev, ev.source, "policy-test", 1)
    assert result is None
    assert [m["role"] for m in transcript if m.get("role") in ("user", "assistant")] == ["user", "assistant"]
    assert transcript[-1]["content"] == "private answer"
    runner._send_voice_reply.assert_not_called()
    assert runner._run_agent.call_args.kwargs["reply_policy_event"] is ev


@pytest.mark.asyncio
async def test_failed_agent_bypasses_policy_and_defer_is_failure(monkeypatch, caplog):
    from gateway.reply_policy import finalize_reply_policy
    from gateway.platforms.base import ProcessingOutcome
    from hermes_cli import plugins
    hook = Mock(return_value=[{"action": "defer", "reason": "recipient_unresolved"}])
    monkeypatch.setattr(plugins, "invoke_hook", hook)
    ev = event()
    assert await finalize_reply_policy(ev, {"failed": True}, "error") is None
    hook.assert_not_called()
    adapter = RecordingAdapter()
    adapter._start_typing_refresh = Mock(return_value=None)
    adapter._stop_typing_refresh = AsyncMock()
    adapter.on_processing_complete = AsyncMock()
    async def handler(event):
        await finalize_reply_policy(event, {"completed": True}, "answer")
    adapter._message_handler = handler
    await adapter._process_message_background(ev, "policy-test")
    assert not adapter.sent
    assert "recipient_unresolved" in caplog.text
    assert adapter.on_processing_complete.call_args.args[1] == ProcessingOutcome.FAILURE


@pytest.mark.asyncio
async def test_direct_voice_gets_one_mentioned_text_before_audio(monkeypatch):
    from gateway.run import GatewayRunner
    from hermes_cli import plugins
    adapter = RecordingAdapter()
    ev = event()
    runner = object.__new__(GatewayRunner)
    runner._adapter_for_source = lambda source: adapter
    runner._should_send_voice_reply = lambda *a, **kw: True
    mentions = [{"id": "ou_trusted", "name": "User"}]
    monkeypatch.setattr(plugins, "invoke_hook", lambda *a, **kw: [{"action": "send", "mentions": mentions}])
    async def voice(event, response):
        assert len(adapter.sent) == 1
        assert adapter.sent[0][0] == "answer"
        assert adapter.sent[0][1]["mentions"] == mentions
    runner._send_voice_reply = AsyncMock(side_effect=voice)
    async def handler(event):
        return await runner._hmwa_deliver_turn_response(event, event.source,
            SimpleNamespace(session_id="s"), "s", 1, {"completed": True}, [], "answer", "", False)
    adapter._message_handler = handler
    adapter._start_typing_refresh = Mock(return_value=None)
    adapter._stop_typing_refresh = AsyncMock()
    adapter._wants_auto_tts = lambda *a, **kw: False
    await adapter._process_message_background(ev, "s")
    runner._send_voice_reply.assert_awaited_once()
    assert len(adapter.sent) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_type", ["group", "dm"])
@pytest.mark.parametrize("mixed_media", [False, True])
async def test_pending_fifo_preserves_events_through_real_drain_and_base_handoff(
        monkeypatch, tmp_path, chat_type, mixed_media):
    import asyncio
    from gateway.platforms.base import MessageType
    from tests.gateway.test_streaming_tts_gateway_regression import (
        _NoopAgent, _setup_monkeypatches, _make_runner,
    )
    from hermes_cli import plugins
    _setup_monkeypatches(monkeypatch, tmp_path)
    monkeypatch.setattr(_NoopAgent, "run_conversation", lambda self, *args, **kwargs: {
        "final_response": "Hello from the agent.", "messages": [],
        "api_calls": 1, "completed": True,
    })
    monkeypatch.setattr(plugins, "invoke_hook", lambda *a, **kw:
        [{"buffer": True}] if kw["context"]["chat_type"] == "group" else [])
    runner = _make_runner()
    adapter = RecordingAdapter()
    runner._adapter_for_source = lambda source: adapter
    runner._delivery_adapter_for = lambda source: adapter
    runner._session_key_for_source = lambda source: "policy-test"
    adapter._start_typing_refresh = Mock(return_value=None)
    adapter._stop_typing_refresh = AsyncMock()
    adapter._wants_auto_tts = lambda *a, **kw: False
    adapter.send = AsyncMock(wraps=adapter.send)
    ev, first, second = [event(chat_type) for _ in range(3)]
    first.message_id, first.text, first.source.user_id = "A", "first request", "sender-A"
    second.message_id, second.text, second.source.user_id = "B", "second request", "sender-B"
    if mixed_media:
        first.message_type, first.media_urls, first.media_types = MessageType.PHOTO, ["first.png"], ["image/png"]
        second.message_type, second.media_urls, second.media_types = MessageType.DOCUMENT, ["second.pdf"], ["application/pdf"]
    expected_media = [(list(e.media_urls), list(e.media_types)) for e in (first, second)]
    # Real enqueue: A occupies the adapter slot; B is gateway-owned overflow.
    runner._enqueue_fifo("policy-test", first, adapter)
    runner._enqueue_fifo("policy-test", second, adapter)
    assert adapter._pending_messages["policy-test"] is first
    assert runner._overflow_queue("policy-test") == [second]

    entered, handled, prepared = [], [], []
    run_agent = runner._run_agent
    async def capture_run(**kwargs):
        entered.append(kwargs["event_message_id"])
        return await run_agent(**kwargs)
    runner._run_agent = capture_run
    # DM inline preprocessing is outside this queue regression; keep the real
    # recursive follow-up, drain, dequeue, promotion and base task ownership.
    async def prepare(**kwargs):
        prepared.append(kwargs["event"])
        return kwargs["event"].text
    runner._prepare_profile_scoped_inbound_message_text = prepare
    async def handler(incoming):
        handled.append(incoming)
        result = await runner._run_agent(message=incoming.text, context_prompt="", history=[],
            source=incoming.source, session_id="session-1", session_key="policy-test",
            event_message_id=incoming.message_id, reply_policy_event=incoming)
        return result["final_response"]
    adapter._message_handler = handler
    await adapter._process_message_background(ev, "policy-test")
    for _ in range(100):
        if adapter._background_tasks:
            await asyncio.wait_for(
                asyncio.gather(*list(adapter._background_tasks)), timeout=10)
        if len(entered) == 3 and len(adapter.sent) == 3:
            break
        await asyncio.sleep(0)

    assert entered == [ev.message_id, "A", "B"]
    assert len(adapter.sent) == 3
    if chat_type == "group":
        assert [call.kwargs.get("reply_to") for call in adapter.send.await_args_list] == [ev.message_id, "A", "B"]
        assert len(handled) == 3 and all(actual is expected for actual, expected in zip(handled, (ev, first, second)))
        assert not prepared  # buffered events each re-enter their own base handler
    else:
        assert handled == [ev]
        assert prepared == [first, second]  # DM keeps the old inline recursion
    assert [first.source.user_id, second.source.user_id] == ["sender-A", "sender-B"]
    assert [first.text, second.text] == ["first request", "second request"]
    assert [(e.media_urls, e.media_types) for e in (first, second)] == expected_media
    assert runner._queue_depth("policy-test", adapter=adapter) == 0


@pytest.mark.asyncio
async def test_failed_mention_prelude_withholds_audio_and_attachments(monkeypatch, tmp_path):
    from gateway.reply_policy import finalize_reply_policy
    from gateway.platforms.base import ProcessingOutcome
    from hermes_cli import plugins
    monkeypatch.setattr(plugins, "invoke_hook", lambda *a, **kw:
        [{"action": "send", "mentions": [{"id": "ou_trusted", "name": "User"}]}])
    adapter = RecordingAdapter()
    ev = event()
    attachment = tmp_path / "report.pdf"
    attachment.write_bytes(b"%PDF-1.4 test")
    text = f"MEDIA:{attachment.as_posix()}"
    async def handler(event):
        await finalize_reply_policy(event, {"completed": True}, text)
        return text
    adapter._message_handler = handler
    adapter._start_typing_refresh = Mock(return_value=None)
    adapter._stop_typing_refresh = AsyncMock()
    adapter._send_with_retry = AsyncMock(return_value=SendResult(success=False, error="rejected"))
    adapter._synthesize_auto_tts = AsyncMock()
    adapter._deliver_attachments = AsyncMock()
    adapter.on_processing_complete = AsyncMock()
    await adapter._process_message_background(ev, "s")
    adapter._deliver_attachments.assert_not_awaited()
    adapter._synthesize_auto_tts.assert_not_awaited()
    assert not getattr(ev, "_reply_policy_text_delivered", False)
    assert adapter.on_processing_complete.call_args.args[1] == ProcessingOutcome.FAILURE

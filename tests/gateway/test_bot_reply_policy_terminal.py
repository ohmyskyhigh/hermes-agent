"""Frozen external-plugin contracts: bot replies never infer a recipient.

The real loader discovers the plugin in a temporary home; no installed profiles or network.
"""
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from gateway.reply_policy import reply_policy_context
from plugins.platforms.feishu.mentions import add_recipient_mentions


@pytest.fixture
def policy(tmp_path, monkeypatch):
    from hermes_cli import plugins
    from hermes_cli.config import atomic_config_write
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    fixture = Path(__file__).parents[1] / "fixtures/fork_plugins/feishu-group-reply-policy"
    shutil.copytree(fixture, home / "plugins/feishu-group-reply-policy")
    atomic_config_write(home / "config.yaml", {"plugins": {"enabled": ["feishu-group-reply-policy"]}})
    monkeypatch.setattr(plugins, "get_bundled_plugins_dir", lambda: tmp_path / "empty-bundled")
    manager = plugins.PluginManager()
    manager.discover_and_load()
    assert manager.has_hook("gateway_reply_policy")
    yield lambda **kwargs: next(iter(manager.invoke_hook("gateway_reply_policy", **kwargs)), None)
    manager.unload()


def context(text, **overrides):
    event = SimpleNamespace(
        source=SimpleNamespace(platform="feishu", chat_type="group", profile="test",
                               chat_id="oc_test", user_id="ou_sender", user_name="Peer",
                               is_bot=False),
        raw_message={"event": {"sender": {"sender_type": "app",
                                          "sender_id": {"open_id": "ou_sender"}}}},
        message_id="om_test", media_urls=[],
    )
    return {**reply_policy_context(event, text), **overrides}


# Representative bot acknowledgements, useful results, and explicit handoffs.
@pytest.mark.parametrize("text", [
    "收到，同事。按你的回报，已完成问候和真正的 @提及，不用再发。后续有任务再接着聊。",
    "好，等有具体任务再接着聊。",
    "收到，不再重复发送，后续有任务再接着聊。",
    "报告已完成：修复了三个问题，回归验证通过。附件包含复现步骤和验收结果。",
    '<at user_id="ou_receiver">Receiver</at> 请复核报告并完成下一步任务。',
    '<at user_id="ou_receiver">Receiver</at> 结果已就绪，请验收。',
])
@pytest.mark.parametrize("chat_type", ["group", "thread", "forum"])
def test_bot_reply_preserves_body_without_inventing_recipient(policy, text, chat_type):
    ctx = context(text, chat_type=chat_type)
    before = dict(ctx)
    decision = policy(phase="final", context=ctx)
    assert decision == {"action": "send", "mentions": [],
                        "reason": "explicit_recipient_required_for_bot"}
    assert ctx == before
    payload = json.dumps({"text": text}, ensure_ascii=False)
    assert add_recipient_mentions("text", payload, decision["mentions"]) == payload
    assert json.loads(add_recipient_mentions("text", payload, decision["mentions"]))["text"] == text


@pytest.mark.parametrize("case", ["human", "human_unresolved", "bot_unresolved", "media",
                                   "courtesy", "progress", "dm", "private", "p2p", "other"])
def test_scope_and_existing_human_courtesy_controls(policy, case):
    ctx = context("结果已经准备好，请验收。")
    expected = {"action": "send", "mentions": [], "reason": "explicit_recipient_required_for_bot"}
    if case in {"human", "human_unresolved"}:
        ctx["sender_is_bot"] = False
        expected = {"action": "send", "mentions": [{"id": "ou_sender", "name": "Peer"}],
                    "reason": "directed_reply"}
    if case in {"human_unresolved", "bot_unresolved"}:
        ctx.update(sender_id="cli_not_an_open_id", sender_open_id=None)
        if case == "human_unresolved":
            expected = {"action": "send", "mentions": [], "reason": "recipient_unresolved_fail_open"}
    if case == "media":
        ctx.update(text="收到", has_media=True)
    if case == "courtesy":
        ctx["text"] = "收到，谢谢！"
        expected = {"action": "suppress", "reason": "courtesy_or_terminal_ack"}
    if case == "progress":
        ctx["text"] = "正在整理资料，暂无阻塞。"
    if case in {"dm", "private", "p2p", "other"}:
        ctx.update({"platform": "slack"} if case == "other" else {"chat_type": case})
        expected = None
        assert policy(phase="prepare", context=ctx) is None
    else:
        assert policy(phase="prepare", context=ctx) == {"buffer": True}
    assert policy(phase="final", context=ctx) == expected
    assert policy(phase="unknown", context=ctx) is None
    if case == "human":
        payload = add_recipient_mentions("text", json.dumps({"text": ctx["text"]}), expected["mentions"])
        assert json.loads(payload)["text"] == '<at user_id="ou_sender">Peer</at>\n' + ctx["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("markdown", [False, True])
async def test_explicit_handoff_reaches_real_adapter_as_native_mention(policy, markdown):
    from gateway.config import PlatformConfig
    from plugins.platforms.feishu.adapter import FeishuAdapter

    text = '<at user_id="ou_receiver">Receiver</at> 请验收报告。'
    if markdown:
        text += '\n**验收结果**：通过。'
    decision = policy(phase="final", context=context(text))
    assert decision["action"] == "send"
    adapter = FeishuAdapter(PlatformConfig())
    adapter._client = object()
    sent = []

    async def transport(**kwargs):
        sent.append(kwargs)
        return SimpleNamespace(success=lambda: True, data=SimpleNamespace(message_id="om_result"))

    adapter._feishu_send_with_retry = transport
    result = await adapter.send("oc_test", text, metadata={"mentions": decision["mentions"]})
    assert result.success
    assert len(sent) == 1
    payload = json.loads(sent[0]["payload"])
    assert "ou_sender" not in sent[0]["payload"]
    if sent[0]["msg_type"] == "text":
        assert '<at user_id="ou_receiver">Receiver</at>' in payload["text"]
    else:
        elements = [element for locale in payload.values() for row in locale["content"] for element in row]
        assert any(element.get("tag") == "at" and element.get("user_id") == "ou_receiver"
                   for element in elements), payload

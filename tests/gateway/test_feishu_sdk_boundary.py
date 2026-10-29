"""Feishu SDK-boundary identity and processing-reaction contracts."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent, MessageType, ProcessingOutcome
from gateway.session import SessionSource


# ---------------------------------------------------------------------------
# SDK-boundary paths that used to be gated on lark-oapi (never installed in CI).
# The Feishu SDK request builders are the external boundary: fake them so the
# adapter logic around them runs on every lane.
# ---------------------------------------------------------------------------


class _FakeSdkBuilder:
    """Stands in for a lark-oapi ``*.builder()``: every setter records its value."""

    def __init__(self):
        self._fields = {}

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def _set(value):
            self._fields[name] = value
            return self

        return _set

    def build(self):
        return SimpleNamespace(**self._fields)


class _FakeSdkRequestType:
    @staticmethod
    def builder():
        return _FakeSdkBuilder()


@pytest.fixture
def fake_lark_requests(monkeypatch):
    """Fake the lazily imported lark-oapi request modules and the raw tenant GET builder."""
    import sys
    import types

    import plugins.platforms.feishu.adapter as feishu_mod

    for parent in ("lark_oapi", "lark_oapi.api", "lark_oapi.api.im", "lark_oapi.api.contact"):
        if parent not in sys.modules:
            monkeypatch.setitem(sys.modules, parent, types.ModuleType(parent))
    im_v1 = types.ModuleType("lark_oapi.api.im.v1")
    for name in ("CreateMessageReactionRequest", "CreateMessageReactionRequestBody", "DeleteMessageReactionRequest"):
        setattr(im_v1, name, _FakeSdkRequestType)
    contact_v3 = types.ModuleType("lark_oapi.api.contact.v3")
    contact_v3.GetUserRequest = _FakeSdkRequestType
    monkeypatch.setitem(sys.modules, "lark_oapi.api.im.v1", im_v1)
    monkeypatch.setitem(sys.modules, "lark_oapi.api.contact.v3", contact_v3)
    monkeypatch.setattr(
        feishu_mod, "_tenant_get_request",
        lambda uri, *, queries=None: SimpleNamespace(uri=uri, queries=queries),
    )


def _plain_feishu_adapter():
    from gateway.config import PlatformConfig
    from plugins.platforms.feishu.adapter import FeishuAdapter

    return FeishuAdapter(PlatformConfig())


def _bot_info_response(open_id, bot_name):
    payload = json.dumps({"code": 0, "bot": {"bot_name": bot_name, "open_id": open_id}}).encode("utf-8")
    return SimpleNamespace(raw=SimpleNamespace(content=payload))


def test_hydrated_bot_identity_wins_over_stale_env_values(fake_lark_requests, monkeypatch):
    """#16993: /bot/v3/info runs even when FEISHU_BOT_* are configured, and the hydrated identity
    replaces the env values so a stale id from an old app registration can't break @mention gating."""
    monkeypatch.setenv("FEISHU_BOT_OPEN_ID", "ou_env")
    monkeypatch.setenv("FEISHU_BOT_NAME", "Env Hermes")
    adapter = _plain_feishu_adapter()
    assert adapter._bot_open_id == "ou_env"
    requests = []

    def _request(req):
        requests.append(req)
        return _bot_info_response("ou_hydrated", "Hydrated Hermes")

    adapter._client = SimpleNamespace(request=_request)

    asyncio.run(adapter._hydrate_bot_identity())

    assert [r.uri for r in requests] == ["/open-apis/bot/v3/info"]
    assert adapter._bot_open_id == "ou_hydrated"
    assert adapter._bot_name == "Hydrated Hermes"


def test_bot_sender_name_is_fetched_via_basic_batch_and_cached(fake_lark_requests):
    """Bot senders resolve via bot/v3/bots/basic_batch (the contact API has no bot names) with a
    repeated ``bot_ids`` query param, and the result is cached so the next lookup is free."""
    adapter = _plain_feishu_adapter()
    requests = []
    body = {"code": 0, "msg": "", "data": {"bots": {"ou_peer": {"bot_id": "ou_peer", "name": "Peer Bot"}}}}

    def _request(req):
        requests.append(req)
        return SimpleNamespace(raw=SimpleNamespace(content=json.dumps(body).encode()))

    adapter._client = SimpleNamespace(request=_request)

    assert asyncio.run(adapter._resolve_sender_name_from_api("ou_peer", is_bot=True)) == "Peer Bot"
    assert asyncio.run(adapter._resolve_sender_name_from_api("ou_peer", is_bot=True)) == "Peer Bot"

    assert len(requests) == 1, "second lookup must hit the cache"
    assert requests[0].uri == "/open-apis/bot/v3/bots/basic_batch"
    # Feishu expects repeated ?bot_ids= params, not comma-joined.
    assert requests[0].queries == [("bot_ids", "ou_peer")]


def test_human_sender_name_is_fetched_from_contact_api_and_cached(fake_lark_requests):
    adapter = _plain_feishu_adapter()
    lookups = []

    def _get(request):
        lookups.append(request)
        user = SimpleNamespace(name="Bob", display_name=None, nickname=None, en_name=None)
        return SimpleNamespace(success=lambda: True, data=SimpleNamespace(user=user))

    adapter._client = SimpleNamespace(contact=SimpleNamespace(v3=SimpleNamespace(user=SimpleNamespace(get=_get))))

    assert asyncio.run(adapter._resolve_sender_name_from_api("ou_bob")) == "Bob"
    assert asyncio.run(adapter._resolve_sender_name_from_api("ou_bob")) == "Bob"

    assert len(lookups) == 1, "second lookup must hit the cache"
    assert (lookups[0].user_id, lookups[0].user_id_type) == ("ou_bob", "open_id")


def _reaction_adapter(*, delete_success=True):
    adapter = _plain_feishu_adapter()
    tracker = SimpleNamespace(created=[], deleted=[])

    def _create(request):
        tracker.created.append(request.request_body.reaction_type["emoji_type"])
        return SimpleNamespace(success=lambda: True, data=SimpleNamespace(reaction_id="r_typing"))

    def _delete(request):
        tracker.deleted.append(request.reaction_id)
        return SimpleNamespace(success=lambda: delete_success, code=0 if delete_success else 99, msg="")

    adapter._client = SimpleNamespace(
        im=SimpleNamespace(v1=SimpleNamespace(message_reaction=SimpleNamespace(create=_create, delete=_delete)))
    )
    return adapter, tracker


def _run_processing(adapter, outcome):
    event = MessageEvent(
        text="hello", message_type=MessageType.TEXT, message_id="om_msg",
        source=SessionSource(platform=Platform.FEISHU, chat_id="oc_test"))
    asyncio.run(adapter.on_processing_start(event))
    asyncio.run(adapter.on_processing_complete(event, outcome))


def test_processing_success_removes_typing_and_adds_nothing(fake_lark_requests, monkeypatch):
    monkeypatch.delenv("FEISHU_REACTIONS", raising=False)
    adapter, tracker = _reaction_adapter()
    _run_processing(adapter, ProcessingOutcome.SUCCESS)
    assert tracker.created == ["Typing"]
    assert tracker.deleted == ["r_typing"]
    assert "om_msg" not in adapter._pending_processing_reactions


def test_processing_failure_swaps_typing_for_cross_mark(fake_lark_requests, monkeypatch):
    monkeypatch.delenv("FEISHU_REACTIONS", raising=False)
    adapter, tracker = _reaction_adapter()
    _run_processing(adapter, ProcessingOutcome.FAILURE)
    assert tracker.created == ["Typing", "CrossMark"]
    assert tracker.deleted == ["r_typing"]


def test_processing_failure_skips_cross_mark_when_typing_removal_fails(fake_lark_requests, monkeypatch):
    """A Typing badge we couldn't remove must not get a CrossMark stacked next to it (the UI would
    read as both working and failed); the handle is kept for LRU eviction."""
    monkeypatch.delenv("FEISHU_REACTIONS", raising=False)
    adapter, tracker = _reaction_adapter(delete_success=False)
    _run_processing(adapter, ProcessingOutcome.FAILURE)
    assert tracker.created == ["Typing"]
    assert tracker.deleted == ["r_typing"]
    assert adapter._pending_processing_reactions["om_msg"] == "r_typing"

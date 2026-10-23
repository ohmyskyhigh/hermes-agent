"""Optional plugin policy at the gateway's conversational egress boundary.

Plugins own classification. Prepare opts into buffering; final decides delivery without
rewriting the body or the persisted assistant turn. Invalid/no decisions fail open.
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


def _field(obj, name, default=None):
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def reply_policy_context(event, text=None):
    source = event.source
    raw = getattr(event, "raw_message", None)
    sender = _field(_field(raw, "event", raw), "sender")
    open_id = _field(_field(sender, "sender_id"), "open_id")
    from hermes_cli.profiles import get_active_profile_name
    context = {
        "platform": str(getattr(source.platform, "value", source.platform)),
        "chat_type": getattr(source, "chat_type", "") or "",
        "profile": getattr(source, "profile", None) or get_active_profile_name() or "default",
        "chat_id": source.chat_id,
        "sender_id": getattr(source, "user_id", None),
        "sender_name": getattr(source, "user_name", None),
        "sender_is_bot": bool(getattr(source, "is_bot", False) or
                              _field(sender, "sender_type") in ("app", "bot")),
        "message_id": getattr(event, "message_id", None),
        "sender_open_id": open_id if isinstance(open_id, str) else None,
        "has_media": bool(getattr(event, "media_urls", None) or
                          (text is not None and ("MEDIA:" in text or "![" in text))),
    }
    if text is not None:
        context["text"] = text
    return context


def _decision(phase, context):
    from hermes_cli.plugins import invoke_hook
    try:
        results = invoke_hook("gateway_reply_policy", phase=phase, context=context)
    except Exception:
        logger.exception("Gateway reply policy failed; leaving delivery unchanged")
        return None
    for result in results:
        if not isinstance(result, dict):
            continue
        if phase == "prepare":
            if result.get("buffer") is True:
                return {"buffer": True}
        elif result.get("action") in ("send", "suppress", "defer"):
            mentions = result.get("mentions", [])
            if not isinstance(mentions, list) or any(
                not isinstance(m, dict) or not isinstance(m.get("id"), str) or not m["id"]
                or not isinstance(m.get("name", ""), str) for m in mentions
            ):
                continue
            return {"action": result["action"], "mentions": [dict(m) for m in mentions],
                    "reason": str(result.get("reason", ""))}
    return None


async def prepare_reply_policy(event):
    if not hasattr(event, "_reply_policy_prepare"):
        event._reply_policy_prepare = await asyncio.to_thread(
            _decision, "prepare", reply_policy_context(event))
    return bool((event._reply_policy_prepare or {}).get("buffer"))


async def finalize_reply_policy(event, result, text):
    if any(result.get(k) for k in ("failed", "error", "interrupted", "partial", "failure_reason")) or result.get("completed") is False:
        return None
    if not hasattr(event, "_reply_policy_final"):
        policy_event = getattr(event, "_reply_policy_event", event)
        event._reply_policy_final = await asyncio.to_thread(
            _decision, "final", reply_policy_context(policy_event, text or ""))
        decision = event._reply_policy_final
        if decision and decision["action"] == "send" and decision["mentions"]:
            event._reply_policy_mentions = decision["mentions"]
        if decision and decision["action"] == "defer":
            logger.warning("Gateway reply blocked/deferred: platform=%s chat=%s message=%s reason=%s",
                           event.source.platform, event.source.chat_id, event.message_id, decision["reason"])
    return event._reply_policy_final


def reply_policy_metadata(event, metadata=None):
    if getattr(event, "_reply_policy_text_delivered", False):
        return without_reply_mentions(metadata)
    mentions = getattr(event, "_reply_policy_mentions", None)
    return {**(metadata or {}), "mentions": mentions} if mentions else metadata


async def send_policy_text_first(adapter, event, text, metadata, send_text):
    """Mention once on the first text, before audio/attachments. Retain on send failure."""
    if getattr(event, "_reply_policy_text_delivered", False):
        return True
    if not metadata or not metadata.get("mentions"):
        return False
    if not text.strip():
        return False
    succeeded = False
    def record(result):
        nonlocal succeeded
        succeeded = bool(getattr(result, "success", False))
    await send_text(text, metadata, record)
    if not succeeded:
        raise RuntimeError("Reply policy recipient text delivery failed; media withheld")
    event._reply_policy_text_delivered = True
    return True


async def policy_text_before_voice(runner, event, response, adapter):
    from gateway.run import _strip_response_attachments_for_direct_send
    from gateway.platforms.base import _thread_metadata_for_event
    text = _strip_response_attachments_for_direct_send(response, adapter)
    metadata = {**(_thread_metadata_for_event(event) or {}), "notify": True}
    async def send(text, metadata, record):
        record(await adapter._send_with_retry(event.source.chat_id, text,
            reply_to=runner._reply_anchor_for_event(event), metadata=metadata))
    return await send_policy_text_first(adapter, event, text, metadata, send)


def without_reply_mentions(metadata):
    if not metadata or "mentions" not in metadata:
        return metadata
    return {k: v for k, v in metadata.items() if k != "mentions"}

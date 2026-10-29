"""List/recover received Feishu attachments in the calling session's scope."""
import json
from pathlib import Path
import re

from tools.registry import registry

_PERMANENT = frozenset(("permission", "not_found", "too_large", "size_limit"))


def _error(kind, retryable=False):
    return json.dumps({"success": False, "error_kind": kind, "retryable": retryable})


def _settings():
    from agent.secret_scope import current_secret_scope_home
    from gateway.config import Platform, load_gateway_config
    from hermes_constants import get_hermes_home
    from plugins.platforms.feishu.adapter import FeishuAdapter

    owner = current_secret_scope_home()
    if owner and Path(owner).resolve() != get_hermes_home().resolve():
        raise ValueError("Feishu secret scope does not own this home")
    platform = load_gateway_config().platforms.get(Platform.FEISHU)
    if platform is None or not platform.enabled:
        raise ValueError("Feishu is not enabled")
    settings = FeishuAdapter._load_settings(platform.extra)
    if not settings.app_id or not settings.app_secret:
        raise ValueError("Feishu credentials are unavailable")
    return settings


def _public(record):
    result = record["result"]
    return {"id": record["attachment_id"], "filename": record["ref"]["file_name"],
            "state": result["state"], "path": result.get("path", ""),
            "mime_type": result.get("mime_type", ""), "size_bytes": result.get("size_bytes", 0),
            "error_kind": result.get("error_kind", ""), "error_code": result.get("error_code", ""),
            "retryable": _retryable(result)}


def _retryable(result):
    if result.get("error_kind") in _PERMANENT or result["state"] == "ready":
        return False
    return result["state"] == "pending" or result.get("retryable") is True


def _max_bytes():
    from gateway.platforms.base import get_inbound_media_max_bytes

    ceiling = 512 * 1024 * 1024
    value = get_inbound_media_max_bytes()
    return min(value, ceiling) if type(value) is int and value > 0 else ceiling


def _recover(store, record, scope, settings):
    if not _retryable(record["result"]):
        return record
    from plugins.platforms.feishu import adapter
    from plugins.platforms.feishu.inbound_resources import download_resource

    ref = {**record["ref"], "state": "pending"}
    ref.pop("error_kind", None)
    store.save(ref, {"state": "pending"})
    try:
        if not adapter._load_lark_oapi():
            store.save({**ref, "state": "failed"}, {"state": "failed", "error_kind": "sdk_unavailable", "retryable": True})
            return store.get(record["attachment_id"], **scope)
        client = adapter._build_lark_client(settings.app_id, settings.app_secret, adapter._sdk_domain(settings.domain_name))
        result = download_resource(
            client, message_id=ref["message_id"], file_key=ref["file_key"],
            resource_type=ref["resource_type"], filename=ref["file_name"], media_hint=ref["media_hint"],
            cache_dir=store.cache_dir, max_bytes=_max_bytes())
        store.save({**ref, "state": result["state"]}, result)
    except Exception:  # health: allow BLE001 -- SDK exceptions can contain secrets; persist only a fixed failure code
        store.save({**ref, "state": "failed"}, {"state": "failed", "error_kind": "download_failed", "retryable": True})
    return store.get(record["attachment_id"], **scope)


def feishu_attachment(action="list", id=None, limit=10) -> str:
    if (action not in ("list", "recover") or type(limit) is not int or limit < 1
            or (id is not None and (type(id) is not str or not re.fullmatch(r"[0-9a-f]{64}", id)))):
        return _error("invalid_arguments")
    from gateway.session_context import get_session_env

    if (get_session_env("HERMES_SESSION_PLATFORM") != "feishu"
            or not get_session_env("HERMES_SESSION_CHAT_ID")):
        return _error("invalid_context")
    from hermes_constants import get_hermes_home
    from plugins.platforms.feishu.attachment_store import AttachmentStore

    try:
        settings = _settings()
    except Exception:  # health: allow BLE001 -- redact arbitrary credential-bearing SDK/config failures
        # SDK/config exceptions may contain credential values; never echo them.
        return _error("configuration_unavailable")
    scope = dict(app_id=settings.app_id, chat_id=get_session_env("HERMES_SESSION_CHAT_ID"),
                 thread_id=get_session_env("HERMES_SESSION_THREAD_ID"))
    try:
        store = AttachmentStore(get_hermes_home())
        return _perform(action, id, limit, settings, scope, store)
    except Exception:  # health: allow BLE001 -- tool boundary exposes only a fixed failure code, never private storage data
        return _error("storage_unavailable", True)


def _perform(action, id, limit, settings, scope, store):
    if action == "list":
        records = store.list(**scope, limit=limit)
        return json.dumps({"success": True, "attachments": [_public(record) for record in records]})
    if id is None:
        records = store.list(**scope, limit=1)
        record = records[0] if records else None
    else:
        record = store.get(id, **scope)
    if record is None:
        return _error("not_found")
    record = _recover(store, record, scope, settings)
    return json.dumps({"success": record["result"]["state"] == "ready", "attachment": _public(record)})


def _handle(args, **kwargs):
    if type(args) is not dict or args.keys() - {"action", "id", "limit"}:
        return _error("invalid_arguments")
    return feishu_attachment(**args)


def check_requirements():
    from tools.feishu_lark import _check_feishu
    return _check_feishu()


registry.register(
    name="feishu_attachment", toolset="feishu_attachment",
    schema={"name": "feishu_attachment", "description": (
        "List or recover attachments already received in this Feishu conversation and topic. "
        "Recovery without an id uses the latest received attachment. Does not search other chats or messages."),
        "parameters": {"type": "object", "additionalProperties": False,
                       "properties": {
                           "action": {"type": "string", "enum": ["list", "recover"]},
                           "id": {"type": "string", "pattern": "^[0-9a-f]{64}$",
                                  "description": "Received attachment id returned by list; omit for latest."},
                           "limit": {"type": "integer", "minimum": 1, "maximum": 20,
                                     "default": 10, "description": "Maximum list results."}},
                       "required": ["action"]}},
    handler=_handle, check_fn=check_requirements,
)

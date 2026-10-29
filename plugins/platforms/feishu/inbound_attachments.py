"""Feishu inbound receipts: persist references before attempting any I/O."""
from __future__ import annotations

import asyncio


def resource_limit() -> int:
    from gateway.platforms.base import get_inbound_media_max_bytes
    configured = get_inbound_media_max_bytes()
    # Use the same finite ceiling as recovery, even when other platforms disable it.
    ceiling = 512 * 1024 * 1024
    return min(configured, ceiling) if type(configured) is int and configured > 0 else ceiling


async def receive_attachments(adapter, message, normalized, sender_id, receipt_sink):
    from gateway.attachment_refs import MAX_ATTACHMENT_REFS, validate_attachment_refs
    from hermes_constants import get_hermes_home
    from plugins.platforms.feishu.attachment_store import AttachmentStore

    resources = [(key, "", "image", "image") for key in normalized.image_keys]
    resources.extend((ref.file_key, ref.file_name, "file", ref.media_hint) for ref in normalized.media_refs)
    if not resources:
        return [], [], ""
    thread_id = getattr(message, "thread_id", None)
    base = {"v": 1, "platform": "feishu", "app_id": adapter._app_id,
            "message_id": message.message_id, "chat_id": getattr(message, "chat_id", ""),
            "thread_id": "" if thread_id is None else thread_id, "sender_id": sender_id}
    refs, budget_refs, notes = [], [], []
    for index, (key, name, kind, hint) in enumerate(resources[:MAX_ATTACHMENT_REFS], 1):
        candidate = dict(base, file_key=key, file_name=name, resource_type=kind, media_hint=hint, state="pending")
        try:
            ref = validate_attachment_refs([candidate])[0]
        except ValueError:
            # Omit only invalid display metadata. Never trim, coerce or repair IDs.
            try:
                ref = validate_attachment_refs([dict(candidate, file_name="")])[0]
            except ValueError:
                notes.append(f"Attachment {index}: rejected invalid resource metadata; not downloaded.")
                continue
        # Reserve the largest permitted failure classification before the first I/O.
        # A later status update must not push the durable event over its JSON cap.
        budget_ref = dict(ref, state="failed", error_kind="x" * 64)
        try:
            validate_attachment_refs([*budget_refs, budget_ref])
        except ValueError:
            notes.append(f"Attachments {index}-{min(len(resources), MAX_ATTACHMENT_REFS)}: "
                         "64-KiB receipt JSON limit reached; remaining items were not recorded or downloaded.")
            break
        budget_refs.append(budget_ref)
        if ref["file_name"] != name:
            notes.append(f"Attachment {index}: unsupported filename metadata omitted; resource identity retained.")
        refs.append(ref)
    receipt_sink.extend(refs)
    store = AttachmentStore(get_hermes_home())
    # Save the whole receipt set first: a process interruption must not erase the later items.
    ids = [await asyncio.to_thread(store.save, ref) for ref in refs]
    paths, types = [], []
    remaining = resource_limit()
    for ref, attachment_id in zip(refs, ids):
        if remaining <= 0:
            result = {"state": "failed", "error_kind": "too_large", "retryable": False,
                      "path": "", "size_bytes": 0}
        else:
            result = await adapter._download_attachment_result(
                message_id=ref["message_id"], file_key=ref["file_key"], resource_type=ref["resource_type"],
                filename=ref["file_name"], media_hint=ref["media_hint"], max_bytes=remaining)
        ref["state"] = result["state"]
        if result["state"] == "ready":
            paths.append(result["path"])
            types.append(result["mime_type"])
            remaining -= result["size_bytes"]
        else:
            ref["error_kind"] = result.get("error_kind") or "download_failed"
            notes.append(f"Attachment {attachment_id}: received but not downloaded ({ref['error_kind']}).")
        await asyncio.to_thread(store.save, ref, result)
    if len(resources) > MAX_ATTACHMENT_REFS:
        notes.append("This message exceeds the 64-attachment receipt limit; excess items were not recorded or downloaded.")
    if notes:
        notes.append("Use feishu_attachment action=list/recover in this conversation to inspect or recover the original. "
                     "Do not ask for re-upload before checking recovery. A receipt is not a downloaded or analyzed file.")
    return paths, types, "\n".join(notes)

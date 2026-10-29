"""Platform-specific routing metadata for outbound replies."""

def thread_metadata_for_source(source, reply_to_message_id: str | None = None) -> dict | None:
    """Platform-aware thread metadata for adapter sends. Telegram DM topics route with
    ``message_thread_id`` + a reply anchor; anchorless synthetic/resumed sends fall back to
    ``direct_messages_topic_id`` when supported."""
    from gateway.platforms.base import _platform_name

    thread_id = getattr(source, "thread_id", None)
    platform = _platform_name(getattr(source, "platform", None))
    metadata = {"thread_id": thread_id} if thread_id is not None else {}
    # Slack workspace identity is routing state: carry it so a multi-workspace Socket Mode
    # gateway never falls back to its primary WebClient.
    scope_id = getattr(source, "scope_id", None) if platform == "slack" else None
    if scope_id:
        metadata["slack_team_id"] = str(scope_id)
    if not metadata:
        return None
    if platform == "telegram" and getattr(source, "chat_type", None) == "dm":
        metadata["telegram_dm_topic_reply_fallback"] = True
        if str(thread_id) not in {"", "1"}:
            metadata["direct_messages_topic_id"] = str(thread_id)
        anchor = reply_to_message_id or getattr(source, "message_id", None)
        if anchor is not None:
            metadata["telegram_reply_to_message_id"] = str(anchor)
    if platform == "feishu" and thread_id is not None and reply_to_message_id is not None:
        # Attachments are delivered after text through metadata-only helpers. Preserve the
        # concrete message anchor so Feishu can use ReplyMessage(reply_in_thread=True)
        # without requiring the bot to list every message in the topic.
        metadata["reply_to_message_id"] = str(reply_to_message_id)
    # Routed profile (multiplex / profile_routes): outbound prune paths must not assume the
    # adapter's static profile stamp.
    profile = str(getattr(source, "profile", None) or "").strip()
    if profile:
        metadata["hermes_profile"] = profile
    return metadata



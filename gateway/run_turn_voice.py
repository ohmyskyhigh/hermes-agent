"""Streaming voice setup for a gateway turn."""
from __future__ import annotations

import logging
from typing import Any, Optional
from gateway.session import SessionSource

logger = logging.getLogger("gateway.run")
def start_streaming_tts(
    self, source: SessionSource, message_type: Optional[str],
    _status_thread_metadata: Optional[dict[str, Any]], streaming_tts_consumer_holder: list,
) -> None:
    """Start the streaming-TTS consumer for a voice-input turn on an auto-TTS chat.

    Created on the gateway loop thread (not run_sync's executor); an inactive consumer leaves
    the holder None so the whole-file fallback path runs."""
    # Skip when streaming TTS already delivered audio for this turn (#60671).
    # This avoids a cross-scope NameError: the outer interrupt / finalisation paths reference the
    # consumer via ``streaming_tts_consumer_holder[0]``. Gates: voice input, auto-TTS enabled for this
    # chat, adapter supports streaming, and a usable streaming TTS provider configured. See #60671.
    from gateway.run_turn import asyncio
    _stts_adapter = self._delivery_adapter_for(source)
    _is_voice_input = (
        message_type is not None
        and str(getattr(message_type, "value", message_type)).lower() == "voice"
    )
    if _stts_adapter is None or not _is_voice_input or not _stts_adapter._should_auto_tts_for_chat(source.chat_id):
        return
    try:
        from gateway.streaming_tts_consumer import StreamingTTSConsumer
        from tools.tts_tool import _load_tts_config
        _stts_consumer = StreamingTTSConsumer(
            adapter=_stts_adapter, chat_id=source.chat_id, tts_config=_load_tts_config(),
            loop=self._gateway_loop or asyncio.get_event_loop(),
            metadata=_status_thread_metadata,
        )
        if _stts_consumer.active:
            streaming_tts_consumer_holder[0] = _stts_consumer
            _stts_consumer.start()
    except Exception as _stts_err:
        logger.exception("Could not set up streaming TTS consumer: %s", _stts_err)

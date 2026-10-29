"""Per-turn constrained dispatch, used by paused Campaign conversations.

The scope travels through existing context-preserving worker boundaries. Tool schemas
and the cached system prompt stay unchanged; a constrained call never reaches a shell.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import json
import logging

_handler = ContextVar('constrained_tool_dispatch', default=None)


@contextmanager
def constrained_tools(handler):
    token = _handler.set(handler)
    try:
        yield
    finally:
        _handler.reset(token)


def constrained_result(name, args):
    handler = _handler.get()
    if handler is None:
        return None
    try:
        result = handler(name, args)
        if not isinstance(result, str):
            raise ValueError('constrained dispatch must return a tool result')
        return result
    except Exception:
        # An unavailable constraint must never turn into permission to run the tool.
        logging.getLogger(__name__).exception('Constrained Campaign tool query failed; normal dispatch refused')
        return json.dumps({'error': 'Paused Campaign dialogue: tool was not executed; constrained query failed.'})


def pre_tool_block_message(agent, function_name, function_args, effective_task_id, tool_call_id, middleware_trace):
    """Existing plugin verdict for ordinary turns; a constrained turn never reaches this hook."""
    try:
        from hermes_cli.plugins import _dispatch_pre_tool_call_hooks
        block_message, modified_args = _dispatch_pre_tool_call_hooks(
            function_name, function_args, task_id=effective_task_id or '',
            session_id=getattr(agent, 'session_id', '') or '', tool_call_id=tool_call_id or '',
            turn_id=getattr(agent, '_current_turn_id', '') or '',
            api_request_id=getattr(agent, '_current_api_request_id', '') or '',
            middleware_trace=list(middleware_trace),
        )
        return block_message, (modified_args if modified_args is not None else function_args)
    except Exception:
        logging.getLogger(__name__).debug('Plugin pre-tool hook failed', exc_info=True)
        return None, function_args

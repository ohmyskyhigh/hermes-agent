"""Reliable task body from Feishu native text/post nodes, independent of receiving bot."""
import json


def native_task_text(message):
    try:
        content = json.loads(message.content)
    except (ValueError, TypeError, AttributeError):
        return None
    kind = getattr(message, 'message_type', None)
    if kind == 'text':
        text = content.get('text', '')
        if not isinstance(text, str):
            return None
        for mention in getattr(message, 'mentions', None) or []:
            key = getattr(mention, 'key', None)
            if isinstance(key, str) and key:
                text = text.replace(key, '')
        return text.strip()
    if kind == 'post':
        post = content if 'content' in content else next(
            (value for value in content.values() if isinstance(value, dict) and 'content' in value), {})
        parts = [post.get('title', '')]
        for row in post.get('content', []):
            parts.append(''.join(node.get('text', '') for node in row if node.get('tag') in {'text', 'a'}))
        return '\n'.join(parts).strip()
    return None

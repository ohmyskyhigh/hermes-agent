"""Encode explicit, sender-app-scoped recipients in Feishu text/post payloads.

This module knows the protocol, not whether a conversation should continue.
"""
from __future__ import annotations

import html
import json
import re


def add_recipient_mentions(msg_type: str, payload: str, mentions) -> str:
    if not mentions:
        return payload
    if not isinstance(mentions, (list, tuple)):
        raise ValueError('Feishu mentions must be a recipient list')
    recipients = {}
    for item in mentions:
        ident = item.get('id') if isinstance(item, dict) else None
        if not isinstance(ident, str) or not re.fullmatch(r'ou_[A-Za-z0-9_-]+', ident):
            raise ValueError('Feishu mention requires a resolved app-scoped open_id, not an app ID or display name')
        recipients.setdefault(ident, str(item.get('name') or ident))
    data = json.loads(payload)
    if msg_type == 'text':
        text = data['text']
        prefix = ' '.join(
            f'<at user_id="{ident}">{html.escape(name)}</at>'
            for ident, name in recipients.items()
            if not re.search(r'<at\s+user_id=([\"\x27])' + re.escape(ident) + r'\1\s*>[^<]*</at>', text)
        )
        if prefix:
            data['text'] = prefix + '\n' + text
    elif msg_type == 'post':
        for locale in data.values():
            rows = locale['content']
            existing = {el.get('user_id') for row in rows for el in row if el.get('tag') == 'at'}
            prefix = [{'tag': 'at', 'user_id': ident} for ident in recipients if ident not in existing]
            if prefix:
                rows.insert(0, prefix)
    else:
        raise ValueError('Recipient mentions require a text or post message')
    return json.dumps(data, ensure_ascii=False)

"""Opt-in Feishu group reply policy. No model calls, tools, or network I/O.

Conservative full-message courtesy rules, not a semantic task-state engine.
Unknown substantive messages are delivered; unresolved recipients are deferred.
"""
import re

_COURTESY = re.compile(
    r'(?:收到|已收到|已了解|了解|明白|好的|好|谢谢|谢谢你|感谢|不客气|辛苦了|'
    r'知道了|我知道了|确认收到|'
    r'ok|okay|thanks|thankyou|yourewelcome|acknowledged)+'
)
_CLOSURE = re.compile(r'(?:验收通过|无需返工|无需回复|这件事结束了|任务已关闭)+')
_PROGRESS = re.compile(
    r'(?:进度[：:]|进度更新[：:])?(?:资料整理中|正在整理资料|正在检索资料|正在执行|处理中|测试中|编译中)'
    r'(?:[，,；; ]*(?:暂无阻塞|没有阻塞|无需协助|无需回复))*[。.!！]*'
)


def gateway_reply_policy(*, phase, context, **kwargs):
    if context.get('platform') != 'feishu' or context.get('chat_type') not in {'group', 'forum', 'thread'}:
        return None
    if phase == 'prepare':
        return {'buffer': True}
    if phase != 'final':
        return None
    text = str(context.get('text') or '').strip()
    name = str(context.get('sender_name') or '')
    if name and text.startswith('@' + name):
        text = text[len(name) + 1:].lstrip()
    # Do not remove question marks or arbitrary words: action-bearing replies must survive.
    courtesy = re.sub(r'[\s，,。.!！、；;：:*_\u200b]', '', text).lower()
    if not context.get('has_media') and (
        courtesy in {'[silent]', 'silent', 'noreply'} or _COURTESY.fullmatch(courtesy)
    ):
        return {'action': 'suppress', 'reason': 'courtesy_or_terminal_ack'}
    # A bot sender is not an implicit next-task recipient. Explicit native body
    # mentions remain intact; useful results must not be discarded to stop pings.
    if context.get('sender_is_bot'):
        return {'action': 'send', 'mentions': [], 'reason': 'explicit_recipient_required_for_bot'}
    if not context.get('has_media') and (_PROGRESS.fullmatch(text) or _CLOSURE.fullmatch(courtesy)):
        return {'action': 'send', 'mentions': [], 'reason': 'non_actionable_status'}
    ident = context.get('sender_open_id')
    if not ident and str(context.get('sender_id') or '').startswith('ou_'):
        ident = context['sender_id']
    if not isinstance(ident, str) or not re.fullmatch(r'ou_[A-Za-z0-9_-]+', ident):
        return {'action': 'defer', 'reason': 'recipient_unresolved'}
    return {
        'action': 'send',
        'mentions': [{'id': ident, 'name': name or ident}],
        'reason': 'directed_reply',
    }


def register(ctx):
    ctx.register_hook('gateway_reply_policy', gateway_reply_policy)

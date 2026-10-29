"""Closed, credential-free resource references; no adapter or SDK dependencies."""
import json
import re


_REQUIRED_FIELDS = (
    'v', 'platform', 'app_id', 'message_id', 'file_key', 'chat_id', 'thread_id',
    'sender_id', 'resource_type', 'media_hint', 'file_name', 'state',
)
_FIELDS = (*_REQUIRED_FIELDS, 'error_kind')
MAX_ATTACHMENT_REFS = 64
MAX_ATTACHMENT_REFS_BYTES = 64 * 1024
_IDENTIFIER_FIELDS = ('app_id', 'message_id', 'file_key', 'chat_id', 'thread_id', 'sender_id')
_IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,511}')
_ERROR_KIND = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}')
_CHOICES = {
    'platform': ('feishu',),
    'resource_type': ('image', 'file'),
    'media_hint': ('image', 'video', 'audio', 'document'),
    'state': ('pending', 'ready', 'failed'),
}


def _validate_ref(ref):
    if (type(ref) is not dict or not set(_REQUIRED_FIELDS) <= ref.keys()
            or not ref.keys() <= set(_FIELDS)):
        raise ValueError('attachment_refs: invalid fields')
    if type(ref['v']) is not int or ref['v'] != 1:
        raise ValueError('attachment_refs: invalid version')
    if any(type(ref[name]) is not str for name in ref if name != 'v'):
        raise ValueError('attachment_refs: expected string fields')
    for name, choices in _CHOICES.items():
        if ref[name] not in choices:
            raise ValueError(f'attachment_refs: invalid {name}')
    for name in _IDENTIFIER_FIELDS:
        if name == 'thread_id' and ref[name] == '':
            continue
        if not _IDENTIFIER.fullmatch(ref[name]):
            raise ValueError(f'attachment_refs: invalid {name}')
    filename = ref['file_name']
    if (len(filename) > 255 or filename in ('.', '..')
            or any(c in filename for c in '/\\')
            or re.match(r'\s*[A-Za-z]:', filename)
            or any(not c.isprintable() for c in filename)):
        raise ValueError('attachment_refs: invalid file_name')
    if 'error_kind' in ref and not _ERROR_KIND.fullmatch(ref['error_kind']):
        raise ValueError('attachment_refs: invalid error_kind')
    # All leaves are immutable primitives; constructing each dict makes a deep copy.
    return {name: ref[name] for name in _FIELDS if name in ref}


def validate_attachment_refs(refs, source=None) -> list[dict]:
    """Return plain independent descriptors; reject rather than repair identifiers.

    None represents no references. Every other input must be a list (max 64)
    of closed v1 descriptors, bounded to 64 KiB of compact UTF-8 JSON. IDs are
    1-512 ASCII letters/digits/underscore/hyphen, never trimmed or coerced.
    Filenames are printable display basenames (max 255 characters; empty is
    allowed). Colons are display text, except in drive-prefixed path forms.
    error_kind is a classification of at most 64 characters, not SDK error text.
    A supplied source binds the exact Feishu chat and thread; only None maps to
    the empty no-thread marker. Sender authorization belongs to the caller.
    """
    if refs is None:
        return []
    if type(refs) is not list:
        raise ValueError('attachment_refs: expected list')
    if len(refs) > MAX_ATTACHMENT_REFS:
        raise ValueError('attachment_refs: too many references')
    result = [_validate_ref(ref) for ref in refs]
    encoded = json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    if len(encoded.encode('utf-8')) > MAX_ATTACHMENT_REFS_BYTES:
        raise ValueError('attachment_refs: JSON budget exceeded')
    if source is not None and result:
        platform = getattr(source, 'platform', None)
        platform = getattr(platform, 'value', platform)
        chat_id = getattr(source, 'chat_id', None)
        thread_id = getattr(source, 'thread_id', None)
        thread_id = '' if thread_id is None else thread_id
        if platform != 'feishu' or any(
                ref['chat_id'] != chat_id or ref['thread_id'] != thread_id for ref in result):
            raise ValueError('attachment_refs: source mismatch')
    return result

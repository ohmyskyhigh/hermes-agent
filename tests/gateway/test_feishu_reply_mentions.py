"""Real Feishu serializer/transport tests; transport is local, no network."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from plugins.platforms.feishu.mentions import add_recipient_mentions
from gateway.config import PlatformConfig
from plugins.platforms.feishu.adapter import FeishuAdapter


def make_adapter():
    adapter = FeishuAdapter(PlatformConfig())
    adapter._client = object()
    return adapter


@pytest.mark.parametrize("text", [
    '<at user_id="ou_receiver"',
    '<at user_id="ou_receiver">receiver',
    '<at user_id="ou_receiver\'>receiver</at>',
])
def test_incomplete_native_mention_does_not_suppress_required_prefix(text):
    payload = add_recipient_mentions('text', json.dumps({'text': text}),
                                     [{'id': 'ou_receiver', 'name': 'Receiver'}])
    assert json.loads(payload)['text'] == '<at user_id="ou_receiver">Receiver</at>\n' + text


@pytest.mark.parametrize("text", [
    '<at user_id="ou_receiver">Receiver</at> answer',
    "answer <at user_id='ou_receiver'>Receiver</at>",
])
def test_complete_native_mention_is_not_duplicated(text):
    payload = add_recipient_mentions('text', json.dumps({'text': text}),
                                     [{'id': 'ou_receiver', 'name': 'Receiver'}])
    assert json.loads(payload)['text'] == text


@pytest.mark.asyncio
async def test_send_mentions_native_post_text_fallback_and_first_chunk_only():
    adapter = make_adapter()
    sent = []
    adapter.truncate_message = lambda text, length: ['**result one**', '**result two**']

    async def transport(**kwargs):
        sent.append(kwargs)
        if len(sent) == 1:
            return SimpleNamespace(success=lambda: False, code=230001, msg='content format of the post type is incorrect')
        return SimpleNamespace(success=lambda: True, data=SimpleNamespace(message_id='om_result'))

    adapter._feishu_send_with_retry = transport
    metadata = {'mentions': [{'id': 'ou_recipient', 'name': 'A & B'}], 'thread_id': 'omt_thread'}
    result = await adapter.send('oc_group', '**result**', reply_to='om_trigger', metadata=metadata)
    assert result.success
    first = json.loads(sent[0]['payload'])
    assert first['zh_cn']['content'][0][0] == {'tag': 'at', 'user_id': 'ou_recipient'}
    assert sent[1]['msg_type'] == 'text'
    assert '<at user_id="ou_recipient">A &amp; B</at>' in json.loads(sent[1]['payload'])['text']
    assert 'ou_recipient' not in sent[2]['payload']
    assert all(s['reply_to'] == 'om_trigger' for s in sent)
    assert metadata['mentions'][0]['id'] == 'ou_recipient'


@pytest.mark.asyncio
async def test_unmentioned_payload_and_invalid_recipient_are_not_silently_changed():
    adapter = make_adapter()
    adapter._feishu_send_with_retry = AsyncMock(return_value=SimpleNamespace(success=lambda: True, data=SimpleNamespace(message_id='om_dm')))
    result = await adapter.send('ou_dm', '普通私聊')
    assert result.success
    assert json.loads(adapter._feishu_send_with_retry.call_args.kwargs['payload']) == {'text': '普通私聊'}
    adapter._feishu_send_with_retry.reset_mock()
    result = await adapter.send('oc_group', '结果', metadata={'mentions': [{'id': 'cli_wrong_identity', 'name': 'wrong'}]})
    assert not result.success
    adapter._feishu_send_with_retry.assert_not_called()


@pytest.mark.asyncio
async def test_failed_mentioned_chunk_cannot_be_hidden_by_later_chunk_success():
    adapter = make_adapter()
    adapter.truncate_message = lambda text, length: ['first', 'second']
    adapter._feishu_send_with_retry = AsyncMock(side_effect=[
        SimpleNamespace(success=lambda: False, code=230002, msg='recipient not found'),
        SimpleNamespace(success=lambda: True, data=SimpleNamespace(message_id='om_later')),
    ])
    result = await adapter.send('oc_group', 'result', metadata={'mentions': [{'id': 'ou_receiver', 'name': 'receiver'}]})
    assert not result.success
    assert adapter._feishu_send_with_retry.call_count == 1

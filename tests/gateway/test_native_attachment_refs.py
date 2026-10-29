"""Durable resource references survive failed downloads without persisting SDK payloads."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import json

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from gateway.session_envelope import prepare_native, restore_native, snapshot_native
from hermes_state_runtime import RuntimeStoreError
from plugins.platforms.feishu.adapter import FeishuAdapter


def attachment_ref(**changes):
    return {
        'v': 1, 'platform': 'feishu', 'app_id': 'cli_fixture-app',
        'message_id': 'om_message-1', 'file_key': 'file_v3_fixture-1',
        'chat_id': 'oc_chat-1', 'thread_id': '', 'sender_id': 'ou_sender-1',
        'resource_type': 'file', 'media_hint': 'document', 'file_name': '报告.pdf',
        'state': 'failed', 'error_kind': 'download_failed', **changes,
    }


@pytest.fixture
def native_runner(tmp_path, monkeypatch):
    monkeypatch.setenv('FEISHU_ALLOWED_USERS', 'ou_sender-1')
    monkeypatch.setattr(GatewayRunner, '_VOICE_MODE_PATH', tmp_path / 'voice.json')
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / 'sessions'))
    runner.adapters = {Platform.FEISHU: FeishuAdapter(PlatformConfig(
        enabled=True, extra={'app_id': 'cli_fixture-app'}))}
    return runner


def source(**changes):
    return SessionSource(**{
        'platform': Platform.FEISHU, 'chat_id': 'oc_chat-1',
        'user_id': 'ou_sender-1', 'thread_id': None, **changes,
    })


def test_failed_download_reference_round_trips_without_media(native_runner):
    refs = [attachment_ref()]
    event = MessageEvent(text='read attachment', source=source(), message_id='om_message-1',
                         message_type=MessageType.DOCUMENT, attachment_refs=refs,
                         raw_message={'token': 'must-not-be-saved', 'sdk': object()})
    payload = snapshot_native(native_runner, event)
    stored = payload['native_text_v1']['event']['attachment_refs']
    assert stored == refs
    assert stored is not refs and stored[0] is not refs[0]
    assert 'media' not in payload['native_text_v1']
    assert 'raw_message' not in payload['native_text_v1']['event']
    refs[0]['state'] = 'ready'
    restored = restore_native(payload)
    assert restored.attachment_refs == [attachment_ref()]
    assert restored.media_urls == []
    assert restored.raw_message is None
    restored.attachment_refs[0]['file_name'] = 'changed.pdf'
    assert stored[0]['file_name'] == '报告.pdf'


@pytest.mark.parametrize('filename', ['meeting 10:30.pdf', 'notes:final.pdf', '报告：终稿.pdf', ' report.pdf '])
def test_display_filename_round_trips_literally(native_runner, filename):
    refs = [attachment_ref(file_name=filename)]
    payload = snapshot_native(native_runner, MessageEvent(text='files', source=source(), attachment_refs=refs))
    assert restore_native(payload).attachment_refs == refs


@pytest.mark.parametrize('changes', [
    {'token': 'secret'}, {'app_secret': 'secret'}, {'api_key': 'secret'},
    {'authorization': 'Bearer secret'}, {'raw_message': {'anything': 'payload'}},
    {'path': 'C:/private/file.pdf'}, {'v': True}, {'v': 2},
    {'platform': 'telegram'}, {'resource_type': 'video'}, {'media_hint': 'text'},
    {'state': 'downloaded'}, {'file_key': 'file/../../secret'},
    {'message_id': ' om_message-1'}, {'chat_id': 'oc_chat-1\n'},
    {'app_id': ''}, {'sender_id': 123}, {'thread_id': None},
    {'file_name': 'C:\\private\\report.pdf'}, {'file_name': '/private/report.pdf'},
    {'file_name': '../report.pdf'}, {'file_name': 'bad\x00name'},
    {'file_name': 'C:report.pdf'}, {'file_name': ' c:report.pdf'},
    {'file_name': '\\\\server\\share\\report.pdf'},
    {'file_name': '.'}, {'file_name': '..'}, {'file_name': 'bad\nname.pdf'},
    {'file_name': '\ud800'}, {'error_kind': {'error': 'payload'}},
    {'error_kind': 'Bearer credentials'}, {'error_kind': 'x' * 65},
    {'app_id': 'a' * 513}, {'message_id': 'm' * 513},
    {'chat_id': 'c' * 513}, {'thread_id': 't' * 513},
    {'sender_id': 's' * 513}, {'file_key': 'f' * 513}, {'file_name': 'x' * 256},
])
def test_closed_reference_schema_rejects_malformed_fields(native_runner, changes):
    from gateway.attachment_refs import validate_attachment_refs
    refs = [attachment_ref(**changes)]
    with pytest.raises(ValueError):
        validate_attachment_refs(refs)
    event = MessageEvent(text='read attachment', source=source(), attachment_refs=refs)
    with pytest.raises(RuntimeStoreError, match='invalid_params') as failure:
        snapshot_native(native_runner, event)
    assert failure.value.field == 'attachment_refs'


@pytest.mark.parametrize('refs', ['', {}, False, 0, (), [None], [1], [[]], [{}]])
def test_reference_container_is_not_silently_coerced(native_runner, refs):
    from gateway.attachment_refs import validate_attachment_refs
    with pytest.raises(ValueError):
        validate_attachment_refs(refs)
    event = MessageEvent(text='read attachment', source=source(), attachment_refs=refs)
    with pytest.raises(RuntimeStoreError, match='invalid_params'):
        snapshot_native(native_runner, event)


def test_reference_validation_returns_independent_plain_data():
    from gateway.attachment_refs import validate_attachment_refs
    refs = [attachment_ref()]
    result = validate_attachment_refs(refs)
    assert result == refs and result is not refs and result[0] is not refs[0]
    result[0]['state'] = 'ready'
    assert refs[0]['state'] == 'failed'
    assert validate_attachment_refs(None) == []
    assert validate_attachment_refs([]) == []
    without_error = attachment_ref(file_name='', resource_type='image', media_hint='image', state='pending')
    without_error.pop('error_kind')
    assert validate_attachment_refs([without_error]) == [without_error]


@pytest.mark.parametrize('missing', [
    'v', 'platform', 'app_id', 'message_id', 'file_key', 'chat_id', 'thread_id',
    'sender_id', 'resource_type', 'media_hint', 'file_name', 'state',
])
def test_reference_schema_requires_identity_and_status(missing):
    from gateway.attachment_refs import validate_attachment_refs
    ref = attachment_ref()
    del ref[missing]
    with pytest.raises(ValueError):
        validate_attachment_refs([ref])


@pytest.mark.parametrize('bound_source', [
    source(platform=Platform.TELEGRAM), source(chat_id='oc_other'),
    source(thread_id='om_other'), source(thread_id=0), source(chat_id='oc_chat-1 '),
])
def test_reference_source_binding_is_exact(native_runner, bound_source):
    from gateway.attachment_refs import validate_attachment_refs
    refs = [attachment_ref()]
    with pytest.raises(ValueError):
        validate_attachment_refs(refs, bound_source)
    # Keep the event authorized so refusal is the attachment binding, not auth.
    event = MessageEvent(text='attachment', source=source(), attachment_refs=refs)
    if bound_source.platform == Platform.FEISHU:
        event.source = bound_source
        with pytest.raises(RuntimeStoreError, match='invalid_params') as failure:
            snapshot_native(native_runner, event)
        assert failure.value.field == 'attachment_refs'


def test_reference_thread_binding_preserves_ids_without_sender_filtering():
    from gateway.attachment_refs import validate_attachment_refs
    refs = [attachment_ref(thread_id='omt_topic-1')]
    assert validate_attachment_refs(refs, source(thread_id='omt_topic-1', user_id='ou_other')) == refs
    assert validate_attachment_refs([attachment_ref()], source(thread_id='')) == [attachment_ref()]
    with pytest.raises(ValueError):
        validate_attachment_refs(refs, source())


def test_reference_count_limit_includes_failed_resources(native_runner):
    from gateway.attachment_refs import validate_attachment_refs
    refs = [attachment_ref() for _ in range(64)]
    assert validate_attachment_refs(refs) == refs
    refs.append(attachment_ref())
    with pytest.raises(ValueError):
        validate_attachment_refs(refs)
    with pytest.raises(RuntimeStoreError, match='invalid_params'):
        snapshot_native(native_runner, MessageEvent(text='files', source=source(), attachment_refs=refs))


def test_reference_json_budget_counts_utf8_bytes(native_runner):
    from gateway.attachment_refs import validate_attachment_refs
    refs = [attachment_ref(file_name='报' * 255) for _ in range(64)]
    encoded = json.dumps(refs, ensure_ascii=False, separators=(',', ':'))
    assert len(encoded) < 64 * 1024 < len(encoded.encode('utf-8'))
    with pytest.raises(ValueError):
        validate_attachment_refs(refs)
    with pytest.raises(RuntimeStoreError, match='invalid_params'):
        snapshot_native(native_runner, MessageEvent(text='files', source=source(), attachment_refs=refs))


@pytest.mark.parametrize('refs', [
    [attachment_ref(token='credential')], [attachment_ref(app_secret='credential')],
    [attachment_ref(raw_message={'secret': 'payload'})],
    [attachment_ref(chat_id='oc_other')], [attachment_ref(thread_id='omt_other')],
    [attachment_ref(file_name='C:/private/report.pdf')], [attachment_ref(v=True)],
    [attachment_ref() for _ in range(65)], {}, '', False,
])
def test_restore_revalidates_untrusted_references(native_runner, refs):
    payload = snapshot_native(native_runner, MessageEvent(text='files', source=source()))
    payload['native_text_v1']['event']['attachment_refs'] = refs
    original = deepcopy(payload)
    with pytest.raises(RuntimeStoreError, match='invalid_params') as failure:
        restore_native(payload)
    assert failure.value.field == 'attachment_refs'
    assert payload == original


@pytest.mark.asyncio
async def test_prepare_freezes_references_before_first_await(native_runner, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    async def pause_reauthorization(*_args):
        entered.set()
        await release.wait()
        return False

    monkeypatch.setattr('gateway.session_ingress_context.reauthorize_roles', pause_reauthorization)
    refs = [attachment_ref()]
    event = MessageEvent(text='files', source=source(), attachment_refs=refs)
    pending = asyncio.create_task(prepare_native(native_runner, event))
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        refs[0]['state'] = 'ready'
        release.set()
        payload = await asyncio.wait_for(pending, timeout=10)
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
    assert payload['native_text_v1']['event']['attachment_refs'] == [attachment_ref()]


def test_empty_references_preserve_legacy_admission_fingerprint(native_runner, monkeypatch):
    from hermes_state_keys import admission_fingerprint
    monkeypatch.setattr(native_runner.session_store, '_generate_session_key', lambda _source: 'legacy-route')
    event = MessageEvent(text='legacy input', source=source(), message_id='om_message-1',
                         timestamp=datetime(2026, 1, 1))
    # Generated by the pre-change codec: adding absent refs must not alter retries.
    legacy_fingerprint = '60d462dff17841cf717d7f4e74315c3c39368ff1d2b1353e5c58bbf476839c41'
    for refs in (None, []):
        payload = snapshot_native(native_runner, replace(event, attachment_refs=refs))
        assert 'attachment_refs' not in payload['native_text_v1']['event']
        assert admission_fingerprint(canonical_target='legacy-target', payload=payload) == legacy_fingerprint
        assert restore_native(payload).attachment_refs is None


@pytest.mark.asyncio
async def test_failed_references_survive_durable_admission(native_runner, monkeypatch):
    from gateway.session_authority import initialize_session_authority
    from hermes_state_runtime import list_session_admissions
    authority = await initialize_session_authority(native_runner, profile_id='default', instance_id='refs')
    monkeypatch.setattr(authority, '_schedule', lambda _ref: None)
    event = MessageEvent(text='files', source=source(), message_id='om_message-1',
                         attachment_refs=[attachment_ref()], sender_open_id='ou_sender-1',
                         mentioned_user_ids=['ou_bot-1'], topic_root_message_id='om_root-1',
                         native_task_text='read document')
    try:
        receipt = await authority.admit_native(event)
        assert event._gateway_accepted
        event.attachment_refs[0]['file_key'] = 'mutated'
        rows = list_session_admissions(authority.db, session_id=receipt.ref.session_id, pending_only=False)
        assert len(rows) == 1
        restored = restore_native(json.loads(json.dumps(rows[0]['payload'])))
        assert restored.attachment_refs == [attachment_ref()]
        assert restored.media_urls == []
        assert restored.sender_open_id == 'ou_sender-1'
        assert restored.mentioned_user_ids == ['ou_bot-1']
        assert restored.topic_root_message_id == 'om_root-1'
        assert restored.native_task_text == 'read document'
    finally:
        authority.db.close()


def test_ready_references_coexist_with_committed_media(native_runner, tmp_path):
    original = tmp_path / 'report.txt'
    original.write_text('committed document', encoding='utf-8')
    event = MessageEvent(text='files', source=source(), message_id='om_message-1',
                         attachment_refs=[attachment_ref(state='ready')],
                         media_urls=[str(original)], media_types=['text/plain'])
    payload = snapshot_native(native_runner, event)
    original.unlink()
    restored = restore_native(payload)
    assert restored.attachment_refs == [attachment_ref(state='ready')]
    assert restored.media_types == ['text/plain']
    from pathlib import Path
    assert Path(restored.media_urls[0]).read_text(encoding='utf-8') == 'committed document'

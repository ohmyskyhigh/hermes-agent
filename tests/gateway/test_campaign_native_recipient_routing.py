"""Native Feishu recipient provenance survives replay into bounded paused dialogue."""
import json
from dataclasses import replace
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from gateway.session_envelope import snapshot_native, restore_native
from gateway.topic_campaign_config import CampaignPolicy
from gateway.topic_campaign_service import TopicCampaignService
from plugins.platforms.feishu.adapter import FeishuAdapter
from hermes_state import SessionDB
from hermes_state_runtime import RuntimeStoreError, admit_session_input, begin_runtime_epoch, get_session_admission


def service_for(tmp_path):
    policy = CampaignPolicy.parse(dict(enabled=True, state_path=str(tmp_path / 'campaigns.db'), groups=[dict(
        platform='feishu', chat_id='oc_test', profiles=['joi', 'br-analyst'], collector_profile='joi',
        default_owner='joi', mentions={'joi': {'ou_known': 'br-analyst', 'ou_joi': 'joi'},
                                      'br-analyst': {'ou_known': 'br-analyst', 'ou_joi': 'joi'}})]))
    runner = NS(config=NS(multiplex_profiles=False), _active_profile_name=lambda: 'joi',
                _delivery_adapter_for=lambda source: None)
    service = TopicCampaignService(runner, policy)
    store = service._store()
    store.ingest_topic(topic_key='feishu:oc_test:omt_test', owner='joi', profile='joi',
        source=dict(platform='feishu', chat_id='oc_test', thread_id='omt_test', user_id='ou_human'),
        transport_profile='joi', message_id='seed', text='original goal', native_task_text='original goal',
        native_targets=['joi'], root_message_id='om_root', is_bot=False, targets=['joi'], now=1,
        review_interval=100, report_interval=100)
    store.control_topic('feishu:oc_test:omt_test', 'pause', 'pause', 2)
    return service


async def native_event(tmp_path, profile, recipient='ou_new', *, user_id=None, content='What is the blocker?',
                       bot_open_id=None, require_mention=False, codec_changes=None):
    adapter = FeishuAdapter(PlatformConfig(enabled=True, extra={'app_id': 'fixture-' + profile,
        'app_secret': 'fixture', 'group_policy': 'open', 'require_mention': False}))
    adapter._seen_message_ids.clear()  # Two fixture transports share one sandbox home.
    adapter._seen_message_order.clear()
    adapter._default_group_policy = 'open'
    adapter._group_policy = 'open'
    adapter._require_mention = require_mention
    adapter._bot_open_id = bot_open_id or ('ou_new' if profile == 'br-analyst' else 'ou_joi')
    adapter._bot_user_id = 'uid_br' if profile == 'br-analyst' else 'uid_joi'
    adapter._bot_name = 'BR Analyst' if profile == 'br-analyst' else 'Joi'
    adapter.get_chat_info = AsyncMock(return_value={'name': 'test', 'chat_type': 'group'})
    adapter._resolve_sender_profile = AsyncMock(return_value=dict(user_id='ou_human', user_id_alt=None, user_name='human'))
    adapter._fetch_message_text = AsyncMock(return_value=None)
    adapter._dispatch_inbound_event = AsyncMock()
    mentions = [] if recipient is None and user_id is None else [NS(
        id=NS(open_id=recipient, user_id=user_id), name='BR Analyst', key='@_user_1')]
    message = NS(message_id='question', chat_id='oc_test', chat_type='group', thread_id='omt_test',
        root_id='om_root', parent_id=None, message_type='text',
        content=json.dumps({'text': ('@_user_1 ' if mentions else '') + content}), mentions=mentions)
    sender = NS(sender_type='user', sender_id=NS(open_id='ou_human', user_id=None, union_id=None))
    await adapter._handle_message_event_data(NS(event=NS(message=message, sender=sender)))
    event = adapter._dispatch_inbound_event.await_args.args[0]
    event.source.profile = profile
    runner = NS(config=NS(multiplex_profiles=False), _adapter_flag=lambda *args: False,
        _adapter_profile_for_source=lambda source: profile,
        _is_user_authorized_for_source=lambda *args, **kwargs: True,
        _adapter_for_source=lambda source: adapter,
        session_store=NS(_generate_session_key=lambda source: 'test-route'))
    payload = snapshot_native(runner, replace(event, **(codec_changes or {})))
    path = tmp_path / f'native-{profile}.db'
    db = SessionDB(db_path=path)
    try:
        db.create_session('fixture-session', source='feishu')
        epoch = begin_runtime_epoch(db, instance_id='isolated-fixture')
        row = admit_session_input(db, epoch=epoch, principal_id=profile, session_id='fixture-session',
                                  request_id=event.message_id, payload=payload, intent='queue')
    finally:
        db.close()
    db = SessionDB(db_path=path)
    try:
        durable = get_session_admission(db, admission_id=row['admission_id'])
        assert durable['status'] == 'queued'
        assert durable['payload'] == payload
        return restore_native(durable['payload']), durable['payload']
    finally:
        db.close()


@pytest.mark.asyncio
async def test_legacy_native_defaults_do_not_add_fingerprint_fields(tmp_path):
    event, payload = await native_event(tmp_path, 'joi', None)
    fields = payload['native_text_v1']['event']
    assert 'native_mention_self' not in fields
    assert 'native_mentions_present' not in fields
    assert event.native_mention_self is False and event.native_mentions_present is False


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['native_mention_self', 'native_mentions_present'])
async def test_codec_refuses_non_boolean_native_recipient_provenance(tmp_path, field):
    with pytest.raises(RuntimeStoreError, match='invalid_params'):
        await native_event(tmp_path, 'joi', None, codec_changes={field: 'true'})


@pytest.mark.asyncio
@pytest.mark.parametrize('order', [('joi', 'br-analyst'), ('br-analyst', 'joi')])
async def test_unknown_open_id_never_becomes_default_owner_or_name_matched_self(tmp_path, order):
    service = service_for(tmp_path)
    for profile in order:
        event, _ = await native_event(tmp_path, profile, 'ou_unknown')
        assert not event.native_mention_self
        consumed, notice = await service.intake(event, event.source)
        assert consumed
        assert bool(notice) is (profile == 'joi')
    item = service._store().current('feishu:oc_test:omt_test')
    assert (item['status'], item['owner_profile']) == ('paused', 'joi')
    assert service._store().input_intent(item['key'], 'question') is None


@pytest.mark.asyncio
async def test_unknown_recipient_cannot_create_initial_default_owner_campaign(tmp_path):
    service = service_for(tmp_path)
    event, _ = await native_event(tmp_path, 'joi', 'ou_unknown')
    event.source.thread_id = 'omt_unresolved'
    consumed, notice = await service.intake(event, event.source)
    assert consumed and notice
    assert service._store().current('feishu:oc_test:omt_unresolved') is None


@pytest.mark.asyncio
@pytest.mark.parametrize('recipient,user_id,bot_open_id', [
    ('ou_known', None, 'ou_known'), ('ou_new', None, None), (None, 'uid_br', None), (None, '', None)])
async def test_verified_self_admission_identity_tiers_survive_codec(tmp_path, recipient, user_id, bot_open_id):
    service = service_for(tmp_path)
    event, payload = await native_event(tmp_path, 'br-analyst', recipient, user_id=user_id,
        bot_open_id=bot_open_id, require_mention=True)
    assert event.native_mention_self is True
    assert payload['native_text_v1']['event']['native_mention_self'] is True
    assert await service.intake(event, event.source) == (False, None)
    item = service._store().current('feishu:oc_test:omt_test')
    assert (item['status'], item['owner_profile']) == ('paused', 'joi')
    assert service._store().input_intent(item['key'], 'question')['targets'] == ['br-analyst']


@pytest.mark.asyncio
@pytest.mark.parametrize('order', [('joi', 'br-analyst'), ('br-analyst', 'joi')])
async def test_known_other_agent_only_never_becomes_receiver_self(tmp_path, order):
    service = service_for(tmp_path)
    admitted = []
    for profile in order:
        event, _ = await native_event(tmp_path, profile, 'ou_joi')
        assert event.native_mention_self is (profile == 'joi')
        consumed, notice = await service.intake(event, event.source)
        assert notice is None
        if not consumed:
            admitted.append(profile)
    assert admitted == ['joi']
    item = service._store().current('feishu:oc_test:omt_test')
    assert (item['status'], item['owner_profile']) == ('paused', 'joi')
    assert service._store().input_intent(item['key'], 'question')['targets'] == ['joi']


@pytest.mark.asyncio
async def test_unmapped_name_only_mention_does_not_become_unmentioned_owner_dialogue(tmp_path):
    service = service_for(tmp_path)
    event, _ = await native_event(tmp_path, 'joi', None, user_id='')
    consumed, notice = await service.intake(event, event.source)
    assert consumed and notice
    item = service._store().current('feishu:oc_test:omt_test')
    assert service._store().input_intent(item['key'], 'question') is None


@pytest.mark.asyncio
@pytest.mark.parametrize('content', ['What is the blocker?', '@_all What is the blocker?'])
async def test_open_group_or_broadcast_admission_is_not_explicit_specialist_addressing(tmp_path, content):
    service = service_for(tmp_path)
    event, payload = await native_event(tmp_path, 'br-analyst', None, content=content)
    assert 'native_mention_self' not in payload['native_text_v1']['event']
    assert await service.intake(event, event.source) == (True, None)
    item = service._store().current('feishu:oc_test:omt_test')
    assert (item['status'], item['owner_profile']) == ('paused', 'joi')
    assert service._store().input_intent(item['key'], 'question')['targets'] == ['joi']


@pytest.mark.asyncio
async def test_unknown_user_id_only_recipient_fails_closed_visibly(tmp_path):
    service = service_for(tmp_path)
    event, _ = await native_event(tmp_path, 'joi', None, user_id='uid_unknown')
    consumed, notice = await service.intake(event, event.source)
    assert consumed and notice
    item = service._store().current('feishu:oc_test:omt_test')
    assert service._store().input_intent(item['key'], 'question') is None
    assert (item['status'], item['owner_profile']) == ('paused', 'joi')


@pytest.mark.asyncio
@pytest.mark.parametrize('order', [('joi', 'br-analyst'), ('br-analyst', 'joi')])
async def test_cross_app_unmapped_recipient_is_not_pinned_to_collector_first(tmp_path, order):
    service = service_for(tmp_path)
    admitted = []
    for profile in order:
        event, _ = await native_event(tmp_path, profile)
        consumed, notice = await service.intake(event, event.source)
        if not consumed:
            admitted.append(profile)
        if profile == 'joi':
            assert consumed
            if order[0] == 'joi':
                assert notice and '原生 @' in notice
    assert admitted == ['br-analyst']
    item = service._store().current('feishu:oc_test:omt_test')
    assert (item['status'], item['owner_profile']) == ('paused', 'joi')
    assert service._store().input_intent(item['key'], 'question')['targets'] == ['br-analyst']


@pytest.mark.asyncio
async def test_unmapped_verified_native_self_replays_to_specialist_not_campaign_owner(tmp_path):
    service = service_for(tmp_path)
    event, _ = await native_event(tmp_path, 'br-analyst')
    assert await service.intake(event, event.source) == (False, None)
    item = service._store().current('feishu:oc_test:omt_test')
    assert (item['status'], item['owner_profile']) == ('paused', 'joi')
    intent = service._store().input_intent(item['key'], 'question')
    assert (intent['targets'], intent['delivery'], intent['mode']) == (['br-analyst'], 'native', 'paused_dialogue')

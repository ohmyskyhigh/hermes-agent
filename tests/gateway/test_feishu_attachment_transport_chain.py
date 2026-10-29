"""Offline transport-to-receipt-to-durable-to-recovery contract, not mocked download."""
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest


@pytest.mark.asyncio
async def test_transport_failure_survives_durable_and_recovers_in_same_thread(tmp_path, monkeypatch):
    from agent.secret_scope import reset_secret_scope, set_secret_scope
    from gateway import config, session_context
    from gateway.platforms import base
    from gateway.platforms.event import MessageEvent
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource
    from gateway.session_envelope import restore_native, snapshot_native
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from plugins.platforms.feishu import adapter as adapter_module
    from plugins.platforms.feishu.attachment_store import AttachmentStore

    home_token = set_hermes_home_override(tmp_path)
    secret_token = set_secret_scope({'FEISHU_APP_ID': 'cli_chain',
                                    'FEISHU_APP_SECRET': 'offline-chain-secret',
                                    'FEISHU_ALLOWED_USERS': 'ou_sender'}, profile_home=str(tmp_path))
    values = {'HERMES_SESSION_PLATFORM': 'feishu', 'HERMES_SESSION_CHAT_ID': 'oc_chain',
              'HERMES_SESSION_THREAD_ID': 'omt_chain'}
    monkeypatch.setattr(session_context, 'get_session_env', lambda key, default='': values.get(key, default))
    monkeypatch.setenv('FEISHU_ALLOWED_USERS', 'ou_sender')
    monkeypatch.setattr(base, 'get_inbound_media_max_bytes', lambda: 1024)
    monkeypatch.setattr(GatewayRunner, '_VOICE_MODE_PATH', tmp_path / 'voice.json')
    store = AttachmentStore(tmp_path)
    mode, calls = {'available': False}, []
    body = b'\x00\x00\x00\x18ftypisom' + b'\x00' * 28

    class Chunks(httpx.SyncByteStream):
        def __init__(self, data):
            self.data = data

        def __iter__(self):
            yield self.data[:12]
            yield self.data[12:]

    def json_response(status, value):
        data = json.dumps(value).encode()
        return httpx.Response(status, headers={'Content-Type': 'application/json', 'Content-Length': str(len(data))}, stream=Chunks(data))

    def transport(request):
        calls.append((request.method, request.url.path))
        if request.url.path == '/open-apis/auth/v3/tenant_access_token/internal':
            assert json.loads(request.content) == {'app_id': 'cli_chain', 'app_secret': 'offline-chain-secret'}
            return json_response(200, {'code': 0, 'tenant_access_token': 'offline-chain-token', 'expire': 7200})
        assert request.url.path == '/open-apis/im/v1/messages/om_chain/resources/file_chain'
        assert request.headers['Authorization'] == 'Bearer offline-chain-token'
        # The actual receive pipeline must have committed pending before transport.
        rows = store.list(app_id='cli_chain', chat_id='oc_chain', thread_id='omt_chain')
        assert len(rows) == 1 and rows[0]['result']['state'] == 'pending'
        if not mode['available']:
            return json_response(503, {'code': 123, 'msg': 'temporary'})
        assert request.headers.get('Range', '').startswith('bytes=0-')
        return httpx.Response(206, headers={'Content-Range': f'bytes 0-{len(body)-1}/{len(body)}',
                                           'Content-Length': str(len(body)), 'Content-Type': 'video/mp4'}, stream=Chunks(body))

    real_client = httpx.Client
    monkeypatch.setattr(httpx, 'Client', lambda *args, **kwargs: real_client(*args, transport=httpx.MockTransport(transport), **kwargs))
    try:
        assert adapter_module._load_lark_oapi()
        platform = config.PlatformConfig(enabled=True, extra={'app_id': 'cli_chain'})
        adapter = adapter_module.FeishuAdapter(platform)
        adapter._client = adapter._build_lark_client(adapter_module._sdk_domain('feishu'))
        runner = GatewayRunner(config.GatewayConfig(sessions_dir=tmp_path / 'sessions'))
        runner.adapters = {config.Platform.FEISHU: adapter}
        message = SimpleNamespace(message_type='post', message_id='om_chain', chat_id='oc_chain',
                                  thread_id='omt_chain', mentions=[], content=json.dumps({'content': [[
                                      {'tag': 'text', 'text': 'Please inspect this video'},
                                      {'tag': 'media', 'file_key': 'file_chain', 'image_key': 'img_cover'}]]}))
        refs = []
        text, kind, paths, mime_types, _inlined, _mentions = await adapter._extract_message_content(
            message, attachment_refs=refs, sender_id='ou_sender')
        assert 'Please inspect this video' in text and 'feishu_attachment' in text
        assert paths == [] and refs[0]['state'] == 'failed'
        assert len([call for call in calls if '/resources/' in call[1]]) == 3, store.list(app_id='cli_chain', chat_id='oc_chain', thread_id='omt_chain')[0]['result']
        event = MessageEvent(text=text, message_type=kind, message_id=message.message_id,
                             source=SessionSource(platform=config.Platform.FEISHU, chat_id='oc_chain',
                                                  thread_id='omt_chain', user_id='ou_sender'),
                             attachment_refs=refs, media_urls=paths, media_types=mime_types)
        restored = restore_native(snapshot_native(runner, event))
        assert restored.attachment_refs == refs and restored.media_urls == []
        monkeypatch.setattr(config, 'load_gateway_config', lambda: SimpleNamespace(platforms={config.Platform.FEISHU: platform}))
        from tools.feishu_attachment_tool import feishu_attachment
        listed = json.loads(feishu_attachment(action='list'))
        item = listed['attachments'][0]
        assert item['state'] == 'failed' and item['retryable'] is True
        mode['available'] = True
        recovered = json.loads(feishu_attachment(action='recover', id=item['id']))
        assert recovered['success'] is True and recovered['attachment']['state'] == 'ready'
        assert Path(recovered['attachment']['path']).read_bytes() == body
        assert recovered['attachment']['mime_type'] == 'video/mp4'
        calls_before = len(calls)
        values['HERMES_SESSION_THREAD_ID'] = 'omt_other'
        denied = json.loads(feishu_attachment(action='recover', id=item['id']))
        assert denied['success'] is False and len(calls) == calls_before
    finally:
        reset_secret_scope(secret_token)
        reset_hermes_home_override(home_token)

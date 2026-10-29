"""Recovery is confined to received refs in the current profile and route."""
import hashlib
import importlib
import importlib.util
import json

from types import SimpleNamespace

import pytest


def ref(**changes):
    return {"v": 1, "platform": "feishu", "app_id": "cli_test", "message_id": "om_first",
            "file_key": "file_test", "chat_id": "oc_test", "thread_id": "", "sender_id": "ou_test",
            "resource_type": "file", "media_hint": "video", "file_name": "clip.mp4",
            "state": "pending", **changes}


@pytest.fixture
def environment(tmp_path, monkeypatch):
    from agent.secret_scope import set_secret_scope, reset_secret_scope
    from gateway import config, session_context
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from plugins.platforms.feishu.attachment_store import AttachmentStore

    values = {"HERMES_SESSION_PLATFORM": "feishu", "HERMES_SESSION_CHAT_ID": "oc_test",
              "HERMES_SESSION_THREAD_ID": ""}
    monkeypatch.setattr(session_context, "get_session_env", lambda name, default="": values.get(name, default))
    platform = SimpleNamespace(enabled=True, extra={})
    monkeypatch.setattr(config, "load_gateway_config", lambda: SimpleNamespace(platforms={config.Platform.FEISHU: platform}))
    home_token = set_hermes_home_override(tmp_path)
    secrets = {"FEISHU_APP_ID": "cli_test", "FEISHU_APP_SECRET": "test-only-secret"}
    secret_token = set_secret_scope(secrets, profile_home=str(tmp_path))
    try:
        yield SimpleNamespace(home=tmp_path, store=AttachmentStore(tmp_path), values=values, platform=platform, secrets=secrets)
    finally:
        reset_secret_scope(secret_token)
        reset_hermes_home_override(home_token)


def test_list_returns_only_public_current_route_metadata(environment):
    env = environment
    attachment_id = env.store.save(ref())
    env.store.save(ref(chat_id="oc_other", message_id="om_other"))
    result = json.loads(tool_module().feishu_attachment(action="list"))
    assert result["success"] is True
    assert result["attachments"] == [{"id": attachment_id, "filename": "clip.mp4", "state": "pending",
                                      "path": "", "mime_type": "", "size_bytes": 0,
                                      "error_kind": "", "error_code": "", "retryable": True}]
    encoded = json.dumps(result)
    assert "file_key" not in encoded and "file_test" not in encoded and "secret" not in encoded


@pytest.mark.parametrize("initial", ["pending", "missing", "tampered", "outside", "retryable"])
def test_recover_latest_persists_pending_then_download_and_reuses_cache(environment, monkeypatch, initial):
    from gateway.platforms import base
    from plugins.platforms.feishu import adapter, inbound_resources

    env = environment
    env.store.save(ref(message_id="om_older"))
    path = env.home / "cache" / "saved.mp4"
    original = {"state": "ready", "path": str(path), "size_bytes": 6,
                "sha256": hashlib.sha256(b"sample").hexdigest(), "mime_type": "video/mp4"}
    if initial == "tampered":
        path.write_bytes(b"TAMPER")
    cached = None if initial == "pending" else original
    reference = ref()
    if initial == "outside":
        outside = env.home / "other_profile" / "cache" / "saved.mp4"
        outside.parent.mkdir(parents=True)
        outside.write_bytes(b"sample")
        cached = {**original, "path": str(outside)}
    elif initial == "retryable":
        reference = ref(state="failed", error_kind="network")
        cached = {"state": "failed", "error_kind": "network", "retryable": True}
    attachment_id = env.store.save(reference, cached)
    client = object()
    clients, downloads = [], []
    monkeypatch.setattr(adapter, "_load_lark_oapi", lambda: True)
    monkeypatch.setattr(adapter, "_sdk_domain", lambda domain: domain)

    def build(app_id, app_secret, domain):
        assert app_id == "cli_test" and app_secret == "test-only-secret" and domain == "feishu"
        clients.append(client)
        return client

    def download(actual_client, **kwargs):
        assert actual_client is client
        assert env.store.get(attachment_id, "cli_test", "oc_test")["result"]["state"] == "pending"
        assert kwargs == {"message_id": "om_first", "file_key": "file_test", "resource_type": "file",
                          "filename": "clip.mp4", "media_hint": "video", "cache_dir": env.home / "cache",
                          "max_bytes": 1024}
        downloads.append(kwargs)
        path.write_bytes(b"sample")
        return original

    monkeypatch.setattr(adapter, "_build_lark_client", build)
    monkeypatch.setattr(inbound_resources, "download_resource", download)
    monkeypatch.setattr(base, "get_inbound_media_max_bytes", lambda: 1024)
    output = json.loads(tool_module().feishu_attachment(action="recover"))
    assert output["success"] is True
    assert output["attachment"]["id"] == attachment_id
    assert output["attachment"]["state"] == "ready"
    assert output["attachment"]["path"] == str(path)
    assert env.store.get(attachment_id, "cli_test", "oc_test")["result"]["sha256"] == original["sha256"]
    assert "error_kind" not in env.store.get(attachment_id, "cli_test", "oc_test")["ref"]
    assert json.loads(tool_module().feishu_attachment(action="recover", id=attachment_id)) == output
    assert len(downloads) == len(clients) == 1


@pytest.mark.parametrize("kind,code,retryable,attempts", [
    ("busy", "queue_timeout", True, 0),
    ("busy", "queue_timeout", False, 0),
    ("authentication", "503", True, 3),
    ("authentication", "403", False, 1),
    ("permission", "403", False, 1),
    ("permission", "403", True, 1),
])
def test_complete_failure_recovery_obeys_transient_eligibility(
        environment, monkeypatch, kind, code, retryable, attempts):
    from plugins.platforms.feishu import adapter, inbound_resources
    from plugins.platforms.feishu.attachment_store import AttachmentStore

    env = environment
    initial = inbound_resources._failed(inbound_resources._DownloadFailure(kind, code, retryable), attempts)
    reference = ref(state="failed", error_kind=kind)
    attachment_id = env.store.save(reference, initial)
    reopened = AttachmentStore(env.home)
    record = {"attachment_id": attachment_id, "ref": reference, "result": initial}
    assert reopened.get(attachment_id, "cli_test", "oc_test") == record
    assert reopened.list("cli_test", "oc_test") == [record]
    eligible = retryable and kind != "permission"
    listed = json.loads(tool_module().feishu_attachment(action="list"))["attachments"]
    assert listed[0]["retryable"] is eligible

    downloads, builds = [], []
    path = env.home / "cache" / "recovered.mp4"
    ready = {"state": "ready", "path": str(path), "size_bytes": 6,
             "sha256": hashlib.sha256(b"sample").hexdigest(), "mime_type": "video/mp4",
             "attempts": 1, "error_kind": "", "error_code": "", "retryable": False}
    monkeypatch.setattr(adapter, "_load_lark_oapi", lambda: True)

    def build(*args):
        builds.append(args)
        return object()

    def download(client, **kwargs):
        assert eligible, "permanent failure attempted another download"
        assert env.store.get(attachment_id, "cli_test", "oc_test")["result"] == {"state": "pending"}
        downloads.append(kwargs)
        path.write_bytes(b"sample")
        return ready

    monkeypatch.setattr(adapter, "_build_lark_client", build)
    monkeypatch.setattr(inbound_resources, "download_resource", download)
    output = json.loads(tool_module().feishu_attachment(action="recover", id=attachment_id))
    assert output["success"] is eligible
    assert len(downloads) == len(builds) == int(eligible)
    expected = ready if eligible else initial
    assert reopened.get(attachment_id, "cli_test", "oc_test")["result"] == expected
    assert reopened.list("cli_test", "oc_test")[0]["result"] == expected
    assert json.loads(tool_module().feishu_attachment(action="recover", id=attachment_id)) == output
    assert len(downloads) == len(builds) == int(eligible)


def test_real_busy_result_is_persisted_and_recovers_later(environment, monkeypatch):
    from plugins.platforms.feishu import adapter, inbound_resources

    slots = inbound_resources._DOWNLOAD_SLOTS

    class BusySlots:
        def acquire(self, *, timeout):
            assert timeout == 10.0
            return False

        def release(self):
            pytest.fail("unacquired download slot released")

    monkeypatch.setattr(inbound_resources, "_DOWNLOAD_SLOTS", BusySlots())
    result = inbound_resources.download_resource(
        object(), message_id="om_first", file_key="file_test", resource_type="file",
        filename="clip.mp4", media_hint="video", cache_dir=environment.store.cache_dir,
        max_bytes=1024)
    expected = inbound_resources._failed(inbound_resources._DownloadFailure("busy", "queue_timeout", True), 0)
    assert result == expected
    attachment_id = environment.store.save(ref(state="failed", error_kind="busy"), result)
    assert environment.store.get(attachment_id, "cli_test", "oc_test")["result"] == expected
    assert json.loads(tool_module().feishu_attachment(action="list"))["attachments"][0]["retryable"] is True

    monkeypatch.setattr(inbound_resources, "_DOWNLOAD_SLOTS", slots)
    monkeypatch.setattr(adapter, "_load_lark_oapi", lambda: True)
    monkeypatch.setattr(adapter, "_build_lark_client", lambda *args: object())
    path = environment.store.cache_dir / "after-busy.mp4"
    downloads = []
    ready = {"state": "ready", "path": str(path), "size_bytes": 6,
             "sha256": hashlib.sha256(b"sample").hexdigest(), "mime_type": "video/mp4",
             "attempts": 1, "error_kind": "", "error_code": "", "retryable": False}

    def download_once(client, **kwargs):
        assert environment.store.get(attachment_id, "cli_test", "oc_test")["result"] == {"state": "pending"}
        downloads.append(kwargs)
        path.write_bytes(b"sample")
        return ready

    monkeypatch.setattr(inbound_resources, "_download_once", download_once)
    output = json.loads(tool_module().feishu_attachment(action="recover", id=attachment_id))
    assert output["success"] is True
    assert len(downloads) == 1
    assert environment.store.get(attachment_id, "cli_test", "oc_test")["result"] == ready
    assert environment.store.list("cli_test", "oc_test")[0]["result"] == ready
    assert json.loads(tool_module().feishu_attachment(action="recover", id=attachment_id)) == output
    assert len(downloads) == 1


@pytest.mark.parametrize("kind", ["permission", "not_found", "too_large", "size_limit"])
def test_permanent_failure_is_not_retried_even_with_bad_retry_flag(environment, monkeypatch, kind):
    from plugins.platforms.feishu import adapter
    env = environment
    attachment_id = env.store.save(ref(state="failed"), {"state": "failed", "error_kind": kind, "retryable": True})
    builds = []
    monkeypatch.setattr(adapter, "_load_lark_oapi", lambda: builds.append(True))
    monkeypatch.setattr(adapter, "_build_lark_client", lambda *args: pytest.fail("permanent failure built a client"))
    output = json.loads(tool_module().feishu_attachment(action="recover", id=attachment_id))
    assert output["success"] is False
    assert output["attachment"]["error_kind"] == kind
    assert output["attachment"]["retryable"] is False
    assert builds == []


@pytest.mark.parametrize("key", ["app_id", "chat_id", "thread_id", "profile", "file_key", "message_id"])
def test_registry_cannot_accept_scope_or_bootstrap_arguments(environment, key):
    from tools.registry import registry
    tool_module()
    entry = registry.get_entry("feishu_attachment")
    assert entry is not None
    assert entry.toolset == "feishu_attachment"
    assert entry.check_fn is not None
    params = entry.schema["parameters"]
    assert set(params["properties"]) == {"action", "id", "limit"}
    assert params["additionalProperties"] is False
    output = json.loads(entry.handler({"action": "recover", key: "foreign"}))
    assert output["success"] is False
    assert output["error_kind"] == "invalid_arguments"


@pytest.mark.parametrize("kwargs", [{"action": "download"}, {"action": "recover", "id": "om_any"},
                                   {"action": "recover", "id": ""}, {"limit": -1}, {"limit": True}])
def test_invalid_arguments_do_not_route_to_other_operations(environment, kwargs):
    output = json.loads(tool_module().feishu_attachment(**kwargs))
    assert output["success"] is False
    assert output["error_kind"] == "invalid_arguments"


@pytest.mark.parametrize("scope", ["chat", "thread", "app", "profile", "unknown"])
def test_recover_cannot_route_an_id_outside_current_scope(environment, monkeypatch, scope):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from plugins.platforms.feishu import adapter
    env = environment
    attachment_id = env.store.save(ref())
    monkeypatch.setattr(adapter, "_build_lark_client", lambda *args: pytest.fail("out-of-scope client construction"))
    home_token = None
    if scope == "chat":
        env.values["HERMES_SESSION_CHAT_ID"] = "oc_other"
    elif scope == "thread":
        env.values["HERMES_SESSION_THREAD_ID"] = "omt_other"
    elif scope == "app":
        env.secrets["FEISHU_APP_ID"] = "cli_other"
    elif scope == "profile":
        home_token = set_hermes_home_override(env.home / "other_profile")
    else:
        attachment_id = "0" * 64
    try:
        output = json.loads(tool_module().feishu_attachment(action="recover", id=attachment_id))
        assert output["success"] is False
    finally:
        if home_token is not None:
            reset_hermes_home_override(home_token)


@pytest.mark.parametrize("problem", ["missing_app", "missing_secret", "disabled", "loader_exception"])
def test_configuration_failures_are_closed_and_do_not_leak(environment, monkeypatch, problem):
    from gateway import config
    from plugins.platforms.feishu import adapter
    env = environment
    env.store.save(ref())
    monkeypatch.setattr(adapter, "_build_lark_client", lambda *args: pytest.fail("invalid config built client"))
    if problem == "missing_app":
        env.secrets.pop("FEISHU_APP_ID")
    elif problem == "missing_secret":
        env.secrets.pop("FEISHU_APP_SECRET")
    elif problem == "disabled":
        env.platform.enabled = False
    else:
        def broken():
            raise RuntimeError("DO_NOT_EXPOSE" * 1000)
        monkeypatch.setattr(config, "load_gateway_config", broken)
    output = tool_module().feishu_attachment(action="recover")
    assert "DO_NOT_EXPOSE" not in output
    assert json.loads(output) == {"success": False, "error_kind": "configuration_unavailable", "retryable": False}


@pytest.mark.parametrize("configured,expected", [(0, 512 * 1024 * 1024), (-1, 512 * 1024 * 1024),
                          (float("inf"), 512 * 1024 * 1024), (float("nan"), 512 * 1024 * 1024),
                          ("invalid", 512 * 1024 * 1024), (True, 512 * 1024 * 1024),
                          (1024 ** 4, 512 * 1024 * 1024), (4096, 4096)])
def test_recovery_always_has_a_finite_download_bound(environment, monkeypatch, configured, expected):
    from gateway.platforms import base
    from plugins.platforms.feishu import adapter, inbound_resources
    environment.store.save(ref())
    monkeypatch.setattr(adapter, "_load_lark_oapi", lambda: True)
    monkeypatch.setattr(adapter, "_build_lark_client", lambda *args: object())
    monkeypatch.setattr(base, "get_inbound_media_max_bytes", lambda: configured)
    bounds = []
    def download(client, **kwargs):
        bounds.append(kwargs["max_bytes"])
        return {"state": "failed", "error_kind": "network", "retryable": True}
    monkeypatch.setattr(inbound_resources, "download_resource", download)
    output = json.loads(tool_module().feishu_attachment(action="recover"))
    assert bounds == [expected]
    assert output["attachment"]["retryable"] is True


@pytest.mark.parametrize("stage", ["client", "download"])
def test_recovery_exception_is_bounded_and_persisted_without_sdk_text(environment, monkeypatch, stage):
    from plugins.platforms.feishu import adapter, inbound_resources
    env = environment
    attachment_id = env.store.save(ref())
    monkeypatch.setattr(adapter, "_load_lark_oapi", lambda: True)
    def broken(*args, **kwargs):
        raise RuntimeError("DO_NOT_EXPOSE")
    monkeypatch.setattr(adapter, "_build_lark_client", broken if stage == "client" else lambda *args: object())
    monkeypatch.setattr(inbound_resources, "download_resource", broken)
    output = tool_module().feishu_attachment(action="recover")
    assert "DO_NOT_EXPOSE" not in output
    assert len(output) < 2048
    result = json.loads(output)
    assert result["success"] is False
    assert result["attachment"]["state"] == "failed"
    assert result["attachment"]["error_kind"] == "download_failed"
    assert result["attachment"]["retryable"] is True
    assert env.store.get(attachment_id, "cli_test", "oc_test")["result"]["error_kind"] == "download_failed"


def test_secret_scope_for_another_home_is_not_borrowed(environment):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    env = environment
    token = set_hermes_home_override(env.home / "other_profile")
    try:
        output = json.loads(tool_module().feishu_attachment(action="list"))
        assert output == {"success": False, "error_kind": "configuration_unavailable", "retryable": False}
        assert not (env.home / "other_profile").exists()
    finally:
        reset_hermes_home_override(token)


def test_unreadable_ledger_returns_bounded_error(environment):
    env = environment
    env.store.path.write_bytes(b"not sqlite")
    output = json.loads(tool_module().feishu_attachment(action="list"))
    assert output == {"success": False, "error_kind": "storage_unavailable", "retryable": True}


def test_missing_sdk_does_not_construct_a_client(environment, monkeypatch):
    from plugins.platforms.feishu import adapter
    environment.store.save(ref())
    monkeypatch.setattr(adapter, "_load_lark_oapi", lambda: False)
    monkeypatch.setattr(adapter, "_build_lark_client", lambda *args: pytest.fail("missing SDK built client"))
    output = json.loads(tool_module().feishu_attachment(action="recover"))
    assert output["success"] is False
    assert output["attachment"]["error_kind"] == "sdk_unavailable"


def tool_module():
    name = "tools.feishu_attachment_tool"
    assert importlib.util.find_spec(name) is not None, "attachment tool is not implemented"
    return importlib.import_module(name)


@pytest.mark.parametrize("platform,chat", [("", ""), ("telegram", "oc_test"), ("feishu", "")])
def test_missing_or_wrong_session_fails_closed(tmp_path, monkeypatch, platform, chat):
    from gateway import session_context
    import hermes_constants
    values = {"HERMES_SESSION_PLATFORM": platform, "HERMES_SESSION_CHAT_ID": chat}
    monkeypatch.setattr(session_context, "get_session_env", lambda name, default="": values.get(name, default))
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    result = json.loads(tool_module().feishu_attachment(action="recover"))
    assert result["success"] is False
    assert result["error_kind"] == "invalid_context"
    assert result["retryable"] is False
    assert not (tmp_path / "cache").exists()

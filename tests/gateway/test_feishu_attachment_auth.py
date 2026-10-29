"""Offline regression tests for scoped, bounded Feishu authentication."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest


class Chunks(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.reads = 0
        self.closed = False

    def __iter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    def close(self):
        self.closed = True


def envelope(**values):
    return Chunks([json.dumps(values).encode()])


@pytest.fixture
def harness(monkeypatch):
    path = Path(__file__).parents[2] / "plugins/platforms/feishu/inbound_auth.py"
    module = SimpleNamespace(get_tenant_token=None)
    if path.exists():
        spec = importlib.util.spec_from_file_location("feishu_auth_under_test", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    pytest.importorskip("lark_oapi")
    from lark_oapi.core.model import Config
    from lark_oapi.core.http import Transport

    config = Config()
    config.app_id = "offline-app"
    config.app_secret = "offline-secret-A"
    config.domain = "https://open.larksuite.com"
    calls, responses, options = [], [], []
    real_client = httpx.Client

    def handle(request):
        calls.append(request)
        assert responses, "Unexpected request or hidden authentication retry"
        outcome = responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome(request) if callable(outcome) else outcome

    def client(**kwargs):
        options.append(kwargs)
        return real_client(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    monkeypatch.setattr(Transport, "execute", lambda *a, **k: pytest.fail("SDK transport forbidden"))
    clock = [100.0]
    if hasattr(module, "time"):
        monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    return SimpleNamespace(module=module, config=config, calls=calls,
                           responses=responses, options=options, clock=clock)


def test_valid_token_uses_exact_client_credentials_and_domain(harness):
    assert callable(harness.module.get_tenant_token), "scoped authentication API must exist"
    stream = envelope(code=0, tenant_access_token="scoped-token-A")
    harness.responses.append(httpx.Response(200, stream=stream))
    assert harness.module.get_tenant_token(harness.config, deadline=110.0) == "scoped-token-A"
    assert len(harness.calls) == 1
    request = harness.calls[0]
    assert request.method == "POST"
    assert str(request.url) == "https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal"
    assert json.loads(request.content) == {"app_id": "offline-app", "app_secret": "offline-secret-A"}
    assert request.headers["Accept-Encoding"] == "identity"
    assert "Authorization" not in request.headers
    assert all(0 < value <= 10 for value in request.extensions["timeout"].values())
    assert harness.options[0]["follow_redirects"] is False
    assert stream.closed


def failed(harness, *, deadline=110.0):
    with pytest.raises(Exception) as caught:
        harness.module.get_tenant_token(harness.config, deadline=deadline)
    error = caught.value
    assert isinstance(error, getattr(harness.module, "AuthenticationFailure", ())), type(error)
    assert isinstance(error.error_kind, str)
    assert isinstance(error.error_code, str)
    assert type(error.retryable) is bool
    assert "offline-secret" not in str(error)
    assert "leaked-message" not in str(error)
    return error


def test_cross_origin_307_never_forwards_credentials(harness):
    stream = envelope(code=0, tenant_access_token="must-not-accept-redirect-body")
    harness.responses.append(httpx.Response(307, headers={"Location": "https://attacker.invalid/collect"}, stream=stream))
    error = failed(harness)
    assert (error.error_kind, error.error_code, error.retryable) == ("redirect", "307", False)
    assert [request.url.host for request in harness.calls] == ["open.larksuite.com"]
    assert stream.reads == 0
    assert stream.closed


def test_seeded_process_global_sdk_cache_cannot_authenticate_other_secret(harness, monkeypatch):
    import time
    from lark_oapi.core.cache import LocalCache
    from lark_oapi.core.token.manager import TokenManager

    seeded_cache = LocalCache()
    seeded_cache.set("self_tenant_token:offline-app", "token-from-profile-A", int(time.time()) + 3600)
    monkeypatch.setattr(TokenManager, "cache", seeded_cache)
    harness.config.app_secret = "offline-secret-B"
    harness.responses.append(httpx.Response(200, stream=envelope(
        code=10014, msg="leaked-message offline-secret-B", tenant_access_token="must-not-accept")))
    error = failed(harness)
    assert (error.error_kind, error.error_code, error.retryable) == ("authentication", "10014", False)
    assert len(harness.calls) == 1
    assert json.loads(harness.calls[0].content)["app_secret"] == "offline-secret-B"
    assert seeded_cache.get("self_tenant_token:offline-app") == "token-from-profile-A"


@pytest.mark.parametrize("status,retryable", [(401, False), (403, False), (404, False), (429, True), (500, True), (503, True)])
def test_http_errors_are_sanitized_and_never_retried_here(harness, status, retryable):
    stream = envelope(code=999, msg="leaked-message offline-secret-A")
    harness.responses.append(httpx.Response(status, stream=stream))
    error = failed(harness)
    assert (error.error_kind, error.error_code, error.retryable) == ("authentication", str(status), retryable)
    assert len(harness.calls) == 1
    assert stream.reads <= 1
    assert stream.closed


@pytest.mark.parametrize("headers,expected_reads", [
    ({"Content-Length": "16385"}, 0), ({}, 3), ({"Content-Length": "1"}, 3),
])
def test_token_envelope_is_capped_before_accumulating_over_16k(harness, headers, expected_reads):
    stream = Chunks([b" " * 8192, b" " * 8192, b"x", b"must-not-read"])
    harness.responses.append(httpx.Response(200, headers=headers, stream=stream))
    error = failed(harness)
    assert (error.error_code, error.retryable) == ("auth_response_too_large", False)
    assert stream.reads == expected_reads
    assert stream.closed


def test_trickle_consumes_wall_budget_per_raw_chunk(harness):
    def trickle():
        for _ in range(50):
            harness.clock[0] += 1.0
            yield b" "
        yield b'{"code":0,"tenant_access_token":"late-token"}'

    stream = Chunks(trickle())
    harness.responses.append(httpx.Response(200, stream=stream))
    error = failed(harness, deadline=102.5)
    assert (error.error_kind, error.error_code, error.retryable) == ("timeout", "deadline", False)
    assert stream.reads == 3
    assert stream.closed


def test_expired_deadline_does_not_construct_client_or_use_network(harness):
    harness.responses.append(httpx.Response(200, stream=envelope(code=0, tenant_access_token="token")))
    error = failed(harness, deadline=100.0)
    assert (error.error_kind, error.error_code, error.retryable) == ("timeout", "deadline", False)
    assert harness.calls == []
    assert harness.options == []


def test_16k_envelope_and_4096_character_token_are_allowed(harness):
    token = "t" * 4096
    body = json.dumps({"code": 0, "tenant_access_token": token}).encode()
    body += b" " * (16384 - len(body))
    stream = Chunks([body[:8192], body[8192:]])
    harness.responses.append(httpx.Response(200, headers={"Content-Length": "16384"}, stream=stream))
    assert harness.module.get_tenant_token(harness.config, deadline=101.0) == token
    assert all(0 < value <= 1 for value in harness.calls[0].extensions["timeout"].values())
    assert stream.closed


@pytest.mark.parametrize("payload", [
    {}, [], None, {"code": False, "tenant_access_token": "token"},
    {"code": "0", "tenant_access_token": "token"},
    {"code": 0.0, "tenant_access_token": "token"},
    {"code": "leaked-message offline-secret-A"}, {"code": -1}, {"code": 10**12},
    {"code": 0}, {"code": 0, "tenant_access_token": ""},
    {"code": 0, "tenant_access_token": "x" * 4097},
    {"code": 0, "tenant_access_token": None},
    {"code": 0, "tenant_access_token": "bad\r\nAuthorization: leaked-message"},
    {"code": 0, "tenant_access_token": " "},
])
def test_malformed_envelopes_and_tokens_are_safely_rejected(harness, payload):
    harness.responses.append(httpx.Response(200, stream=Chunks([json.dumps(payload).encode()])))
    error = failed(harness)
    assert (error.error_kind, error.error_code, error.retryable) == ("authentication", "auth_response_invalid", False)
    assert "leaked-message" not in repr(error)


@pytest.mark.parametrize("body", [b"not JSON leaked-message offline-secret-A", b"\xff", b'[' * 1200],
                         ids=["not-json", "invalid-utf8", "deep-nesting"])
def test_invalid_json_does_not_escape_parser_errors(harness, body):
    harness.responses.append(httpx.Response(200, stream=Chunks([body])))
    error = failed(harness)
    assert (error.error_code, error.retryable) == ("auth_response_invalid", False)


@pytest.mark.parametrize("headers", [{"Content-Encoding": "gzip"}, {"Content-Length": "invalid"}, {"Content-Length": "-1"}])
def test_invalid_envelope_headers_abort_before_body(harness, headers):
    stream = envelope(code=0, tenant_access_token="token")
    harness.responses.append(httpx.Response(200, headers=headers, stream=stream))
    error = failed(harness)
    assert error.error_code in ("auth_response_invalid", "content_encoding")
    assert error.retryable is False
    assert stream.reads == 0
    assert stream.closed


@pytest.mark.parametrize("domain", [
    "http://open.feishu.cn", "https://attacker.invalid", "https://open.feishu.cn.attacker.invalid",
    "https://user@open.feishu.cn", "https://open.feishu.cn:443", "https://open.feishu.cn:",
    "https://open.feishu.cn?x=1", "https://open.feishu.cn?", "https://open.feishu.cn#",
    "https://open.feishu.cn/path", "https://open.feishu.cn//", "https://open.feishu.cn\\@attacker.invalid",
    " https://open.feishu.cn", "https://open.feishu.cn\n", None,
])
def test_direct_helper_misuse_cannot_post_credentials_to_invalid_domain(harness, domain):
    harness.config.domain = domain
    harness.responses.append(httpx.Response(200, stream=envelope(code=0, tenant_access_token="token")))
    error = failed(harness)
    assert (error.error_kind, error.error_code, error.retryable) == ("invalid_input", "", False)
    assert harness.calls == []
    assert harness.options == []


@pytest.mark.parametrize("app_type", ["ISV", "SELF", 1, None])
def test_only_actual_sdk_self_enum_is_supported(harness, app_type):
    from lark_oapi.core.enum import AppType

    harness.config.app_type = AppType.ISV if app_type == "ISV" else app_type
    harness.responses.append(httpx.Response(200, stream=envelope(code=0, tenant_access_token="token")))
    error = failed(harness)
    assert (error.error_kind, error.error_code, error.retryable) == ("authentication", "unsupported_app_type", False)
    assert harness.calls == []
    assert harness.options == []


@pytest.mark.parametrize("deadline", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_deadline_cannot_disable_bound(harness, deadline):
    harness.responses.append(httpx.Response(200, stream=envelope(code=0, tenant_access_token="token")))
    error = failed(harness, deadline=deadline)
    assert error.error_code == "deadline"
    assert harness.calls == []
    assert harness.options == []


@pytest.mark.parametrize("field,value", [("app_id", None), ("app_id", ""), ("app_secret", None), ("app_secret", ""), ("app_secret", 42)])
def test_invalid_credentials_do_not_make_request(harness, field, value):
    setattr(harness.config, field, value)
    harness.responses.append(httpx.Response(200, stream=envelope(code=0, tenant_access_token="token")))
    error = failed(harness)
    assert (error.error_kind, error.error_code, error.retryable) == ("invalid_input", "", False)
    assert harness.calls == []


def test_each_call_uses_current_config_without_any_token_cache(harness):
    domains = ["https://open.feishu.cn", "https://open.larksuite.com/", "https://open.feishu-boe.cn"]
    for index, domain in enumerate(domains):
        harness.config.domain = domain
        harness.config.app_secret = f"offline-secret-{index}"
        harness.responses.append(httpx.Response(200, stream=envelope(code=0, tenant_access_token=f"token-{index}")))
        assert harness.module.get_tenant_token(harness.config, deadline=110.0) == f"token-{index}"
        request = harness.calls[-1]
        assert str(request.url) == domain.rstrip("/") + "/open-apis/auth/v3/tenant_access_token/internal"
        assert json.loads(request.content)["app_secret"] == f"offline-secret-{index}"
    assert len(harness.calls) == 3


@pytest.mark.parametrize("exception,code,retryable", [
    (httpx.ConnectError("leaked-message offline-secret-A"), "connection", True),
    (httpx.ReadError("leaked-message offline-secret-A"), "connection", True),
    (httpx.RemoteProtocolError("leaked-message offline-secret-A"), "connection", True),
    (httpx.ConnectTimeout("leaked-message offline-secret-A"), "timeout", True),
    (httpx.ReadTimeout("leaked-message offline-secret-A"), "timeout", True),
    (httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] leaked-message offline-secret-A"), "tls", False),
])
def test_transport_failure_classification_never_retries_here(harness, exception, code, retryable):
    harness.responses.append(exception)
    error = failed(harness)
    assert (error.error_kind, error.error_code, error.retryable) == ("network", code, retryable)
    assert len(harness.calls) == 1
    assert error.__suppress_context__ is True


def test_wrapped_tls_certificate_failure_is_not_retryable(harness):
    import ssl

    exception = httpx.ConnectError("leaked-message offline-secret-A")
    exception.__cause__ = ssl.SSLCertVerificationError("leaked-message certificate")
    harness.responses.append(exception)
    error = failed(harness)
    assert (error.error_kind, error.error_code, error.retryable) == ("network", "tls", False)


def test_body_read_timeout_is_retryable_and_closes_stream(harness):
    def chunks():
        yield b'{"code":'
        raise httpx.ReadTimeout("leaked-message offline-secret-A")

    stream = Chunks(chunks())
    harness.responses.append(httpx.Response(200, stream=stream))
    error = failed(harness)
    assert (error.error_kind, error.error_code, error.retryable) == ("network", "timeout", True)
    assert stream.closed
    assert len(harness.calls) == 1

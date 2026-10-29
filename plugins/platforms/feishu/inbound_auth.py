"""Scoped tenant authentication for inbound Feishu resources (no SDK cache)."""

import json
import math
import re
import ssl
import time

import httpx

_MAX_ENVELOPE_BYTES = 16 * 1024


class AuthenticationFailure(Exception):
    """Redacted auth failure; retries belong to the resource downloader."""

    def __init__(self, error_kind: str, error_code: str, retryable: bool = False):
        self.error_kind = error_kind
        self.error_code = error_code
        self.retryable = retryable
        super().__init__(error_kind, error_code)


def get_tenant_token(config, *, deadline: float) -> str:
    """Fetch a SELF token using this client's config, never a shared SDK cache.

    ``deadline`` is an absolute ``time.monotonic()`` deadline owned by the
    downloader. Socket I/O is capped at ten seconds and the remaining budget;
    wall time is checked between raw chunks (cooperative, not OS cancellation).
    There are no retries, client mutations, environment credential lookups or
    cached tokens. Only fixed labels / bounded numeric codes escape on failure.
    """
    _remaining(deadline)
    from lark_oapi.core.enum import AppType

    if getattr(config, "app_type", None) is not AppType.SELF:
        raise AuthenticationFailure("authentication", "unsupported_app_type")
    domain = getattr(config, "domain", None)
    if not isinstance(domain, str) or not re.fullmatch(
        r"https://(?:open\.feishu\.cn|open\.larksuite\.com|open\.feishu-boe\.cn)/?", domain
    ):
        raise AuthenticationFailure("invalid_input", "")
    credentials = {"app_id": getattr(config, "app_id", None),
                   "app_secret": getattr(config, "app_secret", None)}
    if not all(isinstance(value, str) and value for value in credentials.values()):
        raise AuthenticationFailure("invalid_input", "")
    try:
        with httpx.Client(timeout=_remaining(deadline), follow_redirects=False) as client:
            with client.stream(
                "POST", domain.rstrip("/") + "/open-apis/auth/v3/tenant_access_token/internal",
                json=credentials, headers={"Accept-Encoding": "identity"},
                timeout=_remaining(deadline),
            ) as response:
                _remaining(deadline)
                status = response.status_code
                if 300 <= status < 400:
                    raise AuthenticationFailure("redirect", str(status))
                if status != 200:
                    # Zero body bytes read on errors: status alone is sufficient.
                    raise AuthenticationFailure("authentication", str(status),
                                                status == 429 or 500 <= status < 600)
                body = _read_envelope(response, deadline)
    except httpx.TimeoutException:
        raise AuthenticationFailure("network", "timeout", True) from None
    except (httpx.NetworkError, httpx.RemoteProtocolError, httpx.ProxyError) as exc:
        tls = _is_tls_error(exc)
        raise AuthenticationFailure("network", "tls" if tls else "connection", not tls) from None
    except ssl.SSLError:
        raise AuthenticationFailure("network", "tls") from None
    except httpx.RequestError:
        raise AuthenticationFailure("protocol", "auth_response_invalid") from None
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        raise AuthenticationFailure("authentication", "auth_response_invalid") from None
    _remaining(deadline)
    code = payload.get("code") if isinstance(payload, dict) else None
    if type(code) is not int or not 0 <= code < 10**12:
        raise AuthenticationFailure("authentication", "auth_response_invalid")
    if code != 0:
        raise AuthenticationFailure("authentication", str(code))
    token = payload.get("tenant_access_token")
    if not isinstance(token, str) or not re.fullmatch(r"[!-~]{1,4096}", token):
        raise AuthenticationFailure("authentication", "auth_response_invalid")
    return token


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if not math.isfinite(remaining) or remaining <= 0:
        raise AuthenticationFailure("timeout", "deadline")
    return min(10.0, remaining)


def _is_tls_error(error):
    # HTTPX/httpcore normally retain the SSL cause; some transports retain only
    # its certificate marker. Inspect it locally, never return exception text.
    for _ in range(16):
        if isinstance(error, ssl.SSLError):
            return True
        text = str(error).lower()
        if "certificate_verify_failed" in text or "certificate verify failed" in text:
            return True
        error = error.__cause__ or error.__context__
        if error is None:
            break
    return False


def _read_envelope(response, deadline):
    if response.headers.get("Content-Encoding", "identity").lower() != "identity":
        raise AuthenticationFailure("protocol", "content_encoding")
    declared = response.headers.get("Content-Length")
    if declared is not None:
        if not re.fullmatch(r"[0-9]{1,20}", declared):
            raise AuthenticationFailure("authentication", "auth_response_invalid")
        if int(declared) > _MAX_ENVELOPE_BYTES:
            raise AuthenticationFailure("authentication", "auth_response_too_large")
    body = bytearray()
    # No chunk_size: HTTPX must not buffer a slow trickle before our wall check.
    chunks = response.iter_raw()
    while True:
        _remaining(deadline)
        try:
            chunk = next(chunks)
        except StopIteration:
            break
        _remaining(deadline)
        if len(body) + len(chunk) > _MAX_ENVELOPE_BYTES:
            raise AuthenticationFailure("authentication", "auth_response_too_large")
        body.extend(chunk)
    _remaining(deadline)
    return body

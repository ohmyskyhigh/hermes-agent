"""Bounded, synchronous Feishu message-resource downloads.

The SDK's ``message_resource.get`` eagerly materializes ``RawResponse.content``
and ``BytesIO``. Only its client configuration is reused here; attachment
bodies are consumed from HTTPX's raw network chunks, never ``read/content``.
The caller owns the cache directory and retention policy. No profile lookup,
environment credential lookup, SDK monkeypatch or client mutation occurs here.
"""

import hashlib
import json
import mimetypes
import os
import re
import ssl
import threading
import time
import uuid
from contextlib import ExitStack
from email.message import Message
from pathlib import Path
from urllib.parse import quote

import httpx
import requests

_DOWNLOAD_SLOTS = threading.BoundedSemaphore(2)
_TOTAL_TIMEOUT = 180.0
_IO_TIMEOUT = 10.0
_RANGE_BYTES = 8 * 1024 * 1024
_MAX_RANGE_REQUESTS = 128
_MAX_BYTES = 512 * 1024 * 1024


class _DownloadFailure(Exception):
    def __init__(self, kind, code="", retryable=False):
        self.kind = kind
        self.code = code
        self.retryable = retryable
        super().__init__(kind)


def _failed(error, attempts):
    return {"state": "failed", "path": "", "mime_type": "", "size_bytes": 0,
            "sha256": "", "attempts": attempts, "error_kind": error.kind,
            "error_code": error.code, "retryable": error.retryable}


def download_resource(client, *, message_id, file_key, resource_type="file",
                      filename="", media_hint="", cache_dir: Path, max_bytes: int) -> dict:
    """Return a ready file or a redacted failure; ordinary failures never escape.

    ``attempts`` includes authentication attempts (zero for invalid input/busy).
    Ready results contain an absolute path, actual size and SHA-256; failures
    contain empty path/MIME/hash and size zero. ``error_code`` is a numeric API
    code, HTTP status or fixed local label, never response/exception text. ``retryable`` marks
    transient network/429/5xx failures even when the three-attempt budget ends.

    The per-process limit is two calls, queue wait <= 10s, socket inactivity
    <= 10s, and a 180s budget across retries checked between raw reads. These
    are cooperative/socket limits, not OS-level hard cancellation. Authentication
    uses the exact client config through the bounded, uncached auth helper.
    Files request 8 MiB ranges; images never send Range. An initial whole HTTP
    200 is supported. The effective byte limit cannot exceed 512 MiB. Retries
    restart the whole attempt with a new partial file and digest.

    Semantic video/audio/media use type=file. A filename extension or concrete
    MIME hint (e.g. video/mp4) can resolve octet-stream; an unnamed ISO media
    container is recognized by its ftyp signature, not a guessed extension. No cover key
    is selected, and image MIME responses for semantic media are rejected.
    """
    valid_ids = all(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,512}", value)
                    for value in (message_id, file_key))
    if (not valid_ids or type(max_bytes) is not int or max_bytes <= 0
            or resource_type not in ("file", "image", "video", "audio", "media")):
        return _failed(_DownloadFailure("invalid_input"), 0)
    max_bytes = min(max_bytes, _MAX_BYTES)
    if not _DOWNLOAD_SLOTS.acquire(timeout=10.0):
        return _failed(_DownloadFailure("busy", "queue_timeout", True), 0)
    try:
        return _download_with_retries(client, message_id=message_id, file_key=file_key,
                                      resource_type=resource_type, filename=filename,
                                      media_hint=media_hint, cache_dir=cache_dir, max_bytes=max_bytes)
    finally:
        _DOWNLOAD_SLOTS.release()


def _download_with_retries(client, **kwargs):
    deadline = time.monotonic() + _TOTAL_TIMEOUT
    for attempt in range(1, 4):
        try:
            _remaining(deadline)
            result = _download_once(client, deadline=deadline, **kwargs)
            result["attempts"] = attempt
            return result
        except _DownloadFailure as exc:
            error = exc
        except httpx.TimeoutException:
            error = _DownloadFailure("network", "timeout", True)
        except (httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            tls = _is_tls_error(exc)
            error = _DownloadFailure("network", "tls" if tls else "connection", not tls)
        except OSError:
            error = _DownloadFailure("filesystem")
        except Exception:
            # Never return exception text: SDK/network errors can contain credentials.
            error = _DownloadFailure("internal")
        if not error.retryable or attempt == 3:
            return _failed(error, attempt)
        time.sleep(0.25 * 2 ** (attempt - 1))
    return _failed(_DownloadFailure("internal"), 3)


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _DownloadFailure("timeout", "deadline")
    return min(_IO_TIMEOUT, remaining)


def _is_tls_error(error):
    for _ in range(8):
        if isinstance(error, (ssl.SSLError, requests.exceptions.SSLError)):
            return True
        error = error.__cause__ or error.__context__
        if error is None:
            return False
    return False


def _auth_from_client(client, deadline):
    from .inbound_auth import AuthenticationFailure, get_tenant_token

    config = client._config
    domain = config.domain
    if not isinstance(domain, str) or domain.rstrip("/") not in (
            "https://open.feishu.cn", "https://open.larksuite.com"):
        raise _DownloadFailure("invalid_input")
    try:
        token = get_tenant_token(config, deadline=deadline)
    except AuthenticationFailure as exc:
        raise _DownloadFailure(exc.error_kind, exc.error_code, exc.retryable) from None
    except httpx.HTTPError:
        raise
    except Exception:
        raise _DownloadFailure("authentication") from None
    return domain.rstrip("/"), token


def _download_once(client, *, message_id, file_key, resource_type, filename, media_hint,
                   cache_dir, max_bytes, deadline):
    domain, token = _auth_from_client(client, deadline)
    expects_media = (resource_type in ("video", "audio", "media")
                     or media_hint.split("/", 1)[0] in ("video", "audio"))
    resource_type = "image" if resource_type == "image" else "file"
    url = (domain + "/open-apis/im/v1/messages/"
           + quote(message_id, safe="") + "/resources/" + quote(file_key, safe=""))

    digest = hashlib.sha256()
    size = 0
    total = None
    etag = None
    output = partial = target = None
    first_headers = {}
    mime = ""
    created = False
    headers = {"Authorization": "Bearer " + token,
               "Accept-Encoding": "identity"}
    try:
        with httpx.Client(timeout=_remaining(deadline), follow_redirects=False) as http, ExitStack() as files:
            while True:
                end = min(size + _RANGE_BYTES, total if total is not None else max_bytes) - 1
                if resource_type == "file":
                    headers["Range"] = f"bytes={size}-{end}"
                with http.stream("GET", url, params={"type": resource_type}, headers=headers,
                                 timeout=_remaining(deadline)) as response:
                    status = response.status_code
                    total, etag, expected = _response_bounds(
                        response, resource_type, size, end, total, etag, max_bytes, deadline)
                    response_mime, suffix = _file_metadata(response.headers, filename, media_hint)
                    if expects_media and response_mime.startswith("image/"):
                        raise _DownloadFailure("media_mismatch", "image_cover")
                    if output is None:
                        mime = response_mime
                        first_headers = response.headers
                        directory = Path(cache_dir).resolve()
                        directory.mkdir(parents=True, exist_ok=True)
                        target = directory / (uuid.uuid4().hex + suffix)
                        partial = target.with_name(target.name + ".part")
                        output = files.enter_context(partial.open("xb"))
                        created = True
                    size = _copy_body(response, output, digest, size, expected, max_bytes, deadline)
                if status == 200 or size == total:
                    break
        assert partial is not None and target is not None
        target, mime = _probe_file(partial, target, mime, first_headers, filename)
        _remaining(deadline)
        os.replace(partial, target)
    finally:
        if created and partial is not None:
            partial.unlink(missing_ok=True)

    return {"state": "ready", "path": str(target), "mime_type": mime,
            "size_bytes": size, "sha256": digest.hexdigest(), "attempts": 1,
            "error_kind": "", "error_code": "", "retryable": False}


def _probe_file(partial, target, mime, headers, filename):
    with partial.open("rb") as probe:
        prefix = probe.read(4096)
    # Feishu business errors can use HTTP 200. A named JSON attachment is data.
    if (mime == "application/json" and not headers.get("Content-Disposition")
            and not filename.lower().endswith(".json")):
        try:
            error_body = json.loads(prefix)
        except (ValueError, UnicodeError):
            error_body = None
        if (isinstance(error_body, dict) and type(error_body.get("code")) is int
                and error_body["code"] != 0):
            code = error_body["code"]
            raise _DownloadFailure("api", str(code) if 0 < code < 10 ** 12 else "")
    if (mime == "application/octet-stream" and len(prefix) >= 12
            and prefix[4:8] == b"ftyp"
            and prefix[8:12] in (b"isom", b"iso2", b"mp41", b"mp42", b"avc1", b"M4V ", b"qt  ")):
        mime, suffix = ("video/quicktime", ".mov") if prefix[8:12] == b"qt  " else ("video/mp4", ".mp4")
        target = target.with_suffix(suffix)
    return target, mime


def _response_bounds(response, resource_type, start, end, total, etag, max_bytes, deadline):
    status = response.status_code
    if 300 <= status < 400:
        raise _DownloadFailure("redirect", str(status))
    if status not in (200, 206):
        raise _http_failure(response, deadline)
    if status == 200 and start:
        raise _DownloadFailure("protocol", "range_mismatch")
    length = response.headers.get("Content-Length", "")
    declared = int(length) if re.fullmatch(r"[0-9]{1,20}", length) else None
    if declared is not None and declared > max_bytes:
        raise _DownloadFailure("size_limit")
    expected = declared
    if status == 206:
        if resource_type != "file":
            raise _DownloadFailure("protocol", "range_mismatch")
        next_total = _range_total(response.headers, start, end, max_bytes)
        if total is not None and next_total != total:
            raise _DownloadFailure("protocol", "range_mismatch")
        total = next_total
        next_etag = response.headers.get("ETag")
        if etag is not None and next_etag is not None and etag != next_etag:
            raise _DownloadFailure("protocol", "etag_mismatch")
        if next_etag is not None:
            etag = next_etag
        expected = min(end + 1, total) - start
        if declared is not None and declared != expected:
            raise _DownloadFailure("protocol", "length_mismatch")
    if response.headers.get("Content-Encoding", "identity").lower() != "identity":
        raise _DownloadFailure("protocol", "content_encoding")
    return total, etag, expected


def _copy_body(response, output, digest, size, expected, max_bytes, deadline):
    response_size = 0
    # Raw reads keep memory bounded and expose slow trickles to the deadline.
    for chunk in response.iter_raw():
        _remaining(deadline)
        size += len(chunk)
        response_size += len(chunk)
        if size > max_bytes:
            raise _DownloadFailure("size_limit")
        if response.status_code == 206 and expected is not None and response_size > expected:
            raise _DownloadFailure("protocol", "length_mismatch")
        output.write(chunk)
        digest.update(chunk)
    if expected is not None and response_size != expected:
        raise _DownloadFailure("protocol", "length_mismatch")
    return size


def _http_failure(response, deadline):
    status = response.status_code
    retryable = status == 429 or 500 <= status < 600
    prefix = bytearray()
    if response.headers.get("Content-Encoding", "identity").lower() == "identity":
        for chunk in response.iter_raw():
            _remaining(deadline)
            prefix.extend(chunk[:4096 - len(prefix)])
            if len(prefix) == 4096:
                break
    try:
        body = json.loads(prefix)
    except (ValueError, UnicodeError):
        body = None
    code = body.get("code") if isinstance(body, dict) else None
    if type(code) is int and 0 < code < 10 ** 12:
        return _DownloadFailure("size_limit" if code == 234037 else "api", str(code), retryable)
    return _DownloadFailure("http", str(status), retryable)


def _range_total(headers, start, requested_end, max_bytes):
    if headers.get("Content-Type", "").lower().startswith("multipart/"):
        raise _DownloadFailure("protocol", "range_mismatch")
    match = re.fullmatch(r"bytes ([0-9]{1,20})-([0-9]{1,20})/([0-9]{1,20})",
                         headers.get("Content-Range", ""))
    if not match:
        raise _DownloadFailure("protocol", "range_mismatch")
    first, last, total = map(int, match.groups())
    if (total <= 0 or first != start or last < first
            or last != min(requested_end, total - 1)):
        raise _DownloadFailure("protocol", "range_mismatch")
    if total > max_bytes:
        raise _DownloadFailure("size_limit")
    if total > _RANGE_BYTES * _MAX_RANGE_REQUESTS:
        raise _DownloadFailure("protocol", "range_mismatch")
    return total


def _file_metadata(headers, filename, media_hint):
    disposition = Message()
    disposition["Content-Disposition"] = headers.get("Content-Disposition", "")
    name = filename or disposition.get_filename() or ""
    suffix = Path(name).suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,12}", suffix):
        suffix = ""
    known = {".mp4": "video/mp4", ".mov": "video/quicktime", ".ogg": "audio/ogg",
             ".wav": "audio/wav", ".mp3": "audio/mpeg", ".png": "image/png"}
    mime = headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if not mime or mime == "application/octet-stream":
        mime = known.get(suffix) or mimetypes.guess_type("file" + suffix)[0]
        if not mime and re.fullmatch(r"(?:video|audio|image)/[a-z0-9.+-]+", media_hint):
            mime = media_hint
    mime = mime or "application/octet-stream"
    if not suffix:
        suffix = next((ext for ext, value in known.items() if value == mime), "")
        suffix = suffix or mimetypes.guess_extension(mime) or ""
    return mime, suffix

"""Offline streaming-resource contracts; no credentials or live API calls."""

import hashlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import httpx
import pytest


@pytest.fixture
def resource_dir(tmp_path):
    directory = tmp_path / "download-cache"
    directory.mkdir()
    return directory


class Chunks(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    def __iter__(self):
        yield from self.chunks

    def close(self):
        self.closed = True


@pytest.fixture
def harness(monkeypatch):
    path = Path(__file__).parents[2] / "plugins/platforms/feishu/inbound_resources.py"
    package = ModuleType("feishu_resource_under_test")
    package.__path__ = [str(path.parent)]
    monkeypatch.setitem(sys.modules, package.__name__, package)
    modules = {}
    for stem in ("inbound_resources", "inbound_auth"):
        name = package.__name__ + "." + stem
        spec = importlib.util.spec_from_file_location(name, path.with_name(stem + ".py"))
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        modules[stem] = module
    module = modules["inbound_resources"]

    pytest.importorskip("lark_oapi")
    from lark_oapi.core.model import Config
    from lark_oapi.core.token.manager import TokenManager
    from lark_oapi.core.http import Transport

    config = Config()
    config.app_id = "offline-app"
    config.app_secret = "offline-secret"
    client = SimpleNamespace(_config=config)
    monkeypatch.setattr(TokenManager, "get_self_tenant_token",
                        lambda *a, **k: pytest.fail("SDK global token cache is forbidden"))
    monkeypatch.setattr(Transport, "execute", lambda *a, **k: pytest.fail("SDK full-body transport is forbidden"))
    original_client = httpx.Client
    calls = []
    responses = []
    auth_calls = []
    auth_responses = []

    def handle(request):
        if request.method == "POST" and request.url.path == "/open-apis/auth/v3/tenant_access_token/internal":
            auth_calls.append(request)
            outcome = auth_responses.pop(0) if auth_responses else httpx.Response(200, stream=Chunks([
                b'{"code":0,"tenant_access_token":"offline-token","expire":7200}']))
        else:
            calls.append(request)
            outcome = responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if callable(outcome):
            return outcome(request)
        return outcome

    def factory(**kwargs):
        return original_client(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(httpx, "Client", factory)

    def download(resource_dir, **kwargs):
        options = dict(message_id="om-test", file_key="file-test", cache_dir=resource_dir, max_bytes=1024)
        options.update(kwargs)
        return module.download_resource(client, **options)

    return SimpleNamespace(module=module, client=client, calls=calls,
                           responses=responses, download=download, auth=modules["inbound_auth"],
                           auth_calls=auth_calls, auth_responses=auth_responses)


def test_streams_authenticated_resource_to_disk(harness, resource_dir):
    assert callable(harness.module.download_resource), "streaming download API must exist"
    stream = Chunks([b"first-", b"second"])
    harness.responses.append(httpx.Response(200, headers={"Content-Type": "application/pdf"}, stream=stream))
    result = harness.download(resource_dir, filename="report.pdf")
    assert result == {
        "state": "ready", "path": result["path"], "mime_type": "application/pdf",
        "size_bytes": 12, "sha256": hashlib.sha256(b"first-second").hexdigest(),
        "attempts": 1, "error_kind": "", "error_code": "", "retryable": False,
    }
    target = Path(result["path"])
    assert target.read_bytes() == b"first-second"
    assert target.parent == resource_dir.resolve()
    assert target.suffix == ".pdf"
    request = harness.calls[0]
    assert request.url.path == "/open-apis/im/v1/messages/om-test/resources/file-test"
    assert request.url.params["type"] == "file"
    assert request.headers["Authorization"] == "Bearer offline-token"
    assert all(0 < value <= 30 for value in request.extensions["timeout"].values())
    assert stream.closed


@pytest.mark.parametrize("declared", [None, "1", "invalid", "-1", "10000"])
def test_enforces_actual_byte_limit_and_removes_partial_files(harness, resource_dir, declared):
    headers = {} if declared is None else {"Content-Length": declared}
    stream = Chunks([b"a" * 600, b"b" * 600])
    harness.responses.append(httpx.Response(200, headers=headers, stream=stream))
    result = harness.download(resource_dir)
    assert result["state"] == "failed"
    assert result["error_kind"] == "size_limit"
    assert result["retryable"] is False
    assert result["path"] == result["sha256"] == ""
    assert result["attempts"] == 1
    assert list(resource_dir.iterdir()) == []
    assert stream.closed


def test_publishes_only_after_complete_stream_by_atomic_rename(harness, resource_dir, monkeypatch):
    import os

    seen = []
    original_replace = os.replace

    def replace(source, target):
        seen.append((Path(source), Path(target)))
        assert Path(source).suffix == ".part"
        assert Path(source).read_bytes() == b"complete"
        assert not Path(target).exists()
        return original_replace(source, target)

    monkeypatch.setattr(os, "replace", replace)

    def chunks():
        yield b"com"
        entries = list(resource_dir.iterdir())
        assert len(entries) == 1 and entries[0].suffix == ".part"
        yield b"plete"

    harness.responses.append(httpx.Response(200, stream=Chunks(chunks())))
    result = harness.download(resource_dir, filename="../../CON:unsafe\\video.mp4")
    assert result["state"] == "ready"
    assert len(seen) == 1
    assert Path(result["path"]).parent == resource_dir.resolve()
    assert Path(result["path"]).suffix == ".mp4"
    assert list(resource_dir.glob("*.part")) == []


@pytest.mark.parametrize("outcomes, state, kind, code, retryable", [
    ([429, 503, 200], "ready", "", "", False),
    ([503, 503, 503], "failed", "http", "503", True),
    ([429, 429, 429], "failed", "http", "429", True),
    ([403], "failed", "http", "403", False),
    ([404], "failed", "http", "404", False),
    ([302], "failed", "redirect", "302", False),
    ([httpx.ConnectError("offline-secret"), 200], "ready", "", "", False),
    ([httpx.ReadTimeout("offline-token")] * 3, "failed", "network", "timeout", True),
])
def test_retries_only_transient_failures_with_bounded_backoff(
        harness, resource_dir, monkeypatch, outcomes, state, kind, code, retryable):
    import time

    sleeps = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    for outcome in outcomes:
        harness.responses.append(httpx.Response(outcome, headers={
            "Location": "https://untrusted.example/steal", "Retry-After": "999999"},
            stream=Chunks([b"ok"])) if isinstance(outcome, int) else outcome)
    result = harness.download(resource_dir)
    assert result["state"] == state
    assert result["error_kind"] == kind
    assert result["error_code"] == code
    assert result["retryable"] is retryable
    assert result["attempts"] == len(outcomes)
    assert len(harness.calls) == len(outcomes)
    assert len(sleeps) == len(outcomes) - 1
    assert all(0 < delay <= 2 for delay in sleeps)
    assert "offline-secret" not in str(result) and "offline-token" not in str(result)
    assert list(resource_dir.glob("*.part")) == []
    if state == "failed":
        assert list(resource_dir.iterdir()) == []


@pytest.mark.parametrize("resource_type, filename, hint, disposition, expected_type, mime, suffix", [
    ("video", "video.mp4", "", "", "file", "video/mp4", ".mp4"),
    ("audio", "audio.ogg", "", "", "file", "audio/ogg", ".ogg"),
    ("media", "", "video/mp4", "", "file", "video/mp4", ".mp4"),
    ("file", "", "", "attachment; filename*=UTF-8''clip%20one.mov", "file", "video/quicktime", ".mov"),
    ("image", "image.png", "", "", "image", "image/png", ".png"),
    ("file", "", "audio/wav", "", "file", "audio/wav", ".wav"),
])
def test_media_download_type_and_reliable_mime_preserve_extension(
        harness, resource_dir, resource_type, filename, hint, disposition, expected_type, mime, suffix):
    harness.responses.append(httpx.Response(200, headers={
        "Content-Type": "application/octet-stream", "Content-Disposition": disposition},
        stream=Chunks([b"actual-file-not-cover"])))
    result = harness.download(resource_dir, resource_type=resource_type, filename=filename, media_hint=hint)
    assert harness.calls[0].url.params["type"] == expected_type
    assert harness.calls[0].url.path.endswith("/resources/file-test")
    assert result["mime_type"] == mime
    assert Path(result["path"]).suffix == suffix
    assert Path(result["path"]).read_bytes() == b"actual-file-not-cover"


@pytest.mark.parametrize("stage, kind", [("auth", "authentication"), ("disk", "filesystem"),
                                         ("stream", "internal"), ("close", "internal")])
def test_business_failures_are_redacted_and_cleanup_partial_files(harness, resource_dir, monkeypatch, stage, kind):
    import os

    def fail(*args, **kwargs):
        if stage == "disk":
            raise PermissionError("offline-secret private-path")
        raise ValueError("offline-token offline-secret private-path")

    if stage == "auth":
        monkeypatch.setattr(harness.auth, "get_tenant_token", fail)
    elif stage == "disk":
        monkeypatch.setattr(os, "replace", fail)

    def chunks():
        yield b"partial"
        if stage == "stream":
            fail()

    stream = Chunks(chunks())
    if stage == "close":
        def fail_close():
            stream.closed = True
            fail()
        monkeypatch.setattr(stream, "close", fail_close)
    harness.responses.append(httpx.Response(200, stream=stream))
    result = harness.download(resource_dir)
    assert result["state"] == "failed"
    assert result["error_kind"] == kind
    assert result["attempts"] == 1
    assert result["retryable"] is False
    assert not any(word in str(result) for word in ("offline-token", "offline-secret", "private-path"))
    assert list(resource_dir.iterdir()) == []
    assert stage == "auth" or stream.closed


@pytest.mark.parametrize("headers, expected_kind, expected_code", [
    ({"Content-Length": "2048"}, "size_limit", ""),
    ({"Content-Encoding": "gzip"}, "protocol", "content_encoding"),
    ({"Content-Length": "9"}, "protocol", "length_mismatch"),
])
def test_rejects_unsafe_response_metadata(harness, resource_dir, headers, expected_kind, expected_code):
    reads = []

    def chunks():
        reads.append(True)
        yield b"tiny"

    harness.responses.append(httpx.Response(200, headers=headers, stream=Chunks(chunks())))
    result = harness.download(resource_dir)
    assert result["state"] == "failed"
    assert result["error_kind"] == expected_kind
    assert result["error_code"] == expected_code
    assert result["retryable"] is False
    assert list(resource_dir.iterdir()) == []
    if expected_code != "length_mismatch":
        assert reads == []
    assert harness.calls[0].headers["Accept-Encoding"] == "identity"


def test_global_download_concurrency_is_bounded_to_two(harness, resource_dir):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    lock = threading.Lock()
    release = threading.Event()
    all_entered = threading.Event()
    start = threading.Barrier(4)
    active = peak = 0

    def serve(request):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active == 4:
                all_entered.set()

        def chunks():
            nonlocal active
            try:
                assert release.wait(10)
                yield b"ok"
            finally:
                with lock:
                    active -= 1

        return httpx.Response(200, stream=Chunks(chunks()))

    harness.responses.extend([serve] * 4)

    def run():
        start.wait(timeout=10)
        return harness.download(resource_dir)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(run) for _ in range(4)]
        try:
            all_entered.wait(2)
        finally:
            release.set()
        results = [future.result(timeout=10) for future in futures]
    assert all(result["state"] == "ready" for result in results)
    assert peak == 2
    assert len({result["path"] for result in results}) == 4


def test_slow_stream_cannot_outlive_total_deadline(harness, resource_dir, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(harness.module, "time", SimpleNamespace(
        monotonic=lambda: now[0], sleep=lambda delay: None))
    monkeypatch.setattr(harness.auth, "time", harness.module.time)

    def chunks():
        yield b"first"
        now[0] = 10000.0
        yield b"late"

    harness.responses.append(httpx.Response(200, stream=Chunks(chunks())))
    result = harness.download(resource_dir)
    assert result["state"] == "failed"
    assert result["error_kind"] == "timeout"
    assert result["error_code"] == "deadline"
    assert result["attempts"] == 1
    assert list(resource_dir.iterdir()) == []


def test_queue_wait_is_bounded_without_starting_auth_or_http(harness, resource_dir, monkeypatch):
    timeouts = []

    class Occupied:
        def __enter__(self):
            pytest.fail("unbounded semaphore wait")

        def __exit__(self, *args):
            return False

        def acquire(self, *, timeout):
            timeouts.append(timeout)
            return False

        def release(self):
            pytest.fail("must not release an unacquired slot")

    monkeypatch.setattr(harness.module, "_DOWNLOAD_SLOTS", Occupied())
    result = harness.download(resource_dir)
    assert result["state"] == "failed"
    assert result["error_kind"] == "busy"
    assert result["retryable"] is True
    assert result["attempts"] == 0
    assert len(timeouts) == 1 and 0 < timeouts[0] <= 10
    assert harness.calls == []


@pytest.mark.parametrize("overrides", [
    {"max_bytes": 0}, {"max_bytes": -1}, {"max_bytes": True}, {"max_bytes": "1024"},
    {"message_id": ""}, {"message_id": ".."}, {"file_key": "../../other"},
    {"resource_type": "unsupported"},
])
def test_invalid_inputs_fail_before_auth_or_disk(harness, resource_dir, overrides):
    harness.responses.append(httpx.Response(200, stream=Chunks([b"ok"])))
    result = harness.download(resource_dir, **overrides)
    assert result["state"] == "failed"
    assert result["error_kind"] == "invalid_input"
    assert result["attempts"] == 0
    assert result["retryable"] is False
    assert harness.calls == []
    assert list(resource_dir.iterdir()) == []


@pytest.mark.parametrize("configured_timeout", [None, 600])
def test_auth_receives_exact_client_config_and_deadline(harness, resource_dir, monkeypatch, configured_timeout):
    original = harness.client._config
    original.timeout = configured_timeout
    seen = []

    def token(config, *, deadline):
        seen.append(config)
        assert config is original
        assert config.app_id == original.app_id and config.app_secret == original.app_secret
        assert 0 < deadline - harness.module.time.monotonic() <= 180
        return "offline-token"

    monkeypatch.setattr(harness.auth, "get_tenant_token", token)
    harness.responses.append(httpx.Response(200, stream=Chunks([b"ok"])))
    result = harness.download(resource_dir)
    assert result["state"] == "ready"
    assert len(seen) == 1
    assert original.timeout == configured_timeout


@pytest.mark.parametrize("stage, state, attempts, code", [
    ("auth_timeout", "ready", 2, ""), ("midstream", "ready", 2, ""),
    ("tls", "failed", 1, "tls"),
])
def test_transient_auth_or_stream_errors_retry_but_certificate_errors_do_not(
        harness, resource_dir, monkeypatch, stage, state, attempts, code):
    import ssl
    import time

    monkeypatch.setattr(time, "sleep", lambda delay: None)
    if stage == "auth_timeout":
        harness.auth_responses.append(httpx.ReadTimeout("offline-secret"))
    if stage == "tls":
        error = httpx.ConnectError("offline-token")
        error.__cause__ = ssl.SSLCertVerificationError("offline-secret")
        harness.responses.append(error)
    elif stage == "midstream":
        def broken():
            yield b"must-not-survive"
            raise httpx.ReadError("offline-token")
        harness.responses.append(httpx.Response(200, stream=Chunks(broken())))
    harness.responses.append(httpx.Response(200, stream=Chunks([b"fresh"])))
    result = harness.download(resource_dir)
    assert result["state"] == state
    assert result["attempts"] == attempts
    assert result["error_code"] == code
    assert list(resource_dir.glob("*.part")) == []
    if state == "ready":
        assert Path(result["path"]).read_bytes() == b"fresh"
        assert result["size_bytes"] == 5
    else:
        assert result["retryable"] is False


@pytest.mark.parametrize("status", [200, 206])
def test_large_resource_creates_cache_directory_without_buffering_body(harness, resource_dir, status):
    import tracemalloc

    size = 134458842
    chunk = b"x" * 65536
    expected = hashlib.sha256()

    def chunks(remaining):
        while remaining:
            piece = chunk[:min(len(chunk), remaining)]
            expected.update(piece)
            remaining -= len(piece)
            yield piece

    responses = []

    def serve(request):
        if status == 206:
            start, end = map(int, request.headers["Range"][6:].split("-"))
            end = min(end, size - 1)
            headers = {"Content-Range": f"bytes {start}-{end}/{size}"}
            length = end - start + 1
        else:
            headers, length = {}, size
        headers["Content-Type"] = "application/octet-stream"
        response = httpx.Response(status, headers=headers, stream=Chunks(chunks(length)))
        responses.append(response)
        return response

    harness.responses.extend([serve] * ((size + harness.module._RANGE_BYTES - 1) // harness.module._RANGE_BYTES))
    cache = resource_dir / "new-cache" / "resources"
    tracemalloc.start()
    try:
        result = harness.download(cache, max_bytes=512 * 1024 * 1024, filename="video.mp4")
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert result["state"] == "ready"
    assert result["size_bytes"] == size
    assert result["sha256"] == expected.hexdigest()
    assert Path(result["path"]).stat().st_size == size
    assert Path(result["path"]).suffix == ".mp4"
    assert result["mime_type"] == "video/mp4"
    assert peak < 4 * 1024 * 1024
    assert len(harness.calls) == (1 if status == 200 else (size + harness.module._RANGE_BYTES - 1) // harness.module._RANGE_BYTES)
    for response in responses:
        with pytest.raises(httpx.ResponseNotRead):
            _ = response.content


@pytest.mark.parametrize("resource_type, hint", [("video", ""), ("media", "video"),
                                                   ("file", "video/mp4"), ("audio", "")])
def test_image_cover_is_not_accepted_as_media_body(harness, resource_dir, resource_type, hint):
    harness.responses.append(httpx.Response(200, headers={"Content-Type": "image/jpeg"},
                                            stream=Chunks([b"cover"])))
    result = harness.download(resource_dir, resource_type=resource_type, media_hint=hint,
                              filename="video.mp4")
    assert harness.calls[0].url.params["type"] == "file"
    assert result["state"] == "failed"
    assert result["error_kind"] == "media_mismatch"
    assert result["retryable"] is False
    assert list(resource_dir.iterdir()) == []


def test_failed_exclusive_create_does_not_delete_preexisting_file(harness, resource_dir, monkeypatch):
    monkeypatch.setattr(harness.module.uuid, "uuid4", lambda: SimpleNamespace(hex="occupied"))
    existing = resource_dir / "occupied.mp4.part"
    existing.write_bytes(b"not-owned-by-this-download")
    harness.responses.append(httpx.Response(200, stream=Chunks([b"new"])))
    result = harness.download(resource_dir, filename="video.mp4")
    assert result["state"] == "failed"
    assert result["error_kind"] == "filesystem"
    assert existing.exists() and existing.read_bytes() == b"not-owned-by-this-download"
    assert list(resource_dir.iterdir()) == [existing]


def test_unnamed_octet_stream_video_is_classified_from_magic(harness, resource_dir):
    body = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 20
    harness.responses.append(httpx.Response(200, headers={"Content-Type": "application/octet-stream"}, stream=Chunks([body])))
    result = harness.download(resource_dir, media_hint="video")
    assert result["state"] == "ready"
    assert result["mime_type"] == "video/mp4" and Path(result["path"]).suffix == ".mp4"


def test_file_streams_two_ranges_into_one_atomic_file(harness, resource_dir, monkeypatch):
    monkeypatch.setattr(harness.module, "_RANGE_BYTES", 4, raising=False)
    responses = [httpx.Response(206, headers={
        "Content-Range": content_range, "Content-Type": "video/mp4"}, stream=Chunks(parts))
        for content_range, parts in [("bytes 0-3/6", [b"ab", b"cd"]),
                                      ("bytes 4-5/6", [b"e", b"f"])]]
    harness.responses.extend(responses)
    result = harness.download(resource_dir, resource_type="video")
    assert result["state"] == "ready"
    assert [call.headers.get("Range") for call in harness.calls] == ["bytes=0-3", "bytes=4-5"]
    assert all(call.headers["Authorization"] == "Bearer offline-token" for call in harness.calls)
    assert all(call.url.params["type"] == "file" for call in harness.calls)
    assert result["size_bytes"] == 6 and result["attempts"] == 1
    assert result["sha256"] == hashlib.sha256(b"abcdef").hexdigest()
    assert result["mime_type"] == "video/mp4"
    assert Path(result["path"]).read_bytes() == b"abcdef"
    assert list(resource_dir.iterdir()) == [Path(result["path"])]
    for response in responses:
        assert response.is_closed
        with pytest.raises(httpx.ResponseNotRead):
            _ = response.content


@pytest.mark.parametrize("content_range, kind", [
    (None, "protocol"), ("", "protocol"), ("bytes */8", "protocol"),
    ("bytes 0-3/*", "protocol"), ("bytes 0-3/0", "protocol"),
    ("bytes 0-3/-8", "protocol"), ("items 0-3/8", "protocol"),
    ("bytes 1-3/8", "protocol"), ("bytes 3-0/8", "protocol"),
    ("bytes 0-4/8", "protocol"), ("bytes 0-2/8", "protocol"),
    ("bytes 0-3/3", "protocol"), ("bytes 0-3/8, bytes 0-3/8", "protocol"),
    ("bytes 0-3/9", "size_limit"),
])
def test_rejects_invalid_range_metadata_before_reading_body(
        harness, resource_dir, monkeypatch, content_range, kind):
    monkeypatch.setattr(harness.module, "_RANGE_BYTES", 4)
    reads = []

    def chunks():
        reads.append(True)
        yield b"abcd"

    headers = {} if content_range is None else {"Content-Range": content_range}
    stream = Chunks(chunks())
    harness.responses.append(httpx.Response(206, headers=headers, stream=stream))
    result = harness.download(resource_dir, max_bytes=8)
    assert result["state"] == "failed" and result["error_kind"] == kind
    assert result["retryable"] is False and result["attempts"] == 1
    assert reads == [] and stream.closed
    assert len(harness.calls) == 1 and list(resource_dir.iterdir()) == []


def test_range_accepts_total_exactly_at_size_boundary(harness, resource_dir, monkeypatch):
    monkeypatch.setattr(harness.module, "_RANGE_BYTES", 4)
    harness.responses.extend([
        httpx.Response(206, headers={"Content-Range": "bytes 0-3/8"}, stream=Chunks([b"abcd"])),
        httpx.Response(206, headers={"Content-Range": "bytes 4-7/8"}, stream=Chunks([b"efgh"])),
    ])
    result = harness.download(resource_dir, max_bytes=8)
    assert result["state"] == "ready" and result["size_bytes"] == 8
    assert Path(result["path"]).read_bytes() == b"abcdefgh"


@pytest.mark.parametrize("body, declared", [
    (b"", None), (b"abc", None), (b"abcde", None),
    (b"a", "1"), (b"abcde", "5"), (b"abc", "4"), (b"abcde", "4"),
])
def test_range_checks_actual_bytes_against_content_range(harness, resource_dir, monkeypatch, body, declared):
    monkeypatch.setattr(harness.module, "_RANGE_BYTES", 4)
    headers = {"Content-Range": "bytes 0-3/4"}
    if declared is not None:
        headers["Content-Length"] = declared

    def chunks():
        yield body
        if len(body) > 4:
            pytest.fail("oversized range must stop before the next raw read")

    stream = Chunks(chunks())
    harness.responses.append(httpx.Response(206, headers=headers, stream=stream))
    result = harness.download(resource_dir)
    assert result["state"] == "failed" and result["error_kind"] == "protocol"
    assert result["error_code"] == "length_mismatch"
    assert result["attempts"] == 1 and result["retryable"] is False
    assert len(harness.calls) == 1 and stream.closed
    assert list(resource_dir.iterdir()) == []


@pytest.mark.parametrize("status, headers, code", [
    (206, {"Content-Range": "bytes 0-3/8"}, "range_mismatch"),  # repeated
    (206, {"Content-Range": "bytes 3-7/8"}, "range_mismatch"),  # overlap
    (206, {"Content-Range": "bytes 5-7/8"}, "range_mismatch"),  # gap
    (206, {"Content-Range": "bytes 4-7/9"}, "range_mismatch"),
    (206, {"Content-Range": "bytes 4-6/7"}, "range_mismatch"),
    (206, {"Content-Range": "bytes 4-7/8", "ETag": '"changed"'}, "etag_mismatch"),
    (200, {}, "range_mismatch"),
])
def test_range_continuation_must_describe_same_resource(harness, resource_dir, monkeypatch, status, headers, code):
    monkeypatch.setattr(harness.module, "_RANGE_BYTES", 4)
    reads = []

    def chunks():
        reads.append(True)
        yield b"efgh"

    harness.responses.extend([
        httpx.Response(206, headers={"Content-Range": "bytes 0-3/8", "ETag": '"original"'},
                       stream=Chunks([b"abcd"])),
        httpx.Response(status, headers=headers, stream=Chunks(chunks())),
    ])
    result = harness.download(resource_dir)
    assert result["state"] == "failed" and result["error_kind"] == "protocol"
    assert result["error_code"] == code
    assert result["attempts"] == 1 and result["retryable"] is False
    assert reads == [] and len(harness.calls) == 2
    assert list(resource_dir.iterdir()) == []


@pytest.mark.parametrize("resource_type, content_type, code", [
    ("image", "image/png", "range_mismatch"),
    ("file", "multipart/byteranges; boundary=offline", "range_mismatch"),
])
def test_rejects_unsolicited_or_multipart_ranges(harness, resource_dir, resource_type, content_type, code):
    reads = []

    def chunks():
        reads.append(True)
        yield b"abcd"

    harness.responses.append(httpx.Response(206, headers={
        "Content-Range": "bytes 0-3/4", "Content-Type": content_type}, stream=Chunks(chunks())))
    result = harness.download(resource_dir, resource_type=resource_type)
    assert result["state"] == "failed" and result["error_kind"] == "protocol"
    assert result["error_code"] == code and result["retryable"] is False
    assert reads == [] and list(resource_dir.iterdir()) == []
    if resource_type == "image":
        assert "Range" not in harness.calls[0].headers


def test_range_rejects_excessive_request_count(harness, resource_dir, monkeypatch):
    monkeypatch.setattr(harness.module, "_RANGE_BYTES", 4)
    monkeypatch.setattr(harness.module, "_MAX_RANGE_REQUESTS", 2, raising=False)
    reads = []

    def chunks():
        reads.append(True)
        yield b"abcd"

    harness.responses.append(httpx.Response(206, headers={"Content-Range": "bytes 0-3/9"},
                                            stream=Chunks(chunks())))
    result = harness.download(resource_dir)
    assert result["state"] == "failed" and result["error_kind"] == "protocol"
    assert result["error_code"] == "range_mismatch" and result["retryable"] is False
    assert reads == [] and len(harness.calls) == 1 and list(resource_dir.iterdir()) == []


@pytest.mark.parametrize("resource_type, expected_range", [("file", "bytes=0-8388607"), ("image", None)])
def test_first_request_range_policy_accepts_whole_200(harness, resource_dir, resource_type, expected_range):
    harness.responses.append(httpx.Response(200, stream=Chunks([b"whole"])))
    result = harness.download(resource_dir, resource_type=resource_type, max_bytes=16 * 1024 * 1024)
    assert result["state"] == "ready" and len(harness.calls) == 1
    assert harness.calls[0].headers.get("Range") == expected_range
    assert Path(result["path"]).read_bytes() == b"whole"


@pytest.mark.parametrize("code, kind", [(234037, "size_limit"), (99991663, "api"), (234001, "api")])
def test_http_400_preserves_only_api_error_code(harness, resource_dir, code, kind):
    import json

    body = json.dumps({"code": code, "msg": "offline-token offline-secret private-path"}).encode()
    response = httpx.Response(400, headers={"Content-Type": "application/json"},
                              stream=Chunks([body[:10], body[10:]]))
    harness.responses.append(response)
    result = harness.download(resource_dir)
    assert result["state"] == "failed" and result["error_kind"] == kind
    assert result["error_code"] == str(code) and result["retryable"] is False
    assert result["attempts"] == 1 and response.is_closed
    assert not any(value in str(result) for value in ("offline-token", "offline-secret", "private-path"))
    assert list(resource_dir.iterdir()) == []
    with pytest.raises(httpx.ResponseNotRead):
        _ = response.content


def test_http_error_probe_is_bounded_to_4096_bytes(harness, resource_dir):
    reads = []

    def chunks():
        for _ in range(4):
            reads.append(True)
            yield b" " * 1024
        pytest.fail("error-body probe read beyond 4096 bytes")

    stream = Chunks(chunks())
    harness.responses.append(httpx.Response(400, headers={"Content-Type": "application/json"}, stream=stream))
    result = harness.download(resource_dir)
    assert result["state"] == "failed" and result["error_kind"] == "http"
    assert result["error_code"] == "400" and result["attempts"] == 1
    assert len(reads) == 4 and stream.closed
    assert list(resource_dir.iterdir()) == []


@pytest.mark.parametrize("status, headers", [
    (200, {"Content-Length": str(512 * 1024 * 1024 + 1)}),
    (206, {"Content-Range": f"bytes 0-8388607/{512 * 1024 * 1024 + 1}"}),
])
def test_hard_cap_cannot_be_raised_by_caller(harness, resource_dir, status, headers):
    reads = []

    def chunks():
        reads.append(True)
        yield b"tiny"

    harness.responses.append(httpx.Response(status, headers=headers, stream=Chunks(chunks())))
    result = harness.download(resource_dir, max_bytes=10 ** 30)
    assert result["state"] == "failed" and result["error_kind"] == "size_limit"
    assert result["attempts"] == 1 and result["retryable"] is False
    assert reads == [] and list(resource_dir.iterdir()) == []


@pytest.mark.parametrize("domain", [
    "http://open.feishu.cn", "https://open.feishu.cn.evil.example", "https://evil.example",
    "https://open.feishu.cn@evil.example", "https://open.feishu.cn/path", "https://open.feishu.cn?x=1",
])
def test_invalid_domain_fails_before_auth_or_resource_request(harness, resource_dir, domain):
    harness.client._config.domain = domain
    result = harness.download(resource_dir)
    assert result["state"] == "failed" and result["error_kind"] == "invalid_input"
    assert harness.auth_calls == harness.calls == []
    assert list(resource_dir.iterdir()) == []


@pytest.mark.parametrize("domain", ["https://open.feishu.cn", "https://open.larksuite.com/"])
def test_real_auth_helper_uses_same_origin_without_sdk_cache(harness, resource_dir, domain):
    import json

    harness.client._config.domain = domain
    harness.responses.append(httpx.Response(200, stream=Chunks([b"ok"])))
    result = harness.download(resource_dir)
    assert result["state"] == "ready"
    assert len(harness.auth_calls) == 1
    auth_request = harness.auth_calls[0]
    assert json.loads(auth_request.content) == {"app_id": "offline-app", "app_secret": "offline-secret"}
    assert auth_request.url.host == harness.calls[0].url.host
    assert all(0 < value <= 10 for value in auth_request.extensions["timeout"].values())


@pytest.mark.parametrize("status, kind, retryable, attempts", [
    (302, "redirect", False, 1), (403, "authentication", False, 1),
    (503, "authentication", True, 3), (429, "authentication", True, 3),
])
def test_auth_failures_translate_without_downloading(harness, resource_dir, monkeypatch, status, kind, retryable, attempts):
    monkeypatch.setattr(harness.module.time, "sleep", lambda delay: None)
    harness.auth_responses.extend(httpx.Response(status, headers={"Location": "https://evil.example"},
                                               stream=Chunks([b"offline-secret"])) for _ in range(attempts))
    result = harness.download(resource_dir)
    assert result["state"] == "failed" and result["error_kind"] == kind
    assert result["error_code"] == str(status) and result["retryable"] is retryable
    assert result["attempts"] == attempts and len(harness.auth_calls) == attempts
    assert harness.calls == [] and list(resource_dir.iterdir()) == []


@pytest.mark.parametrize("failure", ["network", "429", "503"])
def test_range_retry_restarts_partial_and_digest_without_duplication(harness, resource_dir, monkeypatch, failure):
    monkeypatch.setattr(harness.module, "_RANGE_BYTES", 4)
    monkeypatch.setattr(harness.module.time, "sleep", lambda delay: None)

    def broken():
        yield b"xx"
        raise httpx.ReadError("offline-secret")

    failed_response = (httpx.Response(206, headers={"Content-Range": "bytes 4-7/8"}, stream=Chunks(broken()))
                       if failure == "network" else httpx.Response(int(failure), stream=Chunks([b"busy"])))
    harness.responses.extend([
        httpx.Response(206, headers={"Content-Range": "bytes 0-3/8"}, stream=Chunks([b"old!"])),
        failed_response,
        httpx.Response(206, headers={"Content-Range": "bytes 0-3/8"}, stream=Chunks([b"abcd"])),
        httpx.Response(206, headers={"Content-Range": "bytes 4-7/8"}, stream=Chunks([b"efgh"])),
    ])
    result = harness.download(resource_dir)
    assert result["state"] == "ready" and result["attempts"] == 2
    assert result["size_bytes"] == 8 and result["sha256"] == hashlib.sha256(b"abcdefgh").hexdigest()
    assert Path(result["path"]).read_bytes() == b"abcdefgh"
    assert [call.headers["Range"] for call in harness.calls] == ["bytes=0-3", "bytes=4-7"] * 2
    assert len(harness.auth_calls) == 2 and list(resource_dir.iterdir()) == [Path(result["path"])]


def test_range_preserves_first_response_mime(harness, resource_dir, monkeypatch):
    monkeypatch.setattr(harness.module, "_RANGE_BYTES", 4)
    harness.responses.extend([
        httpx.Response(206, headers={"Content-Range": "bytes 0-3/8", "Content-Type": "video/mp4"},
                       stream=Chunks([b"abcd"])),
        httpx.Response(206, headers={"Content-Range": "bytes 4-7/8"}, stream=Chunks([b"efgh"])),
    ])
    result = harness.download(resource_dir)
    assert result["state"] == "ready" and result["mime_type"] == "video/mp4"
    assert Path(result["path"]).suffix == ".mp4"


@pytest.mark.parametrize("code, expected_code", [(99991663, "99991663"), (10 ** 12, ""), (-1, "")])
def test_http_200_api_failure_is_not_a_downloaded_attachment(harness, resource_dir, code, expected_code):
    import json

    body = json.dumps({"code": code, "msg": "offline-secret"}).encode()
    harness.responses.append(httpx.Response(200, headers={"Content-Type": "application/json"}, stream=Chunks([body])))
    result = harness.download(resource_dir, media_hint="video")
    assert result["state"] == "failed"
    assert result["error_kind"] == "api" and result["error_code"] == expected_code
    assert "offline-secret" not in str(result)
    assert list(resource_dir.iterdir()) == []

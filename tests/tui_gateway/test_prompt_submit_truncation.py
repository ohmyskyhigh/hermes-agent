"""Transcript truncation identity contracts with scoped, completed dispatch workers.

The fixtures assert history and durable row identity; their placeholder agents cannot run
inference. Join every real dispatch before monkeypatch teardown so no continuation resolves
another test's agent/emitter, and keep this group isolated by the canonical per-file runner.
"""
import threading

import pytest

from tui_gateway import server
from tests.tui_gateway.test_tui_gateway_server import _join_turn_thread, _session


@pytest.fixture(autouse=True)
def _scope_truncation_dispatch(monkeypatch):
    monkeypatch.setattr(server, "_run_after_agent_ready", lambda *args, **kwargs: None)


def test_prompt_submit_truncates_by_message_id(monkeypatch):
    """#82756: truncate_before_message_id resolves target message and cuts history accurately."""
    replaced = []

    class _FakeDB:
        def replace_messages(
            self,
            key,
            messages,
            active_only=False,
            archive_dropped=False,
            reject_active_turn_lease=False,
        ):
            replaced.append((key, list(messages)))

    history = [
        {"id": "msg-1", "role": "user", "content": "first"},
        {"role": "assistant", "content": "reply 1"},
        {"id": "msg-2", "role": "user", "content": "second"},
        {"role": "assistant", "content": "reply 2"},
    ]
    server._sessions["msg-id-trunc-sid"] = _session(history=list(history))
    monkeypatch.setattr(server, "_get_db", lambda: _FakeDB())
    monkeypatch.setattr(
        server, "_start_agent_build", lambda *a, **k: None
    )
    monkeypatch.setattr(
        server, "_start_inflight_turn", lambda *a, **k: None
    )

    try:
        resp = server.handle_request(
            {
                "id": "1",
                "method": "prompt.submit",
                "params": {
                    "session_id": "msg-id-trunc-sid",
                    "text": "new turn",
                    "truncate_before_message_id": "msg-2",
                    "confirm_truncate": True,
                },
            }
        )
        assert resp.get("result") is not None
        assert len(replaced) == 1
        assert replaced[0][1] == history[:2]
    finally:
        _join_turn_thread(server._sessions['msg-id-trunc-sid'])
        server._sessions.pop("msg-id-trunc-sid", None)


def test_prompt_submit_truncation_falls_back_to_sid_when_session_key_null(monkeypatch):
    """#81904: a NULL session_key must not FK-fail the truncation persist.

    CLI-origin sessions resumed in the Desktop have no session_key; the
    truncation path used to call replace_messages(None, ...), whose reinsert
    violated the messages.session_id FK ("FOREIGN KEY constraint failed" →
    "Restore failed"). The persist must key off the session id instead —
    for CLI-origin rows the durable sessions.id IS the requested sid.
    """
    replaced = []

    class _FakeDB:
        def replace_messages(
            self,
            key,
            messages,
            active_only=False,
            archive_dropped=False,
            reject_active_turn_lease=False,
        ):
            replaced.append((key, list(messages)))

    history = [
        {"_row_id": 101, "role": "user", "content": "first"},
        {"_row_id": 102, "role": "assistant", "content": "reply 1"},
        {"_row_id": 103, "role": "user", "content": "second"},
        {"_row_id": 104, "role": "assistant", "content": "reply 2"},
    ]
    server._sessions["null-key-trunc-sid"] = _session(
        history=list(history), session_key=None
    )
    monkeypatch.setattr(server, "_get_db", lambda: _FakeDB())
    monkeypatch.setattr(server, "_start_agent_build", lambda *a, **k: None)
    monkeypatch.setattr(server, "_start_inflight_turn", lambda *a, **k: None)
    # This invariant ends at durable truncation; no model turn is requested by
    # the fixture. Keep its asynchronous continuation inside this test's scope.
    monkeypatch.setattr(server, "_run_prompt_submit", lambda *a, **k: None)

    try:
        resp = server.handle_request(
            {
                "id": "1",
                "method": "prompt.submit",
                "params": {
                    "session_id": "null-key-trunc-sid",
                    "text": "new turn",
                    "truncate_before_row_id": 103,
                    "truncate_before_user_ordinal": 1,
                    "confirm_truncate": True,
                },
            }
        )
        assert resp.get("result") is not None
        assert len(replaced) == 1
        # Keyed by the session id, never None (the FK-violating value).
        assert replaced[0][0] == "null-key-trunc-sid"
        assert replaced[0][1] == history[:2]
    finally:
        # The prompt worker resolves server bindings when it runs. Reap it before
        # monkeypatch teardown or it can consume the next test's notification.
        session = server._sessions.get("null-key-trunc-sid")
        worker = session.get("_run_thread") if session else None
        if worker is not None:
            worker.join(timeout=5)
            assert not worker.is_alive()
        _join_turn_thread(server._sessions['null-key-trunc-sid'])
        server._sessions.pop("null-key-trunc-sid", None)


def test_prompt_submit_truncates_by_row_id(monkeypatch):
    """#82959: prompt.submit with truncate_before_row_id must cut at the target row id."""
    replaced = []

    class _FakeDB:
        def replace_messages(
            self,
            key,
            messages,
            active_only=False,
            archive_dropped=False,
            reject_active_turn_lease=False,
        ):
            replaced.append((key, list(messages)))

    history = [
        {"_row_id": 101, "role": "user", "content": "first"},
        {"_row_id": 102, "role": "assistant", "content": "reply 1"},
        {"_row_id": 103, "role": "user", "content": "second"},
        {"_row_id": 104, "role": "assistant", "content": "reply 2"},
    ]
    sess = _session(history=list(history))
    server._sessions["row-id-trunc-sid"] = sess
    monkeypatch.setattr(server, "_get_db", lambda: _FakeDB())
    started = []
    monkeypatch.setattr(
        server, "_start_agent_build", lambda *a, **k: started.append(k)
    )

    try:
        resp = server.handle_request(
            {
                "id": "1",
                "method": "prompt.submit",
                "params": {
                    "session_id": "row-id-trunc-sid",
                    "text": "new turn",
                    "truncate_before_row_id": 103,
                    "truncate_before_user_ordinal": 1,
                    "confirm_truncate": True,
                },
            }
        )
        assert resp.get("error") is None
        assert len(sess["history"]) == 2
        assert sess["history"][-1]["content"] == "reply 1"
        assert len(replaced) == 1
        assert replaced[0][1] == history[:2]
    finally:
        _join_turn_thread(server._sessions['row-id-trunc-sid'])
        server._sessions.pop("row-id-trunc-sid", None)


def test_prompt_submit_truncates_by_string_row_id(monkeypatch):
    """#82959: String row IDs in history match correctly against integer truncate_before_row_id."""
    replaced = []

    class _FakeDB:
        def replace_messages(
            self,
            key,
            messages,
            active_only=False,
            archive_dropped=False,
            reject_active_turn_lease=False,
        ):
            replaced.append((key, list(messages)))

    history = [
        {"_row_id": "101", "role": "user", "content": "first"},
        {"_row_id": "102", "role": "assistant", "content": "reply 1"},
        {"_row_id": "103", "role": "user", "content": "second"},
        {"_row_id": "104", "role": "assistant", "content": "reply 2"},
    ]
    sess = _session(history=list(history))
    server._sessions["str-row-id-trunc-sid"] = sess
    monkeypatch.setattr(server, "_get_db", lambda: _FakeDB())
    monkeypatch.setattr(server, "_start_agent_build", lambda *a, **k: None)

    try:
        resp = server.handle_request(
            {
                "id": "1",
                "method": "prompt.submit",
                "params": {
                    "session_id": "str-row-id-trunc-sid",
                    "text": "new turn",
                    "truncate_before_row_id": 103,
                    "confirm_truncate": True,
                },
            }
        )
        assert resp.get("error") is None
        assert len(sess["history"]) == 2
    finally:
        _join_turn_thread(server._sessions['str-row-id-trunc-sid'])
        server._sessions.pop("str-row-id-trunc-sid", None)


def test_prompt_submit_resolves_row_id_absorbed_into_marker_merge(monkeypatch):
    """#94486: after a mid-session model switch, the marker is persisted as
    role=user (#48338) and the next real user row merges into it under
    repair. The merged row must stay addressable (plain row's _row_id kept,
    display classification dropped) so prompt.submit with
    truncate_before_row_id resolves instead of failing closed with the
    user's input silently dropped.
    """
    import copy

    from agent.agent_runtime_helpers import repair_message_sequence

    verbatim = [
        {"_row_id": 12131, "role": "user", "content": "first"},
        {"_row_id": 12133, "role": "assistant", "content": "reply 1"},
        {
            "_row_id": 12134,
            "role": "user",
            "display_kind": "model_switch",
            "content": "[System: The active model for this chat has changed to ds4.]",
        },
        {"_row_id": 12135, "role": "user", "content": "the dropped prompt"},
    ]
    repaired = copy.deepcopy(verbatim)
    assert repair_message_sequence(None, repaired) == 1
    # The merged pair survives as ONE addressable user row carrying the
    # plain row's durable id — this is the property the gateway relies on.
    assert len(repaired) == 3
    assert repaired[2]["_row_id"] == 12135
    assert not repaired[2].get("display_kind")

    replaced = []

    class _FakeDB:
        def get_messages_as_conversation(
            self, key, include_ancestors=False, repair_alternation=False,
            include_row_ids=False, **_kwargs
        ):
            assert key == "session-key"
            assert include_row_ids is True
            return copy.deepcopy(repaired if repair_alternation else verbatim)

        def replace_messages(self, key, messages, active_only=False, archive_dropped=False, **_kwargs):
            replaced.append((key, list(messages)))

    import contextlib

    @contextlib.contextmanager
    def _fake_session_db(_session):
        yield _FakeDB()

    # Live history as a RUNNING session holds it after a turn rewrite: the
    # repaired, merged shape with row-id stamps still attached (the live
    # dicts carry them), so the in-memory lookup resolves directly.
    live = copy.deepcopy(repaired)
    assert live[2]["_row_id"] == 12135
    server._sessions["merged-marker-sid"] = _session(history=list(live))
    monkeypatch.setattr(server, "_session_db", _fake_session_db)
    monkeypatch.setattr(server, "_get_db", lambda: _FakeDB())
    monkeypatch.setattr(server, "_start_agent_build", lambda *a, **k: None)
    monkeypatch.setattr(server, "_start_inflight_turn", lambda *a, **k: None)

    try:
        resp = server.handle_request(
            {
                "id": "1",
                "method": "prompt.submit",
                "params": {
                    "session_id": "merged-marker-sid",
                    "text": "new turn",
                    "truncate_before_row_id": 12135,
                    "confirm_truncate": True,
                },
            }
        )
        assert resp.get("error") is None, resp.get("error")
        # The cut lands before the merged marker+prompt pair: everything the
        # pair would have buried is kept, the pair itself is replaced by the
        # freshly submitted turn. (Rows carry _row_id stamps because the
        # durable fallback healed the live list's missing stamps first.)
        assert len(replaced) == 1
        assert replaced[0][0] == "session-key"
        assert replaced[0][1] == [
            {"role": "user", "content": "first", "_row_id": 12131},
            {"role": "assistant", "content": "reply 1", "_row_id": 12133},
        ]
        assert len(server._sessions["merged-marker-sid"]["history"]) == 2
    finally:
        _join_turn_thread(server._sessions['merged-marker-sid'])
        server._sessions.pop("merged-marker-sid", None)


def test_prompt_submit_resolves_row_id_swallowed_by_marker_merge(monkeypatch):
    """#94486 (live-session shape): model-switch markers persist as role=user
    (#48338), so the just-sent prompt forms a user;user run that
    repair_message_sequence merges into the FIRST row — the target's _row_id
    vanishes from the repaired view and truncate_before_row_id fails closed
    (4018) even though the row is physically present. Identity resolution must
    read the un-repaired durable transcript, the same discipline the rebind
    path already applies ('repair can merge a physical user;user pair while
    preserving only the first row id').
    """
    import contextlib
    import copy

    from agent.agent_runtime_helpers import repair_message_sequence

    verbatim = [
        {"_row_id": 201, "role": "user", "content": "first"},
        {"_row_id": 202, "role": "assistant", "content": "reply 1"},
        {
            "_row_id": 203,
            "role": "user",
            "display_kind": "model_switch",
            "content": "[System: The active model for this chat has changed to ds4.]",
        },
        {
            "_row_id": 204,
            "role": "user",
            "display_kind": "model_switch",
            "content": "[System: The active model for this chat has changed to kk3.]",
        },
        {"_row_id": 205, "role": "user", "content": "the interrupted prompt"},
    ]
    repaired = copy.deepcopy(verbatim)
    assert repair_message_sequence(None, repaired) == 2  # 3-row run, two merges
    # On main the merge buries the plain row's id inside the marker.
    assert len(repaired) == 3

    replaced = []
    fake_db = _mk_merge_fake_db(verbatim, repaired, replaced)

    @contextlib.contextmanager
    def _fake_session_db(_session):
        yield fake_db

    # Live history as a running session holds it after a turn rewrite: every
    # physical row present, row-id stamps lost (provider-format rewrite), so
    # resolution must go through the durable fallback.
    live = [
        {k: v for k, v in message.items() if k != "_row_id"} for message in verbatim
    ]
    server._sessions["marker-merge-sid"] = _session(history=list(live))
    monkeypatch.setattr(server, "_session_db", _fake_session_db)
    monkeypatch.setattr(server, "_get_db", lambda: fake_db)
    monkeypatch.setattr(server, "_start_agent_build", lambda *a, **k: None)
    monkeypatch.setattr(server, "_start_inflight_turn", lambda *a, **k: None)

    try:
        resp = server.handle_request(
            {
                "id": "1",
                "method": "prompt.submit",
                "params": {
                    "session_id": "marker-merge-sid",
                    "text": "edited prompt",
                    "truncate_before_row_id": 205,
                    "confirm_truncate": True,
                },
            }
        )
        assert isinstance(resp, dict)
        err = resp.get("error")
        assert err is None, err
        # The cut lands before row 205: the markers and everything earlier
        # survive (durable rows healed their stamps onto the live list).
        assert len(replaced) == 1
        assert replaced[0][0] == "session-key"
        assert [m["content"] for m in replaced[0][1]] == [
            "first",
            "reply 1",
            verbatim[2]["content"],
            verbatim[3]["content"],
        ]
        assert len(server._sessions["marker-merge-sid"]["history"]) == 4
    finally:
        _join_turn_thread(server._sessions['marker-merge-sid'])
        server._sessions.pop("marker-merge-sid", None)


def test_prompt_submit_resolves_row_id_swallowed_by_plain_user_merge(monkeypatch):
    """Same swallowed-id class with NO display marker: an interrupted turn
    persists no assistant row, the user's resend lands as a second consecutive
    plain user row, and repair merges it into the earlier one — the resend's
    row id is unresolvable on every later rewind/edit. Repair-side address
    preservation keyed on display_kind cannot reach this shape; reading the
    un-repaired transcript for identity can.
    """
    import contextlib
    import copy

    from agent.agent_runtime_helpers import repair_message_sequence

    verbatim = [
        {"_row_id": 301, "role": "user", "content": "first"},
        {"_row_id": 302, "role": "assistant", "content": "reply 1"},
        {"_row_id": 303, "role": "user", "content": "prompt whose turn was cut off"},
        {"_row_id": 304, "role": "user", "content": "the resent prompt"},
    ]
    repaired = copy.deepcopy(verbatim)
    assert repair_message_sequence(None, repaired) == 1
    assert len(repaired) == 3
    assert repaired[2]["_row_id"] == 303  # merge keeps only the FIRST row id

    replaced = []
    fake_db = _mk_merge_fake_db(verbatim, repaired, replaced)

    @contextlib.contextmanager
    def _fake_session_db(_session):
        yield fake_db

    # Production cold resume materializes the live history from the DB with
    # repair_alternation=True (server._load_resume_transcript), stamps kept:
    # rows 303/304 collapse into ONE live carrier at 303, so 304 has no live
    # user ordinal. The un-repaired DB read finds 304 as physical ordinal 2,
    # but the live list only has user ordinals 0/1 — resolution must map the
    # DB row onto the repaired live carrier instead of trusting the same
    # ordinal against a list that cannot have it.
    live = copy.deepcopy(repaired)
    assert live[2]["_row_id"] == 303
    server._sessions["plain-merge-sid"] = _session(history=list(live))
    monkeypatch.setattr(server, "_session_db", _fake_session_db)
    monkeypatch.setattr(server, "_get_db", lambda: fake_db)
    monkeypatch.setattr(server, "_start_agent_build", lambda *a, **k: None)
    monkeypatch.setattr(server, "_start_inflight_turn", lambda *a, **k: None)

    try:
        resp = server.handle_request(
            {
                "id": "1",
                "method": "prompt.submit",
                "params": {
                    "session_id": "plain-merge-sid",
                    "text": "edited resend",
                    "truncate_before_row_id": 304,
                    "confirm_truncate": True,
                },
            }
        )
        assert isinstance(resp, dict)
        err = resp.get("error")
        assert err is None, err
        assert len(replaced) == 1
        # The cut retains 303: deriving it from the physical durable prefix
        # keeps the inner row boundary instead of collapsing the pair.
        assert [m["content"] for m in replaced[0][1]] == [
            "first",
            "reply 1",
            "prompt whose turn was cut off",
        ]
        assert len(server._sessions["plain-merge-sid"]["history"]) == 3
    finally:
        _join_turn_thread(server._sessions['plain-merge-sid'])
        server._sessions.pop("plain-merge-sid", None)


def _mk_merge_fake_db(verbatim, repaired, replaced):
    """SessionDB stand-in answering exactly like the real store: the repaired view
    (what `_load_durable_truncation_history` historically loaded) has the user;user
    run merged away, the un-repaired view keeps every physical row."""

    class _FakeDB:
        def get_messages_as_conversation(
            self,
            key,
            include_ancestors=False,
            repair_alternation=False,
            include_row_ids=False,
            **_kwargs,
        ) -> list:
            import copy as _copy

            assert key == "session-key"
            assert include_row_ids is True
            return _copy.deepcopy(repaired if repair_alternation else verbatim)

        def replace_messages(
            self, key, messages, active_only=False, archive_dropped=False, **_kwargs
        ):
            replaced.append((key, list(messages)))

    return _FakeDB()


def test_join_turn_thread_waits_for_published_unstarted_worker():
    """A published handoff handle is not joinable until its start event fires."""
    observed = threading.Event()
    finished = threading.Event()

    class _ObservedStart(threading.Event):
        def wait(self, timeout=None):
            observed.set()
            return super().wait(timeout)

    worker = threading.Thread(target=finished.set, daemon=True)
    worker._started = _ObservedStart()
    session = {"_run_thread": worker}

    def finish_handoff():
        if observed.wait(10.0):
            worker.start()

    handoff = threading.Thread(target=finish_handoff, daemon=True)
    handoff.start()
    try:
        _join_turn_thread(session)
        assert finished.is_set()
        assert not worker.is_alive()
    finally:
        # Release and reap both real workers even when the baseline join raises.
        observed.set()
        handoff.join(timeout=10.0)
        assert not handoff.is_alive()
        if worker._started.is_set():
            worker.join(timeout=10.0)
            assert not worker.is_alive()

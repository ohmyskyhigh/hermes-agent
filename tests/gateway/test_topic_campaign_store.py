"""Persistent ownership, acceptance and crash/race recovery for topic campaigns."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from gateway.topic_campaign_store import CampaignStore

KEY = "feishu:oc_command:omt_task"


def ingest(store, **overrides):
    values = dict(key=KEY, owner="analyst", profile="analyst",
                  source={"platform": "feishu", "chat_id": "oc_command", "thread_id": "omt_task", "profile": "analyst"},
                  transport_profile="analyst", message_id="om_first", text="research and deliver",
                  root_message_id="om_first", is_bot=False, targets=["analyst"], now=100,
                  review_interval=300, report_interval=900)
    values.update(overrides)
    return store.ingest(**values)


def test_concurrent_bot_delivery_creates_one_campaign_and_preserves_owner(tmp_path):
    store = CampaignStore(tmp_path / "campaigns.db")
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda p: ingest(store, profile=p), ["analyst", "joi", "writer", "analyst"]))
    assert len(store.list()) == 1
    assert len(store.events(KEY)) == 1
    assert store.get(KEY)["owner_profile"] == "analyst"
    ingest(store, profile="writer", targets=["writer"], message_id="om_follow", text="write the draft")
    task = store.get(KEY)
    assert task["owner_profile"] == "analyst"
    assert "writer" in task["participating_profiles"]
    assert task["work"]["message:om_follow:writer"]["status"] == "pending"


def test_restart_reclaims_expired_tick_and_retries_unacknowledged_report(tmp_path):
    path = tmp_path / "campaigns.db"
    store = CampaignStore(path)
    ingest(store)
    _, old = store.claim_due(100, lambda _: True, lease_seconds=60)
    restored = CampaignStore(path)
    assert restored.claim_due(120, lambda _: True) is None
    task, new = restored.claim_due(161, lambda _: True)
    assert task["next_review_at"] == 100
    assert not restored.finish_tick(KEY, old, {"last_reported_at": 120})
    restored.finish_tick(KEY, new, {"next_check_at": 221})
    assert restored.get(KEY)["last_reported_at"] == 0


def test_pause_wins_over_inflight_timer_and_cannot_be_undone_by_owner(tmp_path):
    store = CampaignStore(tmp_path / "campaigns.db")
    task = ingest(store)
    _, token = store.claim_due(100, lambda _: True)
    store.control(KEY, "pause", 101)
    store.finish_tick(KEY, token, {"next_review_at": 999, "wake_pending_until": 999}, task["revision"])
    assert store.get(KEY)["wake_pending_until"] == 0
    assert store.claim_due(1000, lambda _: True) is None
    with pytest.raises(ValueError, match="human resume"):
        store.update(KEY, "analyst", {"status": "running"}, 102)
    store.control(KEY, "resume", 103)
    assert store.claim_due(103, lambda _: True)


def test_completion_requires_owner_criteria_evidence_and_all_work_settled(tmp_path):
    store = CampaignStore(tmp_path / "campaigns.db")
    ingest(store)
    with pytest.raises(ValueError, match="only the campaign owner"):
        store.update(KEY, "writer", {"status": "completed"}, 101)
    with pytest.raises(ValueError, match="criteria and evidence"):
        store.update(KEY, "analyst", {"status": "completed"}, 101)
    store.work_update(KEY, "analyst", "draft", {"profile": "writer"}, 102)
    with pytest.raises(ValueError, match="unfinished work"):
        store.update(KEY, "analyst", {"done_when": "verified draft", "evidence": "draft link", "status": "completed"}, 103)
    with pytest.raises(ValueError, match="another profile"):
        store.work_update(KEY, "sales", "draft", {"status": "done", "summary": "ok"}, 104)
    store.work_update(KEY, "writer", "draft", {"status": "done", "summary": "draft link"}, 105)
    result = store.update(KEY, "analyst", {"done_when": "verified draft", "evidence": "draft link", "status": "completed"}, 106)
    assert result["final_report_pending"]
    assert result["next_report_at"] == 106
    assert CampaignStore(store.path).get(KEY)["status"] == "completed"
    with pytest.raises(ValueError, match="are closed"):
        store.control(KEY, "pause", 107)


def test_blocked_requires_reason_and_still_has_periodic_review(tmp_path):
    store = CampaignStore(tmp_path / "campaigns.db")
    ingest(store)
    with pytest.raises(ValueError, match="concrete blocker"):
        store.update(KEY, "analyst", {"status": "blocked"}, 101)
    store.update(KEY, "analyst", {"status": "blocked", "blocker": "user must supply source"}, 102)
    assert store.claim_due(1001, lambda _: True)


def test_bot_output_cannot_create_campaign_or_replace_human_route(tmp_path):
    store = CampaignStore(tmp_path / "campaigns.db")
    assert ingest(store, is_bot=True) is None
    initial = ingest(store)
    ingest(store, is_bot=True, message_id="om_bot", source={"user_id": "bot"})
    assert store.get(KEY)["routes"] == initial["routes"]

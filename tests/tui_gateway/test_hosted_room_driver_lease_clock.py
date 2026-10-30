"""Short driver leases renew and expire at their injected-clock boundaries."""
from pathlib import Path

import pytest

from gateway import hosted_room_driver as state
from tests.tui_gateway.test_hosted_room_driver_runtime import (
    BINDING, FakeSessionRPC, _admit, _identity, _runtime,
)
from tests.tui_gateway.test_hosted_room_driver_runtime import db as db  # noqa: PLC0414 -- pytest fixture export


def test_short_lease_renews_after_its_half_life(db):
    now = [100.0]
    runtime = _runtime(db, FakeSessionRPC(), clock=lambda: now[0])
    lease = runtime._ensure_lease(BINDING)
    assert lease.expires_at == pytest.approx(100.4)
    now[0] = 100.21

    renewed = runtime._renew_lease_if_needed(lease)

    assert renewed.expires_at == pytest.approx(100.61)
    assert renewed.lease_generation == lease.lease_generation
    assert renewed.process_generation == runtime.process_generation


def test_expired_short_lease_cannot_claim_or_renew(db):
    now = [100.0]
    identity = _identity()
    _admit(db, identity)
    runtime = _runtime(db, FakeSessionRPC(), clock=lambda: now[0])
    lease = runtime._ensure_lease(BINDING)
    now[0] = lease.expires_at

    with pytest.raises(state.StaleLeaseError):
        runtime._renew_lease_if_needed(lease, force=True)
    with pytest.raises(state.StaleLeaseError):
        state.start_task(db, identity, lease, expected_cancel_generation=0, clock=lambda: now[0])
    assert state.get_task(db, identity)["status"] == "queued"


def test_runtime_uses_unique_process_generation(db: Path):
    first = _runtime(db, FakeSessionRPC())
    second = _runtime(db, FakeSessionRPC())

    assert first.process_generation != second.process_generation

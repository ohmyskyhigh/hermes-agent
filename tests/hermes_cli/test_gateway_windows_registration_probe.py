"""A failed schtasks query must not turn an installed service into an install prompt."""

import pytest

from hermes_cli import gateway, gateway_windows


@pytest.mark.platforms("windows")
@pytest.mark.parametrize("state", ["Ready", "Running", "Queued", "Disabled", "Unknown"])
def test_failed_cli_query_uses_independent_scheduler_state(monkeypatch, state):
    monkeypatch.setattr(gateway_windows, "get_task_name", lambda: "Hermes_Gateway_probe")
    monkeypatch.setattr(gateway_windows, "_exec_schtasks", lambda args: (124, "", "schtasks timed out"))
    queried = []

    def native_state(name):
        queried.append(name)
        return state

    monkeypatch.setattr(gateway, "_windows_scheduled_task_state", native_state)
    assert gateway_windows.is_task_registered()
    assert queried == ["Hermes_Gateway_probe"]


@pytest.mark.platforms("windows")
def test_only_verified_missing_task_is_uninstalled(monkeypatch):
    monkeypatch.setattr(gateway_windows, "get_task_name", lambda: "Hermes_Gateway_probe")
    monkeypatch.setattr(gateway_windows, "_exec_schtasks", lambda args: (1, "", "query failed"))
    monkeypatch.setattr(gateway, "_windows_scheduled_task_state", lambda name: "MISSING")
    assert not gateway_windows.is_task_registered()
    monkeypatch.setattr(gateway, "_windows_scheduled_task_state", lambda name: None)
    with pytest.raises(RuntimeError, match="Could not determine.*Hermes_Gateway_probe"):
        gateway_windows.is_task_registered()


@pytest.mark.platforms("windows")
def test_successful_cli_query_needs_no_fallback(monkeypatch):
    monkeypatch.setattr(gateway_windows, "get_task_name", lambda: "Hermes_Gateway_probe")
    monkeypatch.setattr(gateway_windows, "_exec_schtasks", lambda args: (0, "Ready", ""))

    def unexpected_fallback(name):
        raise AssertionError("Successful query must not launch another subprocess")

    monkeypatch.setattr(gateway, "_windows_scheduled_task_state", unexpected_fallback)
    assert gateway_windows.is_task_registered()

"""``hermes update --list-venv-holders``: read-only JSON twin of the Windows venv-holder refusal.

Drives ``cmd_update`` (the production entry point) so the wiring in ``_update_preflight_handled``
is what is proven, not the helper alone.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

import pytest

from hermes_cli import main as cli_main
from hermes_cli import update_cmd_windows
from tests.waiting import wait_until


def _args(**overrides):
    base = dict(list_venv_holders=True, plan=False, check=False, gateway=False, branch=None)
    base.update(overrides)
    return argparse.Namespace(**base)


@pytest.fixture
def _quiet_preflight(monkeypatch):
    monkeypatch.setattr("hermes_cli.config.is_managed", lambda: False)
    # No live psutil lookups: the fixture tuple IS the process table.
    monkeypatch.setattr(update_cmd_windows, "_psutil", lambda: None)


def test_list_venv_holders_json_and_exit_3_when_holders_present(monkeypatch, capsys, _quiet_preflight):
    holders = [
        (4242, "python.exe", r"C:\hermes\venv\Scripts\python.exe -m hermes_cli.main serve --port 8642"),
        (4343, "python.exe", r"C:\hermes\venv\Scripts\python.exe -m hermes_cli.main gateway run"),
        (4444, "python.exe", r"C:\hermes\venv\Scripts\python.exe -m hermes_cli.main -p work kanban list"),
        (4545, "python.exe", r"C:\hermes\venv\Scripts\python.exe some_script.py"),
    ]
    monkeypatch.setattr(update_cmd_windows, "_detect_venv_python_processes", lambda: holders)

    def retired_hook(**_kwargs):
        raise AssertionError("current read-only command used the retired updater hook")

    monkeypatch.setattr(cli_main, "_detect_venv_python_processes", retired_hook)

    with pytest.raises(SystemExit) as exc:
        cli_main.cmd_update(_args())

    assert exc.value.code == update_cmd_windows.VENV_HOLDERS_EXIT == 3
    payload = json.loads(capsys.readouterr().out)
    assert [(h["pid"], h["kind"]) for h in payload] == [
        (4242, "backend"), (4343, "gateway"), (4444, "hermes:kanban"), (4545, "python")]
    assert set(payload[0]) == {"pid", "exe", "argv", "kind"}
    assert payload[0]["argv"].endswith("serve --port 8642")


def test_list_venv_holders_empty_list_exits_zero_without_updating(monkeypatch, capsys, _quiet_preflight):
    monkeypatch.setattr(update_cmd_windows, "_detect_venv_python_processes", list)

    def _boom(*_a, **_k):  # the mutating update body must never run behind the read-only flag
        raise AssertionError("update body ran")

    monkeypatch.setattr("hermes_cli.update_cmd._cmd_update_impl", _boom)

    assert cli_main.cmd_update(_args()) is None
    assert json.loads(capsys.readouterr().out) == []


@pytest.mark.platforms("windows")
@pytest.mark.parametrize("layout", ["legacy", "managed"])
def test_read_only_command_reports_a_live_native_interpreter(monkeypatch, capsys, tmp_path, layout):
    """Exercise cmd_update's real scanner against an owned throwaway venv."""
    monkeypatch.setattr("hermes_cli.config.is_managed", lambda: False)
    project = tmp_path / "install"
    project.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    from pm.environments import install_state_dir, runtime_facts_path

    state = install_state_dir(project)
    assert state.is_relative_to(tmp_path)
    environment = (state / "environments" / "fixture" / "venv") if layout == "managed" else project / "venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(environment)],
                   check=True, timeout=60, encoding="utf-8", capture_output=True)
    if layout == "managed":
        runtime_facts_path(project).write_text(json.dumps({"packages": {"venv": {"environment": str(environment)}}}),
                                              encoding="utf-8")
    monkeypatch.setattr(cli_main, "PROJECT_ROOT", project)
    ready = tmp_path / "holder-ready"
    child = subprocess.Popen(
        [str(environment / "Scripts/python.exe"), "-c", "import pathlib,sys; "
         "pathlib.Path(sys.argv[1]).write_text('ready', encoding='utf-8'); sys.stdin.read()", str(ready)],
        cwd=tmp_path, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        wait_until(ready.is_file, timeout=15, what="native interpreter holder ready")
        with pytest.raises(SystemExit) as exc:
            cli_main.cmd_update(_args())
        assert exc.value.code == update_cmd_windows.VENV_HOLDERS_EXIT == 3
        holders = json.loads(capsys.readouterr().out)
        assert child.pid in {entry["pid"] for entry in holders}
    finally:
        child.stdin.close()
        child.wait(timeout=15)

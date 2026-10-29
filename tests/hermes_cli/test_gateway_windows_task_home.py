"""Service identity follows the registered task across a relocated default home."""
from xml.sax.saxutils import escape

import pytest

from hermes_cli import gateway as g, gateway_windows as w
from hermes_cli.gateway_runtime_service import service_suffix

pytestmark = pytest.mark.platforms("windows")


def task_xml(home):
    launcher = escape(str(home / "gateway-service" / "Hermes_Gateway.vbs"))
    return (f'<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
            f'<Actions><Exec><Command>wscript.exe</Command>'
            f'<Arguments>//B //Nologo "{launcher}"</Arguments></Exec></Actions></Task>')


@pytest.fixture
def relocated_home(tmp_path, monkeypatch):
    import hermes_constants
    native, moved = tmp_path / "native", tmp_path / "moved"
    native.mkdir()
    moved.mkdir()
    monkeypatch.setattr(hermes_constants, "account_default_hermes_home", lambda: native)
    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda: moved)
    monkeypatch.setattr(g, "_native_service_homes", lambda: {native})
    monkeypatch.setattr(g, "get_hermes_home", lambda: moved)
    monkeypatch.setattr(w, "_exec_schtasks", lambda args, **kwargs: (0, task_xml(moved), ""))
    return native, moved


def test_relocated_home_keeps_the_installed_task_name(relocated_home):
    native, moved = relocated_home
    assert native.stat().st_ino != moved.stat().st_ino
    assert g._profile_suffix() == ""
    assert w.get_task_name() == "Hermes_Gateway"
    assert service_suffix(moved) == ""


def test_other_homes_cannot_take_the_registered_task(relocated_home, monkeypatch):
    native, moved = relocated_home
    monkeypatch.setattr(g, "get_hermes_home", lambda: native)
    assert g._profile_suffix()
    assert service_suffix(native)
    named = moved / "profiles" / "worker"
    named.mkdir(parents=True)
    monkeypatch.setattr(g, "get_hermes_home", lambda: named)
    assert g._profile_suffix() == "worker"
    assert service_suffix(named) == "worker"


@pytest.mark.parametrize("response", [(1, "", "not found"), (0, "<broken>", "")])
def test_unavailable_binding_preserves_default_naming(relocated_home, monkeypatch, response):
    native, moved = relocated_home
    monkeypatch.setattr(w, "_exec_schtasks", lambda args, **kwargs: response)
    assert g._profile_suffix()
    assert service_suffix(moved)
    monkeypatch.setattr(g, "get_hermes_home", lambda: native)
    assert g._profile_suffix() == ""


def test_similarly_named_foreign_launcher_does_not_claim_home(relocated_home, monkeypatch):
    native, moved = relocated_home
    foreign = moved.with_name(moved.name + "-other")
    foreign.mkdir()
    monkeypatch.setattr(w, "_exec_schtasks", lambda args, **kwargs: (0, task_xml(foreign), ""))
    assert g._profile_suffix()
    assert service_suffix(moved)


@pytest.mark.parametrize("change", ["extra_action", "foreign_command", "foreign_script"])
def test_only_exact_single_gateway_launcher_can_bind_home(relocated_home, monkeypatch, change):
    native, moved = relocated_home
    xml = task_xml(moved)
    replacements = {
        "extra_action": ("</Actions>", "<ComHandler /></Actions>"),
        "foreign_command": ("wscript.exe", "notepad.exe"),
        "foreign_script": ("Hermes_Gateway.vbs", "Hermes_Gateway_other.vbs"),
    }
    xml = xml.replace(*replacements[change])
    monkeypatch.setattr(w, "_exec_schtasks", lambda args, **kwargs: (0, xml, ""))
    assert g._profile_suffix()

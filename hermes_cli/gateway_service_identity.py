"""Ownership of the bare platform service name across relocated homes."""
from pathlib import Path


def home_owns_bare_service_name(home: Path) -> bool:
    """True for the resolved ``home`` that owns the bare service name (see ``_profile_suffix``)."""
    from hermes_cli import gateway as gw

    if gw.is_windows():
        from hermes_cli.gateway_windows_identity import bare_task_home
        pinned = bare_task_home()
        if pinned is not None:
            return home == pinned
    return home in gw._native_service_homes() or home == gw._bare_unit_pinned_home()



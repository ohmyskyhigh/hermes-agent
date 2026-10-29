"""Windows service naming anchored to the existing task's launcher location."""
from __future__ import annotations

from pathlib import Path
import re
from xml.etree import ElementTree


def bare_task_home() -> Path | None:
    """The home named by the installed bare task, without relying on a native-home junction.

    Require one exact Hermes launcher action. A directory substring must never make
    another profile's or another installation's task belong to the caller.
    """
    from hermes_cli import gateway_windows as w

    xml = w._query_scheduled_task_xml(w._TASK_NAME_DEFAULT)
    if xml is None:
        return None
    try:
        root = ElementTree.fromstring(xml)
        actions = root.findall("./{*}Actions")
        if len(actions) != 1 or len(actions[0]) != 1:
            return None
        action = actions[0][0]
        if action.tag.rsplit("}", 1)[-1] != "Exec":
            return None
        command = action.findtext("{*}Command", "").strip('"')
        if Path(command).name.casefold() not in {"wscript.exe", "cscript.exe"}:
            return None
        arguments = action.findtext("{*}Arguments", "")
        match = re.fullmatch(r'//B\s+//Nologo\s+"([^"\r\n]+)"', arguments.strip(), re.IGNORECASE)
        if match is None:
            return None
        launcher = Path(match[1])
        if (not launcher.is_absolute() or "%" in str(launcher)
                or launcher.name.casefold() != f"{w._TASK_NAME_DEFAULT}.vbs".casefold()
                or launcher.parent.name.casefold() != "gateway-service"):
            return None
        return launcher.parent.parent.resolve()
    except (ElementTree.ParseError, OSError, RuntimeError, ValueError):
        return None

"""tui_gateway test fixtures.

Several files here import ``tui_gateway.server`` inside a ``patch.dict("sys.modules", {"hermes_constants":
MagicMock(...)})`` window so the module binds a fixed home. The server's import graph reaches
``agent.process_bootstrap`` → ``hermes_bootstrap``, which is process boot: PM dependency activation reads
the real install root through ``hermes_constants`` and exits the process when that is a MagicMock.
Importing it once here, before any window opens, keeps boot out of the mocked import.
"""

import hermes_bootstrap

import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def _preserve_native_subprocess_reader_threads(monkeypatch):
    """Inline agent-thread fakes must not replace Windows Popen's pipe reader threads.

    Popen uses Thread(target=reader, args=(pipe, buffer)) and joins both readers. The synchronous
    agent fakes intentionally implement only target(), so give subprocess its real threading API.
    """
    if sys.platform == "win32":
        monkeypatch.setattr(subprocess, "threading", SimpleNamespace(**vars(threading)))

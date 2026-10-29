"""The hermetic Windows runner retains Python's native WMI fallback environment."""
import subprocess
import sys

import pytest

pytestmark = pytest.mark.platforms("windows")


def test_isolated_interpreter_knows_machine_when_wmi_is_unavailable():
    code = (
        "import platform\n"
        "def unavailable(*args): raise OSError('WMI unavailable')\n"
        "platform._wmi_query = unavailable\n"
        "print(platform.machine())\n"
    )
    result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True,
                            text=True, check=True, timeout=60)
    assert result.stdout.strip() in {"AMD64", "ARM64", "x86"}

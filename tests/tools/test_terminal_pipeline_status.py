"""The bash wrapper reports each pipeline segment's own exit status."""

import subprocess
import shlex

import pytest

from tools.environments.base_session_env import _split_cwd_marker, _wrap_command_script

pytestmark = pytest.mark.platforms("posix")

CWD_MARKER = "__HERMES_CWD_test__"
PIPE_MARKER = "__HERMES_PIPE_test__"


def _script(command: str, pipe_marker: str = PIPE_MARKER) -> str:
    return _wrap_command_script(
        command, quoted_cwd="/tmp", quoted_snap="/nonexistent-snapshot",
        snap_tmp_template="/tmp/hermes-snap.XXXXXX", passthrough_names=(),
        snapshot_ready=False, cwd_marker=CWD_MARKER, pipe_marker=pipe_marker,
    )


def _run_result(command: str, pipe_marker: str = PIPE_MARKER) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", _script(command, pipe_marker)], capture_output=True, text=True, check=False,
    )


def _run(command: str) -> str:
    return _run_result(command).stdout


def test_pipeline_status_captures_the_leading_command():
    output = _run("false | true")

    assert f"{PIPE_MARKER}1 0{PIPE_MARKER}" in output


def test_plain_command_reports_one_status():
    output = _run("false")

    assert f"{PIPE_MARKER}1{PIPE_MARKER}" in output


def _seen(result: subprocess.CompletedProcess) -> tuple[int, str, str]:
    """Exit code and output as the model sees them, markers stripped like production does."""
    stdout = result.stdout
    for marker in (PIPE_MARKER, CWD_MARKER):
        split = _split_cwd_marker(stdout, marker)
        stdout = split[1] if split else stdout
    return result.returncode, stdout, result.stderr


@pytest.mark.parametrize("command", [
    'if false; then echo yes; fi',
    'while read line; do echo "$line"; done < /dev/null',
    'set -o pipefail; false | true',
    '! false',
    '! true',
    "echo 'unbalanced",
    'echo "unbalanced',
    'echo $(echo unclosed',
    'echo hi \\',
    "cat <<'EOF'\nunterminated heredoc",
    'exit 3',
    "trap 'echo T' ERR; false",
])
def test_capturing_pipestatus_changes_nothing_the_model_sees(command):
    """The capture is side data: the shell's own ``$?`` and output, no wrapper text."""
    seen_code, seen_stdout, seen_stderr = _seen(_run_result(command))

    reference = subprocess.run(["bash", "-c", "eval " + shlex.quote(command)],
                               capture_output=True, text=True, check=False)
    assert (seen_code, seen_stdout) == (reference.returncode, reference.stdout)
    assert "__hermes" not in seen_stderr

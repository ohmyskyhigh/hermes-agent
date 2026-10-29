"""Recovery history must not obscure a healthy current Gateway lifecycle."""
import json

import pytest

from hermes_cli import gateway_windows as w


def write_events(path, events):
    path.write_text(''.join(json.dumps(event) + '\n' for event in events), encoding='utf-8')


@pytest.mark.parametrize('verdict', ['ok', 'absent', 'deferred'])
def test_recovered_current_process_keeps_history_without_failing(tmp_path, capsys, verdict):
    log = tmp_path / 'exit-diag.log'
    events = [
        {'tag': 'gateway.start', 'pid': 73, 'ts': '2026-10-28T03:04:13+00:00'},
        {'tag': 'gateway.previous_unclean_exit', 'pid': 73, 'prior_pid': 42, 'state_db_integrity': 'deferred'},
    ]
    if verdict != 'deferred':
        events.append({'tag': 'gateway.state_db_integrity', 'prior_pid': 42, 'state_db_integrity': verdict})
    write_events(log, events)
    w._probe_exit_diag(log, running_pid=73)
    text = capsys.readouterr().out
    assert '[6] PASS' in text
    assert 'gateway.start' in text
    assert '42' in text and 'previous' in text.lower()
    assert ('pending' if verdict == 'deferred' else verdict) in text


@pytest.mark.parametrize('fault', ['integrity', 'exit', 'different_pid', 'not_running'])
def test_actual_current_failures_remain_failures(tmp_path, capsys, fault):
    log = tmp_path / 'exit-diag.log'
    events = [{'tag': 'gateway.start', 'pid': 73}]
    if fault == 'integrity':
        events.append({'tag': 'gateway.state_db_integrity', 'state_db_integrity': 'check-failed: malformed database'})
    if fault == 'exit':
        events.append({'tag': 'gateway.exit', 'pid': 73})
    write_events(log, events)
    running = {'different_pid': 74, 'not_running': None}.get(fault, 73)
    w._probe_exit_diag(log, running_pid=running)
    assert '[6] FAIL' in capsys.readouterr().out


def test_invalid_last_line_is_not_hidden_by_an_older_start(tmp_path, capsys):
    log = tmp_path / 'exit-diag.log'
    log.write_text('{"tag":"gateway.start","pid":73}\nnot json\n', encoding='utf-8')
    w._probe_exit_diag(log, running_pid=73)
    assert '[6] FAIL' in capsys.readouterr().out

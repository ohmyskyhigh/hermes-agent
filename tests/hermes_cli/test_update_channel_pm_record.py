"""A live PM generation is a claimed install even without legacy install.json."""
import json

from hermes_cli.update_channel import stale_channel_records
from pm.environments import install_key, install_state_dir, runtime_facts_path


def test_channel_record_tracks_committed_pm_environment(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'home'))
    root = tmp_path / 'checkout'
    root.mkdir()
    environment = install_state_dir(root) / 'environments' / 'generation' / 'venv'
    environment.mkdir(parents=True)
    (environment / 'pyvenv.cfg').write_text('include-system-site-packages = false\n')
    runtime_facts_path(root).write_text(json.dumps({
        'packages': {'venv': {'environment': str(environment)}}}))
    config = {'update': {'installs': {install_key(root): {'path': str(root), 'channel': 'main'}}}}
    assert stale_channel_records(config) == []
    (environment / 'pyvenv.cfg').unlink()
    assert stale_channel_records(config)[0][2] == 'unclaimed'

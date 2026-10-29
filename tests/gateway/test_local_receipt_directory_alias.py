"""A Windows data-directory junction must preserve local session IDs after a move."""
import json
import asyncio
import subprocess
from types import SimpleNamespace

import pytest


@pytest.mark.platforms("windows")
@pytest.mark.asyncio
@pytest.mark.parametrize('record_migration', [False, True])
async def test_junction_receipt_recovers_without_rewriting_history(tmp_path, monkeypatch, record_migration):
    from gateway import run
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from gateway.session_authority import initialize_session_authority
    from gateway.session_contract import Principal
    from gateway.session_local import create_local_session
    from hermes_state_local import local_receipt

    home = tmp_path / 'current'
    home.mkdir()
    alias = tmp_path / 'old-location'
    await asyncio.to_thread(
        subprocess.run, ['cmd.exe', '/c', 'mklink', '/J', str(alias), str(home)],
        check=True, capture_output=True)
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(run, '_load_gateway_config', lambda: {'platform_toolsets': {'cli': []}})
    import hermes_state
    monkeypatch.setattr(hermes_state, 'DEFAULT_DB_PATH', hermes_state._IMPORT_DEFAULT_DB_PATH)

    def runner():
        store = SessionStore(home / 'sessions', GatewayConfig())
        return SimpleNamespace(session_store=store, _session_db=store._db,
                               adapters={}, _draining=False)

    owners = []
    try:
        first = await initialize_session_authority(runner(), profile_id=str(alias), instance_id='old')
        owners.append(first)
        actor = Principal('owner', str(alias), frozenset({'session:create', 'session:read'}), 'socket')
        ref = create_local_session(first, actor, {'request_id': 'before-move', 'source': 'gui',
                                   'cwd': str(tmp_path), 'model': 'frozen', 'toolsets': []})
        first.db.append_message(ref.session_id, 'user', 'History must survive the directory alias.')
        before = json.dumps(local_receipt(first.db, ref.session_id), sort_keys=True)
        if record_migration:
            from gateway.session_local_recovery import record_profile_directory_alias
            record_profile_directory_alias(first.db, str(alias), str(home))
            alias.rmdir()  # Login tasks cannot see the historic path after migration.
        second = await initialize_session_authority(runner(), profile_id=str(home), instance_id='new')
        owners.append(second)
        assert ref.session_id in second.sessions
        assert json.dumps(local_receipt(second.db, ref.session_id), sort_keys=True) == before
        assert second.db.get_messages(ref.session_id)[0]['content'] == 'History must survive the directory alias.'
    finally:
        for owner in owners:
            owner.db.close()
        if alias.exists():
            alias.rmdir()


def test_directory_alias_does_not_admit_another_profile(tmp_path):
    from gateway.session_local_recovery import _same_profile_directory
    alpha, beta = tmp_path / 'alpha', tmp_path / 'beta'
    alpha.mkdir()
    beta.mkdir()
    assert not _same_profile_directory(str(alpha), str(beta))
    assert not _same_profile_directory(str(tmp_path / 'missing'), str(alpha))


def test_recorded_directory_does_not_accept_another_destination(tmp_path):
    from gateway.session_local_recovery import _recorded_profile_directory
    from hermes_state import SessionDB

    home, foreign = tmp_path / 'owner', tmp_path / 'foreign'
    home.mkdir()
    foreign.mkdir()
    db = SessionDB(home / 'state.db')
    try:
        stat = foreign.stat()
        db.set_meta('gateway.local_profile_directory_aliases.v1', json.dumps({
            'old': {'canonical': str(home), 'device': stat.st_dev, 'inode': stat.st_ino}}))
        assert not _recorded_profile_directory(SimpleNamespace(db=db, profile_id=str(home)), 'old')
    finally:
        db.close()

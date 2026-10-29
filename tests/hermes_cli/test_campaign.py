"""Campaign CLI respects the active actor and persistent owner acceptance gates."""
import argparse
import json

import pytest

from hermes_cli.campaign import command, configure
from tests.gateway.test_topic_campaign_store import KEY, ingest
from gateway.topic_campaign_store import CampaignStore


def test_cli_actor_cannot_close_another_profiles_campaign(tmp_path, monkeypatch, capsys):
    from hermes_cli import profiles
    path = tmp_path / "campaigns.db"
    ingest(CampaignStore(path))
    parser = argparse.ArgumentParser()
    configure(parser)
    args = parser.parse_args(["--db", str(path), "update", KEY,
                              "--done-when", "checked", "--evidence", "artifact", "--status", "completed"])
    monkeypatch.setattr(profiles, "current_profile_name", lambda _: "writer")
    assert command(args) == 1
    assert "only the campaign owner" in json.loads(capsys.readouterr().out)["error"]
    monkeypatch.setattr(profiles, "current_profile_name", lambda _: "analyst")
    assert command(args) == 0
    assert json.loads(capsys.readouterr().out)["data"]["status"] == "completed"
    assert CampaignStore(path).get(KEY)["final_report_pending"]


def test_cli_requires_explicit_existing_shared_database(tmp_path):
    from hermes_cli.campaign import execute
    parser = argparse.ArgumentParser()
    configure(parser)
    with pytest.raises(ValueError, match="existing absolute"):
        execute(parser.parse_args(["--db", str(tmp_path / "missing.db"), "list"]), "analyst")


def test_prompt_command_runs_in_hermes_terminal_shell(tmp_path, monkeypatch):
    import subprocess
    from gateway.topic_campaign_prompt import cli_command
    from tools.environments.local import _find_bash

    path = tmp_path / "campaign's state.db"
    CampaignStore(path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    result = subprocess.run([_find_bash(), "-c", cli_command("default", path) + " list"],
                            capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stderr + result.stdout
    assert json.loads(result.stdout)["data"] == []
    ingest(CampaignStore(path), owner="default", profile="default", targets=["default"])
    result = subprocess.run([_find_bash(), "-c", cli_command("default", path)
                             + f" work {KEY} review --assignee writer --status pending"],
                            capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stderr + result.stdout
    assert json.loads(result.stdout)["data"]["profile"] == "writer"
    assert CampaignStore(path).get(KEY)["owner_profile"] == "default"

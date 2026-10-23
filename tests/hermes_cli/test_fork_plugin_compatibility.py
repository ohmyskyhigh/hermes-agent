"""A frozen reply-policy plugin stays scoped across real home A -> B -> A discovery."""
from pathlib import Path
import shutil

from hermes_cli import plugins
from hermes_cli.config import atomic_config_write
from hermes_constants import reset_hermes_home_override, set_hermes_home_override


def test_reply_policy_is_scoped_and_disabled_or_absent_is_noop(tmp_path, monkeypatch):
    fixture = Path(__file__).parents[1] / "fixtures/fork_plugins/feishu-group-reply-policy"
    homes = [tmp_path / name for name in ("a", "b", "absent")]
    for home in homes:
        home.mkdir()
    for home in homes[:2]:
        shutil.copytree(fixture, home / "plugins/feishu-group-reply-policy")
    atomic_config_write(homes[0] / "config.yaml", {"plugins": {"enabled": ["feishu-group-reply-policy"]}})
    atomic_config_write(homes[1] / "config.yaml", {"plugins": {"disabled": ["feishu-group-reply-policy"]}})
    monkeypatch.setattr(plugins, "get_bundled_plugins_dir", lambda: tmp_path / "empty-bundled")
    plugins._reset_plugin_managers_for_tests()
    try:
        for home, expected in ((homes[0], True), (homes[1], False), (homes[0], True), (homes[2], False)):
            token = set_hermes_home_override(home)
            try:
                plugins.discover_plugins()
                results = plugins.invoke_hook("gateway_reply_policy", phase="prepare",
                                              context={"platform": "feishu", "chat_type": "group"})
                assert ({"buffer": True} in results) is expected
            finally:
                reset_hermes_home_override(token)
    finally:
        plugins._reset_plugin_managers_for_tests()

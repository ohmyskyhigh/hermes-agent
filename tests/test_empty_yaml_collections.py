"""Removing the last collection item must not corrupt a commented config."""

import pytest

from hermes_cli.config import atomic_config_replace
from utils import atomic_roundtrip_yaml_update
from utils import yaml


@pytest.mark.parametrize("writer", ["replace", "update"])
def test_empty_commented_mapping_preserves_readable_config(tmp_path, writer):
    path = tmp_path / "config.yaml"
    path.write_text(
        "browser:\n\n  # Keep this section documented.\n  backend: browser-use\n"
        "\ndisplay:\n  tool_progress: 'off'\n", encoding="utf-8",
    )

    if writer == "replace":
        atomic_config_replace(path, {"browser": {}, "display": {"tool_progress": "off"}})
    else:
        atomic_roundtrip_yaml_update(path, "browser.backend", None)

    saved = path.read_text(encoding="utf-8")
    assert yaml.safe_load(saved) == {"browser": {}, "display": {"tool_progress": "off"}}
    assert "# Keep this section documented." in saved


def test_empty_sequence_cannot_publish_invalid_yaml(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "skills:\n  external_dirs:\n\n    # Optional directories.\n    - /example\n"
        "\ndisplay:\n  tool_progress: 'off'\n", encoding="utf-8",
    )
    expected = {"skills": {"external_dirs": []}, "display": {"tool_progress": "off"}}

    original = path.read_bytes()
    # The current emitter rejects this case; a future emitter may support it.
    # Either a readable replacement or an untouched original is acceptable.
    try:
        atomic_config_replace(path, expected)
    except yaml.YAMLError:
        assert path.read_bytes() == original
    else:
        assert yaml.safe_load(path.read_text(encoding="utf-8")) == expected

"""`Settings` is the ONE declaration of Loggair's configuration.

Every setting's name, type, default, environment variable and coercion live on
one dataclass field; `Overrides` is its typed override face. These tests pin the
two to each other and pin the user documentation to both, so adding a setting
without its documentation fails here instead of being noticed by a reader.
"""

import dataclasses
import json
import re
from pathlib import Path
from typing import Any

import pytest

import loggair
from loggair import Overrides, Settings, configure_logging, resolve_settings
from loggair.settings import OVERRIDABLE, SECRET, documented_settings

PROJECT = Path(__file__).resolve().parent.parent

# The public vocabulary get_active_config() and `python -m loggair` printed before
# Settings existed, in order. A change here changes what every consumer sees.
PUBLIC_KEYS = [
    "log_dir",
    "script_name",
    "log_file",
    "file_level",
    "console_level",
    "module_levels",
    "rotation_on_startup",
    "retention",
    "compression",
    "rotation",
    "enqueue",
    "colorize",
    "serialize",
    "console_format",
    "file_format",
    "reload_signal",
    "debug_signal",
    "alert_level",
    "alert_throttle",
    "worker_files",
    "intercept",
    "intercept_exclude",
    "capture_warnings",
    "is_main_process",
    "rank",
]


def test_every_override_key_is_a_settings_field_and_vice_versa() -> None:
    """`Overrides` (what you may pass) and the non-derived `Settings` fields (what
    is resolved) are two spellings of one list; they must never drift apart."""
    assert set(Overrides.__annotations__) == set(OVERRIDABLE)
    assert set(OVERRIDABLE) == {f.name for f in dataclasses.fields(Settings) if f.metadata.get("override", False)}


def test_to_dict_keeps_the_public_vocabulary_and_its_order() -> None:
    settings = resolve_settings()
    assert list(settings.to_dict()) == PUBLIC_KEYS


def test_to_dict_is_json_serializable_and_carries_no_secret() -> None:
    settings = resolve_settings(alert_urls=["json://localhost/a"])
    public = settings.to_dict()
    json.dumps(public)
    assert "alert_urls" not in public
    assert "alert_urls" in SECRET
    # The raw value stays reachable in-process, exactly as the old `_alert_urls` was.
    assert settings.alert_urls == ["json://localhost/a"]


def test_settings_is_frozen() -> None:
    settings = resolve_settings()
    with pytest.raises(dataclasses.FrozenInstanceError):
        settings.retention = 1  # type: ignore[misc]


def test_an_unknown_setting_is_refused_by_name_from_both_entry_points() -> None:
    with pytest.raises(TypeError, match="retentoin"):
        resolve_settings(retentoin=3)  # type: ignore[call-arg]
    with pytest.raises(TypeError, match="retentoin"):
        configure_logging(retentoin=3)  # type: ignore[call-arg]
    assert not loggair.is_configured()  # refused BEFORE any side effect


def test_a_none_override_means_not_given(monkeypatch: Any) -> None:
    """`reconfigure` and the signals forward recorded kwargs; an explicit None must
    fall through to env, files and defaults exactly as an absent argument does."""
    monkeypatch.setenv("LOGGAIR_RETENTION", "9")
    assert resolve_settings(retention=None).retention == 9
    assert resolve_settings().retention == 9


def test_every_setting_is_documented_in_the_example_yaml_and_the_reference() -> None:
    """The drift pin: a setting added to `Settings` without its documentation fails here."""
    example = (PROJECT / "loggair.example.yaml").read_text()
    reference = (PROJECT / "docs" / "configuration.md").read_text()
    yaml_keys = set(re.findall(r"^#? ?([a-z_]+):", example, re.M))
    yaml_envs = set(re.findall(r"LOGGAIR_[A-Z_]+", example))
    reference_rows = set(re.findall(r"^\| `([a-z_]+)` \|", reference, re.M))
    missing = []
    for name, spec in documented_settings().items():
        if spec.config and name not in yaml_keys:
            missing.append(f"{name}: no key in loggair.example.yaml")
        if spec.env and spec.env not in yaml_envs:
            missing.append(f"{name}: {spec.env} not mentioned in loggair.example.yaml")
        if name not in reference_rows:
            missing.append(f"{name}: no row in docs/configuration.md")
        if spec.env and spec.env not in reference:
            missing.append(f"{name}: {spec.env} not mentioned in docs/configuration.md")
    assert not missing, "\n".join(missing)

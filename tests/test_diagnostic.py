"""`python -m loggair` diagnostic + the pure resolve_settings it is built on."""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import loggair
from loggair.__main__ import main
from loggair.core import LoggingState, configure_logging, resolve_settings


def _chdir(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.chdir(tmp_path)


# --- resolve_settings: the shared PURE resolver --------------------------------


def test_resolve_settings_is_side_effect_free(tmp_path: Path, monkeypatch: Any) -> None:
    """Resolution must not create the log dir, configure, or write env vars."""
    _chdir(tmp_path, monkeypatch)
    (tmp_path / "loggair.yaml").write_text('log_dir: "./never_created"\n')

    settings = resolve_settings()

    assert settings["log_dir"].endswith("never_created")
    assert not (tmp_path / "never_created").exists()  # no mkdir
    assert LoggingState.configured is False  # no configure
    assert "LOGGAIR_SCRIPT_NAME" not in os.environ  # no env write


def test_resolve_settings_matches_active_config(tmp_path: Path, monkeypatch: Any) -> None:
    """The resolver output IS what configure_logging snapshots (single source)."""
    _chdir(tmp_path, monkeypatch)
    pre = resolve_settings(log_dir=tmp_path / "logs", script_name="match", retention=3, serialize=True)
    configure_logging(log_dir=tmp_path / "logs", script_name="match", retention=3, serialize=True)

    active = loggair.get_active_config()
    for key, value in pre.items():
        if key.startswith("_") or key == "alert_urls":
            continue
        assert active[key] == value, key


# --- python -m loggair ----------------------------------------------------------


def test_main_reports_without_configuring_or_rotating(tmp_path: Path, monkeypatch: Any, capsys: Any) -> None:
    """The doctor next to a live run must not archive its log or touch state."""
    _chdir(tmp_path, monkeypatch)
    log_dir = tmp_path / "mylogs"
    log_dir.mkdir()
    live = log_dir / "app.log"
    live.write_text("live run content")
    (tmp_path / "loggair.yaml").write_text('log_dir: "./mylogs"\nscript_name: "app"\nretention: 1\n')

    rc = main([])

    assert rc == 0
    out = capsys.readouterr().out
    assert loggair.__version__ in out
    assert "loggair.yaml" in out and "found" in out
    assert '"retention": 1' in out or "retention: 1" in out
    # THE invariant: no rotation, no state mutation
    assert live.read_text() == "live run content"
    assert list(log_dir.iterdir()) == [live]
    assert loggair.is_configured() is False
    assert "LOGGAIR_SCRIPT_NAME" not in os.environ


def test_main_json_redacts_alert_secrets(tmp_path: Path, monkeypatch: Any, capsys: Any) -> None:
    _chdir(tmp_path, monkeypatch)
    monkeypatch.setenv("LOGGAIR_ALERT_URLS", "slack://SecretTokA/SecretTokB/#ops")

    rc = main(["--json"])

    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["loggair"] == loggair.__version__
    assert report["resolved"]["alert_urls"] == ["slack://****"]
    assert "SecretTok" not in json.dumps(report)


def test_main_reports_rank_and_source(tmp_path: Path, monkeypatch: Any, capsys: Any) -> None:
    _chdir(tmp_path, monkeypatch)
    monkeypatch.setenv("RANK", "3")

    rc = main(["--json"])

    report = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert report["process"]["rank"] == 3
    assert report["process"]["rank_source"] == "RANK"


def test_main_reports_config_errors_with_exit_1(tmp_path: Path, monkeypatch: Any, capsys: Any) -> None:
    """Broken config is exactly what a doctor is for: report + exit 1, no crash."""
    _chdir(tmp_path, monkeypatch)
    (tmp_path / "loggair.yaml").write_text('file_level: "BOGUS"\n')

    rc = main([])

    assert rc == 1
    out = capsys.readouterr().out
    assert "CONFIGURATION ERROR" in out and "BOGUS" in out
    assert loggair.__version__ in out  # version/context still shown


def test_python_dash_m_entrypoint(tmp_path: Path) -> None:
    """The real `python -m loggair` invocation works and stays read-only."""
    env = dict(os.environ, HOME=str(tmp_path))
    env.pop("XDG_CONFIG_HOME", None)
    env = {k: v for k, v in env.items() if not k.startswith("LOGGAIR_")}
    result = subprocess.run(
        [sys.executable, "-m", "loggair"], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60
    )

    assert result.returncode == 0, result.stderr
    assert loggair.__version__ in result.stdout
    assert not (tmp_path / "logs").exists()  # default ./logs NOT created


def test_python_dash_m_self_heals_package_shadowing(tmp_path: Path) -> None:
    """From a directory containing a loggair/ folder (the mono-repo workspace
    root case), the editable-install namespace shadow must be HEALED for the
    diagnostic process — full report, exit 0 — while the hazard is flagged
    (plain `import loggair` scripts from that cwd still hit the shadow)."""
    shadow_dir = tmp_path / "loggair"
    shadow_dir.mkdir()  # shadowing project-style directory
    env = dict(os.environ, HOME=str(tmp_path))
    env.pop("XDG_CONFIG_HOME", None)
    env = {k: v for k, v in env.items() if not k.startswith("LOGGAIR_")}
    result = subprocess.run(
        [sys.executable, "-m", "loggair", "--json"], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    report = json.loads(result.stdout)
    assert report["shadowed_path_ignored"] == [str(shadow_dir)]
    assert "resolved" in report  # the report is COMPLETE despite the shadow

    human = subprocess.run(
        [sys.executable, "-m", "loggair"], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60
    )
    assert human.returncode == 0
    assert "WARNING:" in human.stdout and "shadows the installed package" in human.stdout


def test_console_script_immune_to_shadowing(tmp_path: Path) -> None:
    """The `loggair` entry-point script has no cwd on sys.path — it must work
    even from a directory containing a shadowing loggair/ folder."""
    script = Path(sys.executable).parent / "loggair"
    if not script.exists():
        pytest.skip("loggair console script not installed in this environment")
    (tmp_path / "loggair").mkdir()
    env = dict(os.environ, HOME=str(tmp_path))
    env.pop("XDG_CONFIG_HOME", None)
    result = subprocess.run([str(script)], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)

    assert result.returncode == 0, result.stderr
    assert "loggair" in result.stdout

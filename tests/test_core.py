import io
import json
import os
import re
import signal
import time
from pathlib import Path
from typing import Any

import pytest
from loguru import logger

from loggair.core import (
    _ARCHIVE_RE,
    LoggingState,
    _resolve_colorize,
    configure_logging,
    force_no_color,
    get_logger,
    is_configured,
    reconfigure,
    shutdown_logging,
)

_needs_usr_signals = pytest.mark.skipif(
    not hasattr(signal, "SIGUSR1"), reason="POSIX user signals unavailable on this platform"
)


def _send_signal_and_wait(signum: int) -> None:
    """Deliver a runtime signal to ourselves and join the deferred worker.

    The handler runs at the next bytecode boundary of the main thread (i.e.
    before this function proceeds past os.kill) and only SPAWNS the worker;
    joining the stored thread makes the reconfiguration deterministic — no
    sleeps, per the workspace synchronization mandate.
    """
    os.kill(os.getpid(), signum)
    thread = LoggingState.last_signal_thread
    assert thread is not None
    thread.join(timeout=10)
    assert not thread.is_alive()


def _emit(name: str, level: str, msg: str) -> None:
    """Emit a log record with a forced `record["name"]` so per-logger filtering can be tested."""

    def _patch(record: Any) -> None:
        record["name"] = name

    logger.patch(_patch).log(level, msg)


def test_version_exposed_at_runtime() -> None:
    """__version__ reads the installed `loggair` distribution metadata (single
    source of truth — pyproject); source checkouts fall back to a dev marker."""
    import loggair

    assert isinstance(loggair.__version__, str)
    assert re.fullmatch(r"\d+\.\d+\.\d+.*", loggair.__version__) or loggair.__version__ == "0.0.0.dev0"
    assert "__version__" in loggair.__all__


def test_configure_default(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    script_name = "test_app"

    configure_logging(log_dir=log_dir, script_name=script_name)

    test_logger = get_logger("test")
    test_logger.info("Test message")
    shutdown_logging()

    log_file = log_dir / f"{script_name}.log"
    assert log_file.exists()
    assert "Test message" in log_file.read_text()


def test_configure_env_overrides_file(tmp_path: Path) -> None:
    log_dir = tmp_path / "env_test"
    # Create config file
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("[tool.loggair]\nfile_level = 'INFO'")

    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    os.environ["LOGGAIR_FILE_LEVEL"] = "TRACE"
    try:
        configure_logging(log_dir=log_dir, script_name="env_over")
        test_logger = get_logger("env_test")
        test_logger.trace("Trace message")
        shutdown_logging()

        # Check the actual file created
        log_file = log_dir / "env_over.log"
        assert log_file.exists()
        assert "Trace message" in log_file.read_text()
    finally:
        os.chdir(old_cwd)
        del os.environ["LOGGAIR_FILE_LEVEL"]


def test_configure_args_overrides_env(tmp_path: Path) -> None:
    log_dir = tmp_path / "arg_test"
    os.environ["LOGGAIR_FILE_LEVEL"] = "INFO"
    try:
        # Pass TRACE via argument
        configure_logging(log_dir=log_dir, script_name="arg_over", file_level="TRACE")
        test_logger = get_logger("arg_test")
        test_logger.trace("Trace message from arg")
        shutdown_logging()

        log_file = log_dir / "arg_over.log"
        assert log_file.exists()
        assert "Trace message from arg" in log_file.read_text()
    finally:
        del os.environ["LOGGAIR_FILE_LEVEL"]


def test_configure_rank_non_zero(tmp_path: Path) -> None:
    log_dir = tmp_path / "rank_test"
    os.environ["RANK"] = "1"
    try:
        configure_logging(log_dir=log_dir, script_name="rank_app")
        test_logger = get_logger("rank")
        test_logger.info("Rank 1 message")
        shutdown_logging()

        log_file = log_dir / "rank_app.log"
        assert log_file.exists()
        content = log_file.read_text()
        assert "Rank 1 message" in content
        assert "[rank 1]" in content
    finally:
        del os.environ["RANK"]


def test_configure_rank_mocked(tmp_path: Path, monkeypatch: Any) -> None:
    log_dir = tmp_path / "mock_rank"
    # Mock get_rank to return 2
    import loggair.discovery

    monkeypatch.setattr(loggair.discovery, "get_rank", lambda: 2)

    configure_logging(log_dir=log_dir, script_name="mocked")
    test_logger = get_logger("test")
    test_logger.info("Mocked rank message")
    shutdown_logging()

    log_file = log_dir / "mocked.log"
    assert log_file.exists()
    content = log_file.read_text()
    assert "Mocked rank message" in content
    assert "[rank 2]" in content


def test_configure_no_rotation(tmp_path: Path) -> None:
    log_dir = tmp_path / "no_rotate"
    log_dir.mkdir()
    log_file = log_dir / "app.log"
    log_file.write_text("old\n")

    # Wait a bit so mtime is different if needed
    time.sleep(0.1)

    # Initial config (this will clobber or append depending on mode)
    # Since it's the first config in this process, it might rotate if rotation_on_startup is True
    configure_logging(log_dir=log_dir, script_name="app", rotation_on_startup=False)
    test_logger = get_logger("no_rotate")
    test_logger.info("new")
    shutdown_logging()

    content = log_file.read_text()
    assert "old" in content
    assert "new" in content


def test_startup_rotation(tmp_path: Path) -> None:
    log_dir = tmp_path / "rotation_test"
    log_dir.mkdir()
    log_file = log_dir / "rotate.log"
    log_file.write_text("old content")

    # Small sleep to ensure mtime is distinct
    time.sleep(0.1)

    # First configuration: should rotate the existing file
    configure_logging(log_dir=log_dir, script_name="rotate", rotation_on_startup=True)
    get_logger().info("new content")
    shutdown_logging()

    # Check that a rotated file exists
    rotated_files = list(log_dir.glob("rotate.*.log"))
    assert len(rotated_files) == 1
    assert "old content" in rotated_files[0].read_text()
    assert "new content" in (log_dir / "rotate.log").read_text()


# --- Per-logger, per-sink level overrides (`module_levels`) -----------------


def _write_yaml(path: Path, body: str) -> None:
    path.write_text(body)


def test_module_levels_demotes_logger_on_file_sink(tmp_path: Path) -> None:
    """Global file_level=DEBUG, override file=WARNING for pkg.a — INFO from pkg.a dropped, INFO from pkg.b kept."""
    _write_yaml(
        tmp_path / "loggair.yaml",
        'file_level: "DEBUG"\nconsole_level: "INFO"\nmodule_levels:\n  "pkg.a":\n    file: WARNING\n',
    )
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        configure_logging(log_dir=tmp_path / "logs", script_name="demote")
        _emit("pkg.a", "INFO", "from-a-info")
        _emit("pkg.a", "WARNING", "from-a-warn")
        _emit("pkg.b", "INFO", "from-b-info")
        shutdown_logging()

        text = (tmp_path / "logs" / "demote.log").read_text()
        assert "from-a-info" not in text
        assert "from-a-warn" in text
        assert "from-b-info" in text
    finally:
        os.chdir(old_cwd)


def test_module_levels_per_sink_split(tmp_path: Path) -> None:
    """Override console=ERROR, file=DEBUG for same logger — INFO appears in file but not console."""
    _write_yaml(
        tmp_path / "loggair.yaml",
        (
            'file_level: "DEBUG"\n'
            'console_level: "INFO"\n'
            "module_levels:\n"
            '  "pkg.split":\n'
            "    console: ERROR\n"
            "    file: DEBUG\n"
        ),
    )
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        configure_logging(log_dir=tmp_path / "logs", script_name="split")
        _emit("pkg.split", "INFO", "split-info")
        _emit("pkg.split", "DEBUG", "split-debug")
        shutdown_logging()

        text = (tmp_path / "logs" / "split.log").read_text()
        # File sink: DEBUG threshold for this logger, so both lines pass.
        assert "split-info" in text
        assert "split-debug" in text
    finally:
        os.chdir(old_cwd)


def test_module_levels_promotes_above_global(tmp_path: Path) -> None:
    """Global file_level=INFO, override file=DEBUG for pkg.a — DEBUG from pkg.a written, DEBUG from pkg.b dropped."""
    _write_yaml(
        tmp_path / "loggair.yaml",
        'file_level: "INFO"\nconsole_level: "INFO"\nmodule_levels:\n  "pkg.a":\n    file: DEBUG\n',
    )
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        configure_logging(log_dir=tmp_path / "logs", script_name="promote")
        _emit("pkg.a", "DEBUG", "a-debug")
        _emit("pkg.b", "DEBUG", "b-debug")
        _emit("pkg.b", "INFO", "b-info")
        shutdown_logging()

        text = (tmp_path / "logs" / "promote.log").read_text()
        assert "a-debug" in text
        assert "b-debug" not in text
        assert "b-info" in text
    finally:
        os.chdir(old_cwd)


def test_module_levels_workers_only_no_effect_in_main(tmp_path: Path) -> None:
    """workers_only=true rule is a no-op when current_process().name == 'MainProcess'."""
    _write_yaml(
        tmp_path / "loggair.yaml",
        (
            'file_level: "DEBUG"\n'
            "module_levels:\n"
            '  "pkg.workeronly":\n'
            "    file: WARNING\n"
            "    workers_only: true\n"
        ),
    )
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        configure_logging(log_dir=tmp_path / "logs", script_name="wo_main")
        _emit("pkg.workeronly", "INFO", "main-info")
        shutdown_logging()

        text = (tmp_path / "logs" / "wo_main.log").read_text()
        assert "main-info" in text
    finally:
        os.chdir(old_cwd)


def test_module_levels_workers_only_applies_in_child(tmp_path: Path, monkeypatch: Any) -> None:
    """workers_only=true rule fires when current_process().name != 'MainProcess'."""
    _write_yaml(
        tmp_path / "loggair.yaml",
        (
            'file_level: "DEBUG"\n'
            "module_levels:\n"
            '  "pkg.workeronly":\n'
            "    file: WARNING\n"
            "    workers_only: true\n"
        ),
    )
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        # Patch current_process BEFORE configure_logging so the filter closure
        # is built against the real function, and the patch is visible at
        # record-emission time (the filter calls current_process() per record).
        import loggair.core

        class _FakeProc:
            name = "Worker-1"

        monkeypatch.setattr(loggair.core, "current_process", lambda: _FakeProc())

        configure_logging(log_dir=tmp_path / "logs", script_name="wo_child")
        _emit("pkg.workeronly", "INFO", "child-info")
        _emit("pkg.workeronly", "WARNING", "child-warn")
        _emit("pkg.other", "INFO", "other-info")
        shutdown_logging()

        text = (tmp_path / "logs" / "wo_child.log").read_text()
        assert "child-info" not in text
        assert "child-warn" in text
        assert "other-info" in text
    finally:
        os.chdir(old_cwd)


def test_module_levels_longest_prefix_wins(tmp_path: Path) -> None:
    """Keys 'pkg' (WARNING) and 'pkg.sub' (DEBUG) — DEBUG record from pkg.sub.mod passes."""
    _write_yaml(
        tmp_path / "loggair.yaml",
        (
            'file_level: "DEBUG"\n'
            "module_levels:\n"
            '  "pkg":\n'
            "    file: WARNING\n"
            '  "pkg.sub":\n'
            "    file: DEBUG\n"
        ),
    )
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        configure_logging(log_dir=tmp_path / "logs", script_name="longest")
        _emit("pkg.sub.mod", "DEBUG", "submod-debug")  # matches longer "pkg.sub" → DEBUG threshold
        _emit("pkg.other", "DEBUG", "other-debug")  # matches only "pkg" → WARNING threshold
        _emit("pkg.other", "WARNING", "other-warn")
        shutdown_logging()

        text = (tmp_path / "logs" / "longest.log").read_text()
        assert "submod-debug" in text
        assert "other-debug" not in text
        assert "other-warn" in text
    finally:
        os.chdir(old_cwd)


def test_module_levels_prefix_does_not_match_substring(tmp_path: Path) -> None:
    """Key 'foo' must NOT silence logs from 'foobar.baz'."""
    _write_yaml(
        tmp_path / "loggair.yaml",
        'file_level: "DEBUG"\nmodule_levels:\n  "foo":\n    file: ERROR\n',
    )
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        configure_logging(log_dir=tmp_path / "logs", script_name="substr")
        _emit("foobar.baz", "INFO", "foobar-info")  # must NOT match "foo"
        _emit("foo.child", "INFO", "foo-child-info")  # must match
        _emit("foo", "INFO", "foo-info")  # exact match
        shutdown_logging()

        text = (tmp_path / "logs" / "substr.log").read_text()
        assert "foobar-info" in text
        assert "foo-child-info" not in text
        assert "foo-info" not in text
    finally:
        os.chdir(old_cwd)


def test_module_levels_invalid_level_raises(tmp_path: Path) -> None:
    _write_yaml(
        tmp_path / "loggair.yaml",
        'file_level: "DEBUG"\nmodule_levels:\n  "pkg.a":\n    file: BOGUS\n',
    )
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        with pytest.raises(ValueError, match="BOGUS"):
            configure_logging(log_dir=tmp_path / "logs", script_name="bad_level")
    finally:
        os.chdir(old_cwd)


def test_module_levels_unknown_subkey_raises(tmp_path: Path) -> None:
    """Typo guard — silent ignores are how this feature rotted unimplemented."""
    _write_yaml(
        tmp_path / "loggair.yaml",
        'file_level: "DEBUG"\nmodule_levels:\n  "pkg.a":\n    file: INFO\n    file_level: INFO\n',
    )
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        with pytest.raises(ValueError, match="file_level"):
            configure_logging(log_dir=tmp_path / "logs", script_name="typo")
    finally:
        os.chdir(old_cwd)


def test_module_levels_absent_is_no_op(tmp_path: Path) -> None:
    """Config without `module_levels` behaves exactly as before."""
    _write_yaml(tmp_path / "loggair.yaml", 'file_level: "DEBUG"\nconsole_level: "INFO"\n')
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        configure_logging(log_dir=tmp_path / "logs", script_name="noop")
        _emit("anything.at.all", "INFO", "noop-info")
        _emit("anything.at.all", "DEBUG", "noop-debug")
        shutdown_logging()

        text = (tmp_path / "logs" / "noop.log").read_text()
        assert "noop-info" in text
        assert "noop-debug" in text
    finally:
        os.chdir(old_cwd)


def test_module_levels_requires_at_least_one_sink(tmp_path: Path) -> None:
    _write_yaml(
        tmp_path / "loggair.yaml",
        'file_level: "DEBUG"\nmodule_levels:\n  "pkg.a":\n    workers_only: true\n',
    )
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        with pytest.raises(ValueError, match="at least one"):
            configure_logging(log_dir=tmp_path / "logs", script_name="empty_rule")
    finally:
        os.chdir(old_cwd)


class _FakeStderr(io.StringIO):
    """A StringIO that lies about being (or not being) a TTY.

    Used to exercise loguru's ``colorize=None`` auto-detection on the console
    sink without depending on the real terminal the suite happens to run under.
    It is deliberately NOT ``sys.__stderr__``, so loguru's CI / PyCharm
    heuristics in ``_colorama.should_colorize`` are bypassed and the decision
    falls through to ``isatty()`` — making the test deterministic in CI too.
    """

    def __init__(self, *, tty: bool) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def test_console_color_emitted_on_tty(tmp_path: Path, monkeypatch: Any) -> None:
    """colorize=None: an interactive (TTY) stderr receives ANSI color codes."""
    stream = _FakeStderr(tty=True)
    monkeypatch.setattr("sys.stderr", stream)

    configure_logging(log_dir=tmp_path / "logs", script_name="tty")
    get_logger("test").info("colored hello")
    logger.complete()

    out = stream.getvalue()
    assert "colored hello" in out
    assert "\x1b[" in out  # ANSI escape sequence present


def test_console_color_stripped_when_not_a_tty(tmp_path: Path, monkeypatch: Any) -> None:
    """colorize=None: a redirected/piped (non-TTY) stderr receives plain text."""
    stream = _FakeStderr(tty=False)
    monkeypatch.setattr("sys.stderr", stream)

    configure_logging(log_dir=tmp_path / "logs", script_name="notty")
    get_logger("test").info("plain hello")
    logger.complete()

    out = stream.getvalue()
    assert "plain hello" in out
    assert "\x1b[" not in out  # no ANSI escape codes pollute redirected output


# --- colorize tri-state resolution (pure helper) ----------------------------


def test_resolve_colorize_precedence(monkeypatch: Any) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)

    # 1. explicit arg beats everything (incl. NO_COLOR)
    assert _resolve_colorize(False) is False
    assert _resolve_colorize(True) is True

    # 2. NO_COLOR (any non-empty) forces off; an explicit arg still wins over it
    monkeypatch.setenv("NO_COLOR", "1")
    assert _resolve_colorize(None) is False
    assert _resolve_colorize(True) is True
    monkeypatch.delenv("NO_COLOR")

    # 3. NO_COLOR= empty deliberately re-allows colors -> auto-detect
    monkeypatch.setenv("NO_COLOR", "")
    assert _resolve_colorize(None) is None
    monkeypatch.delenv("NO_COLOR")

    # 4. unset -> None (auto-detect)
    assert _resolve_colorize(None) is None


# --- colorize override applied to the real console sink ----------------------


def test_no_color_env_strips_on_a_tty(tmp_path: Path, monkeypatch: Any) -> None:
    """The standard NO_COLOR env var disables colors even on an interactive TTY."""
    monkeypatch.setenv("NO_COLOR", "1")
    stream = _FakeStderr(tty=True)
    monkeypatch.setattr("sys.stderr", stream)

    configure_logging(log_dir=tmp_path / "logs", script_name="nocolor")
    get_logger("test").info("respect no_color")
    logger.complete()

    out = stream.getvalue()
    assert "respect no_color" in out
    assert "\x1b[" not in out


def test_colorize_arg_forces_off_on_a_tty(tmp_path: Path, monkeypatch: Any) -> None:
    """An explicit colorize=False strips colors even on an interactive TTY."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    stream = _FakeStderr(tty=True)
    monkeypatch.setattr("sys.stderr", stream)

    configure_logging(log_dir=tmp_path / "logs", script_name="argoff", colorize=False)
    get_logger("test").info("no colors please")
    logger.complete()

    out = stream.getvalue()
    assert "no colors please" in out
    assert "\x1b[" not in out


def test_colorize_arg_forces_on_when_not_a_tty(tmp_path: Path, monkeypatch: Any) -> None:
    """An explicit colorize=True forces colors on a non-TTY (redirected) stream."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    stream = _FakeStderr(tty=False)
    monkeypatch.setattr("sys.stderr", stream)

    configure_logging(log_dir=tmp_path / "logs", script_name="argon", colorize=True)
    get_logger("test").info("colors anyway")
    logger.complete()

    out = stream.getvalue()
    assert "colors anyway" in out
    assert "\x1b[" in out


def test_colorize_arg_overrides_no_color(tmp_path: Path, monkeypatch: Any) -> None:
    """An explicit colorize= argument beats the NO_COLOR env var."""
    monkeypatch.setenv("NO_COLOR", "1")
    stream = _FakeStderr(tty=False)
    monkeypatch.setattr("sys.stderr", stream)

    configure_logging(log_dir=tmp_path / "logs", script_name="argwins", colorize=True)
    get_logger("test").info("arg wins")
    logger.complete()

    assert "\x1b[" in stream.getvalue()


def test_reconfigure_can_turn_colors_off(tmp_path: Path, monkeypatch: Any) -> None:
    """reconfigure() reloads sinks and applies a new colorize after initial setup."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    stream = _FakeStderr(tty=True)
    monkeypatch.setattr("sys.stderr", stream)

    # First config auto-detects -> colored on the TTY stream.
    configure_logging(log_dir=tmp_path / "logs", script_name="reconf")
    get_logger("test").info("colored first")
    logger.complete()
    assert "\x1b[" in stream.getvalue()

    # Reload with colors forced off; the new sink writes plain text.
    stream.truncate(0)
    stream.seek(0)
    reconfigure(colorize=False)
    get_logger("test").info("plain after reload")
    logger.complete()

    out = stream.getvalue()
    assert "plain after reload" in out
    assert "\x1b[" not in out


# --- force_no_color / is_configured (non-interactive consumer helper) --------


def test_is_configured_reflects_state(tmp_path: Path) -> None:
    assert is_configured() is False
    configure_logging(log_dir=tmp_path / "logs", script_name="cfgstate")
    assert is_configured() is True


def test_force_no_color_sets_no_color_without_configuring(monkeypatch: Any) -> None:
    """When Loggair is not yet configured, force_no_color only sets NO_COLOR."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert is_configured() is False

    force_no_color()

    assert os.environ.get("NO_COLOR") == "1"
    assert is_configured() is False  # did NOT front-load configuration


def test_force_no_color_reloads_when_already_configured(tmp_path: Path, monkeypatch: Any) -> None:
    """The real bug: Loggair already colorized -> force_no_color must reload it off."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    stream = _FakeStderr(tty=True)
    monkeypatch.setattr("sys.stderr", stream)

    # Configure FIRST (auto-detect -> colored on the TTY) — the baked-in state.
    configure_logging(log_dir=tmp_path / "logs", script_name="fnc")
    get_logger("test").info("colored before")
    logger.complete()
    assert "\x1b[" in stream.getvalue()

    # force_no_color must set NO_COLOR AND reload so the live sink goes plain.
    stream.truncate(0)
    stream.seek(0)
    force_no_color()
    assert os.environ.get("NO_COLOR") == "1"

    get_logger("test").info("plain after")
    logger.complete()
    out = stream.getvalue()
    assert "plain after" in out
    assert "\x1b[" not in out


# --- falsy config-file values must be honored (presence-based resolution) ----


def test_yaml_rotation_on_startup_false_is_honored(tmp_path: Path) -> None:
    """`rotation_on_startup: false` in YAML must disable rotation (it used to be
    silently swallowed by an `or`-chain and fall back to the default True)."""
    _write_yaml(tmp_path / "loggair.yaml", "rotation_on_startup: false\n")
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True)
    log_file = log_dir / "norot.log"
    log_file.write_text("old\n")

    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        configure_logging(log_dir=log_dir, script_name="norot")
        get_logger().info("new")
        shutdown_logging()

        assert list(log_dir.glob("norot.*.log")) == []  # nothing rotated
        content = log_file.read_text()
        assert "old" in content
        assert "new" in content
    finally:
        os.chdir(old_cwd)


def test_yaml_retention_zero_is_honored(tmp_path: Path) -> None:
    """`retention: 0` in YAML must keep zero archives (not fall back to 5)."""
    _write_yaml(tmp_path / "loggair.yaml", "retention: 0\n")
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "keepnone.log").write_text("old content")

    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        time.sleep(0.05)
        configure_logging(log_dir=log_dir, script_name="keepnone")
        shutdown_logging()

        assert list(log_dir.glob("keepnone.*.log")) == []  # rotated archive purged
    finally:
        os.chdir(old_cwd)


# --- get_logger(name) overrides record["name"] --------------------------------


def test_get_logger_name_appears_in_output(tmp_path: Path) -> None:
    """The name given to get_logger must show up as the {name} field."""
    configure_logging(log_dir=tmp_path / "logs", script_name="named")
    get_logger("my.custom.name").info("named line")
    shutdown_logging()

    text = (tmp_path / "logs" / "named.log").read_text()
    assert "my.custom.name" in text


def test_get_logger_name_participates_in_module_levels(tmp_path: Path) -> None:
    """A module_levels rule keyed on the bound name must gate the named logger."""
    _write_yaml(
        tmp_path / "loggair.yaml",
        'file_level: "DEBUG"\nmodule_levels:\n  "bound.pkg":\n    file: WARNING\n',
    )
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        configure_logging(log_dir=tmp_path / "logs", script_name="boundml")
        bound = get_logger("bound.pkg.mod")
        bound.info("bound-info")
        bound.warning("bound-warn")
        get_logger("other.pkg").info("other-info")
        shutdown_logging()

        text = (tmp_path / "logs" / "boundml.log").read_text()
        assert "bound-info" not in text
        assert "bound-warn" in text
        assert "other-info" in text
    finally:
        os.chdir(old_cwd)


# --- structured / JSON logging (loguru serialize=True) ------------------------


def _parse_json_lines(text: str) -> list:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_serialize_file_sink_emits_json(tmp_path: Path) -> None:
    """serialize=True: every file-sink line is a JSON object with the full
    structured record (level, timestamp, name, extra incl. rank_tag)."""
    configure_logging(log_dir=tmp_path / "logs", script_name="json", serialize=True)
    get_logger("svc.worker").info("structured hello")
    shutdown_logging()

    objs = _parse_json_lines((tmp_path / "logs" / "json.log").read_text())
    assert objs  # nothing non-JSON slipped through — every line parsed
    rec = next(o["record"] for o in objs if o["record"]["message"] == "structured hello")
    assert rec["level"]["name"] == "INFO"
    assert rec["name"] == "svc.worker"  # get_logger(name) lands in record.name
    assert "rank_tag" in rec["extra"]
    assert isinstance(rec["time"]["timestamp"], float)


def test_serialize_console_sink_json_without_ansi_even_on_tty(tmp_path: Path, monkeypatch: Any) -> None:
    """serialize forces colors off: even on a TTY with colorize=True, the
    console emits parseable JSON with no ANSI inside the text field."""
    stream = _FakeStderr(tty=True)
    monkeypatch.setattr("sys.stderr", stream)

    configure_logging(log_dir=tmp_path / "logs", script_name="jsontty", serialize=True, colorize=True)
    get_logger("test").info("json console")
    logger.complete()

    out = stream.getvalue()
    assert "\x1b[" not in out
    objs = _parse_json_lines(out)
    assert any(o["record"]["message"] == "json console" for o in objs)
    assert all("\x1b[" not in o["text"] for o in objs)


def test_serialize_via_env_var(tmp_path: Path) -> None:
    os.environ["LOGGAIR_SERIALIZE"] = "true"
    try:
        configure_logging(log_dir=tmp_path / "logs", script_name="jsonenv")
        get_logger("test").info("env json")
        shutdown_logging()

        objs = _parse_json_lines((tmp_path / "logs" / "jsonenv.log").read_text())
        assert any(o["record"]["message"] == "env json" for o in objs)
    finally:
        del os.environ["LOGGAIR_SERIALIZE"]


def test_serialize_via_yaml_and_false_stays_plain(tmp_path: Path) -> None:
    """YAML `serialize: true` enables JSON; `serialize: false` stays plain."""
    _write_yaml(tmp_path / "loggair.yaml", "serialize: true\n")
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        configure_logging(log_dir=tmp_path / "logs", script_name="jsonyaml")
        get_logger("test").info("yaml json")
        shutdown_logging()
        objs = _parse_json_lines((tmp_path / "logs" / "jsonyaml.log").read_text())
        assert any(o["record"]["message"] == "yaml json" for o in objs)

        _write_yaml(tmp_path / "loggair.yaml", "serialize: false\n")
        reconfigure(log_dir=tmp_path / "logs", script_name="plainyaml")
        get_logger("test").info("plain line")
        shutdown_logging()
        text = (tmp_path / "logs" / "plainyaml.log").read_text()
        plain = next(line for line in text.splitlines() if "plain line" in line)
        with pytest.raises(ValueError):
            json.loads(plain)
    finally:
        os.chdir(old_cwd)


def test_serialize_respects_module_levels(tmp_path: Path) -> None:
    """Per-logger, per-sink filtering applies unchanged in JSON mode."""
    _write_yaml(
        tmp_path / "loggair.yaml",
        'serialize: true\nfile_level: "DEBUG"\nmodule_levels:\n  "quiet.pkg":\n    file: WARNING\n',
    )
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        configure_logging(log_dir=tmp_path / "logs", script_name="jsonml")
        _emit("quiet.pkg", "INFO", "quiet-info")
        _emit("quiet.pkg", "WARNING", "quiet-warn")
        shutdown_logging()

        messages = [o["record"]["message"] for o in _parse_json_lines((tmp_path / "logs" / "jsonml.log").read_text())]
        assert "quiet-info" not in messages
        assert "quiet-warn" in messages
    finally:
        os.chdir(old_cwd)


# --- configurable sink formats -------------------------------------------------


def test_file_format_arg_overrides_default(tmp_path: Path) -> None:
    """A custom file_format renders loguru fields (pid/tid) verbatim."""
    configure_logging(
        log_dir=tmp_path / "logs",
        script_name="fmta",
        file_format="pid={process.id} tid={thread.id} | {level} | {message}",
    )
    get_logger("t").info("custom formatted")
    shutdown_logging()

    line = next(
        line for line in (tmp_path / "logs" / "fmta.log").read_text().splitlines() if "custom formatted" in line
    )
    assert re.fullmatch(r"pid=\d+ tid=\d+ \| INFO \| custom formatted", line)


def test_console_format_via_env(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("LOGGAIR_CONSOLE_FORMAT", "CONSOLE>>{message}")
    stream = _FakeStderr(tty=False)
    monkeypatch.setattr("sys.stderr", stream)

    configure_logging(log_dir=tmp_path / "logs", script_name="fmtenv")
    get_logger("t").info("env format")
    logger.complete()

    assert "CONSOLE>>env format" in stream.getvalue()


def test_file_format_via_yaml_extends_default(tmp_path: Path) -> None:
    """YAML file_format works; DEFAULT_FILE_FORMAT is public for extension."""
    from loggair import DEFAULT_FILE_FORMAT

    _write_yaml(tmp_path / "loggair.yaml", f'file_format: "{DEFAULT_FILE_FORMAT} | pid={{process.id}}"\n')
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        configure_logging(log_dir=tmp_path / "logs", script_name="fmtyaml")
        get_logger("t").info("extended")
        shutdown_logging()

        line = next(line for line in (tmp_path / "logs" / "fmtyaml.log").read_text().splitlines() if "extended" in line)
        assert re.search(r"\| pid=\d+$", line)  # appended to the default layout
        assert " | INFO " in line  # default layout still present
    finally:
        os.chdir(old_cwd)


def test_custom_format_keeps_rank_tag_available(tmp_path: Path, monkeypatch: Any) -> None:
    """The filter stamps extra[rank_tag] regardless of format, so a custom
    format may reference it."""
    import loggair.discovery

    monkeypatch.setattr(loggair.discovery, "get_rank", lambda: 3)
    configure_logging(log_dir=tmp_path / "logs", script_name="fmtrank", file_format="{extra[rank_tag]}{message}")
    get_logger("t").info("tagged")
    shutdown_logging()

    text = (tmp_path / "logs" / "fmtrank.log").read_text()
    assert "[rank 3] | tagged" in text


def test_invalid_console_markup_fails_fast(tmp_path: Path) -> None:
    """Bad color markup raises at configure time (loguru validates at add())."""
    with pytest.raises(ValueError, match="markup"):
        configure_logging(log_dir=tmp_path / "logs", script_name="fmtbad", console_format="<nope>{message}</nope>")


# --- runtime rotation (loguru rotation= on the file sink) ---------------------


def _emit_bulk(n: int = 40, payload: str = "x" * 60) -> None:
    lg = get_logger("bulk")
    for i in range(n):
        lg.info(f"{i} {payload}")


def test_runtime_rotation_by_size(tmp_path: Path) -> None:
    """rotation='<size>' rotates the ACTIVE file at runtime (loguru-named
    archives with a microseconds timestamp), keeping the active file present."""
    log_dir = tmp_path / "logs"
    configure_logging(log_dir=log_dir, script_name="rt", rotation="400 B")
    _emit_bulk()
    shutdown_logging()

    assert (log_dir / "rt.log").exists()
    archives = list(log_dir.glob("rt.*.log"))
    assert archives  # at least one runtime rotation happened
    assert all(_ARCHIVE_RE.fullmatch(a.name) for a in archives)  # sweep recognises them
    assert all(re.search(r"_\d{6}\.log$", a.name) for a in archives)  # loguru naming


def test_runtime_rotation_retention_and_compression(tmp_path: Path) -> None:
    """With rotation active, loguru's sink-level retention prunes per stem at
    rotation time and compression fires on the runtime archives."""
    log_dir = tmp_path / "logs"
    configure_logging(log_dir=log_dir, script_name="rtc", rotation="400 B", retention=2, compression="gz")
    _emit_bulk()
    shutdown_logging()

    archives = list(log_dir.glob("rtc.*.log.gz"))
    assert 1 <= len(archives) <= 2  # pruned to retention at rotation time
    assert list(log_dir.glob("rtc.*.log")) == []  # nothing left uncompressed


def test_runtime_rotation_via_env(tmp_path: Path) -> None:
    os.environ["LOGGAIR_ROTATION"] = "400 B"
    try:
        configure_logging(log_dir=tmp_path / "logs", script_name="rtenv")
        _emit_bulk()
        shutdown_logging()
        assert list((tmp_path / "logs").glob("rtenv.*.log"))
    finally:
        del os.environ["LOGGAIR_ROTATION"]


def test_runtime_rotation_via_yaml(tmp_path: Path) -> None:
    _write_yaml(tmp_path / "loggair.yaml", 'rotation: "400 B"\n')
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        configure_logging(log_dir=tmp_path / "logs", script_name="rtyaml")
        _emit_bulk()
        shutdown_logging()
        assert list((tmp_path / "logs").glob("rtyaml.*.log"))
    finally:
        os.chdir(old_cwd)


def test_invalid_rotation_raises_at_configure(tmp_path: Path) -> None:
    """Loguru validates the rotation string at add() — fail fast, no silent ignore."""
    with pytest.raises(ValueError, match="rotation"):
        configure_logging(log_dir=tmp_path / "logs", script_name="rtbad", rotation="every blue moon")


def test_runtime_rotation_only_in_main_process(tmp_path: Path, monkeypatch: Any) -> None:
    """A worker/child process must NEVER get a rotating sink — a rename racing
    multiple writers would split the stream. Only the main process rotates."""
    import loggair.core

    class _FakeProc:
        name = "Worker-1"

    monkeypatch.setattr(loggair.core, "current_process", lambda: _FakeProc())

    configure_logging(log_dir=tmp_path / "logs", script_name="rtchild", rotation="400 B")
    params = loggair.core.LoggingState.file_sink_params
    assert params is not None
    assert "rotation" not in params


def test_startup_sweep_prunes_loguru_runtime_archives(tmp_path: Path) -> None:
    """The startup sweep recognises loguru's microseconds-stamped runtime
    archives (plain and compressed) and prunes them per stem."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    for i in range(4):
        p = log_dir / f"old.2023-01-0{i + 1}_00-00-00_{i:06d}.log"
        p.write_text("runtime archive")
        os.utime(p, (time.time() - (100 - i), time.time() - (100 - i)))
    (log_dir / "old.2023-01-05_00-00-00_000000.log.gz").write_text("compressed runtime archive")

    configure_logging(log_dir=log_dir, script_name="sweeper", retention=2, force=True)

    remaining = [f.name for f in log_dir.iterdir() if f.name.startswith("old.")]
    assert len(remaining) == 2


# --- get_active_config: resolved-settings introspection ------------------------


def test_get_active_config_unconfigured_no_side_effect() -> None:
    from loggair import get_active_config

    assert get_active_config() == {"configured": False}
    assert is_configured() is False  # introspection must NOT trigger lazy config


def test_get_active_config_reflects_resolved_settings(tmp_path: Path) -> None:
    from loggair import get_active_config

    _write_yaml(tmp_path / "loggair.yaml", 'module_levels:\n  "pkg.noisy":\n    file: WARNING\n')
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        configure_logging(
            log_dir=tmp_path / "logs",
            script_name="insp",
            retention=3,
            compression="gz",
            serialize=True,
            rotation="50 MB",
        )
        cfg = get_active_config()

        assert cfg["configured"] is True
        assert cfg["log_file"] == str(tmp_path / "logs" / "insp.log")
        assert cfg["retention"] == 3
        assert cfg["compression"] == "gz"
        assert cfg["rotation"] == "50 MB"
        assert cfg["serialize"] is True
        assert cfg["colorize"] is False  # serialize forces colors off
        assert cfg["module_levels"] == {"pkg.noisy": {"file": "WARNING"}}
        assert cfg["is_main_process"] is True
        json.dumps(cfg)  # MUST stay JSON-serializable (MCP tool surface)
    finally:
        os.chdir(old_cwd)


def test_get_active_config_returns_a_copy_and_tracks_reconfigure(tmp_path: Path) -> None:
    from loggair import get_active_config

    configure_logging(log_dir=tmp_path / "logs", script_name="cpy", retention=4)
    cfg = get_active_config()
    cfg["retention"] = 999
    cfg["module_levels"]["injected"] = {"file": "ERROR"}
    assert get_active_config()["retention"] == 4  # mutation did not leak
    assert get_active_config()["module_levels"] == {}

    reconfigure(file_level="ERROR")
    after = get_active_config()
    assert after["file_level"] == "ERROR"
    assert after["retention"] == 4  # explicit arg preserved through reconfigure


# --- dynamic level adjustment at runtime (signals + reconfigure merge) --------


@_needs_usr_signals
def test_debug_signal_toggles_debug_mode(tmp_path: Path) -> None:
    """SIGUSR2-style toggle: DEBUG everywhere on first signal, restored on second."""
    _write_yaml(tmp_path / "loggair.yaml", 'file_level: "WARNING"\n')
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        configure_logging(log_dir=tmp_path / "logs", script_name="dbgsig", debug_signal="SIGUSR2")
        lg = get_logger("dbg")

        lg.debug("dropped-before-toggle")
        _send_signal_and_wait(signal.SIGUSR2)
        lg.debug("visible-during-toggle")
        _send_signal_and_wait(signal.SIGUSR2)
        lg.debug("dropped-after-toggle")
        shutdown_logging()

        text = (tmp_path / "logs" / "dbgsig.log").read_text()
        assert "dropped-before-toggle" not in text
        assert "visible-during-toggle" in text
        assert "dropped-after-toggle" not in text
    finally:
        os.chdir(old_cwd)


@_needs_usr_signals
def test_reload_signal_applies_edited_config(tmp_path: Path) -> None:
    """SIGUSR1-style reload: edit loggair.yaml, signal, new levels apply."""
    _write_yaml(tmp_path / "loggair.yaml", 'file_level: "WARNING"\n')
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        configure_logging(log_dir=tmp_path / "logs", script_name="rlsig", reload_signal="SIGUSR1")
        lg = get_logger("rl")

        lg.debug("dropped-before-reload")
        _write_yaml(tmp_path / "loggair.yaml", 'file_level: "DEBUG"\n')
        _send_signal_and_wait(signal.SIGUSR1)
        lg.debug("visible-after-reload")
        shutdown_logging()

        text = (tmp_path / "logs" / "rlsig.log").read_text()
        assert "dropped-before-reload" not in text
        assert "visible-after-reload" in text
    finally:
        os.chdir(old_cwd)


def test_invalid_signal_name_fails_fast(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="invalid signal"):
        configure_logging(log_dir=tmp_path / "logs", script_name="badsig", reload_signal="SIGNOPE")


@_needs_usr_signals
def test_reset_logging_restores_signal_handlers(tmp_path: Path) -> None:
    from loggair import reset_logging

    before = signal.getsignal(signal.SIGUSR1)
    configure_logging(log_dir=tmp_path / "logs", script_name="restoresig", reload_signal="SIGUSR1")
    assert signal.getsignal(signal.SIGUSR1) is not before

    reset_logging()
    assert signal.getsignal(signal.SIGUSR1) == before


def test_reconfigure_preserves_explicit_args(tmp_path: Path) -> None:
    """reconfigure(colorize=False) must not pivot the log away from an
    explicitly-passed log_dir (it used to re-resolve log_dir from config
    files/defaults, silently moving the file)."""
    explicit_dir = tmp_path / "explicit"
    configure_logging(log_dir=explicit_dir, script_name="keepdir")
    assert LoggingState.log_file == explicit_dir / "keepdir.log"

    reconfigure(colorize=False)

    assert LoggingState.log_file == explicit_dir / "keepdir.log"
    get_logger("t").info("still here")
    shutdown_logging()
    assert "still here" in (explicit_dir / "keepdir.log").read_text()


# --- reset_logging: full restore of process-global state ----------------------


def test_reset_logging_restores_process_globals(tmp_path: Path) -> None:
    import logging as std_logging
    import multiprocessing.process as mp_process
    import warnings as std_warnings

    from loggair import reset_logging
    from loggair.intercept import InterceptHandler, redirect_warnings

    configure_logging(log_dir=tmp_path / "logs", script_name="resetme", enqueue=True)
    assert is_configured() is True
    assert std_warnings.showwarning is redirect_warnings
    assert any(isinstance(h, InterceptHandler) for h in std_logging.getLogger().handlers)
    assert getattr(mp_process.BaseProcess.__init__, "_loggair_patched", False) is True
    assert os.environ.get("LOGGAIR_SCRIPT_NAME") == "resetme"

    reset_logging()

    assert is_configured() is False
    assert std_warnings.showwarning is not redirect_warnings
    assert not any(isinstance(h, InterceptHandler) for h in std_logging.getLogger().handlers)
    assert getattr(mp_process.BaseProcess.__init__, "_loggair_patched", False) is False
    assert "LOGGAIR_SCRIPT_NAME" not in os.environ

    # And Loggair comes back cleanly after a reset
    configure_logging(log_dir=tmp_path / "logs", script_name="resetme2")
    get_logger("post.reset").info("alive again")
    shutdown_logging()
    assert "alive again" in (tmp_path / "logs" / "resetme2.log").read_text()


def test_force_no_color_respects_empty_no_color(tmp_path: Path, monkeypatch: Any) -> None:
    """A pre-set NO_COLOR= (empty) re-allows colors: setdefault won't clobber it."""
    monkeypatch.setenv("NO_COLOR", "")  # explicit "allow colors"
    stream = _FakeStderr(tty=True)
    monkeypatch.setattr("sys.stderr", stream)

    configure_logging(log_dir=tmp_path / "logs", script_name="emptync")
    force_no_color()  # must NOT clobber the explicit empty value
    assert os.environ.get("NO_COLOR") == ""

    get_logger("test").info("still colored")
    logger.complete()
    assert "\x1b[" in stream.getvalue()

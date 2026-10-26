"""Loggair starts on the FIRST MESSAGE, never on ``get_logger()`` alone.

A library calls ``logger = get_logger(__name__)`` at module scope; a program that merely imports it — and logs
nothing — must not print ``Loggair initialized``, open a log file, prune archives or take over the stdlib's logging.
So ``get_logger()`` before configuration hands back a stand-in that configures Loggair the first time it is used and
from then on IS the logger it stands for: the records carry the caller's module, function and line, a name given
still names them, and the stand-in pickles for spawn workers. Once configured, ``get_logger()`` returns the logger
itself, as it always did.
"""

from __future__ import annotations

import pickle
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from loguru import logger

from loggair.core import configure_logging, get_logger, is_configured
from loggair.null_logger import NullLogger


@pytest.fixture
def log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path / "logs"
    monkeypatch.setenv("LOGGAIR_DIR", str(folder))
    monkeypatch.setenv("LOGGAIR_SCRIPT_NAME", "deferred")
    return folder


def _log_text(log_dir: Path) -> str:
    logger.complete()
    return (log_dir / "deferred.log").read_text()


def test_get_logger_alone_starts_nothing(log_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    get_logger(__name__)
    get_logger()
    assert not is_configured()
    assert not log_dir.exists()
    assert "Loggair initialized" not in capsys.readouterr().err


def test_the_first_message_starts_loggair_and_is_written(log_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    log = get_logger(__name__)
    log.info("the first message")
    assert is_configured()
    err = capsys.readouterr().err
    assert "Loggair initialized: deferred.log" in err and "the first message" in err
    assert "the first message" in _log_text(log_dir)


def test_a_message_names_the_caller_and_the_name_given(log_dir: Path) -> None:
    def emitting_function() -> None:
        get_logger("acme.models").warning("from the caller")

    emitting_function()
    line = next(text for text in _log_text(log_dir).splitlines() if "from the caller" in text)
    assert "acme.models:emitting_function:" in line


def test_without_a_name_the_record_names_the_module_that_logged(log_dir: Path) -> None:
    get_logger().warning("unnamed")
    line = next(text for text in _log_text(log_dir).splitlines() if "unnamed" in text)
    assert f"{__name__}:test_without_a_name_the_record_names_the_module_that_logged:" in line


def test_a_configuration_made_after_get_logger_and_before_the_first_message_is_the_one_used(
    tmp_path: Path, log_dir: Path
) -> None:
    """A program imports its libraries (each calling `get_logger`), then configures: its settings must win."""
    log = get_logger(__name__)
    chosen = tmp_path / "chosen"
    configure_logging(log_dir=str(chosen), script_name="chosen")
    log.info("into the chosen folder")
    logger.complete()
    assert "into the chosen folder" in (chosen / "chosen.log").read_text()
    assert not log_dir.exists()


def test_once_configured_get_logger_returns_the_logger_itself(log_dir: Path) -> None:
    get_logger().info("configure")
    assert get_logger() is logger


def test_a_stand_in_used_after_configuration_is_the_same_logger(log_dir: Path) -> None:
    early = get_logger()
    get_logger().info("configured by another")
    assert early.bind(x=1) is not None
    early.info("through the early stand-in")
    assert "through the early stand-in" in _log_text(log_dir)


def test_a_stand_in_pickles_and_logs_where_it_lands(log_dir: Path) -> None:
    """A spawn worker unpickles its module-level logger; it must be a working logger there too."""
    restored = pickle.loads(pickle.dumps(get_logger("acme.worker")))
    restored.info("from a worker")
    line = next(text for text in _log_text(log_dir).splitlines() if "from a worker" in text)
    assert "acme.worker:" in line


def test_disabled_logging_still_hands_out_the_no_op_logger(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOGGAIR_DISABLE_LOGGING", "1")
    assert isinstance(get_logger(__name__), NullLogger)


def test_importing_a_library_that_only_asks_for_its_logger_prints_and_writes_nothing(tmp_path: Path) -> None:
    (tmp_path / "quiet_library.py").write_text(
        textwrap.dedent(
            """
            from loggair import get_logger

            logger = get_logger(__name__)
            """
        )
    )
    result = subprocess.run(
        [sys.executable, "-c", "import quiet_library; import loggair; print(loggair.is_configured())"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "LOGGAIR_DIR": str(tmp_path / "logs")},
    )
    assert result.returncode == 0, result.stderr
    assert (result.stdout, result.stderr) == ("False\n", "")
    assert not (tmp_path / "logs").exists()


def test_a_probe_for_a_special_name_starts_nothing(log_dir: Path) -> None:
    """`hasattr(x, "__wrapped__")`, copy and introspection probes ask for dunders; none of them is a message."""
    assert not hasattr(get_logger(__name__), "__wrapped__")
    assert not is_configured()


def test_a_method_can_be_replaced_as_on_the_logger_itself(log_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A consumer's test replaces `module.logger.warning` with `monkeypatch.setattr` to read what it warns."""
    log = get_logger("acme.module")
    said: list[str] = []
    monkeypatch.setattr(log, "warning", said.append)
    log.warning("replaced")
    assert said == ["replaced"]
    monkeypatch.undo()
    log.warning("restored")
    assert "restored" in _log_text(log_dir)

"""Loggair removes only the sinks it added, and loguru's own stderr sink; never a sink the application added.

loguru's logger is one object shared by everything in the process. Loggair replaces its own sinks whenever it
configures: on the first message, ``reconfigure()``, ``force_no_color()``, the reload signal and a move to a new log
folder. A sink the application added with ``logger.add(...)`` (a test catching a warning, a host showing the log in a
panel) keeps receiving records through every one of them.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from loguru import logger

import loggair.core as core
from loggair.core import configure_logging, get_logger, reconfigure


@pytest.fixture
def log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path / "logs"
    monkeypatch.setenv("LOGGAIR_DIR", str(folder))
    monkeypatch.setenv("LOGGAIR_SCRIPT_NAME", "sinks")
    return folder


def _application_sink() -> list[str]:
    """A sink the application adds itself, holding the text of every warning it receives."""
    caught: list[str] = []
    logger.add(lambda message: caught.append(message.record["message"]), level="WARNING")
    return caught


def _handler_ids() -> set[int]:
    return set(logger._core.handlers)  # type: ignore[attr-defined]


def test_a_sink_added_after_get_logger_receives_the_first_message(log_dir: Path) -> None:
    log = get_logger("mylib")  # a library asks for its logger at import
    caught = _application_sink()
    log.warning("the first message")  # Loggair starts here
    logger.complete()
    assert caught == ["the first message"]


def test_a_sink_the_application_added_survives_a_reconfigure(log_dir: Path) -> None:
    configure_logging()
    caught = _application_sink()
    reconfigure(colorize=False)
    get_logger().warning("after the reconfigure")
    logger.complete()
    assert caught == ["after the reconfigure"]


def test_a_sink_the_application_added_survives_a_move_to_another_log_folder(tmp_path: Path, log_dir: Path) -> None:
    configure_logging()
    caught = _application_sink()
    reconfigure(log_dir=str(tmp_path / "moved"))
    get_logger().warning("in the new folder")
    logger.complete()
    assert caught == ["in the new folder"]
    assert "in the new folder" in (tmp_path / "moved" / "sinks.log").read_text()


def test_a_reconfigure_leaves_none_of_loggairs_old_sinks(log_dir: Path) -> None:
    configure_logging()
    first = _handler_ids()
    reconfigure(colorize=False)
    second = _handler_ids()
    assert len(first) == len(second) == 2  # the console and the file sink
    assert not first & second


def test_a_reconfigure_removes_the_file_sink_the_queue_was_started_on(tmp_path: Path) -> None:
    """Starting the queue re-adds the file sink under a new id; that one is Loggair's too."""
    configure_logging(log_dir=tmp_path, script_name="queued", enqueue=True)
    core._upgrade_to_enqueue()
    queued = core.LoggingState.file_sink_id
    reconfigure(colorize=False)
    assert queued not in _handler_ids()
    assert len(_handler_ids()) == 2


def _run(script: str, tmp_path: Path, **env: str) -> subprocess.CompletedProcess[str]:
    """`script` in a fresh interpreter, whose loguru starts with its own stderr sink as id 0."""
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "LOGGAIR_DIR": str(tmp_path / "logs"), **env},
    )


def test_loggurus_own_stderr_sink_is_replaced_so_a_message_prints_once(tmp_path: Path) -> None:
    result = _run("from loggair import get_logger; get_logger().warning('printed once')", tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stderr.count("printed once") == 1


def test_with_loggurus_own_sink_turned_off_the_applications_sink_with_id_0_is_kept(tmp_path: Path) -> None:
    """Without `LOGURU_AUTOINIT`, loguru adds no sink of its own, so id 0 is the application's first one."""
    result = _run(
        """
        from loguru import logger
        caught = []
        sink_id = logger.add(lambda message: caught.append(message.record["message"]), level="WARNING")
        from loggair import get_logger
        get_logger().warning("kept")
        logger.complete()
        print(sink_id, caught)
        """,
        tmp_path,
        LOGURU_AUTOINIT="false",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "0 ['kept']\n"

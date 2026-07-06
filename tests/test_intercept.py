import logging
import os
import warnings
from pathlib import Path

import pytest

from loggair.core import configure_logging, shutdown_logging


def test_intercept_standard_logging(tmp_path: Path) -> None:
    log_dir = tmp_path / "intercept_logs"
    configure_logging(log_dir=log_dir, script_name="standard")

    std_logger = logging.getLogger("test_lib")
    std_logger.info("Message from standard logging")

    shutdown_logging()

    log_file = log_dir / "standard.log"
    assert log_file.exists()
    assert "Message from standard logging" in log_file.read_text()


def test_intercept_exception(tmp_path: Path) -> None:
    log_dir = tmp_path / "exception_logs"
    configure_logging(log_dir=log_dir, script_name="exception")

    std_logger = logging.getLogger("exception_lib")
    try:
        raise ValueError("Intercepted error")
    except ValueError:
        std_logger.exception("An error occurred")

    shutdown_logging()

    log_file = log_dir / "exception.log"
    assert log_file.exists()
    content = log_file.read_text()
    assert "An error occurred" in content
    assert "ValueError: Intercepted error" in content


def test_intercept_unknown_level(tmp_path: Path) -> None:
    log_dir = tmp_path / "unknown_level_logs"
    configure_logging(log_dir=log_dir, script_name="unknown")

    # Manually emit a record with an unknown level
    record = logging.LogRecord(
        name="test",
        level=99,  # Unknown level
        pathname="test.py",
        lineno=1,
        msg="Unknown level message",
        args=None,
        exc_info=None,
    )
    from loggair.intercept import InterceptHandler

    handler = InterceptHandler()
    handler.emit(record)

    shutdown_logging()

    log_file = log_dir / "unknown.log"
    assert log_file.exists()
    content = log_file.read_text()
    assert "Unknown level message" in content
    # Note: Loguru typically maps unknown levels to level names like 'Level 99'
    assert "Level 99" in content or "99" in content


def test_third_party_defaults_silence_at_stdlib_layer(tmp_path: Path) -> None:
    """Without a user rule, the chatty third-party defaults stay gated at
    WARNING in stdlib — their DEBUG records never reach the log file."""
    log_dir = tmp_path / "tp_default"
    configure_logging(log_dir=log_dir, script_name="tpdef")

    assert logging.getLogger("httpx").level == logging.WARNING
    logging.getLogger("httpx").debug("httpx-debug-hidden")
    logging.getLogger("httpx").warning("httpx-warn-visible")

    shutdown_logging()
    content = (log_dir / "tpdef.log").read_text()
    assert "httpx-debug-hidden" not in content
    assert "httpx-warn-visible" in content


def test_third_party_default_yields_to_module_levels_rule(tmp_path: Path) -> None:
    """A module_levels rule targeting a defaulted third-party logger lifts the
    stdlib gate so the user's per-sink threshold decides instead."""
    (tmp_path / "loggair.yaml").write_text(
        'file_level: "DEBUG"\nmodule_levels:\n  "httpx":\n    file: DEBUG\n',
    )
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        log_dir = tmp_path / "tp_override"
        configure_logging(log_dir=log_dir, script_name="tpov")

        assert logging.getLogger("httpx").level == logging.NOTSET
        logging.getLogger("httpx").debug("httpx-debug-now-visible")

        shutdown_logging()
        content = (log_dir / "tpov.log").read_text()
        assert "httpx-debug-now-visible" in content
    finally:
        os.chdir(old_cwd)


def test_intercept_emit_called_directly_shallow_stack(tmp_path: Path) -> None:
    """The frame walk must not blow up when emit() is invoked from a shallow
    stack (the old hardcoded sys._getframe(6) could raise ValueError)."""
    log_dir = tmp_path / "shallow"
    configure_logging(log_dir=log_dir, script_name="shallow")

    from loggair.intercept import InterceptHandler

    record = logging.LogRecord(
        name="direct", level=logging.INFO, pathname="x.py", lineno=1, msg="direct emit", args=None, exc_info=None
    )
    InterceptHandler().emit(record)  # must not raise

    shutdown_logging()
    assert "direct emit" in (log_dir / "shallow.log").read_text()


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def test_coexist_mode_keeps_existing_root_handlers(tmp_path: Path) -> None:
    """intercept='coexist' appends Loggair's handler; a framework's pre-existing
    root handler stays installed and keeps receiving records."""
    root = logging.getLogger()
    pre = _ListHandler()
    root.addHandler(pre)
    try:
        configure_logging(log_dir=tmp_path / "logs", script_name="coex", intercept="coexist")
        logging.getLogger("somelib").info("coexist line")
        shutdown_logging()

        assert pre in root.handlers
        assert "coexist line" in pre.messages  # framework handler still fed
        assert "coexist line" in (tmp_path / "logs" / "coex.log").read_text()  # and Loggair too
    finally:
        root.removeHandler(pre)


def test_full_mode_respects_intercept_exclude(tmp_path: Path) -> None:
    """Excluded logger prefixes keep their handlers, propagate, and level even
    in full mode; non-excluded loggers are stripped as usual."""
    uv = logging.getLogger("uvicorn.error")
    uvh = _ListHandler()
    uv.addHandler(uvh)
    uv.propagate = False
    stripped = logging.getLogger("stripme")
    sh = _ListHandler()
    stripped.addHandler(sh)
    try:
        configure_logging(log_dir=tmp_path / "logs", script_name="excl", intercept_exclude=["uvicorn"])

        assert uvh in uv.handlers
        assert uv.propagate is False
        assert sh not in stripped.handlers
    finally:
        uv.handlers = []
        uv.propagate = True


def test_intercept_exclude_skips_third_party_defaults(tmp_path: Path) -> None:
    configure_logging(log_dir=tmp_path / "logs", script_name="exclhttpx", intercept_exclude=["httpx"])
    assert logging.getLogger("httpx").level == logging.NOTSET  # left alone
    assert logging.getLogger("urllib3").level == logging.WARNING  # default still applied


def test_intercept_off_leaves_stdlib_and_warnings_alone(tmp_path: Path) -> None:
    original_showwarning = warnings.showwarning
    configure_logging(log_dir=tmp_path / "logs", script_name="offmode", intercept="off")
    logging.getLogger("nolib").warning("must not reach loggair")
    shutdown_logging()

    assert warnings.showwarning is original_showwarning
    assert "must not reach loggair" not in (tmp_path / "logs" / "offmode.log").read_text()


def test_capture_warnings_false_leaves_showwarning(tmp_path: Path) -> None:
    original_showwarning = warnings.showwarning
    configure_logging(log_dir=tmp_path / "logs", script_name="nowarn", capture_warnings=False)

    assert warnings.showwarning is original_showwarning


def test_invalid_intercept_mode_fails_fast(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="intercept"):
        configure_logging(log_dir=tmp_path / "logs", script_name="badmode", intercept="everything")


def test_intercept_warnings(tmp_path: Path) -> None:
    log_dir = tmp_path / "warning_logs"
    configure_logging(log_dir=log_dir, script_name="warnings")

    # Trigger a python warning
    warnings.warn("Custom warning message", UserWarning)

    shutdown_logging()

    log_file = log_dir / "warnings.log"
    assert log_file.exists()
    content = log_file.read_text()
    assert "Custom warning message" in content
    assert "UserWarning" in content

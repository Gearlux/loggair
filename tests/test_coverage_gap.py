import shutil
from pathlib import Path
from typing import Any

from loguru import logger

import loggair.core
import loggair.discovery
from loggair.core import configure_logging, shutdown_logging


def test_rotate_non_zero_rank(tmp_path: Path, monkeypatch: Any) -> None:
    """Line 52: if discovery.get_rank() not in (None, 0): return"""
    log_file = tmp_path / "test.log"
    log_file.write_text("content")

    monkeypatch.setattr(loggair.discovery, "get_rank", lambda: 1)

    from loggair.core import _rotate

    _rotate(log_file)

    assert log_file.exists()
    assert len(list(tmp_path.glob("test.*.log"))) == 0


def test_rotate_cleanup_error(tmp_path: Path, monkeypatch: Any) -> None:
    """Lines 57-62: Exception handling in _rotate cleanup loop"""
    log_file = tmp_path / "test.log"
    log_file.write_text("content")

    # Create an old file that we will try to unlink, but make it fail
    old_file = tmp_path / "test.2023-01-01_00-00-00.log"
    old_file.write_text("old")

    monkeypatch.setattr(loggair.discovery, "get_rank", lambda: 0)

    # Mock unlink to raise exception
    original_unlink = Path.unlink

    def mock_unlink(self: Any) -> None:
        if self.name == old_file.name:
            raise PermissionError("Mocked error")
        original_unlink(self)

    monkeypatch.setattr(Path, "unlink", mock_unlink)

    from loggair.core import _rotate

    # Trigger rotation with retention=0 to force cleanup of old_file
    _rotate(log_file, retention=0)

    assert old_file.exists()  # Should still exist due to caught exception


def test_configure_already_configured_no_force() -> None:
    """Early return when already configured and force=False."""
    loggair.core.LoggingState.configured = True
    # If it returns early, it shouldn't raise errors even if args are invalid
    configure_logging(log_dir="/non/existent/path", force=False)
    assert loggair.core.LoggingState.configured is True


def test_pivot_copy_error(tmp_path: Path, monkeypatch: Any) -> None:
    """Lines 118-119: shutil.copy2 exception handling in pivot"""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    # Initial config
    configure_logging(log_dir=log_dir, script_name="first", force=True)
    first_log = log_dir / "first.log"
    first_log.write_text("data")

    # Mock shutil.copy2 to fail
    def mock_copy2(src: Any, dst: Any) -> None:
        raise IOError("Mocked copy failure")

    monkeypatch.setattr(shutil, "copy2", mock_copy2)

    # Pivot to "second"
    configure_logging(log_dir=log_dir, script_name="second", force=True)

    # Should have caught the error and continued
    assert first_log.exists()


def test_pivot_unlink_error(tmp_path: Path, monkeypatch: Any) -> None:
    """Line 120 (approx, was 120 in previous version): log_file_val.unlink() error handling"""
    # Note: in current file it is around 120.
    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    configure_logging(log_dir=log_dir, script_name="first", force=True)
    first_log = log_dir / "first.log"
    first_log.touch()

    # Mock unlink to fail
    def mock_unlink(self: Any) -> Any:
        if self.name == "first.log":
            raise PermissionError("Mocked unlink failure")
        return Path.unlink

    monkeypatch.setattr(Path, "unlink", mock_unlink)

    # Pivot
    configure_logging(log_dir=log_dir, script_name="second", force=True)
    # Should not crash
    assert first_log.exists()


def test_global_retention_unlink_error(tmp_path: Path, monkeypatch: Any) -> None:
    """f.unlink() error in the startup archive sweep must warn, not crash."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    # Timestamped archives of another stem (the sweep's only candidates)
    old1 = log_dir / "other.2023-01-01_00-00-00.log"
    old2 = log_dir / "other.2023-01-02_00-00-00.log"
    old1.write_text("old")
    old2.write_text("old")

    # Mock unlink to fail for the older archive
    original_unlink = Path.unlink

    def mock_unlink(self: Any) -> None:
        if self.name == old1.name:
            raise OSError("Fail")
        original_unlink(self)

    monkeypatch.setattr(Path, "unlink", mock_unlink)

    configure_logging(log_dir=log_dir, script_name="current", retention=1, force=True)
    # Should not crash; the failing archive survives, the purge continued
    assert old1.exists()


def test_shutdown_logging_exception(monkeypatch: Any) -> None:
    """Lines 196-197: logger.complete() exception handling"""

    def mock_complete() -> None:
        raise Exception("Complete failed")

    monkeypatch.setattr(logger, "complete", mock_complete)

    shutdown_logging()  # Should not raise


def test_pivot_copy_generic_exception(tmp_path: Path, monkeypatch: Any) -> None:
    """Lines 118-119: shutil.copy2 generic Exception handling in pivot"""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    configure_logging(log_dir=log_dir, script_name="first", force=True)
    first_log = log_dir / "first.log"
    first_log.write_text("data")

    # Mock shutil.copy2 to fail with generic Exception
    def mock_copy2(src: Any, dst: Any) -> None:
        raise Exception("Generic failure")

    monkeypatch.setattr(shutil, "copy2", mock_copy2)

    # Pivot
    configure_logging(log_dir=log_dir, script_name="second", force=True)
    assert first_log.exists()


def test_rotate_rename_generic_exception(tmp_path: Path, monkeypatch: Any) -> None:
    """Lines 61-62: catch block for path.rename in _rotate"""
    log_file = tmp_path / "test.log"
    log_file.write_text("content")

    def mock_rename(self: Any, target: Any) -> None:
        raise Exception("Rename fail")

    monkeypatch.setattr(Path, "rename", mock_rename)

    from loggair.core import _rotate

    _rotate(log_file)
    assert log_file.exists()


def test_pivot_complete_exception(tmp_path: Path, monkeypatch: Any) -> None:
    """Lines 118-119: logger.complete() exception handling during pivot"""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    # First config
    configure_logging(log_dir=log_dir, script_name="first", force=True)

    # Mock logger.complete to fail
    def mock_complete() -> None:
        raise Exception("Pivot complete failed")

    monkeypatch.setattr(logger, "complete", mock_complete)

    # Pivot to second - should trigger the catch block
    configure_logging(log_dir=log_dir, script_name="second", force=True)
    # Should not crash and proceed to configure "second"
    assert (log_dir / "second.log").exists()


def test_get_logger_no_name() -> None:
    """Line 202: return logger if not name"""
    from loggair.core import get_logger

    assert get_logger() is logger

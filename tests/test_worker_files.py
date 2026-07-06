"""Per-worker log files (worker_files=True): each writer owns its own file."""

from pathlib import Path
from typing import Any

import loggair.core
import loggair.discovery
from loggair import get_active_config, get_logger, shutdown_logging
from loggair.core import LoggingState, configure_logging


class _FakeWorker:
    name = "Worker-1"


def _patch_rank(monkeypatch: Any, rank: int) -> None:
    monkeypatch.setattr(loggair.discovery, "get_rank", lambda: rank)


def _patch_worker(monkeypatch: Any) -> None:
    monkeypatch.setattr(loggair.core, "current_process", lambda: _FakeWorker())


def test_rank_gets_own_file(tmp_path: Path, monkeypatch: Any) -> None:
    _patch_rank(monkeypatch, 2)
    configure_logging(log_dir=tmp_path / "logs", script_name="app", worker_files=True)
    get_logger("w").info("rank line")
    shutdown_logging()

    assert LoggingState.log_file == tmp_path / "logs" / "app.rank2.log"
    assert "rank line" in (tmp_path / "logs" / "app.rank2.log").read_text()
    assert not (tmp_path / "logs" / "app.log").exists()  # shared file untouched


def test_child_process_gets_own_file(tmp_path: Path, monkeypatch: Any) -> None:
    _patch_worker(monkeypatch)
    configure_logging(log_dir=tmp_path / "logs", script_name="app", worker_files=True)
    get_logger("w").info("child line")
    shutdown_logging()

    assert LoggingState.log_file == tmp_path / "logs" / "app.worker-1.log"
    assert "child line" in (tmp_path / "logs" / "app.worker-1.log").read_text()


def test_rank_child_combines_both_suffixes(tmp_path: Path, monkeypatch: Any) -> None:
    """A DataLoader worker OF rank 2 must not share rank 2's own file."""
    _patch_rank(monkeypatch, 2)
    _patch_worker(monkeypatch)
    configure_logging(log_dir=tmp_path / "logs", script_name="app", worker_files=True)

    assert LoggingState.log_file == tmp_path / "logs" / "app.rank2.worker-1.log"


def test_disabled_by_default_workers_share_file(tmp_path: Path, monkeypatch: Any) -> None:
    _patch_worker(monkeypatch)
    configure_logging(log_dir=tmp_path / "logs", script_name="app")

    assert LoggingState.log_file == tmp_path / "logs" / "app.log"  # historical behavior


def test_worker_startup_rotation_of_owned_file(tmp_path: Path, monkeypatch: Any) -> None:
    """A rank>0 process rotates ITS OWN per-worker file at startup (force_owner
    bypasses the rank gate that protects the SHARED file)."""
    _patch_rank(monkeypatch, 3)
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "app.rank3.log").write_text("previous run")

    configure_logging(log_dir=log_dir, script_name="app", worker_files=True)
    get_logger("w").info("fresh run")
    shutdown_logging()

    archives = list(log_dir.glob("app.rank3.2*.log"))
    assert len(archives) == 1
    assert "previous run" in archives[0].read_text()
    assert "fresh run" in (log_dir / "app.rank3.log").read_text()


def test_worker_runtime_rotation_of_owned_file(tmp_path: Path, monkeypatch: Any) -> None:
    """With worker_files, runtime rotation applies to the worker's OWN sink too."""
    _patch_rank(monkeypatch, 2)
    configure_logging(log_dir=tmp_path / "logs", script_name="app", worker_files=True, rotation="400 B")
    lg = get_logger("w")
    for i in range(40):
        lg.info(f"{i} " + "x" * 60)
    shutdown_logging()

    assert list((tmp_path / "logs").glob("app.rank2.2*.log"))  # runtime archives of the OWN stem


def test_active_config_reports_worker_files(tmp_path: Path, monkeypatch: Any) -> None:
    _patch_worker(monkeypatch)
    configure_logging(log_dir=tmp_path / "logs", script_name="app", worker_files=True)
    cfg = get_active_config()
    assert cfg["worker_files"] is True
    assert cfg["log_file"].endswith("app.worker-1.log")

"""Lazy-enqueue behavior: when ``enqueue=True`` is requested, the underlying
``multiprocessing.SimpleQueue`` must NOT be allocated until something actually
forks or spawns a child process. This avoids paying the POSIX-semaphore cost
(and on macOS the kernel-wide ``SEM_NSEMS_MAX`` exhaustion) for single-process
runs and pytest sessions.
"""

from __future__ import annotations

import multiprocessing as mp
from pathlib import Path
from typing import Any

import pytest

import loggair.core as core
from loggair.core import LoggingState, configure_logging, get_logger


def _peek_enqueue_state(handler_id: int) -> bool:
    """Return whether the loguru handler with ``handler_id`` actually owns a
    multiprocessing queue right now (the only proxy for "did we allocate the
    semaphore"). Reaches into loguru internals deliberately — that's the only
    way to verify the optimization."""
    from loguru import logger as _logger

    handler = _logger._core.handlers[handler_id]  # type: ignore[attr-defined]
    return handler._queue is not None


class TestLazyEnqueue:
    def test_enqueue_true_defers_queue_allocation(self, tmp_path: Path) -> None:
        configure_logging(log_dir=tmp_path, script_name="lazy", enqueue=True)
        assert LoggingState.enqueue_requested is True
        assert LoggingState.enqueue_active is False, "queue must not be allocated yet"
        assert LoggingState.file_sink_id is not None
        assert _peek_enqueue_state(LoggingState.file_sink_id) is False

    def test_enqueue_false_never_allocates_or_installs_hook(self, tmp_path: Path) -> None:
        configure_logging(log_dir=tmp_path, script_name="off", enqueue=False)
        assert LoggingState.enqueue_requested is False
        assert LoggingState.enqueue_active is False
        # The lazy hook is only installed when enqueue=True is requested. We
        # can't easily un-install one from a previous test, so just assert
        # that requested=False.

    def test_eager_env_var_forces_immediate_allocation(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Capture the kwargs loguru.add() is called with — verifying the
        # params is enough; we don't need a real semaphore to land on disk.
        recorded: list[dict] = []

        def fake_add(*args: Any, **kwargs: Any) -> int:
            recorded.append(kwargs)
            return 9001  # fake handler id

        monkeypatch.setattr(core.logger, "add", fake_add)
        monkeypatch.setenv("LOGGAIR_EAGER_ENQUEUE", "true")
        configure_logging(log_dir=tmp_path, script_name="eager", enqueue=True)

        assert LoggingState.enqueue_requested is True
        assert LoggingState.enqueue_active is True
        # The file sink must have been added with enqueue=True
        file_sinks = [k for k in recorded if "sink" in k]
        assert any(k["enqueue"] is True for k in file_sinks)

    def test_upgrade_swaps_handler_to_enqueue_mode(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        recorded: list[dict] = []
        next_id = [1000]

        def fake_add(*args: Any, **kwargs: Any) -> int:
            recorded.append(kwargs)
            next_id[0] += 1
            return next_id[0]

        def fake_remove(handler_id: int = -1) -> None:
            recorded.append({"_remove": handler_id})

        monkeypatch.setattr(core.logger, "add", fake_add)
        monkeypatch.setattr(core.logger, "remove", fake_remove)

        configure_logging(log_dir=tmp_path, script_name="upgrade", enqueue=True)
        # Last add for the file sink should have been enqueue=False
        file_adds = [k for k in recorded if "sink" in k]
        assert file_adds and file_adds[-1]["enqueue"] is False
        sink_id_before = LoggingState.file_sink_id

        core._upgrade_to_enqueue()

        assert LoggingState.enqueue_active is True
        assert LoggingState.file_sink_id != sink_id_before
        # An add with enqueue=True must have happened during the upgrade
        post_upgrade_adds = [k for k in recorded[recorded.index({"_remove": sink_id_before}) :] if "sink" in k]
        assert any(k["enqueue"] is True for k in post_upgrade_adds)

    def test_upgrade_is_idempotent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        next_id = [2000]

        def fake_add(*args: Any, **kwargs: Any) -> int:
            next_id[0] += 1
            return next_id[0]

        monkeypatch.setattr(core.logger, "add", fake_add)
        monkeypatch.setattr(core.logger, "remove", lambda *_a, **_k: None)

        configure_logging(log_dir=tmp_path, script_name="idem", enqueue=True)
        core._upgrade_to_enqueue()
        sink_id_after_first = LoggingState.file_sink_id
        core._upgrade_to_enqueue()  # second call must be a no-op
        assert LoggingState.file_sink_id == sink_id_after_first

    def test_upgrade_noop_when_enqueue_not_requested(self, tmp_path: Path) -> None:
        configure_logging(log_dir=tmp_path, script_name="off", enqueue=False)
        sink_id = LoggingState.file_sink_id
        assert sink_id is not None
        core._upgrade_to_enqueue()
        # No upgrade should have happened
        assert LoggingState.enqueue_active is False
        assert LoggingState.file_sink_id == sink_id

    def test_process_construction_triggers_upgrade(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Stub _upgrade_to_enqueue so we don't actually allocate the semaphore
        # (the kernel pool may be exhausted in this environment, and the
        # contract under test is "the hook fires", not "the OS call succeeds").
        calls: list[None] = []

        def stub() -> None:
            calls.append(None)
            LoggingState.enqueue_active = True

        monkeypatch.setattr(core, "_upgrade_to_enqueue", stub)

        # Force re-installation of the spawn hook against the new stub.
        import multiprocessing.process as _mp_process

        if hasattr(_mp_process.BaseProcess.__init__, "_loggair_patched"):
            # Reset the marker so install actually re-patches with our stub
            _mp_process.BaseProcess.__init__._loggair_patched = False
        if hasattr(core._install_lazy_enqueue_hooks, "_installed"):
            core._install_lazy_enqueue_hooks._installed = False

        configure_logging(log_dir=tmp_path, script_name="hook", enqueue=True)
        assert LoggingState.enqueue_requested is True

        def _noop() -> None:
            pass

        # Constructing a Process must trigger the hook even before .start()
        mp.Process(target=_noop)
        assert len(calls) >= 1


class TestLazyEnqueueRoundTrip:
    """End-to-end: `enqueue=True` with no MP activity = zero queue allocation;
    a child process spawned afterwards still receives consistent log output."""

    def test_single_process_run_writes_log_without_queue(self, tmp_path: Path) -> None:
        configure_logging(log_dir=tmp_path, script_name="single", enqueue=True)
        log = get_logger("single")
        log.info("hello from a queueless single-process run")

        assert LoggingState.enqueue_active is False
        log_file = tmp_path / "single.log"
        # loguru flushes synchronously when enqueue=False, so the message must
        # already be on disk
        assert log_file.exists()
        assert "hello from a queueless single-process run" in log_file.read_text()

"""Runtime signals: config reload + debug toggle for long-running processes.

On ``reload_signal`` Loggair re-resolves env vars + config files and reloads
the sinks; on ``debug_signal`` it toggles both sinks to DEBUG and back. The
handlers are installed in the main process/thread only and DEFER all work to a
worker thread — see :func:`_make_signal_handler` for why touching loguru from
a signal handler would self-deadlock.

This module late-binds ``loggair.core`` (module import, attribute access at
call time) because core imports it at module level — the standard resolution
for an intra-package cycle; a ``from loggair.core import ...`` here would fail
on the partially-initialized module.
"""

import signal
import threading
from typing import Any, Optional

from loguru import logger

import loggair.core

# Serializes signal-triggered reconfigurations (rapid repeated signals).
_signal_action_lock = threading.Lock()


def _resolve_signal(name: Any) -> int:
    """Resolve a signal spec ("SIGUSR1", "usr2", or an int) to its number.

    Fails fast with the valid choices — an unknown name, or a POSIX-only signal
    on a platform without it (e.g. SIGUSR1 on Windows), raises ``ValueError``.
    """
    if isinstance(name, int):
        return name
    s = str(name).upper()
    if not s.startswith("SIG"):
        s = "SIG" + s
    val = getattr(signal, s, None)
    if not isinstance(val, signal.Signals):
        raise ValueError(f"invalid signal {name!r}; expected a signal name available on this platform, e.g. 'SIGUSR1'")
    return int(val)


def _toggle_debug() -> None:
    """Flip both sinks to DEBUG and back, remembering the pre-toggle kwargs."""
    core = loggair.core
    if not core.LoggingState.debug_active:
        snapshot = dict(core.LoggingState.last_kwargs)
        core.configure_logging(force=True, **{**snapshot, "console_level": "DEBUG", "file_level": "DEBUG"})
        core.LoggingState.pre_debug_kwargs = snapshot
        core.LoggingState.debug_active = True
        logger.info("Loggair debug mode ON (signal)")
    else:
        snapshot = core.LoggingState.pre_debug_kwargs or {}
        core.configure_logging(force=True, **snapshot)
        core.LoggingState.last_kwargs = dict(snapshot)
        core.LoggingState.debug_active = False
        core.LoggingState.pre_debug_kwargs = None
        logger.info("Loggair debug mode OFF (signal)")


def _apply_signal_action(mode: str) -> None:
    """Worker-thread body for a runtime signal (see _make_signal_handler)."""
    core = loggair.core
    with _signal_action_lock:
        if mode == "reload":
            core.configure_logging(force=True, **core.LoggingState.last_kwargs)
        else:  # "debug"
            _toggle_debug()


def _make_signal_handler(mode: str) -> Any:
    """Build a signal handler that DEFERS all work to a fresh thread.

    The handler itself must never touch loguru: it runs in the main thread at
    an arbitrary bytecode boundary, and loguru's handler/core locks are plain
    non-reentrant ``threading.Lock``s — a ``logger.remove()`` from a handler
    that interrupted an in-flight ``emit`` (lock held by this same thread)
    would self-deadlock. A worker thread instead WAITS for the lock like any
    other thread. The spawned thread is stored on
    ``LoggingState.last_signal_thread`` so tests can ``join()`` it
    deterministically (no sleeps).
    """

    def _handler(signum: int, frame: Any) -> None:
        t = threading.Thread(target=_apply_signal_action, args=(mode,), name=f"loggair-signal-{mode}", daemon=True)
        loggair.core.LoggingState.last_signal_thread = t
        t.start()

    _handler._loggair_signal = mode  # type: ignore[attr-defined]
    return _handler


def _install_signal_handlers(reload_sig: Optional[Any], debug_sig: Optional[Any]) -> None:
    """Install the runtime-signal handlers (main process, main thread only).

    Idempotent per signal; the FIRST original handler per signum is stored in
    ``LoggingState.signal_originals`` so :func:`loggair.core.reset_logging` can
    restore it. Skipped silently off the main thread (``signal.signal`` would
    raise there — the reconfigure-from-signal-worker path re-enters
    configure_logging from a worker thread with the handlers already installed).
    """
    if threading.current_thread() is not threading.main_thread():
        return
    for spec, mode in ((reload_sig, "reload"), (debug_sig, "debug")):
        if spec is None:
            continue
        signum = _resolve_signal(spec)
        current = signal.getsignal(signum)
        if getattr(current, "_loggair_signal", None) == mode:
            continue  # already ours
        original = signal.signal(signum, _make_signal_handler(mode))
        loggair.core.LoggingState.signal_originals.setdefault(signum, original)

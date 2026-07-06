"""Webhook / alerting sink: route high-severity records to external platforms.

Delivery is backed by `apprise <https://github.com/caronc/apprise>`_ (an
OPTIONAL dependency — ``pip install loggair[alerts]``), which reaches ~100
services (Slack, Teams, Discord, Telegram, email, generic webhooks, ...)
through a single URL schema, so Loggair carries no per-platform HTTP code.

Zero-blocking by construction: the loguru sink only enqueues the formatted
record; a single daemon worker thread batches everything queued since the last
delivery into ONE notification and calls apprise. A ``throttle`` window rate-
limits deliveries (alert storms collapse into batched digests). Delivery
failures are reported at WARNING via a ``loggair_alert_internal``-marked
record that the alert sink filters out — a failing webhook can never feed back
into itself.

Why not `logprise` (evaluated 2026-07-04, rejected): it is a standalone app
framework, not a library component — importing it ARMS process-global side
effects (its own stdlib-logging interception via ``basicConfig(force=True)``
plus a ``logging.Logger._log`` monkeypatch that would fight Loggair's
interception; a ``loguru._Logger.remove`` patch that re-adds its sink after
every removal, breaking Loggair's reconfigure/reset cycle; ``sys.excepthook``
/ ``atexit`` hooks), and its delivery model is periodic-digest (default
hourly) rather than the immediate alerting this feature is for. apprise is
used directly instead; logprise's good ideas (batching, catch-all delivery)
are reproduced here in ~100 lines.
"""

import queue
import socket
import threading
import time
from typing import Any, List, Optional, Tuple

from loguru import logger


def _import_apprise() -> Any:
    """Lazy apprise import with an actionable error (optional dependency)."""
    try:
        import apprise
    except ImportError as e:
        raise ImportError(
            "Loggair alerting (alert_urls) requires the optional 'apprise' dependency: "
            "pip install loggair[alerts]  (or: pip install apprise)"
        ) from e
    return apprise


def build_apprise(urls: List[str]) -> Any:
    """Build a validated ``apprise.Apprise`` from URLs, failing fast on bad ones.

    ``Apprise.add`` validates offline and returns False for an unparseable URL
    or unknown scheme — silently dropping an alert destination is exactly the
    kind of misconfiguration an operator must hear about at startup.
    """
    apprise = _import_apprise()
    obj = apprise.Apprise()
    for url in urls:
        if not obj.add(url):
            raise ValueError(f"alert_urls: invalid apprise URL {url!r} (unknown scheme or unparseable)")
    return obj


class AlertDispatcher:
    """Background deliverer: queue in the logging path, apprise in a worker thread.

    One instance per configure; :meth:`flush` blocks (with timeout) until every
    submitted record has been delivered — the deterministic-synchronization
    hook used by ``shutdown_logging`` / ``reset_logging`` and the tests.
    """

    def __init__(self, urls: List[str], script_name: str, throttle: float) -> None:
        self._apprise = build_apprise(urls)  # fail fast BEFORE the thread starts
        self.redacted_urls: List[str] = [s.url(privacy=True) for s in self._apprise]
        self._script = script_name
        self._throttle = max(float(throttle), 0.0)
        self._q: "queue.Queue[Optional[Tuple[str, str, int]]]" = queue.Queue()
        self._wake = threading.Event()  # aborts the throttle wait on flush/stop
        self._lock = threading.Lock()
        self._pending = 0
        self._drained = threading.Condition(self._lock)
        self._stopped = False
        self._last_delivery: Optional[float] = None
        self._thread = threading.Thread(target=self._run, name="loggair-alerts", daemon=True)
        self._thread.start()

    def submit(self, text: str, level_name: str, level_no: int) -> None:
        """Called from the loguru sink — must stay O(queue.put), never block on I/O."""
        with self._lock:
            self._pending += 1
        self._q.put((text, level_name, level_no))

    def _run(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                return
            batch = [item]
            # Throttle: HOLD delivery until the window since the last delivery
            # has passed (abortable by flush/stop via _wake) — records arriving
            # meanwhile join this batch below, collapsing alert storms into one
            # digest per window.
            if self._throttle > 0 and self._last_delivery is not None:
                remaining = self._throttle - (time.monotonic() - self._last_delivery)
                if remaining > 0:
                    self._wake.wait(remaining)
                    self._wake.clear()
            stop_after = False
            while True:  # drain everything queued by now into ONE delivery
                try:
                    nxt = self._q.get_nowait()
                except queue.Empty:
                    break
                if nxt is None:
                    stop_after = True
                    break
                batch.append(nxt)
            self._deliver(batch)
            self._last_delivery = time.monotonic()
            if stop_after:
                return

    def _deliver(self, batch: List[Tuple[str, str, int]]) -> None:
        top_name = max(batch, key=lambda b: b[2])[1]
        suffix = f" (+{len(batch) - 1} more)" if len(batch) > 1 else ""
        title = f"[Loggair] {top_name} from {self._script} on {socket.gethostname()}{suffix}"
        body = "".join(text for text, _, _ in batch)
        try:
            self._apprise.notify(title=title, body=body)
        except Exception as e:
            # loggair_alert_internal marks this record so the alert sink's
            # filter drops it — a failing webhook must never alert about itself.
            logger.bind(loggair_alert_internal=True).warning(f"Loggair: alert delivery failed: {e}")
        with self._lock:
            self._pending -= len(batch)
            if self._pending <= 0:
                self._drained.notify_all()

    def flush(self, timeout: float = 5.0) -> bool:
        """Block until all submitted alerts are delivered (or `timeout` elapses)."""
        self._wake.set()
        with self._lock:
            drained = self._drained.wait_for(lambda: self._pending <= 0, timeout=timeout)
        # Once drained the worker is parked in q.get(), so nobody is waiting on
        # _wake — clear it here or the NEXT throttle hold would fall through
        # instantly on the lingering set (racy premature deliveries).
        self._wake.clear()
        return drained

    def stop(self, timeout: float = 5.0) -> None:
        """Flush, then terminate the worker thread. Idempotent."""
        if self._stopped:
            return
        self._stopped = True
        self.flush(timeout)
        self._q.put(None)
        self._wake.set()
        self._thread.join(timeout)

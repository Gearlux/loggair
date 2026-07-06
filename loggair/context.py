"""Process-wide experiment context injected into every log record.

Training loops (and other long-running jobs) stamp their current position —
epoch, step, run id, anything scalar — once, and EVERY record logged anywhere
in the process afterwards carries it: Loggair's own loggers, third-party
libraries routed through stdlib interception, all of it. Two renderings:

- ``record["extra"]["context_tag"]`` — a prebuilt ``"epoch=3 step=1200 | "``
  string (empty when no context is set) that the DEFAULT sink formats include,
  so context is visible out of the box with zero per-record formatting cost.
- The individual fields land in ``record["extra"]`` (``extra.epoch`` etc.), so
  JSON mode (``serialize=True``) exposes them as structured fields for log
  aggregators. An explicit ``logger.bind(epoch=...)`` on a record wins over
  the ambient context (``setdefault`` semantics).

Design notes: the context is deliberately PROCESS-GLOBAL (a checkpoint-saver
thread's logs should carry the epoch the training thread set — unlike loguru's
contextvars-based ``contextualize``, which is invisible to sibling threads),
inherited by fork-started workers, and empty in spawn-started ones. Updates
are copy-on-write behind a lock; readers (the per-record sink filters) take a
plain reference read of an immutable ``(dict, tag)`` pair — no lock, no
per-record cost beyond a dict access.
"""

from contextlib import contextmanager
from threading import Lock
from typing import Any, Dict, Iterator, Tuple

_lock = Lock()
# Swapped ATOMICALLY as a pair on every write; never mutated in place, so
# lock-free readers always see a consistent (context, prebuilt tag) snapshot.
_context: Dict[str, Any] = {}
_context_tag: str = ""


def _publish(new: Dict[str, Any]) -> None:
    """Swap in a new context dict + its prebuilt tag. Caller holds ``_lock``."""
    global _context, _context_tag
    _context = new
    _context_tag = (" ".join(f"{k}={v}" for k, v in new.items()) + " | ") if new else ""


def set_context(**fields: Any) -> None:
    """Merge `fields` into the process-wide experiment context.

    Example — a training loop::

        loggair.set_context(epoch=3)
        ...
        loggair.set_context(step=global_step)   # epoch stays
    """
    with _lock:
        _publish({**_context, **fields})


def clear_context(*names: str) -> None:
    """Remove the named fields from the context, or ALL fields when called bare."""
    with _lock:
        if names:
            _publish({k: v for k, v in _context.items() if k not in names})
        else:
            _publish({})


def get_context() -> Dict[str, Any]:
    """Return a copy of the current experiment context."""
    return dict(_context)


def context_snapshot() -> Tuple[Dict[str, Any], str]:
    """Internal lock-free read for the sink filters: (context, prebuilt tag).

    The returned dict is the live immutable snapshot — callers must NOT mutate
    it (writers replace it wholesale, never edit it).
    """
    return _context, _context_tag


@contextmanager
def context(**fields: Any) -> Iterator[None]:
    """Scoped context: merge `fields` on entry, restore the ENTRY snapshot on exit.

    Note the exit semantics: the snapshot taken at entry is restored wholesale,
    so ``set_context`` calls made INSIDE the block do not survive it. Use the
    imperative API for state that must outlive the block.

    Example::

        with loggair.context(phase="validation"):
            run_validation()   # every record carries phase=validation
    """
    with _lock:
        saved = _context
        _publish({**_context, **fields})
    try:
        yield
    finally:
        with _lock:
            _publish(saved)

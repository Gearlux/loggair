import inspect
import logging
import warnings
from typing import Any, Callable, Iterable, Literal, Optional, Union, get_args

from loguru import logger

# How aggressively Loggair takes over stdlib logging (closed Literal — the
# runtime validation set in core derives from get_args, one source of truth):
# - "full"    (default): basicConfig(force=True) replaces the root handlers and
#   existing named loggers are stripped to propagate — Loggair owns all logs.
# - "coexist": Loggair APPENDS its handler to the root without touching other
#   root handlers or any named logger — for embedding next to a framework that
#   owns logging (uvicorn, gunicorn). Trade-off: the root level is lowered to
#   DEBUG so records reach Loggair, which may feed pre-existing root handlers
#   more records than before (their own levels still apply); and a named
#   logger with propagate=False stays outside Loggair entirely.
# - "off":     no interception at all (no root rewiring, no warnings redirect,
#   no third-party level defaults).
InterceptMode = Literal["full", "coexist", "off"]
_VALID_INTERCEPT_MODES = frozenset(get_args(InterceptMode))

# Default stdlib levels for chronically chatty third-party loggers. Applied at
# the STDLIB layer (records below the level never reach loguru — zero per-record
# filter cost), but each default yields to the user: a `module_levels` rule
# whose prefix touches the name lifts the stdlib gate to NOTSET so the records
# flow through interception and the per-sink Loggair filters decide instead.
_DEFAULT_THIRD_PARTY_LEVELS = {
    "asyncio": logging.WARNING,
    "httpx": logging.WARNING,
    "urllib3": logging.WARNING,
    "datasets": logging.WARNING,
    "filelock": logging.WARNING,
}

# Original warnings.showwarning, saved by the FIRST setup_interception so
# teardown_interception can restore it. None = never saved / already restored.
_original_showwarning: Optional[Callable[..., Any]] = None


class InterceptHandler(logging.Handler):
    """
    Intercept standard logging messages and redirect them to Loguru.
    """

    def emit(self, record: logging.LogRecord) -> None:
        # Get corresponding Loguru level if it exists
        level: Union[str, int]
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # Find the caller frame the logging call originated from: walk up from
        # here until we leave the stdlib `logging` module (loguru's current
        # recommended recipe). Unlike a hardcoded `sys._getframe(6)`, this never
        # raises on shallow stacks (e.g. `handler.emit(record)` called directly)
        # and stays correct when the stdlib call path changes depth.
        frame, depth = inspect.currentframe(), 0
        while frame and (depth == 0 or frame.f_code.co_filename == logging.__file__):
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def redirect_warnings(
    message: Union[Warning, str],
    category: type[Warning],
    filename: str,
    lineno: int,
    file: Optional[Any] = None,
    line: Optional[Any] = None,
) -> None:
    """
    Redirect Python warnings to loguru.
    """
    logger.opt(depth=2).warning(f"{category.__name__}: {message} ({filename}:{lineno})")


def _rule_touches(prefix: str, name: str) -> bool:
    """True when a `module_levels` rule prefix targets logger `name` or one of
    its descendants (dotted-segment semantics — 'httpx' does not match 'httpxx')."""
    return prefix == name or prefix.startswith(name + ".") or name.startswith(prefix + ".")


def _is_excluded(name: str, exclude: Iterable[str]) -> bool:
    """True when `name` is (or descends from) an `intercept_exclude` prefix."""
    return any(name == p or name.startswith(p + ".") for p in exclude)


def setup_interception(
    module_level_prefixes: Iterable[str] = (),
    mode: str = "full",
    exclude: Iterable[str] = (),
    capture_warnings: bool = True,
) -> None:
    """
    Configure standard logging (and optionally warnings) to use Loguru.

    `mode` selects how invasive the takeover is — see :data:`InterceptMode`.
    `exclude` lists dotted logger-name prefixes Loggair must leave completely
    alone (handlers, propagate, level — e.g. ``["uvicorn"]`` protects
    ``uvicorn.error`` too); it applies to the "full"-mode stripping AND to the
    third-party level defaults. `capture_warnings=False` leaves
    ``warnings.showwarning`` untouched (restoring the original if a previous
    configure had redirected it).

    `module_level_prefixes` are the user's `module_levels` rule prefixes: a
    third-party default in :data:`_DEFAULT_THIRD_PARTY_LEVELS` is skipped
    (stdlib level NOTSET, records flow to the Loggair sink filters) when a rule
    targets that logger — otherwise the user's override could never fire because
    the stdlib gate drops the records before loguru sees them.
    """
    global _original_showwarning

    if mode == "off":
        return
    exclude_t = tuple(exclude)

    # 1. Route standard logging into loguru via the root logger
    if mode == "full":
        logging.basicConfig(handlers=[InterceptHandler()], level=logging.DEBUG, force=True)
        # Reconfigure existing loggers to ensure they propagate to root —
        # except excluded ones, which keep their handlers/propagate verbatim.
        for name in logging.root.manager.loggerDict:
            if _is_excluded(name, exclude_t):
                continue
            lgr = logging.getLogger(name)
            lgr.handlers = []
            lgr.propagate = True
    else:  # "coexist": append-only, other root handlers and named loggers untouched
        root = logging.getLogger()
        if not any(isinstance(h, InterceptHandler) for h in root.handlers):
            root.addHandler(InterceptHandler())
        if root.getEffectiveLevel() > logging.DEBUG:
            # Records below the root level never reach ANY handler, ours
            # included; lowering it is the one shared knob coexistence needs
            # (pre-existing handlers keep their own per-handler levels).
            root.setLevel(logging.DEBUG)

    # 2. Redirect warnings (saving the original exactly once, for teardown)
    if capture_warnings:
        if warnings.showwarning is not redirect_warnings and _original_showwarning is None:
            _original_showwarning = warnings.showwarning
        warnings.showwarning = redirect_warnings
    elif warnings.showwarning is redirect_warnings and _original_showwarning is not None:
        # capture turned OFF on a reconfigure: hand warnings back.
        warnings.showwarning = _original_showwarning
        _original_showwarning = None

    # 3. Third-party defaults, each yielding to a user `module_levels` rule
    #    and skipping excluded loggers entirely.
    prefixes = tuple(module_level_prefixes)
    for name, level in _DEFAULT_THIRD_PARTY_LEVELS.items():
        if _is_excluded(name, exclude_t):
            continue
        if any(_rule_touches(p, name) for p in prefixes):
            logging.getLogger(name).setLevel(logging.NOTSET)
        else:
            logging.getLogger(name).setLevel(level)


def teardown_interception() -> None:
    """Undo :func:`setup_interception` — restore ``warnings.showwarning``, drop
    the root ``InterceptHandler``, and clear the third-party level defaults.

    Root handlers that existed BEFORE the first setup were replaced by
    ``basicConfig(force=True)`` and cannot be restored; stdlib logging falls
    back to its ``lastResort`` handler. Idempotent.
    """
    global _original_showwarning
    if warnings.showwarning is redirect_warnings and _original_showwarning is not None:
        warnings.showwarning = _original_showwarning
    _original_showwarning = None

    root = logging.getLogger()
    root.handlers = [h for h in root.handlers if not isinstance(h, InterceptHandler)]

    for name in _DEFAULT_THIRD_PARTY_LEVELS:
        logging.getLogger(name).setLevel(logging.NOTSET)

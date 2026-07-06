"""A no-op logger used when Loggair logging is disabled at runtime.

:func:`loggair.get_logger` returns a :class:`NullLogger` instead of the real
loguru logger when logging is switched off via an environment variable:

- ``LOGGAIR_DISABLE_LOGGING`` — disable in EVERY process (perf-critical runs, or
  test suites that want no log files / sink / queue overhead).
- ``LOGGAIR_DISABLE_MULTIPROCESS_LOGGING`` — disable ONLY in worker/child
  processes, so the main process keeps logging while e.g. PyTorch DataLoader
  workers (which would each re-emit the same lines) stay silent.

The class mirrors the slice of loguru's ``Logger`` API that application code
touches: every log method is a no-op, while ``bind`` / ``opt`` / ``patch``
return a logger so chained calls keep flowing, ``level`` returns an object
exposing the numeric ``.no`` some call-sites read (``logger.level("DEBUG").no``),
and ``contextualize`` / ``catch`` return working context-manager / decorator
stand-ins. Any *other* attribute resolves to a no-op callable via
``__getattr__``, so an unforeseen loguru call can never raise merely because
logging happens to be disabled.
"""

from __future__ import annotations

from inspect import isclass
from types import TracebackType
from typing import Any, Callable, Optional, Type


class _NullLevel:
    """Stand-in for the object returned by ``loguru.Logger.level(name)``.

    Only ``.no`` (and occasionally ``.name``) is read by callers that gate on a
    level threshold, e.g. ``logger.level("DEBUG").no``.
    """

    __slots__ = ("name", "no", "color", "icon")

    def __init__(self, name: str = "NOTSET", no: int = 0) -> None:
        self.name = name
        self.no = no
        self.color = ""
        self.icon = ""


class _NullContext:
    """No-op context manager returned by :meth:`NullLogger.contextualize`."""

    def __init__(self, logger: NullLogger) -> None:
        self._logger = logger

    def __enter__(self) -> NullLogger:
        return self._logger

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        return None  # returning None never suppresses the exception


class _NullCatcher:
    """Stand-in for ``loguru.Logger.catch`` usable as a decorator OR a context manager.

    Returns the wrapped function unchanged when used as ``@logger.catch(...)``,
    and as a context manager (``with logger.catch():``) does NOT swallow
    exceptions — matching the "disabled logging" intent: we neither log nor hide
    errors.
    """

    def __init__(self, logger: NullLogger) -> None:
        self._logger = logger

    def __call__(self, function: Callable[..., Any]) -> Callable[..., Any]:
        return function

    def __enter__(self) -> None:
        return None

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        return None


class NullLogger:
    """A no-op logger mirroring the loguru ``Logger`` API. See the module docstring."""

    _LEVELS = {
        "TRACE": 5,
        "DEBUG": 10,
        "INFO": 20,
        "SUCCESS": 25,
        "WARNING": 30,
        "ERROR": 40,
        "CRITICAL": 50,
    }

    def __init__(self, name: Optional[str] = None) -> None:
        self._name = name

    # --- binding / chaining: return a logger so chains keep flowing -----------
    def bind(self, **kwargs: Any) -> NullLogger:
        return NullLogger(kwargs.get("name", self._name))

    def opt(self, *args: Any, **kwargs: Any) -> NullLogger:
        return self

    def patch(self, *args: Any, **kwargs: Any) -> NullLogger:
        return self

    # --- level introspection (callers read ``.no``) ---------------------------
    def level(self, name: Any, *args: Any, **kwargs: Any) -> _NullLevel:
        if isinstance(name, str):
            return _NullLevel(name.upper(), self._LEVELS.get(name.upper(), 0))
        return _NullLevel()

    # --- sink management: harmless no-ops -------------------------------------
    def add(self, *args: Any, **kwargs: Any) -> int:
        return 0

    def remove(self, *args: Any, **kwargs: Any) -> None:
        return None

    def complete(self, *args: Any, **kwargs: Any) -> None:
        return None

    # --- contextualize / catch ------------------------------------------------
    def contextualize(self, *args: Any, **kwargs: Any) -> _NullContext:
        return _NullContext(self)

    def catch(self, *args: Any, **kwargs: Any) -> Any:
        # Bare ``@logger.catch`` — the first positional is the decorated function
        # (callable, but NOT an exception class). Mirror loguru's own dispatch so
        # the decorated function is returned unchanged rather than swallowed.
        if args and callable(args[0]) and not (isclass(args[0]) and issubclass(args[0], BaseException)):
            return args[0]
        return _NullCatcher(self)

    # --- log emission: all no-ops ---------------------------------------------
    def log(self, *args: Any, **kwargs: Any) -> None:
        return None

    def trace(self, *args: Any, **kwargs: Any) -> None:
        return None

    def debug(self, *args: Any, **kwargs: Any) -> None:
        return None

    def info(self, *args: Any, **kwargs: Any) -> None:
        return None

    def success(self, *args: Any, **kwargs: Any) -> None:
        return None

    def warning(self, *args: Any, **kwargs: Any) -> None:
        return None

    def error(self, *args: Any, **kwargs: Any) -> None:
        return None

    def critical(self, *args: Any, **kwargs: Any) -> None:
        return None

    def exception(self, *args: Any, **kwargs: Any) -> None:
        return None

    def __repr__(self) -> str:
        return f"NullLogger(name={self._name!r})"

    # --- catch-all: any other loguru method becomes a no-op callable ----------
    def __getattr__(self, name: str) -> Callable[..., None]:
        # Let dunder probes (pickle, copy, ...) fall through to the defaults
        # instead of resolving to a truthy no-op callable.
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)

        def _noop(*args: Any, **kwargs: Any) -> None:
            return None

        return _noop

"""Callers living in a DIFFERENT module than the decorated function.

The trace-location tests need a genuine cross-module call: with caller and callee
in one file, a wrong `{name}` field is indistinguishable from a right one. Named
with a leading underscore so pytest does not collect it.
"""

from typing import Any, Callable

CALL_LINE = 14  # the line of `return fn(*args)` below — asserted by the tests


def call_through(fn: Callable[..., Any], *args: Any) -> Any:
    return fn(*args)

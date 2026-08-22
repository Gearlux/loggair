"""Call tracing decorators: :func:`track` (entry/exit) and :func:`spy` (entry/exit
with parameter values), both emitting at the dedicated ``TRAIL`` level.

``TRAIL`` is registered at import time — ``loggair/__init__.py`` imports this
module for exactly that reason. Registration cannot be deferred to first use: a
``loggair.yaml`` carrying ``file_level: TRAIL`` is resolved by
``core.resolve_settings`` through ``logger.level(name).no``, which raises
``ValueError: Level 'TRAIL' does not exist`` for a level nothing has registered
yet. Configuring Loggair must never depend on whether the user happened to import
a submodule first.

Its number is **7**, between ``TRACE`` (5) and ``DEBUG`` (10) — call tracing is
noisier than tracing, so it sits under it, and the consequence to know is that
running a sink at ``TRACE`` shows call tracing too. (A level BELOW ``TRACE`` used
to be unreachable, because both sinks were added with a hardcoded ``level="TRACE"``
floor; ``core._sink_floor`` now computes each floor from the configured levels, so
sub-``TRACE`` levels do work. The placement above ``TRACE`` is a choice, no longer
a constraint.)

A decorated CLASS traces the methods it defines itself. ``inherited=`` extends that
to base classes, bounded on purpose — see :func:`_bases_to_trace`.

Every trace line locates the DECORATED function and names its caller separately::

    sonair.lightning_classifier:__init__:96 | → LightningClassifier.__init__(...) [from confluid.engine:_construct:1164]

The two used to be fused into one field triple, which produced references that could
not be opened (a line number from one file against a module name from another).

Enabling follows the ordinary configuration surface, with no special casing::

    console_level: TRAIL                        # or LOGGAIR_CONSOLE_LEVEL=TRAIL
    module_levels:
      mypkg.io:
        file: TRAIL                             # trace one module, to the file only

Why the decorators gate themselves (see :func:`core.effective_level_no`): a
switched-off tracer on a hot function must cost nothing, and entering loguru at
all does not qualify. The wrapper compares a cached per-logger threshold before
touching loguru. Measured on a decorated no-op function, same machine, per call::

    undecorated                     0.028 us
    @track  TRAIL disabled          0.084 us
    the same tracer, no guard       0.687 us    (two loguru entries per call)
    @track  TRAIL enabled          26.0 us      (two records, console + file)
    @spy    TRAIL enabled          30.5 us

The guard is what makes the disabled case cheap, and it stays cheap for a second
reason: ``effective_level_no`` is per-LOGGER, while the sink floor loguru checks
is per-SINK and drops to accommodate any promoting ``module_levels`` rule. Switched
ON, tracing is genuinely expensive — that is a debugging tool doing its job, and
the reason `module_levels` targeting matters.
"""

from __future__ import annotations

import functools
import inspect
import os
import sys
import textwrap
import time
from functools import partial
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple, TypeVar, Union, overload

import yaml
from loguru import logger

from loggair.core import LoggingState, effective_level_no, get_logger

#: The dedicated call-tracing level, and its loguru severity number.
TRAIL = "TRAIL"
TRAIL_NO = 7

#: Dunders that ARE wrapped when a class is decorated. Every other underscore
#: name is skipped: `_private` helpers are implementation detail, and wrapping
#: `__repr__` / `__eq__` / `__hash__` would recurse or fire during logging itself.
_WRAPPED_DUNDERS = frozenset({"__init__", "__call__"})

_T = TypeVar("_T")


def _register_trail() -> None:
    """Register ``TRAIL`` with loguru. Idempotent — safe on re-import and re-entry.

    Look-up-then-register, because re-registering an existing level raises
    ``ValueError: Level 'TRAIL' already exists, you can't update its severity no``.
    """
    try:
        logger.level(TRAIL)
    except ValueError:
        logger.level(TRAIL, no=TRAIL_NO, color="<dim>")


_register_trail()


# --------------------------------------------------------------------------- #
# Trace location
# --------------------------------------------------------------------------- #
class _File:
    """Stand-in for loguru's ``record["file"]``, which exposes ``.name`` and ``.path``.

    Duck-typed rather than imported: loguru's ``RecordFile`` is private. Both
    ``{file}`` and ``{file.path}`` render from this — verified — so a custom format
    stays consistent with ``{function}`` and ``{line}``.
    """

    __slots__ = ("name", "path")

    def __init__(self, path: str) -> None:
        self.path = path
        self.name = os.path.basename(path)

    def __str__(self) -> str:
        return self.name

    def __format__(self, spec: str) -> str:
        return format(self.name, spec)


class _Site(NamedTuple):
    """Where the DECORATED function is defined — what the record's location names."""

    module: str
    function: str
    line: int
    file: _File


def _stamp_site(site: _Site, record: Any) -> None:
    """Point the record's whole location at the decorated function.

    All four fields together, because naming a function from one file at a line
    number from another produces a reference that cannot be opened: a real trace
    read ``sonair.lightning_classifier:_construct:1164`` for a 159-line file whose
    ``_construct`` lives in another package entirely. The caller is not lost — the
    wrapper appends it to the MESSAGE, where it is labelled and unambiguous.

    Module-level (bound through ``functools.partial``) so a patched logger stays
    picklable, mirroring ``core._set_record_name``.
    """
    record["name"] = site.module
    record["function"] = site.function
    record["line"] = site.line
    record["file"] = site.file


def _caller() -> str:
    """``module:function:line`` of whoever called the traced function.

    ``sys._getframe`` rather than ``inspect.stack()``: measured 0.097 us against
    128 us, and this runs on every traced call while tracing is switched on.
    """
    frame = sys._getframe(2)  # _caller -> wrapper -> the caller
    module = frame.f_globals.get("__name__", "<unknown>")
    return f"{module}:{frame.f_code.co_name}:{frame.f_lineno}"


# --------------------------------------------------------------------------- #
# Value rendering
# --------------------------------------------------------------------------- #
def _confluid_document(value: Any, multiline: bool) -> Optional[str]:
    """`value` as its Confluid document, or None when Confluid cannot render it.

    Confluid is NOT a dependency and never can be — it depends on Loggair, and
    Loggair is published standalone — so the import is local and its absence is a
    plain fallback to ``repr``, not an error.

    Only ``@configurable`` objects take this path. Handing Confluid an arbitrary
    value would be both wasteful (measured: ~116 us versus 0.3 us for ``repr``)
    and noisy — its dumper emits a ``logger.warning`` for any type it cannot spell
    back into a document, which is a statement about config round-tripping that
    means nothing in a call trace.
    """
    try:
        from confluid import dump
    except Exception:
        return None
    try:
        document = dump(value)
    except Exception:
        return None
    if multiline:
        return "\n" + textwrap.indent(document.rstrip("\n"), "    ")
    try:
        # Flow style keeps one call on one log line, so a trace stays greppable.
        # `dump` emits plain YAML precisely so it can be re-read like this.
        return str(
            yaml.safe_dump(
                yaml.safe_load(document),
                default_flow_style=True,
                sort_keys=False,
                width=10**6,
            )
        ).strip()
    except Exception:
        return None


#: How deep :func:`_project` walks before giving up and taking a ``repr``. A
#: configurable buried deeper than this renders as its repr — documented, bounded,
#: and far past any real config shape.
_MAX_DEPTH = 6


def _project(value: Any, depth: int = 0) -> Tuple[Any, bool]:
    """``(value Confluid can dump, whether anything in it was configurable)``.

    Two jobs, one walk. It finds configurables nested inside CONTAINERS — a
    ``trackers: [...]`` list or a ``metrics: {...}`` dict holds them, and the
    container itself carries no marker, so a top-level check alone rendered a real
    run's slot as ``[Target(matrainer.TensorBoardTracker, {...})]``. And it replaces
    everything Confluid cannot spell with that value's ``repr`` BEFORE the dump, so
    the dumper never reaches its ``logger.warning`` path — that warning is a true
    statement about config round-tripping and a meaningless one in a call trace.
    """
    if hasattr(value, "__confluid_configurable__"):
        return value, True
    if value is None or isinstance(value, (str, int, float, bool)):
        return value, False
    if depth >= _MAX_DEPTH:
        return repr(value), False
    if isinstance(value, (list, tuple)):
        found = False
        items = []
        for item in value:
            projected, hit = _project(item, depth + 1)
            items.append(projected)
            found = found or hit
        return items, found
    if isinstance(value, dict):
        found = False
        mapping: Dict[Any, Any] = {}
        for key, item in value.items():
            projected, hit = _project(item, depth + 1)
            mapping[key if isinstance(key, (str, int, float, bool)) or key is None else repr(key)] = projected
            found = found or hit
        return mapping, found
    return repr(value), False


def _render(value: Any, cap: Optional[int], multiline: bool) -> str:
    """One parameter value as log text: a Confluid document, else its full ``repr``.

    The repr is COMPLETE by default — a trace that quietly elides the one argument
    you were chasing is worse than a long line. Pass ``cap`` to bound it.
    """
    text: Optional[str] = None
    projected, configurable = _project(value)
    if configurable:
        text = _confluid_document(projected, multiline)
    if text is None:
        text = repr(value)
    if cap is not None and len(text) > cap:
        text = text[:cap] + "…"
    return text


def _format_params(
    signature: inspect.Signature,
    args: Tuple[Any, ...],
    kwargs: dict,
    drop_first: bool,
    cap: Optional[int],
    multiline: bool,
) -> str:
    """The call's arguments as ``name=value, ...``, defaults included.

    `drop_first` removes the bound ``self`` / ``cls`` of a decorated method — it is
    passed explicitly by the class decorator, which knows the descriptor kind,
    rather than guessed from a parameter name.
    """
    try:
        bound = signature.bind(*args, **kwargs)
    except TypeError:
        # The call is about to raise anyway; report it instead of masking it.
        return "<arguments do not match the signature>"
    bound.apply_defaults()
    items = list(bound.arguments.items())
    if drop_first and items:
        items = items[1:]
    return ", ".join(f"{name}={_render(value, cap, multiline)}" for name, value in items)


# --------------------------------------------------------------------------- #
# Wrapper construction
# --------------------------------------------------------------------------- #
def _reject_unsupported(func: Callable[..., Any]) -> None:
    """Refuse shapes whose entry/exit a synchronous wrapper would report wrongly.

    A coroutine or generator function RETURNS immediately — the work happens on
    await or on iteration — so a sync wrapper would log a truthful-looking
    ``0.0 ms`` exit for a call that has not started. Failing loudly beats emitting
    a plausible lie; the message carries ``file:line`` so the offending definition
    is one click away.
    """
    if inspect.iscoroutinefunction(func):
        kind = "an async function"
    elif inspect.isasyncgenfunction(func):
        kind = "an async generator function"
    elif inspect.isgeneratorfunction(func):
        kind = "a generator function"
    else:
        return
    code = func.__code__
    raise TypeError(
        f"loggair: cannot trace {func.__qualname__} — it is {kind} "
        f"({code.co_filename}:{code.co_firstlineno}). A synchronous wrapper would time only "
        f"the call that CREATES the coroutine/generator, not the work it does, and report a "
        f"meaningless exit. Trace the functions it awaits or iterates instead."
    )


def _make_wrapper(
    func: Callable[..., Any],
    *,
    level: str,
    timing: bool,
    with_params: bool,
    cap: Optional[int],
    multiline: bool,
    drop_first: bool,
) -> Callable[..., Any]:
    """The traced replacement for `func`."""
    signature = inspect.signature(func)
    # `Op.run`, not `make_op.<locals>.Op.run` — a closure's enclosing scope adds
    # length without adding information.
    qualname = func.__qualname__.rsplit("<locals>.", 1)[-1]
    module = func.__module__
    # Every location field from ONE object. `func` may already be somebody else's
    # wrapper — a validation wrap, a cache, any decorator applied below this one —
    # and `functools.wraps` copies `__module__` but NOT `__code__`, so reading the
    # code object off `func` pairs the real module with the WRAPPER's file and line.
    # Measured on a real trainer under a validating decorator: the trace claimed line
    # 454 of a 160-line file. `unwrap` follows the `__wrapped__` chain to the
    # definition; the wrapper we CALL is still `func`, which must keep doing its work.
    located = inspect.unwrap(func)
    code = located.__code__
    # co_firstlineno is the first DECORATOR line of a decorated function, which is
    # where a reader wants to land: the definition, decorators included.
    site = _Site(located.__module__, located.__name__, code.co_firstlineno, _File(code.co_filename))
    stamp = partial(_stamp_site, site)
    level_no = int(logger.level(level).no)

    # Per-wrapper gate, refreshed only when Loggair's generation changes (a
    # configure / reconfigure / reset). The steady state is one int comparison.
    generation = -1
    threshold = 0
    log: Any = None

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        nonlocal generation, threshold, log
        if generation != LoggingState.generation:
            # get_logger FIRST: it performs the lazy configuration, which bumps
            # the generation, so the value we store has to be read afterwards.
            # No name is passed — `stamp` sets the whole location, name included.
            log = get_logger().patch(stamp)
            threshold = effective_level_no(module)
            generation = LoggingState.generation
        if level_no < threshold:
            return func(*args, **kwargs)

        params = _format_params(signature, args, kwargs, drop_first, cap, multiline) if with_params else ""
        # The record locates the DECORATED function (via `stamp`); the caller goes
        # in the message, labelled, so the two can never be read as one place.
        log.log(level, "→ {}({}) [from {}]", qualname, params, _caller())
        started = time.perf_counter()
        try:
            result = func(*args, **kwargs)
        except BaseException as exc:
            log.log(level, "← {} ✗ {}{}", qualname, type(exc).__name__, _elapsed(timing, started))
            raise
        log.log(level, "← {}{}", qualname, _elapsed(timing, started))
        return result

    return wrapper


def _elapsed(timing: bool, started: float) -> str:
    return f" ({(time.perf_counter() - started) * 1e3:.1f} ms)" if timing else ""


def _bases_to_trace(cls: type, inherited: Union[bool, type]) -> List[type]:
    """Which base classes of `cls` contribute methods, per the `inherited` argument.

    Bounded on purpose. A leaf class here is often a thin config surface over a deep
    framework stack: one real trainer defines ONE traceable method and inherits 167,
    of which 136 come from ``pytorch_lightning`` and ``torch.nn`` — wrapping those
    would trace ``forward`` on every batch. So the default is nothing, a class names
    an INCLUSIVE boundary in the MRO, and ``True`` (everything but ``object``) is the
    blunt instrument you reach for knowingly.
    """
    if inherited is False:
        return []
    mro = [base for base in cls.__mro__[1:] if base is not object]
    if inherited is True:
        return mro
    if not isinstance(inherited, type):
        raise TypeError(
            f"loggair: inherited= takes False, True, or a base class — got {inherited!r} "
            f"({type(inherited).__name__})."
        )
    selected: List[type] = []
    for base in mro:
        selected.append(base)
        if base is inherited:
            return selected
    raise ValueError(
        f"loggair: inherited={inherited.__name__} is not a base of {cls.__qualname__}. "
        f"Its bases are: {', '.join(base.__name__ for base in mro) or '(none)'}."
    )


def _decorate_class(cls: type, **options: Any) -> type:
    """Wrap the class's OWN traceable methods, in place.

    In place (never a subclass) so ``@configurable`` registration, ``isinstance``
    checks and pickling are untouched — the decorated class IS the original object.

    Wrapped: functions defined in this class's ``__dict__``, public or
    ``__init__`` / ``__call__``, including static- and classmethods (unwrapped,
    traced, re-wrapped in the same descriptor). Left alone: inherited methods
    (decorate the base to trace those), ``_private`` names and other dunders,
    properties — reading one would fire its getter, and derived state behind a
    property is exactly what should not be evaluated by a logging decision — and
    async/generator methods, which :func:`_reject_unsupported` explains.
    """
    inherited = options.pop("inherited", False)
    skipped: List[str] = []
    # Names the class defines ITSELF always win — an override is what actually runs,
    # so tracing the base's version underneath it would report the wrong function.
    claimed = set(vars(cls))

    for source in [cls, *_bases_to_trace(cls, inherited)]:
        for name, member in list(vars(source).items()):
            if name.startswith("_") and name not in _WRAPPED_DUNDERS:
                continue
            if source is not cls and name in claimed:
                continue

            rewrap: Optional[Callable[[Any], Any]]
            if isinstance(member, staticmethod):
                func, rewrap, drop_first = member.__func__, staticmethod, False
            elif isinstance(member, classmethod):
                func, rewrap, drop_first = member.__func__, classmethod, True
            elif inspect.isfunction(member):
                func, rewrap, drop_first = member, None, True
            else:
                continue  # properties, nested classes, plain class attributes

            if _is_unsupported(func):
                skipped.append(name)
                continue

            wrapped = _make_wrapper(func, drop_first=drop_first, **options)
            # ALWAYS onto `cls`, never onto `source`: mutating a base would trace
            # every one of its subclasses in the process — for a torch.nn.Module
            # base, every model. The wrapper simply shadows the inherited name.
            setattr(cls, name, rewrap(wrapped) if rewrap is not None else wrapped)
            claimed.add(name)

    if skipped:
        # Discoverable rather than silent, but not a warning: it is a property of
        # the class, not an operator-actionable condition.
        get_logger(cls.__module__).debug(
            "loggair: {}.{{{}}} left untraced — async and generator functions cannot be traced synchronously.",
            cls.__qualname__.rsplit("<locals>.", 1)[-1],
            ", ".join(skipped),
        )
    return cls


def _is_unsupported(func: Callable[..., Any]) -> bool:
    return bool(
        inspect.iscoroutinefunction(func) or inspect.isasyncgenfunction(func) or inspect.isgeneratorfunction(func)
    )


def _build(target: Optional[Any], **options: Any) -> Any:
    """Apply the decorator, whether it was used bare or called with options."""

    def apply(obj: Any) -> Any:
        if isinstance(obj, type):
            return _decorate_class(obj, **options)
        if options.get("inherited", False) is not False:
            # A function has no bases. Silently ignoring the argument would leave
            # the author believing something is traced that never will be.
            raise TypeError(
                f"loggair: inherited= applies to a CLASS, but {obj.__qualname__} is a function "
                f"({obj.__code__.co_filename}:{obj.__code__.co_firstlineno})."
            )
        _reject_unsupported(obj)
        return _make_wrapper(obj, drop_first=False, **{k: v for k, v in options.items() if k != "inherited"})

    return apply(target) if target is not None else apply


# --------------------------------------------------------------------------- #
# Public decorators
# --------------------------------------------------------------------------- #
@overload
def track(target: _T) -> _T: ...


@overload
def track(*, level: str = ..., timing: bool = ..., inherited: Union[bool, type] = ...) -> Callable[[_T], _T]: ...


def track(
    target: Optional[Any] = None,
    *,
    level: str = TRAIL,
    timing: bool = True,
    inherited: Union[bool, type] = False,
) -> Any:
    """Log entry and exit of a function, a method, or every method of a class.

    Parameter values are NOT logged — use :func:`spy` for those. Costs ~0.10 us
    per call while `level` is switched off (see the module docstring).

    Args:
        target: The function or class being decorated. Supplied by Python; pass
            nothing when using the ``@track(...)`` form.
        level: The level to emit at. Any registered level name — ``TRAIL`` by
            default, ``"DEBUG"`` to fold call tracing into ordinary debug output.
        timing: Append the elapsed wall time to the exit line.

    Returns:
        The traced function, or the same class object with its own methods traced.

    Raises:
        TypeError: `target` is an async or generator function, whose entry/exit a
            synchronous wrapper cannot report truthfully.

    Example::

        @track
        def load(path, n=10): ...

        load("/data/x.csv", n=2)
        # → load()
        # ← load (1.4 ms)
    """
    return _build(target, level=level, timing=timing, with_params=False, cap=None, multiline=False, inherited=inherited)


@overload
def spy(target: _T) -> _T: ...


@overload
def spy(
    *,
    level: str = ...,
    timing: bool = ...,
    cap: Optional[int] = ...,
    multiline: bool = ...,
    inherited: Union[bool, type] = ...,
) -> Callable[[_T], _T]: ...


def spy(
    target: Optional[Any] = None,
    *,
    level: str = TRAIL,
    timing: bool = True,
    cap: Optional[int] = None,
    multiline: bool = False,
    inherited: Union[bool, type] = False,
) -> Any:
    """:func:`track`, plus the value of every parameter on the entry line.

    ``@configurable`` arguments are rendered as their Confluid document, so the
    trace shows how an object was CONFIGURED rather than where it lives in memory;
    everything else uses its full ``repr``. Confluid is optional — without it,
    every value falls back to ``repr``.

    Args:
        target: The function or class being decorated. Supplied by Python; pass
            nothing when using the ``@spy(...)`` form.
        level: The level to emit at. ``TRAIL`` by default.
        timing: Append the elapsed wall time to the exit line.
        cap: Truncate any rendered value longer than this many characters. ``None``
            (the default) renders values complete.
        multiline: Render Confluid documents as an indented YAML block instead of
            one flow-style line. Readable for deep configuration trees, at the cost
            of one call no longer being one log line.

    Returns:
        The traced function, or the same class object with its own methods traced.

    Raises:
        TypeError: `target` is an async or generator function.

    Example::

        @spy
        def fit(model, epochs=2): ...

        fit(Model(layers=5), epochs=7)
        # → fit(model={_target_: Model, layers: 5, name: resnet}, epochs=7)
        # ← fit (812.0 ms)
    """
    return _build(
        target, level=level, timing=timing, with_params=True, cap=cap, multiline=multiline, inherited=inherited
    )


__all__ = ["TRAIL", "TRAIL_NO", "spy", "track"]

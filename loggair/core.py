import os
import re
import shutil
import signal
import sys
import threading
import warnings
from dataclasses import dataclass
from functools import partial
from multiprocessing import current_process
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Literal, Optional, Union, cast

from loguru import logger

if TYPE_CHECKING:
    from loguru import Logger

from loggair import discovery
from loggair.alerts import AlertDispatcher
from loggair.config import load_config
from loggair.context import clear_context, context_snapshot, get_context
from loggair.intercept import _VALID_INTERCEPT_MODES, setup_interception, teardown_interception
from loggair.null_logger import NullLogger
from loggair.rotation import (  # noqa: F401  (re-exported: tests/consumers import via loggair.core)
    _ARCHIVE_RE,
    _COMPRESSION_GLOB_SUFFIXES,
    _VALID_COMPRESSIONS,
    CompressionFormat,
    _compress_file,
    _purge_old_files,
    _rotate,
)
from loggair.signals import _install_signal_handlers, _resolve_signal


def _str_to_bool(value: Any) -> bool:
    """Coerce a config/env value to bool (the truthiness used throughout Loggair)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("true", "1", "t", "y", "yes")
    return bool(value)


class LoggingState:
    """Singleton state management for Loggair."""

    configured: bool = False
    log_file: Optional[Path] = None
    # Lazy-enqueue bookkeeping — keep the params for the file sink so we can
    # re-add it with enqueue=True the first time multiprocess activity is
    # detected. Avoids paying the POSIX-semaphore cost in single-process runs.
    file_sink_id: Optional[int] = None
    file_sink_params: Optional[dict] = None
    enqueue_active: bool = False
    enqueue_requested: bool = False
    # The explicitly-passed (non-None) kwargs of the last successful
    # configure_logging call. `reconfigure` and the signal-triggered reload
    # re-apply them so a reload never silently drops a programmatic setting
    # (e.g. an explicit log_dir) back to the config-file/default value.
    last_kwargs: Dict[str, Any] = {}
    # The fully-RESOLVED settings of the last successful configure (all
    # hierarchy layers applied), JSON-serializable. Served by
    # :func:`get_active_config`; every new configure_logging knob MUST be
    # added here too.
    active_config: Dict[str, Any] = {}
    # Active alert dispatcher (webhook/alerting sink), if alert_urls are set.
    alert_dispatcher: Optional[AlertDispatcher] = None
    # Runtime signal bookkeeping (see _install_signal_handlers).
    signal_originals: Dict[int, Any] = {}
    last_signal_thread: Optional[threading.Thread] = None
    debug_active: bool = False
    pre_debug_kwargs: Optional[Dict[str, Any]] = None
    # The PARSED `module_levels` rules of the last successful configure. Kept
    # next to `active_config` (which holds the raw mapping) so
    # :func:`effective_level_no` answers from the same rules the sink filters
    # apply, instead of re-deriving them from the snapshot.
    module_rules: List["ModuleLevelRule"] = []
    # Bumped on every configure / reset. Callers that CACHE a level threshold
    # (see `loggair.track`) compare against it to notice a `reconfigure()`
    # without paying for a fresh resolution on every call.
    generation: int = 0

    @classmethod
    def reset(cls) -> None:
        cls.configured = False
        cls.log_file = None
        cls.file_sink_id = None
        cls.file_sink_params = None
        cls.enqueue_active = False
        cls.enqueue_requested = False
        cls.last_kwargs = {}
        cls.active_config = {}
        cls.alert_dispatcher = None
        cls.last_signal_thread = None
        cls.debug_active = False
        cls.pre_debug_kwargs = None
        cls.module_rules = []
        cls.generation += 1
        logger.remove()
        if hasattr(discovery.get_rank, "cache_clear"):
            discovery.get_rank.cache_clear()


def _upgrade_to_enqueue() -> None:
    """Swap the file sink to ``enqueue=True``. Idempotent.

    Called the first time the parent process is about to fork or spawn a
    child. Allocating the multiprocessing queue here (instead of at handler
    creation) avoids the POSIX semaphore cost — and on macOS the kernel-wide
    semaphore-table exhaustion — for single-process runs.
    """
    if LoggingState.enqueue_active:
        return
    if not LoggingState.enqueue_requested:
        return
    if LoggingState.file_sink_id is None or LoggingState.file_sink_params is None:
        return
    try:
        logger.remove(LoggingState.file_sink_id)
    except ValueError:
        pass  # already removed by an external caller
    params: Any = dict(LoggingState.file_sink_params)
    params["enqueue"] = True
    LoggingState.file_sink_id = logger.add(**params)
    LoggingState.enqueue_active = True


def _install_lazy_enqueue_hooks() -> None:
    """Install one-time fork + spawn hooks that trigger ``_upgrade_to_enqueue``.

    The fork hook is wired via :func:`os.register_at_fork`; the spawn hook is
    a one-shot monkey-patch of ``multiprocessing.process.BaseProcess.__init__``
    so the queue exists in the parent before the spawn pickles parent state.
    Both are idempotent and process-local.
    """
    if getattr(_install_lazy_enqueue_hooks, "_installed", False):
        return

    # The at-fork registration can never be undone (os has no unregister), so it
    # gets its own once-per-process guard that reset_logging() does NOT clear —
    # re-registering on every reset/configure cycle would accumulate duplicate
    # hooks. The hook itself is inert while enqueue_requested is False.
    if not getattr(_install_lazy_enqueue_hooks, "_atfork_registered", False):
        try:
            os.register_at_fork(before=_upgrade_to_enqueue)
            _install_lazy_enqueue_hooks._atfork_registered = True  # type: ignore[attr-defined]
        except (AttributeError, RuntimeError):  # pragma: no cover - platform-dep
            pass

    import multiprocessing.process as _mp_process

    if not getattr(_mp_process.BaseProcess.__init__, "_loggair_patched", False):
        _orig_init = _mp_process.BaseProcess.__init__

        def _patched_init(self: Any, *args: Any, **kwargs: Any) -> None:
            _upgrade_to_enqueue()
            _orig_init(self, *args, **kwargs)

        _patched_init._loggair_patched = True  # type: ignore[attr-defined]
        _patched_init._loggair_orig = _orig_init  # type: ignore[attr-defined]
        _mp_process.BaseProcess.__init__ = _patched_init  # type: ignore[method-assign]

    _install_lazy_enqueue_hooks._installed = True  # type: ignore[attr-defined]


def _uninstall_lazy_enqueue_hooks() -> None:
    """Undo what :func:`_install_lazy_enqueue_hooks` can undo (see its docstring).

    Restores the original ``BaseProcess.__init__`` and clears the install flag so
    a later configure re-patches cleanly. The ``os.register_at_fork`` hook cannot
    be removed — it stays registered but inert (``enqueue_requested`` is False
    after a reset).
    """
    import multiprocessing.process as _mp_process

    init = _mp_process.BaseProcess.__init__
    if getattr(init, "_loggair_patched", False):
        _mp_process.BaseProcess.__init__ = init._loggair_orig  # type: ignore[method-assign, attr-defined]
    _install_lazy_enqueue_hooks._installed = False  # type: ignore[attr-defined]


# --- Sink formats -------------------------------------------------------------
# The defaults, public so a custom format can EXTEND rather than restate them
# (e.g. ``file_format=DEFAULT_FILE_FORMAT + " | {extra}"``). Both sinks' filters
# always stamp ``record["extra"]["rank_tag"]``, so ``{extra[rank_tag]}`` is
# available to any custom format (and omitting it simply drops the rank tag).
DEFAULT_CONSOLE_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
    "<level>{level: <8}</level> | "
    "{extra[rank_tag]}<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
    "{extra[context_tag]}<level>{message}</level>"
)
DEFAULT_FILE_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
    "{extra[rank_tag]}{name}:{function}:{line} | {extra[context_tag]}{message}"
)


# --- Per-logger, per-sink level overrides -----------------------------------

_VALID_RULE_KEYS = {"console", "file", "workers_only"}


@dataclass(frozen=True)
class ModuleLevelRule:
    """A parsed `module_levels` entry.

    `console_no` / `file_no` are loguru level numbers (`logger.level(name).no`)
    or `None` to defer to the sink's global level. `workers_only=True` restricts
    the rule to non-MainProcess processes (e.g. DataLoader workers).
    """

    prefix: str
    console_no: Optional[int]
    file_no: Optional[int]
    workers_only: bool


def _level_no(level: str, where: str) -> int:
    """`level` as a loguru severity number, or a ValueError naming the setting.

    `where` is the fully-qualified setting the value came from — ``"file_level"``,
    ``"module_levels['pkg.a'].console"``, ``"alert_level"`` — and it is the ONLY
    thing the message may name. It used to be prefixed with a hardcoded
    ``module_levels:`` regardless of the caller, so a bad ``file_level`` reported
    ``module_levels: invalid level 'X' for file_level`` and sent the reader to the
    wrong block of their config.
    """
    try:
        return int(logger.level(level.upper()).no)
    except (ValueError, TypeError) as e:
        raise ValueError(f"invalid level '{level}' for {where}") from e


def _parse_module_levels(cfg: Dict[str, Any]) -> List[ModuleLevelRule]:
    """Parse and validate the optional ``module_levels`` config block.

    Fails fast on unknown sub-keys or invalid levels — silent ignores are
    exactly how this feature rotted unimplemented for so long.
    """
    raw = cfg.get("module_levels")
    if raw is None:
        return []
    if not isinstance(raw, dict):
        raise ValueError(f"module_levels: expected a mapping, got {type(raw).__name__}")

    rules: List[ModuleLevelRule] = []
    for prefix, entry in raw.items():
        if not isinstance(prefix, str) or not prefix:
            raise ValueError(f"module_levels: prefix keys must be non-empty strings, got {prefix!r}")
        if not isinstance(entry, dict):
            raise ValueError(
                f"module_levels[{prefix!r}]: expected a mapping with keys {sorted(_VALID_RULE_KEYS)}, "
                f"got {type(entry).__name__}"
            )
        unknown = set(entry.keys()) - _VALID_RULE_KEYS
        if unknown:
            raise ValueError(
                f"module_levels[{prefix!r}]: unknown sub-key(s) {sorted(unknown)}; "
                f"valid keys are {sorted(_VALID_RULE_KEYS)}"
            )
        c = entry.get("console")
        f = entry.get("file")
        if c is None and f is None:
            raise ValueError(f"module_levels[{prefix!r}]: must set at least one of 'console' or 'file'")
        workers_only = entry.get("workers_only", False)
        if not isinstance(workers_only, bool):
            raise ValueError(
                f"module_levels[{prefix!r}].workers_only: expected bool, got {type(workers_only).__name__}"
            )
        rules.append(
            ModuleLevelRule(
                prefix=prefix,
                console_no=(_level_no(c, f"module_levels[{prefix!r}].console") if c is not None else None),
                file_no=(_level_no(f, f"module_levels[{prefix!r}].file") if f is not None else None),
                workers_only=workers_only,
            )
        )

    # Longest prefix first → first-match wins gives most-specific match.
    rules.sort(key=lambda r: len(r.prefix), reverse=True)
    return rules


def _matching_rule(name: str, rules: List[ModuleLevelRule]) -> Optional[ModuleLevelRule]:
    """The first `module_levels` rule whose prefix matches `name`, or None.

    A rule matches on an exact name or a dotted-prefix ancestry (`pkg` matches
    `pkg.io` but not `pkgx`), and `workers_only` rules are inert in the main
    process. Shared by the per-record sink filter and by
    :func:`effective_level_no` — two copies of this loop would let the
    advertised threshold drift from the one records are actually gated on.
    """
    if not rules:
        return None
    in_worker = current_process().name != "MainProcess"
    for rule in rules:
        if rule.workers_only and not in_worker:
            continue
        if name == rule.prefix or name.startswith(rule.prefix + "."):
            return rule
    return None


def _sink_floor(sink: Literal["console", "file"], global_no: int, rules: List[ModuleLevelRule]) -> int:
    """The loguru ``level=`` floor for `sink` — the lowest threshold its filter can apply.

    Loggair gates levels in ``_make_sink_filter``, not at the handler, because a
    ``module_levels`` rule must be able to LOWER a threshold as well as raise it and
    a handler's own ``level=`` can only raise. The floor was therefore pinned open at
    ``"TRACE"``, which had a cost: loguru's one cheap early-out is
    ``level_no < core.min_level``, and with the floor at 5 nothing ever took it — every
    below-threshold record was fully built (frame inspection, timestamp, record dict,
    argument formatting) and then discarded by the filter. Measured on a process
    configured at INFO: **3.4 us** for a dropped ``logger.debug()``, against **0.10 us**
    with the floor computed here. It is also why ``logger.opt(lazy=True)`` defers
    nothing under Loggair — loguru evaluates lazy arguments only after the
    ``min_level`` check the record has already passed.

    The floor is the MINIMUM of the sink's global level and every ``module_levels``
    override for that sink, so by construction no record the filter would have
    accepted is dropped before reaching it. Two omissions look free and are not:

    * Ignoring the rules and using `global_no` alone drops every PROMOTED record —
      a rule setting ``file: DEBUG`` under a global ``ERROR`` stops working.
    * Skipping ``workers_only`` rules because they cannot match in this process
      drops promoted records in FORKED children, which inherit the parent's handler
      objects and therefore this floor. Both are pinned by tests in
      ``tests/test_core.py``; only the first is caught by any other test.
    """
    thresholds = [global_no]
    for rule in rules:
        # Every rule, including workers_only ones inert here — see the docstring.
        override = rule.console_no if sink == "console" else rule.file_no
        if override is not None:
            thresholds.append(override)
    return min(thresholds)


def _make_sink_filter(
    sink: Literal["console", "file"],
    global_no: int,
    rules: List[ModuleLevelRule],
) -> Callable[[Any], bool]:
    """Build a loguru `filter=` callable that gates records by per-logger threshold.

    Also tags `record["extra"]["rank_tag"]` (used by both sinks' format strings).
    """

    def _filter(record: Any) -> bool:
        r = discovery.get_rank()
        record["extra"]["rank_tag"] = f"[rank {r}] | " if r and r > 0 else ""

        # Experiment context: prebuilt tag (empty when unset) + the individual
        # fields for JSON mode. setdefault — an explicit bind() wins.
        ctx, ctx_tag = context_snapshot()
        record["extra"]["context_tag"] = ctx_tag
        if ctx:
            for key, value in ctx.items():
                record["extra"].setdefault(key, value)

        threshold = global_no
        rule = _matching_rule(record["name"] or "", rules)
        if rule is not None:
            override = rule.console_no if sink == "console" else rule.file_no
            if override is not None:
                threshold = override
        return bool(record["level"].no >= threshold)

    return _filter


def _perform_pivot(
    current_log: Path, new_log: Path, do_rotation: bool, retention: int, compression: Optional[str] = None
) -> None:
    """Transition from an interim log file to a final target file."""
    logger.remove()
    try:
        logger.complete()
    except Exception as e:
        warnings.warn(f"Loggair: Failed to complete logger during pivot: {e}")

    if do_rotation:
        _rotate(new_log, retention, compression)

    if current_log.exists():
        try:
            shutil.copy2(current_log, new_log)
            current_log.unlink()
        except Exception as e:
            warnings.warn(f"Loggair: Failed to pivot logs from {current_log} to {new_log}: {e}")
    LoggingState.configured = False


def _resolve_colorize(arg: Optional[bool]) -> Optional[bool]:
    """Resolve the console-sink colorize tri-state.

    Precedence (highest first): explicit ``arg`` (``True``/``False``) > the
    standard ``NO_COLOR`` env var (any non-empty value -> off, per
    https://no-color.org) > ``None`` (loguru auto-detect via ``stream.isatty()``,
    the historical default). ``NO_COLOR`` is the ONLY environment control — there
    is no bespoke Loggair color env var. Non-interactive consumers (the
    navigaitor MCP server, the StreamStudio ComfyUI extension) set ``NO_COLOR`` for
    themselves via :func:`force_no_color`.
    """
    if arg is not None:
        return arg
    if os.getenv("NO_COLOR"):
        return False
    return None


def resolve_settings(
    log_dir: Optional[Union[str, Path]] = None,
    script_name: Optional[str] = None,
    file_level: Optional[str] = None,
    console_level: Optional[str] = None,
    rotation_on_startup: Optional[bool] = None,
    retention: Optional[int] = None,
    compression: Optional[CompressionFormat] = None,
    enqueue: Optional[bool] = None,
    colorize: Optional[bool] = None,
    serialize: Optional[bool] = None,
    rotation: Optional[str] = None,
    console_format: Optional[str] = None,
    file_format: Optional[str] = None,
    reload_signal: Optional[str] = None,
    debug_signal: Optional[str] = None,
    alert_urls: Optional[Union[str, List[str]]] = None,
    alert_level: Optional[str] = None,
    alert_throttle: Optional[float] = None,
    worker_files: Optional[bool] = None,
    intercept: Optional[str] = None,
    intercept_exclude: Optional[Union[str, List[str]]] = None,
    capture_warnings: Optional[bool] = None,
) -> Dict[str, Any]:
    """Resolve every setting through the full hierarchy — PURELY, no side effects.

    The single source of truth for configuration resolution (args > ``LOGGAIR_*``
    env > local YAML > pyproject > XDG > defaults), shared by
    :func:`configure_logging` and the ``python -m loggair`` diagnostic. It MUST
    stay free of side effects: no directory creation, no rotation, no sinks, no
    env-var writes — a diagnostic that mutates the very state it inspects is a
    footgun (see the AGENTS resolver-purity mandate). Validation still fails
    fast here (unknown levels/modes/signals/compression raise ``ValueError``).

    Returns a dict of the resolved values: JSON-serializable public keys (the
    same vocabulary as :func:`get_active_config`) plus ``_``-prefixed internal
    entries for :func:`configure_logging` (parsed rules, level numbers, `Path`
    objects, the RAW alert URLs — never expose those; diagnostics must redact).
    """
    is_main_proc = current_process().name == "MainProcess" and discovery.get_rank() in (None, 0)

    cfg = load_config()

    def resolve(val: Any, env: str, key: str, default: Any) -> Any:
        # Presence-based resolution — falsy-but-valid values (rotation_on_startup:
        # false, retention: 0) must win over the default, so never `or`-chain here.
        # An EMPTY env var counts as unset (matching the historical behavior).
        if val is not None:
            return val
        env_val = os.getenv(env)
        if env_val:
            return env_val
        if key in cfg and cfg[key] is not None:
            return cfg[key]
        return default

    log_dir_val = resolve(log_dir, "LOGGAIR_DIR", "log_dir", "./logs")
    log_dir_path = Path(log_dir_val).expanduser().resolve()

    f_level = str(resolve(file_level, "LOGGAIR_FILE_LEVEL", "file_level", "DEBUG")).upper()
    c_level = str(resolve(console_level, "LOGGAIR_CONSOLE_LEVEL", "console_level", "INFO")).upper()
    f_no = _level_no(f_level, "file_level")
    c_no = _level_no(c_level, "console_level")
    module_rules = _parse_module_levels(cfg)
    retention_val = int(resolve(retention, "LOGGAIR_RETENTION", "retention", 5))
    do_rotation = _str_to_bool(resolve(rotation_on_startup, "LOGGAIR_ROTATION_ON_STARTUP", "rotation_on_startup", True))
    enqueue_val = _str_to_bool(resolve(enqueue, "LOGGAIR_ENQUEUE", "enqueue", False))
    serialize_val = _str_to_bool(resolve(serialize, "LOGGAIR_SERIALIZE", "serialize", False))
    # JSON mode forces colors off: ANSI escapes would otherwise be embedded
    # inside the serialized "text" field, garbling it for log aggregators.
    colorize_val = False if serialize_val else _resolve_colorize(colorize)

    console_fmt = str(resolve(console_format, "LOGGAIR_CONSOLE_FORMAT", "console_format", DEFAULT_CONSOLE_FORMAT))
    file_fmt = str(resolve(file_format, "LOGGAIR_FILE_FORMAT", "file_format", DEFAULT_FILE_FORMAT))

    rotation_val = resolve(rotation, "LOGGAIR_ROTATION", "rotation", None)
    if rotation_val is not None:
        rotation_val = str(rotation_val)

    alert_urls_val = resolve(alert_urls, "LOGGAIR_ALERT_URLS", "alert_urls", None)
    if isinstance(alert_urls_val, str):
        alert_urls_list = [u.strip() for u in alert_urls_val.split(",") if u.strip()]
    else:
        alert_urls_list = [str(u) for u in alert_urls_val] if alert_urls_val else []
    alert_level_val = str(resolve(alert_level, "LOGGAIR_ALERT_LEVEL", "alert_level", "ERROR")).upper()
    _level_no(alert_level_val, "alert_level")  # fail fast on unknown level names
    alert_throttle_val = float(resolve(alert_throttle, "LOGGAIR_ALERT_THROTTLE", "alert_throttle", 60.0))

    intercept_val = str(resolve(intercept, "LOGGAIR_INTERCEPT", "intercept", "full")).lower()
    if intercept_val not in _VALID_INTERCEPT_MODES:
        raise ValueError(
            f"intercept: invalid value {intercept_val!r}; expected one of {sorted(_VALID_INTERCEPT_MODES)}"
        )
    intercept_exclude_val = resolve(intercept_exclude, "LOGGAIR_INTERCEPT_EXCLUDE", "intercept_exclude", None)
    if isinstance(intercept_exclude_val, str):
        intercept_exclude_list = [p.strip() for p in intercept_exclude_val.split(",") if p.strip()]
    else:
        intercept_exclude_list = [str(p) for p in intercept_exclude_val] if intercept_exclude_val else []
    capture_warnings_val = _str_to_bool(resolve(capture_warnings, "LOGGAIR_CAPTURE_WARNINGS", "capture_warnings", True))

    reload_signal_val = resolve(reload_signal, "LOGGAIR_RELOAD_SIGNAL", "reload_signal", None)
    debug_signal_val = resolve(debug_signal, "LOGGAIR_DEBUG_SIGNAL", "debug_signal", None)
    # Validate eagerly (fail fast on a typo'd or platform-unavailable name),
    # even in processes that won't install the handlers.
    for sig_spec in (reload_signal_val, debug_signal_val):
        if sig_spec is not None:
            _resolve_signal(sig_spec)

    compression_val = resolve(compression, "LOGGAIR_COMPRESSION", "compression", None)
    if compression_val is not None:
        compression_val = str(compression_val).lower()
        if compression_val not in _VALID_COMPRESSIONS:
            raise ValueError(
                f"compression: invalid value {compression_val!r}; "
                f"expected one of {sorted(_VALID_COMPRESSIONS)} or None"
            )

    target_name = discovery.determine_script_name(resolve(script_name, "LOGGAIR_SCRIPT_NAME", "script_name", None))

    # Per-worker files: each non-main writer gets its own exclusively-owned
    # file — rank suffix for DDP ranks, process-name suffix for children (a
    # rank's own children carry both, e.g. app.rank2.worker-1.log).
    worker_files_val = _str_to_bool(resolve(worker_files, "LOGGAIR_WORKER_FILES", "worker_files", False))
    suffix_parts: List[str] = []
    if worker_files_val and not is_main_proc:
        rank = discovery.get_rank()
        if rank not in (None, 0):
            suffix_parts.append(f"rank{rank}")
        if current_process().name != "MainProcess":
            proc = re.sub(r"[^A-Za-z0-9_-]+", "-", current_process().name).strip("-").lower()
            suffix_parts.append(proc or f"pid{os.getpid()}")
    worker_suffix = "".join(f".{p}" for p in suffix_parts)
    new_log_file = log_dir_path / f"{target_name}{worker_suffix}.log"

    return {
        # Public, JSON-serializable (get_active_config vocabulary):
        "log_dir": str(log_dir_path),
        "script_name": target_name,
        "log_file": str(new_log_file),
        "file_level": f_level,
        "console_level": c_level,
        "module_levels": dict(cfg.get("module_levels") or {}),
        "rotation_on_startup": do_rotation,
        "retention": retention_val,
        "compression": compression_val,
        "rotation": rotation_val,
        "enqueue": bool(enqueue_val),
        "colorize": colorize_val,
        "serialize": serialize_val,
        "console_format": console_fmt,
        "file_format": file_fmt,
        "reload_signal": reload_signal_val,
        "debug_signal": debug_signal_val,
        "alert_level": alert_level_val,
        "alert_throttle": alert_throttle_val,
        "worker_files": worker_files_val,
        "intercept": intercept_val,
        "intercept_exclude": list(intercept_exclude_list),
        "capture_warnings": capture_warnings_val,
        "is_main_process": is_main_proc,
        "rank": discovery.get_rank(),
        # Internal (never expose raw — alert URLs carry secrets):
        "_log_dir_path": log_dir_path,
        "_log_file_path": new_log_file,
        "_worker_suffix": worker_suffix,
        "_module_rules": module_rules,
        "_console_no": c_no,
        "_file_no": f_no,
        "_alert_urls": alert_urls_list,
    }


def configure_logging(
    log_dir: Optional[Union[str, Path]] = None,
    script_name: Optional[str] = None,
    file_level: Optional[str] = None,
    console_level: Optional[str] = None,
    rotation_on_startup: Optional[bool] = None,
    retention: Optional[int] = None,
    compression: Optional[CompressionFormat] = None,
    enqueue: Optional[bool] = None,
    colorize: Optional[bool] = None,
    serialize: Optional[bool] = None,
    rotation: Optional[str] = None,
    console_format: Optional[str] = None,
    file_format: Optional[str] = None,
    reload_signal: Optional[str] = None,
    debug_signal: Optional[str] = None,
    alert_urls: Optional[Union[str, List[str]]] = None,
    alert_level: Optional[str] = None,
    alert_throttle: Optional[float] = None,
    worker_files: Optional[bool] = None,
    intercept: Optional[str] = None,
    intercept_exclude: Optional[Union[str, List[str]]] = None,
    capture_warnings: Optional[bool] = None,
    force: bool = False,
) -> None:
    """
    Configure the global Loggair system with Atomic Pivot support.

    ``colorize`` controls console-sink ANSI coloring as a tri-state:
    ``True`` forces colors on, ``False`` forces them off, and ``None`` (the
    default) defers to the standard ``NO_COLOR`` env var, falling back to
    loguru's TTY auto-detection. See :func:`_resolve_colorize`.

    ``compression`` (``"gz"`` / ``"zip"`` / ``None``) compresses each
    startup-rotated log archive. Because Loggair performs its own rotation, this
    is applied by :func:`_rotate` (NOT loguru's sink-level ``compression=``).
    Resolved through the standard hierarchy: arg > ``LOGGAIR_COMPRESSION`` env >
    ``compression`` YAML key > ``None``.

    ``serialize`` switches BOTH sinks to structured JSON output (one object per
    line) via loguru's native ``serialize=True``: ``{"text": <formatted line>,
    "record": {...}}`` with the full structured record (level, timestamp,
    name/function/line, process/thread, ``extra`` incl. Loggair's rank tag) —
    the format log aggregators (Elasticsearch/Kibana, Loki, Datadog) ingest
    directly. When enabled, console coloring is FORCED off regardless of
    ``colorize``/``NO_COLOR``: ANSI codes would otherwise be embedded inside the
    JSON ``text`` field (verified empirically). Resolved through the standard
    hierarchy: arg > ``LOGGAIR_SERIALIZE`` env > ``serialize`` YAML key >
    ``False``.

    ``rotation`` enables RUNTIME rotation of the file sink for long-running
    processes (multi-week training loops, FastAPI/MCP services) — passed to
    loguru verbatim, so all its forms work: ``"100 MB"``, ``"daily"``,
    ``"1 week"``, ``"00:00"``, ``"monday at 12:00"``. Invalid values raise
    ``ValueError`` at configure time. Applied in the MAIN process only (single
    rotator — a rename racing multiple writers would split streams); when
    active, loguru's sink-level ``retention=`` (same `retention` count, proven
    per-stem and live-log-safe) and ``compression=`` (same `compression` value)
    prune/compress at rotation time, so a service that never restarts still
    honors both. Startup rotation composes unchanged. Multi-writer caveat: a
    spawn-started child that reconfigures its own sink keeps its fd after a
    main-process rotation, so its subsequent records land in the rotated
    archive (fork-inherited children route through the enqueue queue and are
    unaffected). Resolved: arg > ``LOGGAIR_ROTATION`` env > ``rotation`` YAML
    key > ``None`` (startup-only rotation, the historical behavior).

    ``console_format`` / ``file_format`` override the per-sink loguru format
    strings (defaults: :data:`DEFAULT_CONSOLE_FORMAT` /
    :data:`DEFAULT_FILE_FORMAT` — extend rather than restate, e.g.
    ``file_format=DEFAULT_FILE_FORMAT + " | {extra}"``). Any loguru field works
    (``{process.id}``, ``{thread.id}``, ``{extra[...]}``, color markup on the
    console). ``{extra[rank_tag]}`` is always stamped and available. Invalid
    color markup fails fast at configure time; a format referencing a
    nonexistent field does NOT (loguru reports a loud per-record error on
    stderr instead — the record is dropped). Resolved through the standard
    hierarchy: arg > ``LOGGAIR_CONSOLE_FORMAT`` / ``LOGGAIR_FILE_FORMAT`` env >
    ``console_format`` / ``file_format`` YAML keys > the defaults.

    ``reload_signal`` / ``debug_signal`` (POSIX signal names, e.g. ``"SIGUSR1"``
    / ``"SIGUSR2"``; default off) enable runtime verbosity control for
    long-running processes without a restart. On ``reload_signal`` Loggair
    re-resolves env vars + config files and reloads the sinks (edit
    ``loggair.yaml``, then ``kill -USR1 <pid>``) — the original call's explicit
    arguments are re-applied, so a reload only picks up changes for settings
    not pinned programmatically. On ``debug_signal`` Loggair TOGGLES both sinks
    to DEBUG and back (no file edit needed — the "diagnose a live training
    anomaly" switch). Handlers are installed in the main process/thread only;
    invalid or platform-unavailable signal names raise ``ValueError``.
    Resolved: arg > ``LOGGAIR_RELOAD_SIGNAL`` / ``LOGGAIR_DEBUG_SIGNAL`` env >
    matching YAML keys > ``None``.

    ``alert_urls`` (list, or comma-separated string) routes records at or above
    ``alert_level`` (default ``"ERROR"``) to external platforms via
    `apprise <https://github.com/caronc/apprise>`_ URLs — Slack, Teams,
    Discord, email, generic webhooks, ~100 services (optional dependency:
    ``pip install loggair[alerts]``). Never blocks the logging path: records
    are queued and a background worker delivers them, batching everything
    since the last delivery into one notification, at most one delivery per
    ``alert_throttle`` seconds (default 60; ``0`` disables throttling). Invalid
    URLs and unknown levels raise at configure time. Installed in EVERY
    process (a worker/rank crash alerts too — the throttle collapses storms).
    Resolved: args > ``LOGGAIR_ALERT_URLS`` / ``LOGGAIR_ALERT_LEVEL`` /
    ``LOGGAIR_ALERT_THROTTLE`` env > matching YAML keys > off.

    ``worker_files`` (default False) gives every NON-main writer its own log
    file instead of appending to the shared ``{name}.log``: DDP ranks write
    ``{name}.rank{N}.log`` and spawned/forked child processes
    ``{name}.{process-name}.log`` (a rank's own children combine both parts).
    Each process then EXCLUSIVELY owns its file, which removes the multi-writer
    caveats wholesale: worker files get startup rotation AND runtime
    ``rotation`` too (safe — sole writer), and no rotation can ever split
    another process's stream. Merge at read time with ``lnav ./logs``. The
    startup sweep still never deletes the bare per-worker files (live-log-safe
    rule), only their timestamped archives. Resolved: arg >
    ``LOGGAIR_WORKER_FILES`` env > ``worker_files`` YAML key > ``False``.

    ``intercept`` selects how aggressively stdlib logging is taken over —
    ``"full"`` (default: Loggair owns the root, existing named loggers are
    stripped to propagate), ``"coexist"`` (append-only next to a framework
    that owns logging, e.g. uvicorn — other handlers untouched), or ``"off"``.
    ``intercept_exclude`` (list, or comma-separated) names dotted logger
    prefixes Loggair leaves completely alone even in full mode.
    ``capture_warnings`` (default True) controls the ``warnings.showwarning``
    redirect. See :data:`loggair.intercept.InterceptMode`. Resolved: args >
    ``LOGGAIR_INTERCEPT`` / ``LOGGAIR_INTERCEPT_EXCLUDE`` /
    ``LOGGAIR_CAPTURE_WARNINGS`` env > matching YAML keys > defaults.
    """
    if LoggingState.configured and not force:
        return

    # 1. Resolve Parameters — the PURE shared resolver (also serves the
    # `python -m loggair` diagnostic); side effects happen only below.
    settings = resolve_settings(
        log_dir=log_dir,
        script_name=script_name,
        file_level=file_level,
        console_level=console_level,
        rotation_on_startup=rotation_on_startup,
        retention=retention,
        compression=compression,
        enqueue=enqueue,
        colorize=colorize,
        serialize=serialize,
        rotation=rotation,
        console_format=console_format,
        file_format=file_format,
        reload_signal=reload_signal,
        debug_signal=debug_signal,
        alert_urls=alert_urls,
        alert_level=alert_level,
        alert_throttle=alert_throttle,
        worker_files=worker_files,
        intercept=intercept,
        intercept_exclude=intercept_exclude,
        capture_warnings=capture_warnings,
    )
    is_main_proc = settings["is_main_process"]
    log_dir_path: Path = settings["_log_dir_path"]
    new_log_file: Path = settings["_log_file_path"]
    worker_suffix: str = settings["_worker_suffix"]
    module_rules = settings["_module_rules"]
    c_no, f_no = settings["_console_no"], settings["_file_no"]
    alert_urls_list = settings["_alert_urls"]
    retention_val = settings["retention"]
    do_rotation = settings["rotation_on_startup"]
    enqueue_val = settings["enqueue"]
    colorize_val = settings["colorize"]
    serialize_val = settings["serialize"]
    console_fmt, file_fmt = settings["console_format"], settings["file_format"]
    rotation_val = settings["rotation"]
    compression_val = settings["compression"]
    alert_level_val = settings["alert_level"]
    alert_throttle_val = settings["alert_throttle"]
    intercept_val = settings["intercept"]
    intercept_exclude_list = settings["intercept_exclude"]
    capture_warnings_val = settings["capture_warnings"]
    reload_signal_val = settings["reload_signal"]
    debug_signal_val = settings["debug_signal"]
    target_name = settings["script_name"]

    log_dir_path.mkdir(parents=True, exist_ok=True)

    # 2. PIVOT & ROTATION
    if is_main_proc:
        curr = LoggingState.log_file
        if curr and new_log_file.resolve() != curr.resolve():
            _perform_pivot(curr, new_log_file, do_rotation, retention_val, compression_val)
        elif do_rotation and not LoggingState.configured and new_log_file.exists():
            _rotate(new_log_file, retention_val, compression_val)
    elif worker_suffix and do_rotation and not LoggingState.configured and new_log_file.exists():
        # This process exclusively owns its per-worker file, so rotating it is
        # race-free even off the main rank (force_owner bypasses the rank gate).
        _rotate(new_log_file, retention_val, compression_val, force_owner=True)

    # 3. Setup Sinks
    if not LoggingState.configured or force:
        logger.remove()

        # Tear down any previous alert dispatcher (its loguru sink was just
        # removed); a new one is built below when alert_urls are configured.
        if LoggingState.alert_dispatcher is not None:
            LoggingState.alert_dispatcher.stop(timeout=2.0)
            LoggingState.alert_dispatcher = None

        if is_main_proc:
            logger.add(
                sys.stderr,
                # Not "TRACE": see _sink_floor — an open floor defeats loguru's
                # only cheap early-out and costs ~3.4 us per discarded record.
                level=_sink_floor("console", c_no, module_rules),
                format=console_fmt,
                filter=_make_sink_filter("console", c_no, module_rules),
                # Tri-state (resolved above): True forces colors, False forces
                # them off (the standard NO_COLOR env var, or colorize=False),
                # None lets loguru auto-detect via _colorama.should_colorize() ->
                # stream.isatty() (plus Jupyter / CI / PyCharm heuristics). So a
                # redirected/piped stderr (2> run.log, | tee, plain Jenkins
                # console) stays plain, and NO_COLOR forces plain when
                # auto-detection is wrong. serialize forces False (see above).
                colorize=colorize_val,
                serialize=serialize_val,
            )

        # Lazy-enqueue: when the user asks for ``enqueue=True``, defer the
        # multiprocessing queue (and its POSIX semaphore) until something
        # actually forks/spawns a child. Children re-running configure_logging
        # detect themselves via current_process().name and start eagerly.
        eager_env = os.getenv("LOGGAIR_EAGER_ENQUEUE")
        force_eager = eager_env is not None and _str_to_bool(eager_env)
        in_child = current_process().name != "MainProcess"
        start_with_enqueue = enqueue_val and (force_eager or in_child)
        file_sink_params: dict = dict(
            sink=str(new_log_file),
            # See _sink_floor. Living inside file_sink_params means the
            # lazy-enqueue re-add in _upgrade_to_enqueue carries it unchanged.
            level=_sink_floor("file", f_no, module_rules),
            format=file_fmt,
            filter=_make_sink_filter("file", f_no, module_rules),
            mode="a",
            enqueue=start_with_enqueue,
            serialize=serialize_val,
        )
        # Runtime rotation: a single rotator per FILE. Without worker_files
        # that means the main process only (children share its file); WITH
        # worker_files every process exclusively owns its file, so each may
        # rotate its own. Loguru's sink-level retention/compression then fire
        # at rotation time (proven per-stem and live-log-safe), so a service
        # that never restarts still gets pruning and compressed archives.
        # Loguru validates the rotation string at add() — fail fast right here.
        if rotation_val is not None and (is_main_proc or worker_suffix):
            file_sink_params["rotation"] = rotation_val
            file_sink_params["retention"] = retention_val
            file_sink_params["compression"] = compression_val
        sink_id = logger.add(**file_sink_params)

        LoggingState.file_sink_id = sink_id
        LoggingState.file_sink_params = file_sink_params
        LoggingState.enqueue_active = start_with_enqueue
        LoggingState.enqueue_requested = bool(enqueue_val)

        # Alerting sink: enqueue-only in the logging path, delivery in the
        # dispatcher's worker thread (zero-blocking mandate). The filter drops
        # the dispatcher's own delivery-failure records (no feedback loop).
        if alert_urls_list:
            dispatcher = AlertDispatcher(alert_urls_list, script_name=target_name, throttle=alert_throttle_val)

            def _alert_sink(message: Any) -> None:
                record = message.record
                dispatcher.submit(str(message), record["level"].name, record["level"].no)

            logger.add(
                _alert_sink,
                level=alert_level_val,
                format=file_fmt,
                filter=lambda r: not r["extra"].get("loggair_alert_internal"),
                catch=True,
            )
            LoggingState.alert_dispatcher = dispatcher

        if enqueue_val and not start_with_enqueue:
            _install_lazy_enqueue_hooks()

    was_cfg = LoggingState.configured
    LoggingState.log_file = new_log_file
    LoggingState.configured = True
    # Publish the rules and invalidate cached thresholds (see effective_level_no).
    LoggingState.module_rules = module_rules
    LoggingState.generation += 1
    # Pass the module_levels prefixes so interception does NOT hard-silence a
    # third-party stdlib logger (httpx, urllib3, ...) the user explicitly tuned.
    setup_interception(
        module_level_prefixes=[r.prefix for r in module_rules],
        mode=intercept_val,
        exclude=intercept_exclude_list,
        capture_warnings=capture_warnings_val,
    )

    # Remember the explicitly-passed kwargs so reconfigure / signal reloads
    # re-apply them instead of silently falling back to config-file defaults.
    explicit = dict(
        log_dir=log_dir,
        script_name=script_name,
        file_level=file_level,
        console_level=console_level,
        rotation_on_startup=rotation_on_startup,
        retention=retention,
        compression=compression,
        enqueue=enqueue,
        colorize=colorize,
        serialize=serialize,
        rotation=rotation,
        console_format=console_format,
        file_format=file_format,
        reload_signal=reload_signal,
        debug_signal=debug_signal,
        alert_urls=alert_urls,
        alert_level=alert_level,
        alert_throttle=alert_throttle,
        worker_files=worker_files,
        intercept=intercept,
        intercept_exclude=intercept_exclude,
        capture_warnings=capture_warnings,
    )
    LoggingState.last_kwargs = {k: v for k, v in explicit.items() if v is not None}

    # Snapshot the RESOLVED settings for get_active_config: derived straight
    # from the shared resolver output (single source of truth), minus the
    # "_"-internal keys, plus the runtime-state fields. SECRET-SAFE: the raw
    # alert URLs are replaced by apprise's privacy-redacted forms — this dict
    # is a diagnostics/MCP payload and must never carry tokens.
    LoggingState.active_config = {k: v for k, v in settings.items() if not k.startswith("_")}
    LoggingState.active_config.update(
        configured=True,
        enqueue_active=LoggingState.enqueue_active,
        debug_mode_active=LoggingState.debug_active,
        alert_urls=(LoggingState.alert_dispatcher.redacted_urls if LoggingState.alert_dispatcher else []),
    )

    if is_main_proc:
        os.environ["LOGGAIR_SCRIPT_NAME"] = target_name
        _install_signal_handlers(reload_signal_val, debug_signal_val)

        # Startup sweep: prune TIMESTAMPED ARCHIVES (plain or compressed) down to
        # `retention` per stem — the same per-stem semantic as `_rotate`, extended
        # to stems from earlier runs under other script names. Bare `{name}.log`
        # files are deliberately NEVER candidates: in a shared log dir they may be
        # the live sink of another running process (see `_ARCHIVE_RE`).
        by_stem: Dict[str, List[Path]] = {}
        for f in log_dir_path.iterdir():
            m = _ARCHIVE_RE.fullmatch(f.name)
            if m and f.is_file():
                by_stem.setdefault(m.group("stem"), []).append(f)
        for stem_files in by_stem.values():
            _purge_old_files(stem_files, retention_val)

        logger.info(f"Loggair {'Re-' if was_cfg else ''}initialized: {new_log_file.name}")


def reconfigure(**overrides: Any) -> None:
    """Re-apply Loggair configuration, overriding the current setup.

    A thin convenience wrapper over ``configure_logging(force=True, ...)`` for
    callers that need to change a setting (e.g. ``colorize``) AFTER logging has
    already been configured — including the lazy auto-configuration triggered by
    the first :func:`get_logger`. Without ``force`` such a later call is a no-op,
    so this is the supported "reload" entry point.

    The previous call's EXPLICIT arguments are re-applied underneath the
    overrides, so ``reconfigure(colorize=False)`` after
    ``configure_logging(log_dir="./x")`` keeps logging to ``./x`` instead of
    silently pivoting to the config-file/default directory. Overrides persist
    into subsequent reconfigures the same way.

    Example — an MCP server / GUI extension turning colors off at startup::

        import loggair
        loggair.reconfigure(colorize=False)

    Also the programmatic path for runtime verbosity changes in a long-running
    job (signal-free alternative to ``debug_signal``)::

        loggair.reconfigure(console_level="DEBUG")
    """
    configure_logging(force=True, **{**LoggingState.last_kwargs, **overrides})


def is_configured() -> bool:
    """Return True once :func:`configure_logging` has set up the sinks."""
    return LoggingState.configured


def get_active_config() -> Dict[str, Any]:
    """Return the RESOLVED settings currently in effect, as a JSON-serializable dict.

    All hierarchy layers (args > env > config files > defaults) are already
    applied — this is what the sinks are actually running with, for third-party
    plugins, diagnostics scripts, or an MCP tool surface. Purely read-only: it
    never triggers the lazy configuration, so before the first
    :func:`configure_logging` / :func:`get_logger` it returns
    ``{"configured": False}``.

    Keys: ``configured``, ``log_dir``, ``script_name``, ``log_file``,
    ``file_level``, ``console_level``, ``module_levels`` (the validated raw
    mapping), ``rotation_on_startup``, ``retention``, ``compression``,
    ``rotation``, ``enqueue`` / ``enqueue_active`` (requested vs.
    lazily-materialized queue), ``colorize`` (resolved tri-state),
    ``serialize``, ``console_format`` / ``file_format``, ``reload_signal`` /
    ``debug_signal``, ``debug_mode_active``, ``is_main_process``, ``rank``.

    Returns a fresh copy — mutating it does not affect Loggair. Reflects the
    state as of the last (re)configure; ``enqueue_active`` in particular may
    lag a lazy enqueue upgrade until the next reconfigure.
    """
    if not LoggingState.configured:
        return {"configured": False}
    snapshot = dict(LoggingState.active_config)
    snapshot["module_levels"] = {k: dict(v) for k, v in snapshot.get("module_levels", {}).items()}
    # Live state that can change after configure without a re-snapshot:
    snapshot["enqueue_active"] = LoggingState.enqueue_active
    snapshot["debug_mode_active"] = LoggingState.debug_active
    snapshot["experiment_context"] = get_context()
    return snapshot


def force_no_color() -> None:
    """Disable console coloring for a non-interactive consumer (MCP server / GUI).

    Idempotent. Does TWO things, because either one alone is insufficient:

    1. ``os.environ.setdefault("NO_COLOR", "1")`` — the standard
       https://no-color.org switch, so the choice is inherited by any child
       process this one spawns (whose stderr it captures or relays), and so a
       not-yet-configured Loggair picks it up on its first ``get_logger``.
       ``setdefault`` means a pre-set ``NO_COLOR=`` (empty) deliberately
       re-allows colors.
    2. If Loggair is ALREADY configured, :func:`reconfigure` to apply it NOW.
       Setting the env var is too late on its own once the console sink exists —
       its ``colorize`` was baked at first configure, and a plain second
       ``configure_logging`` returns early. Only a reload re-resolves it.

    Call this at import time in a process whose stderr is captured or relayed —
    the navigaitor MCP server, the StreamStudio ComfyUI extension — ideally before
    the first ``get_logger``, but it self-heals via the reload if not.
    """
    os.environ.setdefault("NO_COLOR", "1")
    if LoggingState.configured:
        reconfigure()


def shutdown_logging() -> None:
    """Flush pending records (enqueued sinks and queued alerts). Sinks stay installed."""
    try:
        logger.complete()
    except Exception as e:
        warnings.warn(f"Loggair: Failed to complete logger during shutdown: {e}")
    if LoggingState.alert_dispatcher is not None:
        LoggingState.alert_dispatcher.flush(timeout=5.0)


def reset_logging() -> None:
    """Tear Loggair down and restore the process-global state it touched.

    For embedders (pytest plugins, notebooks, long-lived hosts) that need to
    switch Loggair off cleanly rather than merely flush it. Flushes and removes
    all sinks, restores ``warnings.showwarning``, the stdlib root
    ``InterceptHandler``, and any runtime-signal handlers Loggair installed,
    un-patches ``BaseProcess.__init__``, forgets the ``LOGGAIR_SCRIPT_NAME``
    handoff env var, and returns to the unconfigured state (a later
    ``get_logger`` / ``configure_logging`` starts fresh).

    Known irreversibles, both harmless: the ``os.register_at_fork`` hook stays
    registered but inert, and stdlib root handlers that existed BEFORE the first
    ``configure_logging`` were already replaced by interception and cannot be
    restored (stdlib logging falls back to its ``lastResort`` handler).
    """
    try:
        logger.complete()
    except Exception as e:
        warnings.warn(f"Loggair: Failed to complete logger during reset: {e}")
    if LoggingState.alert_dispatcher is not None:
        LoggingState.alert_dispatcher.stop(timeout=5.0)
        LoggingState.alert_dispatcher = None
    teardown_interception()
    _uninstall_lazy_enqueue_hooks()
    # Restore any runtime-signal handlers we replaced (main thread only —
    # signal.signal raises elsewhere, and only the main thread installed them).
    if threading.current_thread() is threading.main_thread():
        for signum, original in LoggingState.signal_originals.items():
            signal.signal(signum, original)
        LoggingState.signal_originals = {}
    os.environ.pop("LOGGAIR_SCRIPT_NAME", None)
    clear_context()
    LoggingState.reset()  # also removes all sinks and clears the rank cache


def _logging_disabled() -> bool:
    """Return True when Loggair should hand out a no-op :class:`NullLogger`.

    Two env vars gate this (truthy = ``1`` / ``true`` / ``yes`` / ``t`` / ``y``):

    - ``LOGGAIR_DISABLE_LOGGING`` — disable in EVERY process (perf-critical runs,
      or test suites that want no log files / sink / queue overhead).
    - ``LOGGAIR_DISABLE_MULTIPROCESS_LOGGING`` — disable ONLY in worker/child
      processes (``current_process().name != "MainProcess"``), so the main
      process keeps logging while e.g. PyTorch DataLoader workers stay silent.

    These gate :func:`get_logger` only. An explicit :func:`configure_logging`
    call still sets up sinks; the common path (lazy auto-config on the first
    ``get_logger``) creates no file, since ``get_logger`` short-circuits here
    before ever configuring.
    """
    if _str_to_bool(os.getenv("LOGGAIR_DISABLE_LOGGING")):
        return True
    if _str_to_bool(os.getenv("LOGGAIR_DISABLE_MULTIPROCESS_LOGGING")):
        return current_process().name != "MainProcess"
    return False


#: Returned by :func:`effective_level_no` when no record can reach a sink at all
#: (logging disabled via ``LOGGAIR_DISABLE_*``, or never configured). Chosen above
#: every real level so a plain ``level_no < effective_level_no(...)`` comparison
#: needs no special case at the call site.
NOTHING_REACHABLE = sys.maxsize


def effective_level_no(name: Optional[str] = None) -> int:
    """The LOWEST loguru level number that can still reach a sink for logger `name`.

    A record below this number is dropped by every sink, so a caller can skip
    building it entirely.

    This is strictly more precise than loguru's own early-out, and the difference
    is worth understanding. :func:`_sink_floor` gives each sink a ``level=`` floor,
    which restores loguru's cheap ``level_no < core.min_level`` check — but that
    floor is per-SINK and must accommodate the LOWEST threshold any
    ``module_levels`` rule can impose on it. This function is per-LOGGER. The gap
    opens as soon as one rule promotes one logger: with ``file_level: INFO`` and a
    rule promoting ``pkg.a`` to ``DEBUG``, the file floor drops to 10 for the whole
    sink, so a ``DEBUG`` from ``pkg.b`` clears the floor and is fully built before
    ``_make_sink_filter`` discards it at that logger's real threshold of 20 —
    measured at 3.37 us. ``effective_level_no("pkg.b")`` returns 20 and skips it.

    Note also that ``opt(lazy=True)`` cannot close that gap: loguru evaluates lazy
    arguments after the ``min_level`` check the record has already passed, so they
    run for every record the filter goes on to drop.

    `name` participates in `module_levels` prefix matching exactly as a record's
    ``{name}`` field does. Read-only: it never triggers the lazy configuration
    (an unconfigured Loggair has no sinks, so nothing is reachable).

    CACHE the result against :attr:`LoggingState.generation` — which changes on
    every configure / reconfigure / reset — rather than calling this per record.
    Measured per call: 1.09 us calling it every time, 0.037 us for the cached
    comparison (`loggair.track` does the latter).

    Example::

        if logger.level("TRACE").no >= effective_level_no(__name__):
            logger.trace("expensive: {}", render(payload))
    """
    if _logging_disabled() or not LoggingState.configured:
        return NOTHING_REACHABLE

    config = LoggingState.active_config
    rule = _matching_rule(name or "", LoggingState.module_rules)

    thresholds: List[int] = []
    # The console sink exists on the main process/rank only (see configure_logging).
    if config.get("is_main_process"):
        console = rule.console_no if rule is not None and rule.console_no is not None else None
        thresholds.append(console if console is not None else int(logger.level(config["console_level"]).no))
    file_no = rule.file_no if rule is not None and rule.file_no is not None else None
    thresholds.append(file_no if file_no is not None else int(logger.level(config["file_level"]).no))
    # The alerting sink is a third destination. Its presence is read from the live
    # dispatcher, NOT from the snapshot: `active_config` deliberately strips the
    # `_`-prefixed internals, and the raw `alert_urls` are one of them (they carry
    # tokens and must never appear in a diagnostics payload).
    if LoggingState.alert_dispatcher is not None:
        thresholds.append(int(logger.level(config["alert_level"]).no))

    return min(thresholds)


def _set_record_name(name: str, record: Any) -> None:
    """Patcher installed by :func:`get_logger` — see there. Module-level (used via
    ``functools.partial``) so a named logger stays picklable for spawn workers."""
    record["name"] = name


def get_logger(name: Optional[str] = None) -> "Logger":
    """Return the Loggair logger, lazily configuring on first use.

    When `name` is given it OVERRIDES loguru's auto-detected ``record["name"]``
    (the emitting module) on every record from the returned logger — so it shows
    up as the ``{name}`` field in both sinks and participates in `module_levels`
    prefix matching, mirroring ``logging.getLogger(name)`` semantics. The usual
    ``get_logger(__name__)`` idiom is behavior-identical to the bare logger.
    """
    if _logging_disabled():
        return cast("Logger", NullLogger(name))
    if not LoggingState.configured:
        configure_logging()
    if not name:
        return logger
    return logger.patch(partial(_set_record_name, name))

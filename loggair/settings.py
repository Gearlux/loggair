"""Loggair's configuration as ONE declaration.

:class:`Settings` names every setting once: its type, the ``LOGGAIR_*`` variable
that overrides it, whether a config file may set it, its raw default and the
coercion applied to a value from any source — all in the metadata of one
dataclass field, through :func:`spec`. :func:`resolve` walks those fields, so a
new setting is one field here plus one key in :class:`Overrides`, the typed
face of ``configure_logging(**overrides)``. ``tests/test_settings.py`` pins the
two to each other and pins ``loggair.example.yaml`` and
``docs/configuration.md`` to both.

Before this module one setting was spelled nine times across ``core.py`` (two
signatures, a resolve line, the return dict, a pass-through call, a local
unpack, the explicit-kwargs dict, ...) and a forgotten line failed nothing: the
setting silently vanished from ``get_active_config()`` or silently stopped being
reloadable by ``reconfigure()``.

Resolution is presence-based and layered: an override that is not ``None``,
then the environment variable (empty counts as unset), then the merged config
files, then the default. It is PURE — no directory is created, no sink added,
no env var written — because the ``python -m loggair`` diagnostic calls it
next to live runs.
"""

from __future__ import annotations

import copy
import os
import re
import signal
from dataclasses import dataclass, field, fields
from multiprocessing import current_process
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple, TypedDict, Union

from loguru import logger

from loggair import discovery
from loggair.config import load_config
from loggair.intercept import _VALID_INTERCEPT_MODES, InterceptMode
from loggair.rotation import _VALID_COMPRESSIONS, CompressionFormat

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


# --- Coercions ----------------------------------------------------------------
#: A coercion turns the raw value of a setting (from an argument, the environment
#: or a config file) into its resolved value, or raises ``ValueError`` naming the
#: setting — the second argument — so the message points at the right key.
Coerce = Callable[[Any, str], Any]


def _str_to_bool(value: Any) -> bool:
    """Coerce a config/env value to bool (the truthiness used throughout Loggair)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("true", "1", "t", "y", "yes")
    return bool(value)


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


def as_is(value: Any, name: str) -> Any:
    return value


def as_str(value: Any, name: str) -> str:
    return str(value)


def as_optional_str(value: Any, name: str) -> Optional[str]:
    return None if value is None else str(value)


def as_int(value: Any, name: str) -> int:
    return int(value)


def as_float(value: Any, name: str) -> float:
    return float(value)


def as_bool(value: Any, name: str) -> bool:
    return _str_to_bool(value)


def as_level(value: Any, name: str) -> str:
    """An upper-cased level name, validated against the levels loguru knows."""
    level = str(value).upper()
    _level_no(level, name)
    return level


def as_signal(value: Any, name: str) -> Any:
    """A signal spec, validated eagerly (a typo or a platform-unavailable name
    fails at configure time, even in processes that install no handler) and
    kept as given — the public mapping shows the spelling the user wrote."""
    if value is not None:
        _resolve_signal(value)
    return value


def as_intercept(value: Any, name: str) -> str:
    mode = str(value).lower()
    if mode not in _VALID_INTERCEPT_MODES:
        raise ValueError(f"intercept: invalid value {mode!r}; expected one of {sorted(_VALID_INTERCEPT_MODES)}")
    return mode


def as_compression(value: Any, name: str) -> Optional[str]:
    if value is None:
        return None
    fmt = str(value).lower()
    if fmt not in _VALID_COMPRESSIONS:
        raise ValueError(f"compression: invalid value {fmt!r}; expected one of {sorted(_VALID_COMPRESSIONS)} or None")
    return fmt


def as_csv_list(value: Any, name: str) -> List[str]:
    """A list from a comma-separated string (the environment) or any iterable."""
    if isinstance(value, str):
        return [p.strip() for p in value.split(",") if p.strip()]
    return [str(p) for p in value] if value else []


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


def _resolve_colorize(arg: Optional[bool]) -> Optional[bool]:
    """Resolve the console-sink colorize tri-state.

    Precedence (highest first): explicit ``arg`` (``True``/``False``) > the
    standard ``NO_COLOR`` env var (any non-empty value -> off, per
    https://no-color.org) > ``None`` (loguru auto-detect via ``stream.isatty()``,
    the historical default). ``NO_COLOR`` is the ONLY environment control — there
    is no bespoke Loggair color env var. Non-interactive consumers (an MCP server,
    a GUI extension) set ``NO_COLOR`` for themselves via
    :func:`loggair.force_no_color`.
    """
    if arg is not None:
        return arg
    if os.getenv("NO_COLOR"):
        return False
    return None


# --- The declaration ----------------------------------------------------------
@dataclass(frozen=True)
class Spec:
    """How one setting is resolved: its environment variable (``None`` when the
    environment cannot set it), its raw default — the value before coercion, what
    the documentation shows — whether config files may set it, and its coercion."""

    env: Optional[str]
    default: Any
    coerce: Coerce
    config: bool = True


def spec(
    default: Any, *, env: Optional[str], coerce: Coerce, config: bool = True, secret: bool = False
) -> Mapping[str, Any]:
    """Field metadata of a settable setting: a key of :class:`Overrides`."""
    return {"override": True, "secret": secret, "spec": Spec(env=env, default=default, coerce=coerce, config=config)}


def documented(default: Any, *, env: Optional[str]) -> Mapping[str, Any]:
    """Field metadata of a setting a config file sets but no argument does
    (``module_levels``): documented and pinned like the rest, resolved by hand."""
    return {"override": False, "secret": False, "spec": Spec(env=env, default=default, coerce=as_is, config=True)}


#: Field metadata of a value :func:`resolve` derives; it is neither settable nor documented as a setting.
DERIVED: Mapping[str, Any] = {"derived": True}


@dataclass(frozen=True, kw_only=True)
class Settings:
    """The resolved configuration — what the sinks run with.

    One field per public key, in the order the public mapping always had, so
    ``to_dict()`` is what :func:`loggair.get_active_config` and ``python -m loggair``
    print. There are no defaults: a ``Settings`` is a resolved object, built only by
    :func:`resolve`; the raw defaults live in each field's :class:`Spec`.
    """

    log_dir: str = field(metadata=spec("./logs", env="LOGGAIR_DIR", coerce=as_str))
    script_name: str = field(metadata=spec(None, env="LOGGAIR_SCRIPT_NAME", coerce=as_optional_str))
    log_file: str = field(metadata=DERIVED)
    file_level: str = field(metadata=spec("DEBUG", env="LOGGAIR_FILE_LEVEL", coerce=as_level))
    console_level: str = field(metadata=spec("INFO", env="LOGGAIR_CONSOLE_LEVEL", coerce=as_level))
    module_levels: Dict[str, Dict[str, Any]] = field(metadata=documented({}, env=None))
    rotation_on_startup: bool = field(metadata=spec(True, env="LOGGAIR_ROTATION_ON_STARTUP", coerce=as_bool))
    retention: int = field(metadata=spec(5, env="LOGGAIR_RETENTION", coerce=as_int))
    compression: Optional[CompressionFormat] = field(
        metadata=spec(None, env="LOGGAIR_COMPRESSION", coerce=as_compression)
    )
    rotation: Optional[str] = field(metadata=spec(None, env="LOGGAIR_ROTATION", coerce=as_optional_str))
    enqueue: bool = field(metadata=spec(False, env="LOGGAIR_ENQUEUE", coerce=as_bool))
    # Argument only: NO_COLOR is the one environment control, there is no YAML key,
    # and `serialize` forces it off — both applied in `resolve`, after this loop.
    colorize: Optional[bool] = field(metadata=spec(None, env=None, coerce=as_is, config=False))
    serialize: bool = field(metadata=spec(False, env="LOGGAIR_SERIALIZE", coerce=as_bool))
    console_format: str = field(metadata=spec(DEFAULT_CONSOLE_FORMAT, env="LOGGAIR_CONSOLE_FORMAT", coerce=as_str))
    file_format: str = field(metadata=spec(DEFAULT_FILE_FORMAT, env="LOGGAIR_FILE_FORMAT", coerce=as_str))
    reload_signal: Optional[str] = field(metadata=spec(None, env="LOGGAIR_RELOAD_SIGNAL", coerce=as_signal))
    debug_signal: Optional[str] = field(metadata=spec(None, env="LOGGAIR_DEBUG_SIGNAL", coerce=as_signal))
    alert_level: str = field(metadata=spec("ERROR", env="LOGGAIR_ALERT_LEVEL", coerce=as_level))
    alert_throttle: float = field(metadata=spec(60.0, env="LOGGAIR_ALERT_THROTTLE", coerce=as_float))
    worker_files: bool = field(metadata=spec(False, env="LOGGAIR_WORKER_FILES", coerce=as_bool))
    intercept: InterceptMode = field(metadata=spec("full", env="LOGGAIR_INTERCEPT", coerce=as_intercept))
    intercept_exclude: List[str] = field(metadata=spec(None, env="LOGGAIR_INTERCEPT_EXCLUDE", coerce=as_csv_list))
    capture_warnings: bool = field(metadata=spec(True, env="LOGGAIR_CAPTURE_WARNINGS", coerce=as_bool))
    is_main_process: bool = field(metadata=DERIVED)
    rank: Optional[int] = field(metadata=DERIVED)
    # Last, and secret: the raw URLs carry webhook tokens. Reachable in-process,
    # never in `to_dict()` — diagnostics add their own redacted form.
    alert_urls: List[str] = field(metadata=spec(None, env="LOGGAIR_ALERT_URLS", coerce=as_csv_list, secret=True))

    def to_dict(self) -> Dict[str, Any]:
        """The public, JSON-serializable mapping: every field but the secret ones,
        in declaration order, copied so a caller cannot mutate the live settings."""
        return {f.name: copy.deepcopy(getattr(self, f.name)) for f in fields(self) if not f.metadata.get("secret")}


class Overrides(TypedDict, total=False):
    """What ``configure_logging``, ``reconfigure`` and ``resolve_settings`` accept.

    One key per settable :class:`Settings` field (pinned by
    ``tests/test_settings.py``). A value of ``None`` means "not given": the
    setting falls through to the environment, the config files and its default.
    """

    log_dir: Optional[Union[str, Path]]
    script_name: Optional[str]
    file_level: Optional[str]
    console_level: Optional[str]
    rotation_on_startup: Optional[bool]
    retention: Optional[int]
    compression: Optional[CompressionFormat]
    enqueue: Optional[bool]
    colorize: Optional[bool]
    serialize: Optional[bool]
    rotation: Optional[str]
    console_format: Optional[str]
    file_format: Optional[str]
    reload_signal: Optional[str]
    debug_signal: Optional[str]
    alert_urls: Optional[Union[str, List[str]]]
    alert_level: Optional[str]
    alert_throttle: Optional[float]
    worker_files: Optional[bool]
    intercept: Optional[str]
    intercept_exclude: Optional[Union[str, List[str]]]
    capture_warnings: Optional[bool]


#: The settable settings, in field order — the keys of :class:`Overrides`.
OVERRIDABLE: Tuple[str, ...] = tuple(f.name for f in fields(Settings) if f.metadata.get("override"))
#: Settings whose value never appears in ``to_dict()``.
SECRET = frozenset(f.name for f in fields(Settings) if f.metadata.get("secret"))


def documented_settings() -> Dict[str, Spec]:
    """Every setting a user can set, with how it resolves — what the documentation
    must list (``tests/test_settings.py`` checks the example YAML and the reference
    against this)."""
    return {f.name: f.metadata["spec"] for f in fields(Settings) if "spec" in f.metadata}


@dataclass(frozen=True)
class Compiled:
    """A :class:`Settings` plus what ``configure_logging`` needs beyond it: the
    paths as ``Path`` objects, the parsed ``module_levels`` rules and the level
    numbers of the two sinks. Internal — nothing here is printed."""

    settings: Settings
    log_dir_path: Path
    log_file_path: Path
    worker_suffix: str
    module_rules: List[ModuleLevelRule]
    console_no: int
    file_no: int


def resolve(overrides: Mapping[str, Any]) -> Compiled:
    """Resolve every setting through the full hierarchy — PURELY, no side effects.

    ``overrides`` are :class:`Overrides` entries; an unknown key is refused by
    name with ``TypeError`` before anything else happens, and a ``None`` value
    counts as not given. Each settable field is resolved in declaration order —
    override, then its environment variable, then the merged config files, then
    its raw default — and coerced, which is where an invalid level, intercept
    mode, compression format or signal name raises ``ValueError``. The derived
    fields (the script name, the per-worker suffix and log file, the parsed
    rules, the rank) follow.
    """
    unknown = sorted(set(overrides) - set(OVERRIDABLE))
    if unknown:
        plural = "s" if len(unknown) > 1 else ""
        raise TypeError(
            f"loggair: unknown setting{plural} {', '.join(repr(k) for k in unknown)}; "
            f"valid settings are {', '.join(OVERRIDABLE)}"
        )

    cfg = load_config()
    values: Dict[str, Any] = {}
    for f in fields(Settings):
        if not f.metadata.get("override"):
            continue
        s: Spec = f.metadata["spec"]
        # Presence-based — falsy-but-valid values (rotation_on_startup: false,
        # retention: 0) must win over the default, so never `or`-chain here. An
        # EMPTY env var counts as unset.
        raw = overrides.get(f.name)
        if raw is None:
            env_val = os.getenv(s.env) if s.env else None
            if env_val:
                raw = env_val
            elif s.config and f.name in cfg and cfg[f.name] is not None:
                raw = cfg[f.name]
            else:
                raw = s.default
        values[f.name] = s.coerce(raw, f.name)

    # Derived values, from the resolved ones.
    is_main_proc = current_process().name == "MainProcess" and discovery.get_rank() in (None, 0)
    log_dir_path = Path(values["log_dir"]).expanduser().resolve()
    values["log_dir"] = str(log_dir_path)
    # JSON mode forces colors off: ANSI escapes would otherwise be embedded
    # inside the serialized "text" field, garbling it for log aggregators.
    values["colorize"] = False if values["serialize"] else _resolve_colorize(values["colorize"])
    module_rules = _parse_module_levels(cfg)
    target_name = discovery.determine_script_name(values["script_name"])
    values["script_name"] = target_name

    # Per-worker files: each non-main writer gets its own exclusively-owned
    # file — rank suffix for DDP ranks, process-name suffix for children (a
    # rank's own children carry both, e.g. app.rank2.worker-1.log).
    suffix_parts: List[str] = []
    if values["worker_files"] and not is_main_proc:
        rank = discovery.get_rank()
        if rank not in (None, 0):
            suffix_parts.append(f"rank{rank}")
        if current_process().name != "MainProcess":
            proc = re.sub(r"[^A-Za-z0-9_-]+", "-", current_process().name).strip("-").lower()
            suffix_parts.append(proc or f"pid{os.getpid()}")
    worker_suffix = "".join(f".{p}" for p in suffix_parts)
    log_file_path = log_dir_path / f"{target_name}{worker_suffix}.log"

    settings = Settings(
        **values,
        log_file=str(log_file_path),
        module_levels=dict(cfg.get("module_levels") or {}),
        is_main_process=is_main_proc,
        rank=discovery.get_rank(),
    )
    return Compiled(
        settings=settings,
        log_dir_path=log_dir_path,
        log_file_path=log_file_path,
        worker_suffix=worker_suffix,
        module_rules=module_rules,
        console_no=_level_no(settings.console_level, "console_level"),
        file_no=_level_no(settings.file_level, "file_level"),
    )


__all__ = ["DEFAULT_CONSOLE_FORMAT", "DEFAULT_FILE_FORMAT", "Compiled", "Overrides", "Settings", "Spec", "resolve"]

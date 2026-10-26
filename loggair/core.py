import os
import shutil
import signal
import sys
import threading
import warnings
from functools import partial
from multiprocessing import current_process
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Literal, Mapping, Optional, Unpack, cast

from loguru import logger

if TYPE_CHECKING:
    from loguru import Logger

from loggair import discovery
from loggair.alerts import AlertDispatcher
from loggair.context import clear_context, context_snapshot, get_context
from loggair.intercept import setup_interception, teardown_interception
from loggair.null_logger import NullLogger
from loggair.rotation import _ARCHIVE_RE, _purge_old_files, _rotate
from loggair.settings import ModuleLevelRule, Overrides, Settings, _str_to_bool, resolve
from loggair.signals import _install_signal_handlers


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
    # The resolved settings of the last successful configure, served by
    # :func:`get_active_config` (``to_dict()``) and read by :func:`effective_level_no`.
    settings: Optional[Settings] = None
    # Active alert dispatcher (webhook/alerting sink), if alert_urls are set.
    alert_dispatcher: Optional[AlertDispatcher] = None
    # Runtime signal bookkeeping (see _install_signal_handlers).
    signal_originals: Dict[int, Any] = {}
    last_signal_thread: Optional[threading.Thread] = None
    debug_active: bool = False
    pre_debug_kwargs: Optional[Dict[str, Any]] = None
    # The PARSED `module_levels` rules of the last successful configure. Kept
    # next to `settings` (which holds the raw mapping) so
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
        cls.settings = None
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


# --- Per-logger, per-sink level overrides -----------------------------------


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


def resolve_settings(**overrides: Unpack[Overrides]) -> Settings:
    """Resolve every setting through the full hierarchy — PURELY, no side effects.

    ``overrides`` are the settings of :class:`loggair.Overrides`; a ``None`` counts
    as not given. Resolution is args > ``LOGGAIR_*`` env > local YAML >
    ``pyproject.toml`` > XDG > defaults, by presence, and is shared with
    :func:`configure_logging` and the ``python -m loggair`` diagnostic — so it MUST
    stay free of side effects: no directory, no sink, no env write. Validation
    fails fast here: an unknown level, intercept mode, compression format or
    signal name raises ``ValueError``, an unknown setting name ``TypeError``.

    Returns the frozen :class:`loggair.Settings`; its ``to_dict()`` is the
    JSON-serializable mapping :func:`get_active_config` and the diagnostic print.
    The raw alert URLs are on ``.alert_urls`` and nowhere in that mapping — they
    carry tokens.
    """
    return resolve(overrides).settings


def configure_logging(*, force: bool = False, **overrides: Unpack[Overrides]) -> None:
    """Configure Loggair: resolve every setting, then install sinks, interception and signals.

    Every keyword argument is a key of :class:`loggair.Overrides` — one setting of
    the configuration reference (``docs/configuration.md``) and of
    ``loggair.example.yaml``, which document each one's meaning, environment
    variable and default; its resolved value is the same-named field of
    :class:`loggair.Settings`. This docstring states the contract only.

    Resolution: an argument that is not ``None`` wins, then the ``LOGGAIR_*``
    environment variable, then ``loggair.yaml`` / ``pyproject.toml`` in the working
    directory, then the XDG user config, then the default — by presence, so a
    falsy-but-valid value (``retention=0``) is honoured. :func:`resolve_settings`
    does that without side effects and raises ``ValueError`` for an unknown level,
    intercept mode, compression format or signal name, and ``TypeError`` for an
    unknown setting; everything below happens only after it succeeds.

    Once per process: the first :func:`get_logger` calls this with no arguments,
    and a later call without ``force`` is a no-op. ``force=True`` re-resolves and
    rebuilds the sinks — :func:`reconfigure` and the runtime signals use it, and
    re-applying the previous explicit arguments is their job, not this function's.

    What it does, in order: creates ``log_dir``; rotates an existing log file
    (startup rotation — the shared file by the main process of rank 0, a per-worker
    file by its owner) or pivots from an interim file to a new path; adds the
    console sink (main process of rank 0 only) and the file sink (every process,
    lazily enqueued when ``enqueue`` is requested), plus the alert sink when
    ``alert_urls`` are set; installs stdlib/warnings interception per ``intercept``;
    records the explicit arguments for reloads and the resolved settings for
    :func:`get_active_config`; and, in the main process of rank 0, exports
    ``LOGGAIR_SCRIPT_NAME`` for child processes, installs the signal handlers and
    prunes old archives down to ``retention``.
    """
    _configure(overrides, force=force)


def _configure(overrides: Mapping[str, Any], *, force: bool) -> None:
    """The body of :func:`configure_logging`, taking a plain mapping so that
    :func:`reconfigure` and the runtime signals can forward the recorded kwargs."""
    if LoggingState.configured and not force:
        return

    # 1. Resolve — the PURE shared resolver (also serves `python -m loggair`);
    # an unknown setting is refused here, by name, and side effects start below.
    compiled = resolve(overrides)
    s = compiled.settings
    is_main_proc = s.is_main_process
    log_dir_path = compiled.log_dir_path
    new_log_file = compiled.log_file_path
    worker_suffix = compiled.worker_suffix
    module_rules = compiled.module_rules
    c_no, f_no = compiled.console_no, compiled.file_no

    log_dir_path.mkdir(parents=True, exist_ok=True)

    # 2. PIVOT & ROTATION
    if is_main_proc:
        curr = LoggingState.log_file
        if curr and new_log_file.resolve() != curr.resolve():
            _perform_pivot(curr, new_log_file, s.rotation_on_startup, s.retention, s.compression)
        elif s.rotation_on_startup and not LoggingState.configured and new_log_file.exists():
            _rotate(new_log_file, s.retention, s.compression)
    elif worker_suffix and s.rotation_on_startup and not LoggingState.configured and new_log_file.exists():
        # This process exclusively owns its per-worker file, so rotating it is
        # race-free even off the main rank (force_owner bypasses the rank gate).
        _rotate(new_log_file, s.retention, s.compression, force_owner=True)

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
                format=s.console_format,
                filter=_make_sink_filter("console", c_no, module_rules),
                # Tri-state (resolved above): True forces colors, False forces
                # them off (the standard NO_COLOR env var, or colorize=False),
                # None lets loguru auto-detect via _colorama.should_colorize() ->
                # stream.isatty() (plus Jupyter / CI / PyCharm heuristics). So a
                # redirected/piped stderr (2> run.log, | tee, plain Jenkins
                # console) stays plain, and NO_COLOR forces plain when
                # auto-detection is wrong. serialize forces False (see above).
                colorize=s.colorize,
                serialize=s.serialize,
            )

        # Lazy-enqueue: when the user asks for ``enqueue=True``, defer the
        # multiprocessing queue (and its POSIX semaphore) until something
        # actually forks/spawns a child. Children re-running configure_logging
        # detect themselves via current_process().name and start eagerly.
        eager_env = os.getenv("LOGGAIR_EAGER_ENQUEUE")
        force_eager = eager_env is not None and _str_to_bool(eager_env)
        in_child = current_process().name != "MainProcess"
        start_with_enqueue = s.enqueue and (force_eager or in_child)
        file_sink_params: dict = dict(
            sink=str(new_log_file),
            # See _sink_floor. Living inside file_sink_params means the
            # lazy-enqueue re-add in _upgrade_to_enqueue carries it unchanged.
            level=_sink_floor("file", f_no, module_rules),
            format=s.file_format,
            filter=_make_sink_filter("file", f_no, module_rules),
            mode="a",
            enqueue=start_with_enqueue,
            serialize=s.serialize,
        )
        # Runtime rotation: a single rotator per FILE. Without worker_files
        # that means the main process only (children share its file); WITH
        # worker_files every process exclusively owns its file, so each may
        # rotate its own. Loguru's sink-level retention/compression then fire
        # at rotation time (proven per-stem and live-log-safe), so a service
        # that never restarts still gets pruning and compressed archives.
        # Loguru validates the rotation string at add() — fail fast right here.
        if s.rotation is not None and (is_main_proc or worker_suffix):
            file_sink_params["rotation"] = s.rotation
            file_sink_params["retention"] = s.retention
            file_sink_params["compression"] = s.compression
        sink_id = logger.add(**file_sink_params)

        LoggingState.file_sink_id = sink_id
        LoggingState.file_sink_params = file_sink_params
        LoggingState.enqueue_active = start_with_enqueue
        LoggingState.enqueue_requested = bool(s.enqueue)

        # Alerting sink: enqueue-only in the logging path, delivery in the
        # dispatcher's worker thread (zero-blocking mandate). The filter drops
        # the dispatcher's own delivery-failure records (no feedback loop).
        if s.alert_urls:
            dispatcher = AlertDispatcher(s.alert_urls, script_name=s.script_name, throttle=s.alert_throttle)

            def _alert_sink(message: Any) -> None:
                record = message.record
                dispatcher.submit(str(message), record["level"].name, record["level"].no)

            logger.add(
                _alert_sink,
                level=s.alert_level,
                format=s.file_format,
                filter=lambda r: not r["extra"].get("loggair_alert_internal"),
                catch=True,
            )
            LoggingState.alert_dispatcher = dispatcher

        if s.enqueue and not start_with_enqueue:
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
        mode=s.intercept,
        exclude=s.intercept_exclude,
        capture_warnings=s.capture_warnings,
    )

    # Remember what was passed, for reconfigure / signal reloads, and the resolved
    # settings for get_active_config (the raw alert URLs stay on it, secret).
    LoggingState.last_kwargs = {k: v for k, v in overrides.items() if v is not None}
    LoggingState.settings = s

    if is_main_proc:
        os.environ["LOGGAIR_SCRIPT_NAME"] = s.script_name
        _install_signal_handlers(s.reload_signal, s.debug_signal)

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
            _purge_old_files(stem_files, s.retention)

        logger.info(f"Loggair {'Re-' if was_cfg else ''}initialized: {new_log_file.name}")


def reconfigure(**overrides: Unpack[Overrides]) -> None:
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
    _configure({**LoggingState.last_kwargs, **overrides}, force=True)


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

    Keys: every field of :class:`loggair.Settings` (``Settings.to_dict()``) plus
    ``configured``, ``enqueue_active`` (the lazily-materialized queue, against the
    requested ``enqueue``), ``debug_mode_active`` (the debug-signal toggle),
    ``alert_urls`` (privacy-redacted — the raw ones carry tokens) and
    ``experiment_context`` (the live :func:`get_context` values).

    Returns a fresh copy — mutating it does not affect Loggair. Reflects the
    state as of the last (re)configure; ``enqueue_active`` in particular may
    lag a lazy enqueue upgrade until the next reconfigure.
    """
    settings = LoggingState.settings
    if not LoggingState.configured or settings is None:
        return {"configured": False}
    snapshot = settings.to_dict()
    # Live state that can change after configure without a re-snapshot; the
    # alert URLs come from the dispatcher in apprise's privacy-redacted form.
    snapshot.update(
        configured=True,
        enqueue_active=LoggingState.enqueue_active,
        debug_mode_active=LoggingState.debug_active,
        alert_urls=(LoggingState.alert_dispatcher.redacted_urls if LoggingState.alert_dispatcher else []),
    )
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
    settings = LoggingState.settings
    if _logging_disabled() or not LoggingState.configured or settings is None:
        return NOTHING_REACHABLE

    rule = _matching_rule(name or "", LoggingState.module_rules)

    thresholds: List[int] = []
    # The console sink exists on the main process/rank only (see configure_logging).
    if settings.is_main_process:
        console = rule.console_no if rule is not None and rule.console_no is not None else None
        thresholds.append(console if console is not None else int(logger.level(settings.console_level).no))
    file_no = rule.file_no if rule is not None and rule.file_no is not None else None
    thresholds.append(file_no if file_no is not None else int(logger.level(settings.file_level).no))
    # The alerting sink is a third destination; its presence is read from the live
    # dispatcher, which exists exactly when `settings.alert_urls` is non-empty.
    if LoggingState.alert_dispatcher is not None:
        thresholds.append(int(logger.level(settings.alert_level).no))

    return min(thresholds)


def _set_record_name(name: str, record: Any) -> None:
    """Patcher installed by :func:`get_logger` — see there. Module-level (used via
    ``functools.partial``) so a named logger stays picklable for spawn workers."""
    record["name"] = name


def _named_logger(name: Optional[str]) -> "Logger":
    """The configured logger, its records named ``name`` when one is given."""
    if not name:
        return logger
    return logger.patch(partial(_set_record_name, name))


class _DeferredLogger:
    """What :func:`get_logger` hands out before Loggair is configured: it configures Loggair the first time it is
    USED and from then on passes every attribute to the logger it stands for.

    A library calls ``get_logger(__name__)`` at module scope; a program that only imports it, and logs nothing, must
    not print ``Loggair initialized``, open a log file or take over the stdlib's logging. The attribute is handed
    back to the caller, which calls it itself, so loguru sees the caller's frame: module, function and line are the
    caller's. It pickles as the ``get_logger`` call that made it, so a spawn worker gets a working logger. An attribute
    set on it (a test's ``monkeypatch.setattr(module.logger, "warning", ...)``) is its own and wins, as it would on a
    logger.
    """

    def __init__(self, name: Optional[str]) -> None:
        self._name = name
        self._logger: Optional["Logger"] = None

    def _started(self) -> "Logger":
        if not LoggingState.configured:
            configure_logging()
        if self._logger is None:
            self._logger = _named_logger(self._name)
        return self._logger

    def __getattr__(self, attribute: str) -> Any:
        if attribute.startswith("__"):  # copy/pickle probes must not start Loggair
            raise AttributeError(attribute)
        return getattr(self._started(), attribute)

    def __reduce__(self) -> Any:
        return (get_logger, (self._name,))


def get_logger(name: Optional[str] = None) -> "Logger":
    """Return the Loggair logger; Loggair configures itself on the logger's first USE, never on this call alone.

    Before configuration the logger handed out is a stand-in (:class:`_DeferredLogger`) that configures on its first
    message, so importing a library that asks for its logger prints and writes nothing, and a
    ``configure_logging(...)`` made after the imports and before the first message is the one applied. Once
    configured, the logger itself.

    When `name` is given it OVERRIDES loguru's auto-detected ``record["name"]``
    (the emitting module) on every record from the returned logger — so it shows
    up as the ``{name}`` field in both sinks and participates in `module_levels`
    prefix matching, mirroring ``logging.getLogger(name)`` semantics. The usual
    ``get_logger(__name__)`` idiom is behavior-identical to the bare logger.
    """
    if _logging_disabled():
        return cast("Logger", NullLogger(name))
    if not LoggingState.configured:
        return cast("Logger", _DeferredLogger(name))
    return _named_logger(name)

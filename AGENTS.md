# Loggair Mandates

Rules only. The reasons are in [`docs/architecture.md`](docs/architecture.md) (cited by record
title) and the code docstrings; usage is in the [README](README.md) (cited as README §N). Root
`AGENTS.md` rules are not repeated here.

## Current state

Loguru-backed logging engine for HPC and ML runs: rank-aware sinks, a config hierarchy,
stdlib/framework interception, call tracing. Feature-complete. `0.2.0` on PyPI, released by tag
from the public repo `Gearlux/loggair`. Python >= 3.12. Hard dependencies are `loguru` and
`pyyaml` only; `apprise` is the optional `[alerts]` extra; Confluid is never a dependency.

Modules in `loggair/`: `core.py` (state, `resolve_settings()`, `configure_logging` and friends,
sinks and filters, `get_logger`, `effective_level_no`), `rotation.py`, `signals.py`,
`intercept.py`, `context.py`, `alerts.py`, `discovery.py` (rank, script name), `config.py`
(config-file layers), `null_logger.py`, `track.py` (`@track` / `@spy`, level `TRAIL`) and
`__main__.py` (the read-only diagnostic: `python -m loggair`, also the `loggair` console script).

Every config key is in `loggair.example.yaml` and README §1-§19. In this workspace
`~/.config/loggair/config.yaml` sets `log_dir: ~/logs`: debug a local run from `~/logs`, not
`./logs`.

## Sinks and processes

- Only rank 0 of the main process gets a console sink. The file sink takes every rank and tags
  non-zero ones `[rank N]`. (`tests/test_core.py::test_configure_rank_non_zero`; the console
  side is unpinned.)
- Never block the caller: the alert sink only enqueues, and multi-process file writes go
  through loguru's `enqueue=True`. Add no network I/O or per-record locking to the record path.
  `enqueue` defaults to off.
- Lazy enqueue: `enqueue=True` allocates the queue only when a child is about to fork or spawn
  (an `os.register_at_fork` hook plus a one-shot `BaseProcess.__init__` patch);
  `LOGGAIR_EAGER_ENQUEUE=true` allocates it up front. (`tests/test_lazy_enqueue.py`)
- Sink floors are computed, never a hardcoded `"TRACE"`: the minimum of the sink's global level
  and EVERY `module_levels` override for it, `workers_only` rules included (forked children
  inherit the floor). The floor lives in `file_sink_params` so the lazy-enqueue re-add keeps it.
  Keep `tests/test_core.py::test_sink_floor_admits_a_workers_only_promotion_in_a_forked_child`:
  the narrowed variant passed every other test. Record: "Sink level floors are computed".
- `worker_files=True` gives each non-main writer its own file (`{name}.rank{N}.log`,
  `{name}.{procname}.log`, both suffixes for a rank's child). Exclusive ownership is what makes
  rotation safe there: keep `force_owner=True` startup rotation and runtime rotation. Merge at
  read time (`lnav ./logs`); never build a merged-write path. Default off.
  (`tests/test_worker_files.py`)
- Kill-switch: `get_logger()` returns `NullLogger` when `LOGGAIR_DISABLE_LOGGING` is truthy, or
  `LOGGAIR_DISABLE_MULTIPROCESS_LOGGING` is truthy in a non-`MainProcess`. The gate sits BEFORE
  the lazy `configure_logging()`, so a disabled run creates no file, sink or queue. `NullLogger`
  answers any unforeseen call with a no-op but re-raises `AttributeError` for dunders, so it
  stays picklable. (`tests/test_disable_and_compression.py`)

## Configuration

- Hierarchy: args > `LOGGAIR_*` env > local `loggair.yaml` > local `pyproject.toml` > XDG
  `config.yaml` > defaults. Resolve by presence (`is not None`, `key in cfg`), never an `or`
  chain: `rotation_on_startup: false` and `retention: 0` must win. An empty env var is unset.
  Files merge SHALLOWLY: a local `module_levels:` replaces the global one.
  (`tests/test_core.py::test_yaml_retention_zero_is_honored`)
- `core.resolve_settings()` is the ONE resolver and stays PURE: no mkdir, rotation, sink,
  interception, signal or env write (`configure_logging` does those afterwards), because
  `python -m loggair` calls it next to live runs and stays read-only.
  (`tests/test_diagnostic.py::test_resolve_settings_is_side_effect_free`,
  `::test_main_reports_without_configuring_or_rotating`)
  - Public keys are the `get_active_config()` vocabulary (the snapshot derives from them);
    `_`-prefixed keys are internal. `_alert_urls` is raw and is never printed: the diagnostic
    masks everything after the scheme. A resolution `ValueError` is reported with exit 1.
  - Rank detection is `discovery.detect_rank()`; `get_rank()` caches it. Never copy its list.
  - The shadow guard at the top of `__main__.py` REMOVES the `sys.path` entry that shadows the
    package (a workspace root's empty `loggair/` folder) and re-imports the real one: a scoped
    exception to the no-`sys.path`-hacks rule (removal, diagnostic process only). Never copy it
    into library code. (`tests/test_diagnostic.py::test_python_dash_m_self_heals_package_shadowing`)
- `get_active_config()` tracks every knob: a new `configure_logging` knob goes into
  `resolve_settings()`'s public keys in the same change. It stays JSON-serializable and
  read-only: `{"configured": False}` before configuration, never a lazy configure. Live flags
  (`enqueue_active`, `debug_mode_active`) are read from `LoggingState` at call time.
  (`tests/test_diagnostic.py::test_resolve_settings_matches_active_config`)
- `reconfigure(**overrides)` and the signal reload re-apply the last explicit
  `configure_logging` arguments (`LoggingState.last_kwargs`) under the overrides; otherwise
  `reconfigure(colorize=False)` re-resolves `log_dir` and moves the live log. A setting pinned by
  an argument is therefore not signal-reloadable. (`tests/test_core.py::test_reconfigure_preserves_explicit_args`)
- The config loader (`config.py`) stays bespoke; `loguru-config` was rejected. Record: "The
  configuration loader is bespoke".
- `module_levels` sits at the `file_level` / `console_level` precedence layer: dotted-prefix
  keys, longest prefix wins, and unknown sub-keys or invalid levels raise `ValueError`. No
  silent ignore. (`tests/test_core.py::test_module_levels_unknown_subkey_raises`)
- `DEFAULT_CONSOLE_FORMAT` / `DEFAULT_FILE_FORMAT` are public and overridable through the whole
  hierarchy; never re-hardcode a format in `configure_logging`. Both sink filters stamp
  `extra["rank_tag"]` unconditionally. A nonexistent format field makes loguru print a
  per-record error and drop the record (bad color markup fails at `add()`): document that, and do
  not pre-render formats to force fail-fast. (`tests/test_core.py::test_custom_format_keeps_rank_tag_available`)
- `get_logger(name)` applies `name` with `logger.patch(partial(_set_record_name, name))`, a
  module-level function (never a lambda: it must pickle for spawn). Never `bind(name=…)`: `bind`
  writes `extra`, which neither the formats nor the filters read.
  (`tests/test_core.py::test_get_logger_name_participates_in_module_levels`)

## Rotation and retention

Record: "Rotation: Loggair renames at startup, loguru rotates at runtime, and a live log is
never purged".

- Startup: archive `{name}.log` with a timestamp; only the main rank/process rotates the shared
  file. (`tests/test_coverage_gap.py::test_rotate_non_zero_rank`)
- Runtime rotation is loguru's `rotation=`, passed verbatim to the FILE sink. Never reimplement
  size/time parsing. On the shared file apply it only when `is_main_proc`; with `worker_files`
  each process rotates its own. While it is active, also pass `retention=` and `compression=`
  to the sink. (`tests/test_core.py::test_runtime_rotation_only_in_main_process`)
- Retention counts timestamped archives per stem (`_ARCHIVE_RE`, compressed or plain) in BOTH
  purges (`_rotate` and the startup sweep). `_TIMESTAMP_RE` must keep both naming schemes
  (startup, and runtime with the `_ffffff` suffix).
- A bare `{name}.log` of another script is NEVER a purge candidate: in a shared dir it may be a
  running process's live sink. (`tests/test_forensics.py::test_sweep_never_deletes_other_scripts_live_logs`)
- A purge tolerates an archive vanishing before `stat()`/`unlink()`: skip silently, never warn,
  still enforce `retention` on the rest (the sanctioned try/except exception to check-first).
  (`tests/test_disable_and_compression.py::test_purge_tolerates_*`)
- Startup archives are compressed by `_compress_file` after the rename, because loguru's
  sink-level `compression=` never fires on that path. Compression is best-effort: on failure
  keep the original. Both purges must recognise the suffix (`_COMPRESSION_RE`).

## Interception, color, JSON

- stdlib `logging` (what TensorFlow, PyTorch and JAX log through) and `warnings` are routed into
  Loggair. The `InterceptHandler` frame walk starts at frame 0 and walks up, never a hardcoded
  `sys._getframe(N)`. (`tests/test_intercept.py::test_intercept_emit_called_directly_shallow_stack`)
- How invasive that is stays a knob: `intercept` (`full` | `coexist` | `off`), `intercept_exclude`
  prefixes (hands-off entirely, children included) and `capture_warnings`. Never hardcode
  interception behavior that ignores them. (`tests/test_intercept.py`)
- The chatty-logger defaults (`_DEFAULT_THIRD_PARTY_LEVELS`) apply at the stdlib layer, but a
  `module_levels` rule touching one of those loggers lifts the gate to `NOTSET`. Never
  hard-silence a logger the user tuned.
  (`tests/test_intercept.py::test_third_party_default_yields_to_module_levels_rule`)
- Console color is a tri-state from `_resolve_colorize`: the `colorize=` argument, then a
  non-empty `NO_COLOR`, then `None` (loguru's TTY auto-detect). `NO_COLOR` is the ONLY env knob
  (no `LOGGAIR_COLORIZE`, no YAML `colorize:`), and the console sink gets the RESOLVED value,
  never a literal. A second `configure_logging` without `force` is a no-op, so change color
  after the fact with `reconfigure(colorize=…)`. (`tests/test_core.py::test_resolve_colorize_precedence`)
- Non-interactive consumers (an MCP server, a GUI extension; navigaitor and streamstudio) call
  `loggair.force_no_color()` at import, not a bare `os.environ.setdefault`: the env var alone is
  too late once the sink exists, so it also calls `reconfigure()`.
  (`tests/test_core.py::test_force_no_color_reloads_when_already_configured`)
- JSON output is loguru's `serialize=True` on BOTH sinks, never a hand-rolled `format=`. It
  forces console `colorize` off, since loguru colorizes before serializing.
  (`tests/test_core.py::test_serialize_console_sink_json_without_ansi_even_on_tty`)

## Context, alerts, signals

- Experiment context (`loggair.context`) is one process-global dict, deliberately not
  `contextvars` (a saver thread must carry the epoch the training thread set). Writers publish a
  new `(dict, prebuilt tag)` pair under the lock; the per-record read (`context_snapshot()`) is
  lock-free. Never mutate the published dict, lock per record, or format the tag per record.
  The filter stamps `extra["context_tag"]` unconditionally and copies fields in with
  `setdefault`, so an explicit `bind()` wins. (`tests/test_context.py`)
- Alerts (`alert_urls` / `alert_level` / `alert_throttle`) go through apprise. `logprise` was
  evaluated and rejected (reasons in the `alerts.py` docstring): do not re-propose it.
  - The loguru sink only enqueues; one daemon worker batches and delivers.
  - apprise is the optional `[alerts]` extra, imported lazily with a `pip install loggair[alerts]`
    error. No per-platform HTTP code. An invalid URL fails at configure time.
  - A delivery failure is logged with `loggair_alert_internal` bound and the alert sink's filter
    drops it: a failing webhook never alerts about itself.
  - `get_active_config()` shows only privacy-redacted URLs; raw ones carry tokens.
  - `flush()` clears `_wake` after draining and the worker holds BEFORE delivering; do not
    revert to wait-after-deliver (it delivers early). (`tests/test_alerts.py`)
- The `reload_signal` / `debug_signal` handlers (main process, main thread) only spawn a worker
  thread (`_apply_signal_action`) and return. Never touch loguru in a signal handler: its locks
  are non-reentrant and the handler can interrupt an in-flight `emit`. The thread is stored on
  `LoggingState.last_signal_thread` so tests `join()` it. Do not add a config-file watcher
  (rejected 2026-07-04). Record: "Runtime control is by signal, never by polling".
  (`tests/test_core.py::test_debug_signal_toggles_debug_mode`)

## Call tracing (`@track` / `@spy`)

Pins (`::name`) are in `tests/test_track.py`.

- `TRAIL` is severity 7 (above `TRACE`), registered when `loggair` is imported: `__init__.py`
  imports `loggair.track` for that side effect, so `file_level: TRAIL` resolves. `NullLogger`
  carries it too. Moving the number is a product decision: ask the user.
  (`::test_trail_level_is_registered_at_import_with_number_seven`) Record: "The `TRAIL` level is
  severity 7".
- The wrapper gates on `core.effective_level_no` BEFORE touching loguru, cached against
  `LoggingState.generation`. Never resolve per call, never `opt(lazy=True)`. `_matching_rule` is
  shared by it and `_make_sink_filter`: do not copy the prefix loop.
  (`::test_the_guard_short_circuits_before_rendering_anything`) Record: "The tracing decorators
  gate themselves".
- A trace line locates ONE place: `name`/`function`/`line`/`file` come from
  `inspect.unwrap(func)`, and the caller goes in the message via `sys._getframe`, never
  `inspect.stack()`. (`::test_the_location_sees_through_another_decorator`)
- Confluid is optional and lazily imported, forever (it depends on Loggair). `@spy` renders
  configurables, and lists or dicts holding them (`_project`), as their document, else `repr`.
  Probe the dump FORMAT once (`_FLOW_SUPPORTED`), never the version (`CONFLUID_FLOW_FLOOR` is only
  named in the warning). Do not widen it to arbitrary values. Tests touching Confluid use
  `pytest.importorskip`. (`::test_an_old_confluid_still_renders_the_document_not_a_repr`)
- Class decoration modifies the class in place; `inherited=` is opt-in. Record: "Class decoration
  modifies the class in place, and inherited tracing is bounded".
  - Wrappers go on the DECORATED class, never on a base; a name the class defines wins.
    (`::test_inherited_never_mutates_the_base_class`)
  - A plain method is a `_TracedMethod` descriptor, so class-level access returns the original
    (torch decides `get_extra_state` by raw identity); keep `functools.wraps`.
    (`::test_wrapping_an_inherited_method_does_not_read_as_an_override`,
    `::test_torch_state_dict_survives_inherited_tracing`)
  - `"source"` (`_has_source`) filters the WHOLE MRO and never stops at the first installed base.
    Take the interpreter's roots from `sysconfig`, not from path shapes.
    (`::test_source_scope_is_a_filter_not_a_stop_at_the_first_installed_base`)
  - Decorating an `async` or generator function raises `TypeError`; class decoration skips them
    with one `DEBUG` line. `inherited=` on a function raises *(unpinned)*.
    (`::test_decorating_an_async_function_raises_with_a_located_message`)

## Packaging, release, tests

- Distribution and import package are both `loggair` (settled 2026-07-05; do not re-litigate).
- `py.typed` ships through `[tool.setuptools.package-data]`; `MANIFEST.in` ships
  `loggair.example.yaml` and `CHANGELOG.md` in the sdist.
- Metadata is PEP 639: `license = "MIT"` as a plain SPDX string (build floor `setuptools>=77`)
  and no `License ::` classifier; either one brings back the build deprecation warnings.
- Validate a packaging change with `python -m build` (expect zero deprecation warnings) and
  `twine check --strict dist/*`; both are in the `dev` extra.
- Release by tag: bump `version`, retitle the CHANGELOG `[Unreleased]` section, then
  `git tag v<version> && git push origin v<version>`. `.github/workflows/release.yml` is
  hand-written and aisland never regenerates it; it guards tag == `pyproject` version, builds,
  runs `twine check`, smoke-tests the wheel and publishes by PyPI Trusted Publishing.
- CI runs `mypy .` with only Loggair's own extras, so `confluid.*` stays in
  `[[tool.mypy.overrides]]` as `ignore_missing_imports`.
- Tests mock `HOME` and `XDG_CONFIG_HOME`. The autouse `conftest.py` fixture calls the public
  `reset_logging()` (the full teardown: it also restores `warnings.showwarning`, the stdlib root
  handler and the `BaseProcess.__init__` patch), not just `LoggingState.reset()`.

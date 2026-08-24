# Changelog

All notable changes to Loggair are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-08-24

### Added
- **Call tracing — `@track` and `@spy`.** Two decorators that log when a
  function is entered and left; `@spy` also logs the arguments it was called
  with. Both accept `level=` and `timing=`, `@spy` additionally `cap=` (bound a
  long rendering) and `multiline=`. Applied to a class, they trace the methods
  that class defines itself — static- and classmethods included, inherited
  methods, private names, other dunders and properties excluded, the bound
  `self`/`cls` dropped from the parameters, and the class object modified in
  place so registration, `isinstance` and pickling are unaffected. Decorating an
  `async def` or generator function raises `TypeError` with the offending
  `file:line`, because a synchronous wrapper would time only the call that
  creates the coroutine/generator. Values render with their full `repr`;
  where Confluid is installed, objects it marks as configurable — and lists or
  dicts containing them, which is how most real slots are shaped — render as their
  configuration document instead, with anything Confluid cannot describe replaced
  by its `repr` first so the dumper's own warning never reaches the log. Confluid
  stays entirely optional. Where the installed Confluid emits a document
  `yaml.safe_load` refuses — the `!class:X()` tags every release up to v0.2.0
  produces — the indented block is rendered instead of the single line, so the class
  name and values survive rather than collapsing to a `repr`; a single warning per
  process names the installed version and where plain-YAML dumps arrived. The probe
  is of the round trip itself, not of the version number, since a fork or backport
  can pair old behaviour with new metadata.
  Each trace line locates the **decorated function** (`name`/`function`/`line`/`file`
  together, read through `inspect.unwrap` so a decorator underneath cannot supply a
  line number from its own file) and names the **caller** separately in a
  `[from module:function:line]` suffix on the entry line.
  `inherited=` extends class decoration to base classes, bounded by where the code
  lives rather than by MRO depth: `"own"` (the `False` default), `"package"`,
  `"source"`, `"all"` (`True`), plus a base class as an inclusive MRO boundary or a
  callable predicate. Measured on a real trainer that defines one traceable method
  and inherits 167, the scopes select 1 / 6 / 32 / 168 — the 136 excluded by
  `"source"` belong to `pytorch_lightning` and `torch.nn`, and would put a trace on
  `nn.Module.forward` for every batch. A base counts as `"source"` when its module's
  file is not under `sysconfig`'s interpreter-owned roots, which reads correctly
  even for the common layout where the venv sits inside the source tree; a class
  with no file is never source. The scope filters the whole MRO rather than stopping
  at the first installed base, because linearization interleaves them. Wrappers are
  always installed on the decorated class, never on the base, so decorating one
  subclass cannot trace its siblings, and they are installed as a descriptor that
  returns the original for class-level lookups so tracing never reads as an override.
  That last point is load-bearing: PyTorch detects an implemented `get_extra_state`
  by raw identity against `Module.get_extra_state`, so a plainly-installed wrapper
  made `state_dict()` raise at the first checkpoint save. Instance calls are still
  traced and `functools.wraps` is preserved, so `__code__`-comparison checks such as
  `lightning_utilities`' `is_overridden` stay correct; the cost is that a class-level
  call runs untraced.
- **`TRAIL` log level (severity 7).** Registered when `loggair` is imported, so
  a config file may carry `file_level: TRAIL` without importing anything extra.
  Sits between `TRACE` (5) and `DEBUG` (10), because call tracing is noisier than
  tracing; a sink at `TRACE` therefore shows call tracing too. Composes with the
  whole existing surface — `console_level`, `LOGGAIR_FILE_LEVEL`,
  `module_levels` — unchanged.
- **`loggair.core.effective_level_no(name=None)`** — the lowest level number
  that can still reach a sink for a given logger, so callers can skip building a
  record that would be discarded. More precise than loguru's own check, which is
  per-*sink* where this is per-*logger*: a `module_levels` rule promoting one
  logger lowers the whole sink's floor, so records from every other logger clear
  it and are built in full before the filter drops them (measured: 3.4 µs).
  `logger.opt(lazy=True)` does not help — loguru evaluates lazy arguments after
  the level check the record already passed. The decorators gate on it, which
  keeps a switched-off `@track` at 0.084 µs per call instead of 0.687 µs.
- **CLI diagnostic** — `python -m loggair` (`--json` for machine output)
  prints the package version, the detected distributed rank and which
  environment source decided it, which config files were found, the merged
  file config, and the fully resolved settings. Strictly read-only: it never
  creates the log directory, rotates, configures sinks, or writes env vars —
  safe next to a live run; alert URLs are fully masked. Invalid configuration
  is reported with context and exit code 1. Built on the new pure
  `loggair.core.resolve_settings()` (configuration resolution extracted from
  `configure_logging` into a single shared, side-effect-free source of truth)
  and `loggair.discovery.detect_rank()` (rank + deciding source). Also
  installed as the `loggair` console script, which is immune to mono-repo
  package shadowing; the `-m` form detects a shadowing local `loggair/`
  directory and SELF-HEALS (drops the offending path entry for its own
  process, re-imports the real package, reports with a warning and a
  `shadowed_path_ignored` JSON field); exit 2 only when no installation is
  reachable at all.

### Changed
- **Sink level floors are computed instead of pinned open, making every disabled
  log call ~32× cheaper.** Both sinks were added with a hardcoded
  `level="TRACE"`, which pinned loguru's `core.min_level` to 5 so its one cheap
  early-out never fired: every below-threshold record was fully built — frame
  inspection, timestamp, record dict, argument formatting — and only then
  discarded by the per-sink filter. Each sink now takes a floor of
  `min(its global level, every module_levels override for that sink)`, which by
  construction cannot exclude a record the filter would have accepted. Measured
  on a process configured at `INFO`: a dropped `logger.debug()` goes from
  **3.4 µs to 0.10 µs**. Promotion via `module_levels` is unaffected, including
  `workers_only` promotions in forked children, which inherit the parent's
  handler objects and therefore its floor. One consequence worth knowing: levels
  below `TRACE` are now reachable, where before they were silently dropped.

### Fixed
- **An invalid log level is now reported against the setting it came from.**
  Every level-resolution error carried a hardcoded `module_levels:` prefix
  whatever the caller was, so a typo in `file_level` reported
  `module_levels: invalid level 'X' for file_level` and sent the reader to the
  wrong block of their configuration. The message now names only the setting:
  `invalid level 'X' for file_level`, and rule-scoped errors keep their full
  path (`invalid level 'X' for module_levels['pkg.a'].console`).
- **Retention purge no longer crashes when another process purges the same
  stem concurrently.** In a shared log directory, an archive could vanish
  between the directory listing and the `stat()`/`unlink()` inside
  `_purge_old_files`, raising `FileNotFoundError` out of `configure_logging`.
  Vanished candidates are now skipped silently (someone else's purge simply
  finished first); retention is still enforced on the surviving archives.

## [0.1.0] - 2026-07-05

Initial public release. Loggair is the continuation of the (unpublished)
Loggair project under a clean name where the PyPI distribution and the import
package are both `loggair`.

### Added
- **Multiprocess- and thread-safe logging engine** built on
  [Loguru](https://github.com/Delgan/loguru), with rank-aware console
  filtering (PyTorch DDP / `torchrun`, SLURM, MPI): console output restricted
  to rank 0, the file sink captures every rank with a `[rank N]` tag.
- **Lazy-enqueue file sink** — `enqueue=True` defers the multiprocessing queue
  (and its POSIX semaphore) until the process actually forks/spawns a child;
  single-process runs pay no cost. `LOGGAIR_EAGER_ENQUEUE=true` forces eager
  allocation.
- **Hierarchical configuration** — function args > `LOGGAIR_*` env vars >
  local `loggair.yaml`/`.yml` > `pyproject.toml` `[tool.loggair]` > XDG
  `~/.config/loggair/config.yaml` > defaults. Presence-based resolution
  (falsy-but-valid values like `retention: 0` are honored).
- **Startup rotation** with timestamped archives, per-stem `retention`
  (live-log-safe: a bare `{name}.log` of another script is never purged),
  and optional `compression: gz|zip`.
- **Runtime rotation** (`rotation: "100 MB"` / `"daily"` / `"00:00"` ...)
  for long-running processes; retention and compression enforced at rotation
  time; one rotator per file.
- **Per-worker log files** (`worker_files: true`) — every rank/worker owns
  its file (`app.rank2.log`, `app.worker-1.log`), unlocking full rotation
  everywhere; merge at read time with lnav.
- **Structured / JSON logging** (`serialize: true`) — loguru-native
  one-JSON-object-per-line output for Elasticsearch/Kibana, Loki, Datadog.
- **Framework interception** with configurable modes — `full` (own the
  root), `coexist` (embed next to uvicorn/gunicorn), `off`; plus
  `intercept_exclude` prefixes and a `capture_warnings` switch. Built-in
  third-party silences (asyncio/httpx/urllib3/...) yield to `module_levels`.
- **Per-logger, per-sink level overrides** (`module_levels`) with
  longest-prefix matching, per-sink thresholds, and `workers_only` scoping;
  fail-fast validation.
- **Configurable sink formats** (`console_format` / `file_format`), with the
  defaults exported as `DEFAULT_CONSOLE_FORMAT` / `DEFAULT_FILE_FORMAT` for
  extension.
- **Dynamic level adjustment at runtime** — `reconfigure(...)`
  programmatically, or POSIX signals: `reload_signal` re-reads env+config
  files live, `debug_signal` toggles DEBUG on/off.
- **Experiment context injection** — `set_context(epoch=3, step=1200)`
  stamps every record in the process (intercepted third-party logs included)
  as a compact tag and as structured JSON fields.
- **Webhook & alerting sinks** (`alert_urls`, optional `[alerts]` extra) —
  route ERROR+/chosen-level records to Slack/Teams/email/webhooks via
  [Apprise](https://github.com/caronc/apprise); batched, throttled,
  zero-blocking, secret-redacting.
- **Config introspection** — `get_active_config()` returns the fully
  resolved, JSON-serializable settings in effect.
- **Runtime kill-switch** — `LOGGAIR_DISABLE_LOGGING` /
  `LOGGAIR_DISABLE_MULTIPROCESS_LOGGING` hand out a zero-overhead
  `NullLogger`.
- **Embedding & teardown** — public `reset_logging()` restores every piece
  of process-global state Loggair touched.
- `__version__` via `importlib.metadata`; PEP 639 metadata; py.typed;
  tag-driven PyPI release workflow (Trusted Publishing).

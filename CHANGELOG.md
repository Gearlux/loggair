# Changelog

All notable changes to Loggair are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
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

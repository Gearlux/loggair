# Loggair

Modern, multiprocess-safe logging specifically engineered for High-Performance Computing (HPC) and Machine Learning (ML).

## Why Loggair?
ML experiments and distributed training (like PyTorch DDP) present unique logging challenges:
- **Log Storms:** 128 identical lines when 128 GPUs log simultaneously.
- **Multiprocess Safety:** Corrupted log files when multiple processes write to the same file.
- **Startup Consistency:** Tracking which logs belong to which experiment run.

Loggair solves these by being **distributed-aware** and **framework-agnostic**.

## Design Goals & Requirements

### Core Functionality
- **High-Fidelity Logging:** Provide a thread-safe and multiprocess-safe logging engine.
- **Unified Observability:** Standardize logging levels (TRACE, DEBUG, INFO, SUCCESS, WARNING, ERROR, CRITICAL).
- **Auto-Infrastructure:** Automatically create log directories if they do not exist.
- **Global Configuration:** Support XDG-standard configuration (~/.config/loggair/config.yaml).

### Integration
- **Log-Symmetry:** Support automatic log file naming based on the active script/config name.
- **TTY-Aware Console Output:** Provide colorized, rank-filtered console output (powered by [Loguru](https://github.com/Delgan/loguru)), with ANSI coloring auto-disabled when stderr is piped or redirected.
- **Framework Interception:** Intercept standard library logging and redirect to Loggair.

### Performance
- **Zero-Overhead Inactive Levels:** Ensure that TRACE/DEBUG levels have minimal impact when disabled.
- **Asynchronous Sinks:** Support enqueued logging to prevent blocking the hot path.

## Key Features
- **Rank-Aware:** Automatically filters console output to Rank 0 (supports SLURM, DDP, MPI).
- **Multiprocess Safe:** Uses Loguru's `enqueue=True` for thread/process safety.
- **Startup Rotation:** Archives old logs on script start, giving every run a fresh log file.
- **Runtime Rotation:** Optionally rotate the active log by size or time (`rotation: "100 MB"`, `"daily"`, `"00:00"`) for long-running processes — training loops, FastAPI/MCP services (see [Runtime Log Rotation](#8-runtime-log-rotation-long-running-processes)).
- **Framework Interoperability:** Automatically intercepts and formats logs from **TensorFlow**, **PyTorch**, **JAX**, and standard Python `logging`.
- **Zero-Blocking:** Non-blocking logging via background sinking.
- **Runtime Kill-Switch:** Set `LOGGAIR_DISABLE_LOGGING=1` (or `LOGGAIR_DISABLE_MULTIPROCESS_LOGGING=1` for workers only) to get a zero-overhead no-op logger — no files, sinks, or queues (see [Disabling Logging](#5-disabling-logging)).
- **Compressed Archives:** Optionally gzip/zip rotated log archives via `compression` (see [Compressing Rotated Archives](#6-compressing-rotated-archives)).
- **Structured / JSON Logging:** Set `serialize: true` to emit one JSON object per line on both sinks — machine-readable for Elasticsearch/Kibana, Grafana Loki, or Datadog (see [Structured / JSON Logging](#7-structured--json-logging)).
- **Per-Worker Log Files:** `worker_files: true` gives every rank/worker its own file (`app.rank2.log`, `app.worker-1.log`) — no shared-file races, full rotation everywhere; merge at read time with lnav (see [Per-Worker Log Files](#16-per-worker-log-files)).
- **Interception Modes:** Embed Loggair next to a framework that owns logging (uvicorn, gunicorn) with `intercept: coexist`, protect specific loggers with `intercept_exclude`, or switch interception `off` (see [Interception Modes](#17-interception-modes)).
- **Webhook & Alerting Sinks:** Route `ERROR`/`CRITICAL` records to Slack, Teams, email, or any of ~100 platforms via [Apprise](https://github.com/caronc/apprise) URLs — batched, throttled, and off the logging hot path (see [Webhook & Alerting Sinks](#15-webhook--alerting-sinks)).
- **Experiment Context Injection:** Stamp `epoch`/`step`/`run_id` once with `set_context()` and every record in the process carries it — in the default line layout and as structured JSON fields (see [Experiment Context](#14-experiment-context-injection)).
- **CLI Diagnostic:** `python -m loggair` prints the version, detected rank (and which env var decided it), which config files were found, and the fully resolved settings — strictly read-only (see [Diagnostics](#18-diagnostics-python--m-loggair)).
- **Call Tracing (`@track` / `@spy`):** Decorate a function — or a whole class — to log entry and exit, with parameter values on request, at the dedicated `TRAIL` level. Switched off it costs ~0.08 µs per call, so it can stay on hot code; with [Confluid](https://pypi.org/project/confluid/) installed, configured objects log as their configuration rather than their memory address (see [Call Tracing](#19-call-tracing-track-and-spy)).
- **Config Introspection:** `get_active_config()` returns the fully-resolved, JSON-serializable settings currently in effect (see [Config Introspection](#13-config-introspection)).
- **Dynamic Level Adjustment:** Change verbosity in a *running* job — `reconfigure(console_level="DEBUG")` programmatically, or via POSIX signals: a reload signal re-reads the config files, a debug signal toggles DEBUG on/off (see [Dynamic Level Adjustment](#12-dynamic-level-adjustment-at-runtime)).
- **Custom Log Formats:** Override the per-sink loguru format strings (`console_format` / `file_format`) to add thread/process IDs or custom telemetry fields (see [Custom Log Formats](#11-custom-log-formats)).
- **TTY-Aware Color:** Console colorization is auto-detected by default — colors on an interactive terminal, plain text when stderr is redirected or piped (e.g. `2> run.log`, `| tee`), so log files never get polluted with ANSI escape codes. Set the standard `NO_COLOR` env var to force plain text when auto-detection is wrong (see [Console Coloring](#4-console-coloring)).

## Installation

```bash
pip install loggair                     # from PyPI
pip install "loggair[alerts]"           # + webhook/alerting sinks (apprise)
```

Latest development version straight from GitHub:

```bash
pip install git+https://github.com/Gearlux/loggair.git@main
```

```python
import loggair
print(loggair.__version__)              # installed distribution version
```

## Quick Start
```python
from loggair import get_logger, configure_logging

# Optional: customize levels and directories
configure_logging(log_dir="./experiment_logs", console_level="INFO")

logger = get_logger(__name__)

logger.info("Starting training loop...")
logger.debug("Hyperparameters: batch_size=32, lr=0.001")
logger.success("Model checkpoint saved!")
```

`get_logger(name)` mirrors `logging.getLogger(name)` semantics: the given name
**overrides** the auto-detected module name as the `{name}` field in both sinks
and participates in [`module_levels`](#3-per-logger-per-sink-level-overrides)
prefix matching. The usual `get_logger(__name__)` idiom behaves identically to
the bare logger.

## Configuration
Loggair supports a hierarchical configuration system that allows you to manage settings across different projects and environments.

### 1. Configuration Priority
Settings are resolved in the following order (highest to lowest):
1.  **Function Arguments** (passed to `configure_logging()`)
2.  **Environment Variables** (prefixed with `LOGGAIR_`)
3.  **Local `loggair.yaml` / `loggair.yml`**
4.  **Local `pyproject.toml`** (under `[tool.loggair]`)
5.  **XDG User Config** (`~/.config/loggair/config.yaml`)
6.  **Defaults**

Resolution is **presence-based**: falsy-but-valid values win over defaults, so
`rotation_on_startup: false` or `retention: 0` in any config file are honored.
Config files merge **shallowly** — a `module_levels:` block in a local
`loggair.yaml` fully *replaces* one from the XDG global config (rules are not
merged per-prefix).

### 2. Usage Examples

#### via `pyproject.toml`
```toml
[tool.loggair]
log_dir = "./custom_logs"
console_level = "DEBUG"
retention = 10
```

#### via `loggair.yaml`
```yaml
log_dir: "./experiment_logs"
file_level: "TRACE"
enqueue: true
rotation_on_startup: true
compression: "gz"   # compress rotated archives (see §6)
```

#### via Environment Variables
```bash
export LOGGAIR_DIR="/var/log/myapp"
export LOGGAIR_CONSOLE_LEVEL="ERROR"
export NO_COLOR=1                 # force plain console output (see §4)
```

### 3. Per-logger, Per-sink Level Overrides

`file_level` / `console_level` set global thresholds for the file and console sinks. To override the threshold for specific loggers — and, optionally, only on one of the two sinks — use `module_levels`:

```yaml
file_level: "DEBUG"
console_level: "INFO"

module_levels:
  # Silence a chatty discovery logger on the file sink, in DataLoader workers only.
  # The main process still emits INFO so first-time discovery stays visible.
  "waivefront.rfuav.data.archive":
    file: WARNING
    workers_only: true

  # Quiet on screen, full detail on disk — promotes this logger below the global file_level.
  "noisy.lib.but.useful.in.debug":
    console: ERROR
    file: DEBUG
```

Rules:

- Keys match loguru's `record["name"]` (the calling module's dotted path) by **dotted-segment prefix**: `"pkg.sub"` matches `"pkg.sub"` and `"pkg.sub.mod"`, but **not** `"pkg.subway"`. `"foo"` does not match `"foobar.baz"`.
- On overlap, the **longest matching prefix wins**. YAML order is irrelevant.
- `console:` and `file:` are each optional; an omitted sink falls back to the global level for that logger.
- `workers_only: true` restricts the override to non-`MainProcess` processes (typically PyTorch DataLoader workers).
- Unknown sub-keys or invalid level strings raise `ValueError` at startup — no silent ignores.

**Third-party defaults:** interception gates a few chronically chatty stdlib
loggers (`asyncio`, `httpx`, `urllib3`, `datasets`, `filelock`) at `WARNING` by
default. A `module_levels` rule targeting one of them lifts that gate and lets
your per-sink thresholds decide instead:

```yaml
module_levels:
  "httpx":
    file: DEBUG      # see full httpx detail on disk despite the default silence
```

See [`loggair.example.yaml`](https://github.com/Gearlux/loggair/blob/main/loggair.example.yaml) for the full reference.

### 4. Console Coloring

The console sink colorizes its output with ANSI codes. By default this is
**auto-detected** from the terminal: colors on an interactive TTY, plain text
when stderr is redirected or piped (`2> run.log`, `| tee`, a plain Jenkins
console), so log files never accumulate escape codes.

TTY auto-detection is unreliable in some terminals and wrappers. The **only**
environment control is the standard [`NO_COLOR`](https://no-color.org) variable
(there is no bespoke Loggair color env var); for programmatic control, the
`colorize=` argument is a tri-state. Precedence (highest first):

| Source | Value | Effect |
| --- | --- | --- |
| `configure_logging(colorize=...)` arg | `True` / `False` / `None` | force on / off / auto |
| `NO_COLOR` env | any non-empty value | force off (the cross-tool [no-color.org](https://no-color.org) convention) |
| `NO_COLOR` env | empty (`NO_COLOR=`) | re-allow colors → auto-detect |
| *(unset)* | — | auto-detect |

```bash
# Force plain text (e.g. a terminal whose TTY detection misfires):
export NO_COLOR=1
```

To change coloring **after** logging is already configured — including the lazy
auto-configuration the first `get_logger()` triggers — use `reconfigure()`
(a thin `configure_logging(force=True, ...)` wrapper that reloads the sinks).
Setting the env var alone is too late once the console sink exists: its
`colorize` was baked at first configure, so only a reload re-resolves it.

```python
import loggair

loggair.reconfigure(colorize=False)  # reload with colors off
```

**Non-interactive consumers** (whose stderr is captured or relayed, so ANSI is
noise) default coloring **off** for themselves with one call at import:

```python
import loggair

loggair.force_no_color()  # setdefault NO_COLOR=1 + reload if already configured
```

`force_no_color()` both sets `NO_COLOR=1` (inherited by child processes) **and**
reloads loggair if it was already configured — because env inheritance alone
doesn't help once a coloring decision is baked into the live sink. The
[navigaitor](https://github.com/Gearlux/navigaitor) MCP server and the
[StreamStudio](https://github.com/Gearlux/streamstudio) ComfyUI extension call it at
import; to deliberately re-allow colors there, pre-set `NO_COLOR=` (empty).

### 5. Disabling Logging

For perf-critical runs or test suites that want **no** logging overhead — no log
files, no sinks, no background queue — disable Loggair entirely with an
environment variable. `get_logger()` then returns a no-op `NullLogger` that
mirrors the loguru API (every call is a safe no-op, chains and context managers
still work), so call-sites need no changes:

```bash
# Disable in every process:
export LOGGAIR_DISABLE_LOGGING=1

# OR disable only in worker/child processes (keeps the main process logging,
# silences e.g. PyTorch DataLoader workers that would re-emit the same lines):
export LOGGAIR_DISABLE_MULTIPROCESS_LOGGING=1
```

Both accept the usual truthy values (`1`, `true`, `yes`, `t`, `y`). These gate
`get_logger()` only: the common lazy-config path never creates a file because
`get_logger()` short-circuits before configuring. An explicit
`configure_logging()` call still sets up sinks if you make one.

### 6. Compressing Rotated Archives

[Startup rotation](#key-features) archives the previous run's log with a
timestamp. Set `compression` to gzip/zip those archives:

```python
from loggair import configure_logging

configure_logging(compression="gz")   # or "zip"
```

```bash
export LOGGAIR_COMPRESSION=gz          # env equivalent
```

```yaml
# loggair.yaml
compression: "zip"
```

The archive becomes `{script}.{timestamp}.log.gz` (or `.zip`); the active
`{script}.log` stays plain. Valid values are `"gz"`, `"zip"`, or unset (no
compression) — any other value raises `ValueError` at configure time.
`retention` accounts for compressed archives just like plain ones.

> **Note:** Because Loggair performs its *own* startup rotation (it renames the
> file rather than using loguru's managed rotation), compression is applied by
> Loggair right after the rename — **not** by loguru's sink-level `compression=`
> argument, which would never fire here.

### 7. Structured / JSON Logging

For containerized / cloud runs (Kubernetes, AWS ECS) whose logs are ingested by
an aggregator (Elasticsearch/Kibana, Grafana Loki, Datadog), switch both sinks
to structured JSON — one object per line — with `serialize`:

```python
from loggair import configure_logging

configure_logging(serialize=True)
```

```bash
export LOGGAIR_SERIALIZE=true          # env equivalent
```

```yaml
# loggair.yaml
serialize: true
```

Each line is loguru's native serialization (`serialize=True` on the sinks — no
bespoke formatter):

```json
{"text": "2026-07-04 15:26:39 | INFO     | svc.worker:run:42 | structured hello\n",
 "record": {"message": "structured hello", "level": {"name": "INFO", "no": 20},
            "time": {"repr": "2026-07-04 15:26:39.809+02:00", "timestamp": 1783171599.809},
            "name": "svc.worker", "function": "run", "line": 42,
            "process": {"id": 53369, "name": "MainProcess"},
            "extra": {"rank_tag": ""}, "...": "..."}}
```

Everything else composes unchanged: rank-zero console filtering, `[rank N]`
tags (as `record.extra.rank_tag`), `module_levels` per-sink thresholds,
rotation/retention, and `enqueue`. When `serialize` is on, console coloring is
**forced off** regardless of `colorize`/`NO_COLOR` — ANSI escapes would
otherwise be embedded inside the JSON `text` field.

### 8. Runtime Log Rotation (Long-Running Processes)

Startup rotation alone lets the active file grow unbounded in a process that
never restarts (multi-week training, FastAPI servers, MCP hosts). Set
`rotation` to also rotate **at runtime**, by size or time — the value is passed
to [loguru's `rotation=`](https://loguru.readthedocs.io/en/stable/api/logger.html#file)
verbatim, so all its forms work:

```yaml
# loggair.yaml
rotation: "100 MB"     # or "500 KB", "daily", "1 week", "00:00", "monday at 12:00"
```

```bash
export LOGGAIR_ROTATION="daily"        # env equivalent
```

```python
configure_logging(rotation="100 MB")   # programmatic
```

Invalid values raise `ValueError` at configure time. When runtime rotation is
active, `retention` and `compression` are additionally enforced **at rotation
time** by loguru's sink (same per-stem, live-log-safe semantics as the startup
sweep — verified against Loggair's own archives), so a service that never
restarts still gets pruning and compressed archives. Runtime archives are named
`{name}.{timestamp}_{microseconds}.log`; startup retention recognises both
naming schemes.

> **Multi-writer caveat:** rotation renames the active file, so with a shared
> file it runs in the **main process only**. Fork-inherited children route
> records through the enqueue queue and are unaffected, but a *spawn*-started
> worker that reconfigures its own sink keeps its file descriptor after a
> main-process rotation, so its subsequent records land in the rotated archive.
> **Setting [`worker_files: true`](#16-per-worker-log-files) removes this
> caveat entirely** — every process owns its own file and rotates it safely.

### 9. Retention Semantics (Shared Log Dirs Are Safe)

`retention` is enforced **per script name (stem)**, counting only *timestamped
archives* (`{name}.{timestamp}.log`, compressed or plain). On startup, Loggair
prunes each stem's archives — including stems left behind by earlier runs under
other script names — down to `retention`.

A bare `{name}.log` of **another** script is **never** deleted: in a shared log
directory (e.g. a centralized `~/logs`) it may be the live sink of a
concurrently running process. Only that script's own next run rotates it.

### 10. Embedding & Teardown

`shutdown_logging()` flushes pending records (sinks stay installed) — call it at
normal process exit. Hosts that need to switch Loggair **off** cleanly (pytest
plugins, notebooks, long-lived servers) use `reset_logging()`, which flushes and
removes all sinks, restores `warnings.showwarning` and the stdlib root handler
interception, un-patches the lazy-enqueue hooks, and returns Loggair to the
unconfigured state — a later `get_logger()` starts fresh:

```python
import loggair

loggair.reset_logging()  # full teardown; safe to reconfigure afterwards
```

### 11. Custom Log Formats

The per-sink format strings are configurable through the standard hierarchy —
`console_format` / `file_format` as arguments, `LOGGAIR_CONSOLE_FORMAT` /
`LOGGAIR_FILE_FORMAT` env vars, or YAML/`pyproject.toml` keys. Any
[loguru format field](https://loguru.readthedocs.io/en/stable/api/logger.html#record)
works — `{process.id}`, `{thread.id}`, `{extra[...]}`, color markup on the
console sink:

```yaml
# loggair.yaml — add process/thread IDs for multi-worker debugging
file_format: "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | pid={process.id} tid={thread.id} | {name}:{function}:{line} | {message}"
```

The defaults are public, so a custom format can **extend** rather than restate
them:

```python
from loggair import DEFAULT_FILE_FORMAT, configure_logging

configure_logging(file_format=DEFAULT_FILE_FORMAT + " | job={extra[job]}")
```

Notes:

- `{extra[rank_tag]}` is always stamped by Loggair's sink filters and available
  to any custom format; omitting it simply drops the rank tag from the layout.
- Invalid **color markup** fails fast at configure time. A format referencing a
  **nonexistent field** does not — loguru reports a loud per-record error on
  stderr and drops the record, so test a custom format once before deploying.
- In [JSON mode](#7-structured--json-logging) the format only shapes the `text`
  field; the structured `record` is unaffected.

### 12. Dynamic Level Adjustment at Runtime

Levels are resolved once at configure time, but a long-running job (multi-week
training, a FastAPI/MCP service) doesn't need a restart to change verbosity:

**Programmatic** — `reconfigure()` reloads the sinks with overrides, keeping
every explicitly-passed setting from the original call:

```python
import loggair

loggair.reconfigure(console_level="DEBUG")   # elevate a live run
loggair.reconfigure()                        # re-read env + config files
```

**Signal-driven** (POSIX) — for processes you can't call into, opt in to one or
both signals:

```yaml
# loggair.yaml
reload_signal: "SIGUSR1"   # re-resolve env + config files on signal
debug_signal:  "SIGUSR2"   # toggle DEBUG on both sinks on signal
```

```bash
# Diagnose a live anomaly: flip the running job to DEBUG, then back
kill -USR2 <pid>          # debug ON
kill -USR2 <pid>          # debug OFF (previous levels restored)

# Or edit loggair.yaml (levels, module_levels, ...) and apply it live
kill -USR1 <pid>
```

Details:

- Handlers are installed in the **main process and main thread only**; invalid
  or platform-unavailable signal names raise `ValueError` at configure time.
  The signal handler defers all work to a short-lived worker thread — safe to
  deliver at any moment, even mid-log-call.
- A reload re-applies the original call's explicit arguments, so it only picks
  up changes for settings driven by env vars or config files. Drive levels via
  `loggair.yaml` if you want them signal-reloadable.
- Each toggle/reload announces itself in the log
  (`Loggair debug mode ON (signal)` / `Loggair Re-initialized`).

### 13. Config Introspection

`get_active_config()` returns the **resolved** settings the sinks are actually
running with — all hierarchy layers applied — as a JSON-serializable dict, for
third-party plugins, diagnostics scripts, or an MCP tool surface:

```python
import json, loggair

print(json.dumps(loggair.get_active_config(), indent=2))
# {
#   "configured": true,
#   "log_dir": "/home/me/logs",
#   "log_file": "/home/me/logs/train.log",
#   "file_level": "DEBUG", "console_level": "INFO",
#   "module_levels": {"pkg.noisy": {"file": "WARNING"}},
#   "rotation_on_startup": true, "retention": 5, "compression": "gz",
#   "rotation": "100 MB", "enqueue": true, "enqueue_active": false,
#   "colorize": null, "serialize": false,
#   "console_format": "...", "file_format": "...",
#   "reload_signal": "SIGUSR1", "debug_signal": null,
#   "debug_mode_active": false, "is_main_process": true, "rank": null
# }
```

It is purely read-only (never triggers the lazy configuration — before the
first configure it returns `{"configured": false}`) and hands out a fresh copy
each call. `enqueue` vs `enqueue_active` distinguishes the *requested* queue
from the lazily-materialized one; `debug_mode_active` reflects the
[debug-signal toggle](#12-dynamic-level-adjustment-at-runtime).

### 14. Experiment Context Injection

Stamp the training loop's current position once, and **every** record logged
anywhere in the process afterwards carries it — Loggair loggers and
intercepted third-party stdlib logging alike:

```python
import loggair

for epoch in range(epochs):
    loggair.set_context(epoch=epoch)
    for step, batch in enumerate(loader):
        if step % 100 == 0:
            loggair.set_context(step=step)      # merges — epoch stays
        ...
# 2026-07-04 17:01:41.698 | INFO | train:run:42 | epoch=3 step=1200 | loss improved
```

The API: `set_context(**fields)` merges, `clear_context("step")` removes one
field, `clear_context()` removes all, `get_context()` inspects, and
`with loggair.context(phase="validation"): ...` scopes fields to a block
(the entry snapshot is restored on exit). Behavior:

- The default formats render a compact `epoch=3 step=1200 | ` tag before the
  message (nothing at all when no context is set). Custom formats can place it
  via `{extra[context_tag]}`.
- In [JSON mode](#7-structured--json-logging) each field also lands
  individually in `record.extra` (`extra.epoch`, `extra.step`, ...) for log
  aggregators. An explicit `logger.bind(epoch=...)` wins over the ambient
  context.
- The context is **process-wide** (a checkpoint-saver thread's records carry
  the epoch the training thread set) and inherited by fork-started workers;
  spawn-started workers begin empty. Reads are lock-free — near-zero
  per-record cost.

### 15. Webhook & Alerting Sinks

Route high-severity records to external platforms so operators hear about
crashes immediately. Delivery is backed by
[Apprise](https://github.com/caronc/apprise) (optional dependency):

```bash
pip install "loggair[alerts]"      # pulls apprise
```

Configure one or more Apprise URLs — Slack, Teams, Discord, Telegram, email,
generic webhooks, [~100 services](https://github.com/caronc/apprise/wiki):

```yaml
# loggair.yaml
alert_urls:
  - "slack://TokenA/TokenB/TokenC/#alerts"
  - "mailto://user:pass@gmail.com"
alert_level: "ERROR"      # minimum severity that alerts (default ERROR)
alert_throttle: 60        # at most one delivery per N seconds (0 = every batch)
```

```bash
export LOGGAIR_ALERT_URLS="slack://TokenA/TokenB/TokenC/#alerts,mailto://ops@corp.com"
```

Behavior:

- **Never blocks the hot path**: the sink only enqueues; a background worker
  delivers. Everything queued since the last delivery is **batched into one
  notification** (title carries the highest severity + host + a `(+N more)`
  count), and `alert_throttle` rate-limits deliveries — an alert storm
  collapses into periodic digests instead of thousands of messages.
- Tracebacks from `logger.exception(...)` are included in the body.
- Invalid URLs and unknown levels **fail fast at configure time**; a missing
  `apprise` install raises with the `pip install loggair[alerts]` hint.
- A failing webhook is reported at WARNING in the normal sinks but can never
  feed back into another alert (internally-marked and filtered).
- `shutdown_logging()` flushes queued alerts; `reset_logging()` stops the
  worker. `get_active_config()` reports the URLs in Apprise's
  **privacy-redacted** form — tokens never leak into diagnostics.
- The sink is installed in **every** process, so a worker/rank crash alerts
  too; the per-process throttle keeps distributed storms bounded.

### 16. Per-Worker Log Files

By default all processes append to the shared `{name}.log` (rank-tagged). Set
`worker_files: true` (env `LOGGAIR_WORKER_FILES`) to give every non-main
writer its **own, exclusively-owned** file instead:

| Writer | File |
| --- | --- |
| main process, rank 0 / non-distributed | `app.log` |
| DDP rank 2 | `app.rank2.log` |
| spawned/forked child `Worker-1` | `app.worker-1.log` |
| rank 2's own child `Worker-1` | `app.rank2.worker-1.log` |

Because each process is the sole writer of its path, the multi-writer
restrictions disappear: per-worker files get **startup rotation and
[runtime rotation](#8-runtime-log-rotation-long-running-processes)** too, and
no rotation can ever split another process's stream. The
[startup sweep](#9-retention-semantics-shared-log-dirs-are-safe) still never
deletes the bare per-worker files (live-log-safe rule) — only their
timestamped archives, pruned per stem as usual.

Merge the streams at read time — [lnav](https://lnav.org/) interleaves the
whole directory chronologically:

```bash
lnav ./logs        # app.log + app.rank*.log + app.*worker*.log, one timeline
```

### 17. Interception Modes

By default (`intercept: full`) Loggair **owns** stdlib logging: the root
handler is replaced and existing named loggers are stripped so everything
propagates into Loggair. Two knobs relax this for embedding:

```yaml
# loggair.yaml
intercept: "coexist"              # full | coexist | off
intercept_exclude: ["uvicorn"]    # dotted prefixes Loggair must not touch
capture_warnings: true            # warnings.showwarning redirect (default on)
```

- **`coexist`** — Loggair *appends* its handler to the root and touches
  nothing else: a framework that configured its own logging (uvicorn,
  gunicorn) keeps its handlers, and both receive records. Trade-off: the root
  level is lowered to DEBUG so records reach Loggair (pre-existing handlers
  still apply their own levels), and a logger with `propagate=False` stays
  outside Loggair.
- **`intercept_exclude`** — even in `full` mode, the listed prefixes (and
  their children, e.g. `uvicorn` covers `uvicorn.error`) keep their handlers,
  `propagate` flags, and levels verbatim; the built-in third-party silences
  also skip them.
- **`off`** — no interception at all: no root rewiring, no warnings redirect,
  no third-party level defaults. Only code calling `loggair.get_logger()`
  directly is captured.
- **`capture_warnings: false`** — leave `warnings.showwarning` alone
  (independently of the mode).

An invalid `intercept` value raises `ValueError` at configure time.

### 18. Diagnostics: `python -m loggair`

Verify an installation and inspect the configuration a process *would* get —
without configuring anything:

```console
$ python -m loggair
loggair 0.1.0  (loguru 0.7.3, python 3.12.13)
process:  MainProcess (pid 64497)
rank:     3  (from RANK)

config files (highest priority first):
  [    found] loggair.yaml
  [not found] loggair.yml
  [not found] pyproject.toml
  [    found] /home/me/.config/loggair/config.yaml
merged file config: {"log_dir": "~/logs", "retention": 3}

resolved settings (args omitted — env > files > defaults):
  log_dir: "/home/me/logs"
  ...
```

- **Strictly read-only**: no log directory is created, no rotation happens, no
  sinks or interception are installed — safe to run next to a live training
  job. Alert URLs are shown fully masked (`slack://****`).
- On a cluster, run it *inside* the launcher to debug rank detection:
  `torchrun --nproc-per-node 4 -m loggair` or `srun python -m loggair` shows
  each rank's value and the env var that decided it.
- A broken config (unknown level, bad `intercept` mode, ...) is reported with
  context and exit code 1 — the doctor works precisely when configure would
  fail.
- `--json` emits the same report as one JSON object for scripts.
- The install also provides a **`loggair` console script** (same tool, no cwd
  on `sys.path`). And the `-m` form **self-heals the mono-repo pitfall**: from
  a directory that itself contains a `loggair/` project folder (a workspace
  root with an editable install), Python would resolve the package to that
  folder — the doctor detects the shadow, drops the offending path entry for
  its own process, re-imports the real package, and prints the full report
  with a WARNING (and a `shadowed_path_ignored` JSON field), because plain
  `import loggair` scripts run from that directory still hit the shadow.
  Only if no real installation exists at all does it exit 2 with instructions.

Programmatic equivalents: `loggair.get_active_config()` for the settings a
*configured* process is running with, and `loggair.core.resolve_settings()`
for the same dry resolution this tool prints.

### 19. Call Tracing: `@track` and `@spy`

Two decorators log when a function is entered and left. `@track` logs the call;
`@spy` also logs what it was called with.

```python
from loggair import spy, track

@track
def load(path, n=10):
    return [path] * n

@spy
def train(model, epochs=2, lr=0.001):
    return 0.42
```

Both emit at **`TRAIL`**, a level Loggair registers at severity **7** — between
`TRACE` (5) and `DEBUG` (10), because call tracing is noisier than tracing.
Nothing appears until a sink is set that low:

```yaml
console_level: "TRAIL"
```

```
TRAIL | myapp:load:16 | → load() [from myapp:main:75]
TRAIL | myapp:load:16 | ← load (0.0 ms)
TRAIL | myapp:train:21 | → train(model=<myapp.Model object at 0x10883dd00>, epochs=7, lr=0.001) [from myapp:main:76]
TRAIL | myapp:train:21 | ← train (0.0 ms)
```

Every output in this section is verbatim from `examples/track_demo.py`; run it to
reproduce them.

**Where the line points.** `{name}:{function}:{line}` locates the **decorated
function** — one real place you can open. The **caller** is named separately, in
`[from module:function:line]` at the end of the entry line. Keeping them apart
matters: fusing them produced references that could not exist, like
`sonair.lightning_classifier:_construct:1164` where that file is 159 lines long and
`_construct` belongs to another package entirely. It also keeps `module_levels`
targeting honest — the name field is the decorated function's module, so a rule
keyed to it selects the functions you decorated rather than whoever calls them.

The location sees through other decorators: if something wraps your function below
`@track`, the trace still points at your definition, not at the intermediate
wrapper.

> **If nothing appears**, something probably configured Loggair before you did.
> The first `get_logger()` anywhere in the process — including inside an imported
> library — configures Loggair lazily, and a later plain `configure_logging(...)`
> is a no-op by design (see [Dynamic Level Adjustment](#12-dynamic-level-adjustment-at-runtime)).
> Set the level through a config file or `LOGGAIR_CONSOLE_LEVEL=TRAIL`, which the
> lazy configuration reads too, or call `reconfigure(console_level="TRAIL")`.

An exception is logged as an exit and re-raised unchanged:

```
TRAIL | myapp:crash:36 | → crash() [from myapp:main:80]
TRAIL | myapp:crash:36 | ← crash ✗ ValueError (0.0 ms)
```

Only the entry line carries the caller; repeating it on exit would double the noise.

#### Turning it on for one module only

`TRAIL` is above `TRACE`, so running a sink at `TRACE` shows call tracing too.
Setting it globally traces every decorated function in the process, which is
rarely what you want. Target it instead:

```yaml
console_level: "INFO"
module_levels:
  mypkg.io:
    file: TRAIL        # trace this module, to the log file only
```

The name matched is the **decorated function's own module**, so a rule keyed to
where the function is *defined* is what selects it.

#### Cost

The decorators check whether `TRAIL` can reach any sink before they build
anything, so leaving them on code that runs millions of times is safe. Measured
per call on a no-op function:

| | cost |
| --- | --- |
| undecorated call | 0.027 µs |
| `@track`, `TRAIL` off | 0.081 µs |
| `@spy`, `TRAIL` off | 0.082 µs |
| `@track`, `TRAIL` on | 26.0 µs |
| `@spy`, `TRAIL` on | 30.5 µs |

Switched **on** it is expensive — two records per call, through both sinks. That
is the cost of the information, and the reason to scope it with `module_levels`.

#### Rendering parameter values

`@spy` renders each argument with its full `repr` by default — complete, because
a trace that elides the argument you were chasing is worse than a long line. Use
`cap` to bound it:

```python
@spy(cap=40)
def summarize(values): ...

summarize([round(i * 0.111, 3) for i in range(40)])
```

```
→ summarize(values=[0.0, 0.111, 0.222, 0.333, 0.444, 0.555,…)
```

##### Configured objects render as their configuration

`repr` on an assembled object gives you a memory address, when the interesting
fact is how it was *configured* — which model, how many layers, which weights.
If [Confluid](https://pypi.org/project/confluid/) is installed, `@spy` renders
objects it marks as configurable as their configuration document instead:

```bash
pip install confluid
```

```python
from confluid import configurable

@configurable
class ResNet:
    def __init__(self, layers: int = 50, pretrained: bool = True):
        self.layers = layers
        self.pretrained = pretrained

@spy
def train(model, epochs=2): ...

train(ResNet(), epochs=7)
```

```
→ train(model={_target_: ResNet, layers: 50, pretrained: true}, epochs=7)
```

What is rendered is the document Confluid would reconstruct the object from — so
a class that does not store its constructor arguments has nothing to show, and
logs as a bare `{_target_: ResNet}`.

Without Confluid installed, the same call logs the address instead:

```
→ train(model=<myapp.ResNet object at 0x10883dd00>, epochs=7)
```

Pass `multiline=True` for an indented YAML block instead of one line — easier to
read for deep configuration trees, at the cost of one call no longer being one
log line. Given the same `ResNet` above:

```python
@spy(multiline=True)
def fit(model): ...

fit(ResNet(layers=18))
```

```
→ fit(model=
    _target_: ResNet
    layers: 18
    pretrained: true)
```

Lists and dicts **containing** configurables render the same way, which is how most
real slots are shaped — a list of trackers, a dict of metric templates:

```
→ fit(trackers=[{_target_: TensorBoardTracker, run_name: efficientvit_b0_mnist, save_dir: ./runs}],
      metrics={accuracy: {_target_: MulticlassAccuracy, _partial_: true, top_k: 1}})
```

Anything inside such a container that Confluid cannot describe is replaced by its
`repr` first, so a mixed list renders completely and the dumper's
"cannot reconstruct this" warning — true about configuration round-tripping,
meaningless in a call trace — never reaches your log.

Confluid is entirely optional — Loggair does not depend on it, imports it only
when a spied value carries its marker, and falls back to `repr` whenever it is
absent.

#### Decorating a class

Applied to a class, both decorators trace the methods that class defines itself:

```python
@spy
class Pipeline:
    def __init__(self, source, batch_size=32): ...
    def run(self, limit=None): ...
```

```
TRAIL | myapp:__init__:44 | → Pipeline.__init__(source='/data/train', batch_size=64) [from myapp:main:105]
TRAIL | myapp:__init__:44 | ← Pipeline.__init__ (0.0 ms)
TRAIL | myapp:run:48 | → Pipeline.run(limit=100) [from myapp:main:106]
TRAIL | myapp:run:48 | ← Pipeline.run (0.0 ms)
TRAIL | myapp:describe:59 | → Pipeline.describe(kind='streaming') [from myapp:main:108]
TRAIL | myapp:describe:59 | ← Pipeline.describe (0.0 ms)
```

The bound `self` / `cls` is dropped from the parameters. What is traced, and
what is not:

| | traced |
| --- | --- |
| public methods, `__init__`, `__call__` | yes |
| `staticmethod`, `classmethod` | yes, and they stay static/class methods |
| `_private` methods and other dunders | no |
| properties | no — reading one would fire its getter |
| inherited methods | no — decorate the base class to trace those |
| `async def` and generator methods | no; skipped, with a `DEBUG` line naming them |

The class object itself is modified in place and returned, so `isinstance`
checks, pickling, and any registration the class already carries are unaffected.

#### Tracing inherited methods

By default only the class's own methods are traced, and for a thin subclass that
can be almost nothing. A real example: a Lightning trainer class defined **one**
traceable method and inherited **167** — 136 of them from `pytorch_lightning` and
`torch.nn`. Tracing those would put a line on `nn.Module.forward` for every batch.

So inherited tracing is opt-in and bounded by `inherited=`:

```python
@spy                                  # own methods only — the default
@spy(inherited=LightningRunnable)     # own + every base up to and including that one
@spy(inherited=True)                  # own + the entire MRO except `object`
```

A class names an **inclusive boundary** in the MRO: everything from the decorated
class down to that base is traced, and everything past it is left alone. That is
usually what you want, because your own base classes sit at the top of the MRO and
the framework's sit below them. Naming a class that is not a base raises, listing
the bases that are.

On the same trainer, `inherited=LightningRunnable` turned 2 trace records per run
into 176, covering `fit`, `training_step`, `validation_step`, the epoch hooks and
`configure_optimizers` — the actual machinery — while leaving Lightning and torch
untouched. A base method is traced under the class that defines it, so you can see
which layer you are in:

```
TRAIL | myapp:run:85 | → Job.run(steps=3) [from myapp:main:111]
TRAIL | myapp:warmup:72 | → Engine.warmup(seconds=1) [from myapp:run:86]
TRAIL | myapp:warmup:72 | ← Engine.warmup (0.0 ms)
TRAIL | myapp:run:85 | ← Job.run (0.0 ms)
```

`inherited=True` is the blunt instrument. It is there when you want it, but on any
framework subclass it will trace far more than you meant.

Wrappers are always installed on the **decorated class**, shadowing the inherited
method. The base class is never modified, so decorating one subclass does not trace
its siblings — or, for a `torch.nn.Module` base, every model in the process. A name
the class defines itself always wins over the inherited version.

#### What cannot be traced

Decorating an `async def` or a generator function directly raises `TypeError`.
A synchronous wrapper would time only the call that *creates* the coroutine or
generator — not the work it goes on to do — and report a confident `0.0 ms` exit
for a call that has not started. Trace what the coroutine awaits, or what the
generator does per item, instead.

#### Other options

| argument | default | meaning |
| --- | --- | --- |
| `level` | `"TRAIL"` | Emit at another registered level, e.g. `@track(level="DEBUG")`. |
| `timing` | `True` | Append the elapsed wall time to the exit line. |
| `cap` | `None` | `@spy` only — truncate any rendered value past this many characters. |
| `multiline` | `False` | `@spy` only — render configuration documents as a YAML block. |
| `inherited` | `False` | Classes only — `True` for the whole MRO, or a base class as an inclusive boundary. |

#### Gating your own expensive logging

The check the decorators use is public. Use it whenever building a log message
costs more than the branch that skips it:

```python
from loguru import logger
from loggair.core import effective_level_no

if logger.level("TRACE").no >= effective_level_no(__name__):
    logger.trace("tensor stats: {}", expensive_summary(batch))
```

This is more precise than loguru's own level check, which is per-*sink*.
`effective_level_no` is per-*logger*. The two differ as soon as one
`module_levels` rule promotes one logger: the sink has to accept the lowest level
any of its rules asks for, so records from every *other* logger clear that floor
and are built in full before the filter discards them. With `file_level: INFO`
and a rule promoting `pkg.a` to `DEBUG`, a `DEBUG` from `pkg.b` costs 3.4 µs to
build and throw away; gating on `effective_level_no("pkg.b")` skips it.

`logger.opt(lazy=True)` does not help either — loguru evaluates lazy arguments
after the level check the record has already passed, so they run for every record
the filter goes on to drop.

Cache the result if you call it in a loop; it changes only when Loggair is
reconfigured.

## Log Inspection
For the best experience viewing Loggair logs (especially interleaving logs from multiple ranks/workers), we recommend using **[lnav](https://lnav.org/)** (The Log File Navigator).

`lnav` automatically detects Loggair's timestamp format and can merge multiple log files into a single, chronological view.

### Usage with lnav
```bash
# View all logs in the directory interleaved by time
lnav ./logs
```

For more information, see the **[lnav documentation](https://docs.lnav.org/)**.

## Distributed Training (DDP/SLURM)
Loggair handles ranks automatically. No need to wrap your log calls in `if rank == 0:`.
```python
# In a torchrun or SLURM environment
from loggair import get_logger

logger = get_logger(__name__)

# Only shows up once in console (Rank 0), but saved in file for all Ranks
logger.info("Initializing process group...")
```

## License
MIT

# Configuration reference

How a Loggair setting gets its value, every setting there is, and how to read the
resolved configuration back. What those settings do to log files, ranks and sinks
in a running job is the [operations guide](operations.md); `@track` and `@spy`
are the [tracing guide](tracing.md); the reasons behind the design are the
[architecture decisions](architecture.md).

## How settings resolve

Highest priority first:

1. **Keyword arguments** to `configure_logging()`
2. **Environment variables** prefixed `LOGGAIR_`
3. **`loggair.yaml`** (or `loggair.yml`) in the working directory
4. **`pyproject.toml`** in the working directory, table `[tool.loggair]`
5. **The user config** `~/.config/loggair/config.yaml` (under `$XDG_CONFIG_HOME` when set)
6. **Defaults**

Resolution is **presence-based**: falsy-but-valid values win over defaults, so
`rotation_on_startup: false` or `retention: 0` in any config file are honored. An
environment variable that is set but empty counts as unset. Config files merge
**shallowly** — a `module_levels:` block in a local `loggair.yaml` fully *replaces*
one from the user config; rules are not merged per prefix.

Configuration happens once per process. The first `get_logger()` anywhere —
including inside an imported library — configures Loggair from the environment
and the config files, and a later plain `configure_logging(...)` is a no-op. To
change a live configuration, use
[`reconfigure()`](#changing-settings-in-a-running-process).

## Three front doors

```toml
# pyproject.toml
[tool.loggair]
log_dir = "./custom_logs"
console_level = "DEBUG"
retention = 10
```

```yaml
# loggair.yaml
log_dir: "./experiment_logs"
file_level: "TRACE"
enqueue: true
rotation_on_startup: true
compression: "gz"
```

```bash
export LOGGAIR_DIR="/var/log/myapp"
export LOGGAIR_CONSOLE_LEVEL="ERROR"
export NO_COLOR=1                 # plain console output, see Console coloring
```

```python
from loggair import configure_logging

configure_logging(log_dir="./experiment_logs", console_level="INFO")
```

[`loggair.example.yaml`](../loggair.example.yaml) is the annotated template: every
key with its default, its environment variable and a comment.

## Settings

| Key | Environment variable | Default | Meaning |
| --- | --- | --- | --- |
| `log_dir` | `LOGGAIR_DIR` | `./logs` | Directory for log files, created on first configure |
| `script_name` | `LOGGAIR_SCRIPT_NAME` | the running script's stem | Log file stem (`{script_name}.log`); the main process exports it so child processes write the same file |
| `file_level` | `LOGGAIR_FILE_LEVEL` | `DEBUG` | Threshold of the file sink |
| `console_level` | `LOGGAIR_CONSOLE_LEVEL` | `INFO` | Threshold of the console sink |
| `module_levels` | — (config files only) | `{}` | Per-logger, per-sink thresholds — [below](#per-logger-per-sink-level-overrides) |
| `rotation_on_startup` | `LOGGAIR_ROTATION_ON_STARTUP` | `true` | Archive the existing log file with a timestamp when a run starts — [operations](operations.md#startup-rotation-retention-and-compression) |
| `retention` | `LOGGAIR_RETENTION` | `5` | Timestamped archives kept per log stem; `0` keeps none |
| `rotation` | `LOGGAIR_ROTATION` | unset | Rotate the active file at runtime, in Loguru's syntax: `"100 MB"`, `"daily"`, `"00:00"` — [operations](operations.md#runtime-rotation) |
| `compression` | `LOGGAIR_COMPRESSION` | unset | `gz` or `zip` for rotated archives |
| `enqueue` | `LOGGAIR_ENQUEUE` | `false` | Route file writes through a multiprocessing queue, allocated when the first child process appears — [operations](operations.md#ranks-and-processes) |
| `worker_files` | `LOGGAIR_WORKER_FILES` | `false` | A separate log file per rank and child process — [operations](operations.md#per-worker-log-files) |
| `serialize` | `LOGGAIR_SERIALIZE` | `false` | One JSON object per line on both sinks; forces console colors off — [operations](operations.md#structured-json-logging) |
| `colorize` | — (`NO_COLOR` instead) | auto-detect | Console colors as a keyword argument: `True` / `False` / `None` — [below](#console-coloring) |
| `console_format` | `LOGGAIR_CONSOLE_FORMAT` | `DEFAULT_CONSOLE_FORMAT` | Loguru format string of the console sink — [below](#custom-log-formats) |
| `file_format` | `LOGGAIR_FILE_FORMAT` | `DEFAULT_FILE_FORMAT` | Loguru format string of the file sink |
| `intercept` | `LOGGAIR_INTERCEPT` | `full` | `full` / `coexist` / `off`: how standard-library logging is taken over — [operations](operations.md#interception-modes) |
| `intercept_exclude` | `LOGGAIR_INTERCEPT_EXCLUDE` | `[]` | Dotted logger prefixes left untouched, children included |
| `capture_warnings` | `LOGGAIR_CAPTURE_WARNINGS` | `true` | Redirect `warnings.warn()` into the log |
| `reload_signal` | `LOGGAIR_RELOAD_SIGNAL` | unset | Signal that re-reads env and config files live, e.g. `SIGUSR1` — [below](#changing-settings-in-a-running-process) |
| `debug_signal` | `LOGGAIR_DEBUG_SIGNAL` | unset | Signal that toggles both sinks to `DEBUG` and back, e.g. `SIGUSR2` |
| `alert_urls` | `LOGGAIR_ALERT_URLS` | `[]` | Apprise URLs that receive records at `alert_level` and above — [operations](operations.md#alerting) |
| `alert_level` | `LOGGAIR_ALERT_LEVEL` | `ERROR` | Lowest level that alerts |
| `alert_throttle` | `LOGGAIR_ALERT_THROTTLE` | `60` | Seconds between alert deliveries; `0` delivers every batch |

Every key is also a keyword argument of `configure_logging()`, except
`module_levels` (a mapping, config files only). List values given through an
environment variable (`intercept_exclude`, `alert_urls`) are comma-separated;
booleans accept `1`, `true`, `yes`, `t`, `y`. An unknown level, intercept mode,
compression format or signal name raises `ValueError` at configure time — nothing
is silently ignored.

Switches that exist only as environment variables:

| Variable | Effect |
| --- | --- |
| `LOGGAIR_EAGER_ENQUEUE` | With `enqueue`, allocate the queue at configure time instead of when the first child process is created |
| `LOGGAIR_DISABLE_LOGGING` | `get_logger()` returns a no-op logger in every process — [Disabling logging](#disabling-logging) |
| `LOGGAIR_DISABLE_MULTIPROCESS_LOGGING` | The same, in child processes only |
| `NO_COLOR` | Any non-empty value forces plain console output — [Console coloring](#console-coloring) |

### Levels

`TRACE` (5), `TRAIL` (7), `DEBUG` (10), `INFO` (20), `SUCCESS` (25), `WARNING`
(30), `ERROR` (40), `CRITICAL` (50). `TRAIL` is Loggair's own level, registered
when `loggair` is imported, and carries [call tracing](tracing.md). Because it sits
above `TRACE`, a sink at `TRACE` shows call tracing too.

## Per-logger, per-sink level overrides

`file_level` / `console_level` set global thresholds for the file and console
sinks. To override the threshold for specific loggers — and, optionally, only on
one of the two sinks — use `module_levels`:

```yaml
file_level: "DEBUG"
console_level: "INFO"

module_levels:
  # Silence a chatty discovery logger on the file sink, in DataLoader workers only.
  # The main process still emits INFO so first-time discovery stays visible.
  "mypkg.data.archive":
    file: WARNING
    workers_only: true

  # Quiet on screen, full detail on disk — promotes this logger below the global file_level.
  "noisy.lib.but.useful.in.debug":
    console: ERROR
    file: DEBUG
```

Rules:

- Keys match loguru's `record["name"]` (the calling module's dotted path) by
  **dotted-segment prefix**: `"pkg.sub"` matches `"pkg.sub"` and `"pkg.sub.mod"`,
  but **not** `"pkg.subway"`. `"foo"` does not match `"foobar.baz"`.
- On overlap, the **longest matching prefix wins**. YAML order is irrelevant.
- `console:` and `file:` are each optional; an omitted sink falls back to the
  global level for that logger.
- `workers_only: true` restricts the override to non-`MainProcess` processes
  (typically PyTorch DataLoader workers).
- Unknown sub-keys or invalid level strings raise `ValueError` at startup — no
  silent ignores.
- `get_logger(name)` mirrors `logging.getLogger(name)`: the given name
  **overrides** the auto-detected module name as the `{name}` field in both sinks
  and participates in this prefix matching. The usual `get_logger(__name__)`
  idiom behaves identically to the bare logger.

**Third-party defaults:** interception gates a few chronically chatty stdlib
loggers (`asyncio`, `httpx`, `urllib3`, `datasets`, `filelock`) at `WARNING` by
default. A `module_levels` rule targeting one of them lifts that gate and lets
your per-sink thresholds decide instead:

```yaml
module_levels:
  "httpx":
    file: DEBUG      # see full httpx detail on disk despite the default silence
```

## Console coloring

The console sink colorizes its output with ANSI codes. By default this is
**auto-detected** from the terminal: colors on an interactive TTY, plain text
when stderr is redirected or piped (`2> run.log`, `| tee`, a plain Jenkins
console), so log files never accumulate escape codes.

TTY auto-detection is unreliable in some terminals and wrappers. The **only**
environment control is the standard [`NO_COLOR`](https://no-color.org) variable
(there is no bespoke Loggair color env var and no YAML key); for programmatic
control, the `colorize=` argument is a tri-state. Precedence (highest first):

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
noise — an MCP server, a GUI extension) default coloring **off** for themselves
with one call at import:

```python
import loggair

loggair.force_no_color()  # setdefault NO_COLOR=1 + reload if already configured
```

`force_no_color()` both sets `NO_COLOR=1` (inherited by child processes) **and**
reloads loggair if it was already configured — because env inheritance alone
doesn't help once a coloring decision is baked into the live sink. To deliberately
re-allow colors in such a host, pre-set `NO_COLOR=` (empty).

In [JSON mode](operations.md#structured-json-logging) console coloring is forced
off regardless of `colorize` / `NO_COLOR`.

## Custom log formats

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
  `{extra[context_tag]}` likewise carries the
  [experiment context](operations.md#experiment-context).
- Invalid **color markup** fails fast at configure time. A format referencing a
  **nonexistent field** does not — loguru reports a loud per-record error on
  stderr and drops the record, so test a custom format once before deploying.
- In [JSON mode](operations.md#structured-json-logging) the format only shapes
  the `text` field; the structured `record` is unaffected.

## Disabling logging

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

## Changing settings in a running process

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

## Reading the configuration back

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
#   "alert_level": "ERROR", "alert_throttle": 60.0, "alert_urls": [],
#   "worker_files": false,
#   "intercept": "full", "intercept_exclude": [], "capture_warnings": true,
#   "debug_mode_active": false, "experiment_context": {},
#   "is_main_process": true, "rank": null
# }
```

It is purely read-only (never triggers the lazy configuration — before the
first configure it returns `{"configured": false}`) and hands out a fresh copy
each call. `enqueue` vs `enqueue_active` distinguishes the *requested* queue
from the lazily-materialized one; `debug_mode_active` reflects the
[debug-signal toggle](#changing-settings-in-a-running-process). Alert URLs are
reported in Apprise's privacy-redacted form — tokens never leak into diagnostics.

## Diagnostics

Verify an installation and inspect the configuration a process *would* get —
without configuring anything:

```console
$ python -m loggair
loggair 0.2.0  (loguru 0.7.3, python 3.12.13)
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
*configured* process is running with, and `loggair.resolve_settings()`
for the same dry resolution this tool prints.

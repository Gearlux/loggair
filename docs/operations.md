# Operations guide

Running Loggair in distributed jobs, long-lived services and containers: which
process writes where, how log files are rotated and kept, how records reach an
aggregator or an operator, and how Loggair sits next to a framework that has its
own logging. Settings are named here and defined, with their environment
variables and defaults, in the [configuration reference](configuration.md).

## Ranks and processes

Loggair reads the rank of the current process from the environment, in this
order: `RANK` (torchrun), `SLURM_PROCID` (srun), an MPI launcher's own global
rank — `OMPI_COMM_WORLD_RANK` (Open MPI), `PMI_RANK` (MPICH, Intel MPI),
`PMIX_RANK` (PMIx), `MV2_COMM_WORLD_RANK` (MVAPICH2) — else `LOCAL_RANK`
combined with the node topology (`NODE_RANK` or `GROUP_RANK`, times
`LOCAL_WORLD_SIZE`). That covers `torchrun`, `srun`, `mpirun` / `mpiexec`, and
any launcher that sets one of those variables;
[`python -m loggair`](configuration.md#diagnostics) prints the detected rank and
the variable that decided it.

- The **console sink** exists on rank 0 of the main process only, so a message
  logged by every rank appears once on screen.
- The **file sink** exists in every process. Records from non-zero ranks are
  tagged `[rank N] | ` in the default formats (and as `extra.rank_tag` in JSON).
- A **spawn-started child** has no sinks of its own: call `configure_logging()`
  or `get_logger()` in the child, as [`examples/mp_demo.py`](../examples/mp_demo.py)
  does. The main process exports `LOGGAIR_SCRIPT_NAME` so children resolve the
  same file. A **fork-started child** inherits the parent's sinks and writes
  through them.

```python
# Console: once, from rank 0. File: every rank, tagged.
from loggair import get_logger

logger = get_logger(__name__)
logger.info("Initializing process group...")
```

### Several processes, one file

Several writers appending to one file interleave correctly only when records go
through a queue. `enqueue: true` routes the file sink through a multiprocessing
queue — allocated lazily, the first time the process is about to fork or spawn a
child, so a single-process run pays nothing for it (no POSIX semaphore, no
resource tracker). `LOGGAIR_EAGER_ENQUEUE=true` allocates the queue at configure
time instead.

Two more switches for worker processes:

- `LOGGAIR_DISABLE_MULTIPROCESS_LOGGING=1` keeps the main process logging and
  hands every child a no-op logger — for DataLoader workers that would re-emit
  the same lines ([details](configuration.md#disabling-logging)).
- A `module_levels` rule with `workers_only: true` changes a logger's threshold
  in child processes only
  ([details](configuration.md#per-logger-per-sink-level-overrides)).

The alternative to a shared file is a file per writer.

## Per-worker log files

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
[runtime rotation](#runtime-rotation)** too, and no rotation can ever split
another process's stream. The
[startup sweep](#startup-rotation-retention-and-compression) still never
deletes the bare per-worker files (live-log-safe rule) — only their
timestamped archives, pruned per stem as usual.

Merge the streams at read time — [lnav](#reading-logs-with-lnav) interleaves the
whole directory chronologically.

## Startup rotation, retention and compression

Every run gets a fresh log file. When a run starts and `{name}.log` already
exists, Loggair renames it to `{name}.{timestamp}.log` (`rotation_on_startup`,
on by default) and then prunes that stem's archives down to `retention` (default
5; `retention: 0` keeps none). Only rank 0 of the main process rotates a shared
file.

`retention` is enforced **per script name (stem)**, counting only *timestamped
archives* (`{name}.{timestamp}.log`, compressed or plain). On startup, Loggair
prunes each stem's archives — including stems left behind by earlier runs under
other script names — down to `retention`.

A bare `{name}.log` of **another** script is **never** deleted: in a shared log
directory (e.g. a centralized `~/logs`) it may be the live sink of a
concurrently running process. Only that script's own next run rotates it.

Set `compression` to gzip/zip the archives:

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
> argument, which would never fire here. Compression is best-effort: on failure
> the plain archive is kept.

## Runtime rotation

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
> **Setting [`worker_files: true`](#per-worker-log-files) removes this caveat
> entirely** — every process owns its own file and rotates it safely.

After a few days of a DDP run with `rotation: "100 MB"`, `compression: gz` and
`worker_files: true`, a shared log directory holds:

```
train.log                                   # main process, live
train.2026-10-01_09-00-02.log.gz            # startup archive: Loggair renamed and compressed it
train.2026-10-01_14-31-07_381204.log.gz     # runtime archive: loguru, microseconds suffix
train.rank2.log                             # rank 2's own live file
other_script.log                            # another script's live sink: never a candidate
```

## Structured JSON logging

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

## Experiment context

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
- In [JSON mode](#structured-json-logging) each field also lands
  individually in `record.extra` (`extra.epoch`, `extra.step`, ...) for log
  aggregators. An explicit `logger.bind(epoch=...)` wins over the ambient
  context.
- The context is **process-wide** (a checkpoint-saver thread's records carry
  the epoch the training thread set) and inherited by fork-started workers;
  spawn-started workers begin empty. Reads are lock-free — near-zero
  per-record cost.

## Alerting

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

## Interception modes

By default (`intercept: full`) Loggair **owns** standard-library logging — which
is what PyTorch, TensorFlow (through `absl`) and JAX log through: the root
handler is replaced and existing named loggers are stripped so everything
propagates into Loggair, and `warnings.warn()` is redirected too. Two knobs relax
this for embedding:

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

## Embedding and teardown

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

A host whose stderr is captured or relayed (an MCP server, a GUI extension)
turns console colors off for itself with `loggair.force_no_color()` at import —
see [Console coloring](configuration.md#console-coloring).

## Reading logs with lnav

For the best experience viewing Loggair logs — especially interleaving logs from
multiple ranks/workers — use **[lnav](https://lnav.org/)** (The Log File
Navigator). It detects Loggair's timestamp format and merges multiple log files
into a single, chronological view:

```bash
# View all logs in the directory interleaved by time
lnav ./logs        # app.log + app.rank*.log + app.*worker*.log, one timeline
```

For more information, see the **[lnav documentation](https://docs.lnav.org/)**.

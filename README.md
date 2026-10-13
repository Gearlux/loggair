# Loggair

Multiprocess-safe logging for machine-learning and HPC runs, built on
[Loguru](https://github.com/Delgan/loguru).

One `get_logger()` call gives every process of a distributed job a working setup:
the console shows rank 0 only, the file keeps every rank, each run starts a fresh
log file, and standard-library logging from PyTorch, TensorFlow, JAX and friends
lands in the same stream. Everything else is a setting — a YAML key, an
environment variable, or a keyword argument.

## Install

```bash
pip install loggair                     # depends on loguru and pyyaml only
pip install "loggair[alerts]"           # + Slack/Teams/email alerting via apprise
```

Python 3.12 or newer.

## Quick start

```python
# train.py
from loggair import get_logger

logger = get_logger(__name__)

logger.info("Starting training loop...")
logger.debug("batch_size=32 lr=0.001")
logger.success("Model checkpoint saved!")
```

The console (stderr) shows `INFO` and above:

```
2026-10-13 12:16:29 | INFO     | loggair.core:configure_logging:942 | Loggair initialized: train.log
2026-10-13 12:16:29 | INFO     | __main__:<module>:5 | Starting training loop...
2026-10-13 12:16:29 | SUCCESS  | __main__:<module>:7 | Model checkpoint saved!
```

`./logs/train.log` keeps `DEBUG` and above, with millisecond timestamps:

```
2026-10-13 12:16:29.221 | INFO     | loggair.core:configure_logging:942 | Loggair initialized: train.log
2026-10-13 12:16:29.222 | INFO     | __main__:<module>:5 | Starting training loop...
2026-10-13 12:16:29.222 | DEBUG    | __main__:<module>:6 | batch_size=32 lr=0.001
2026-10-13 12:16:29.222 | SUCCESS  | __main__:<module>:7 | Model checkpoint saved!
```

What happened without any configuration:

- The first `get_logger()` configured Loggair. The log file is named after the
  running script and lives in `./logs`.
- A `train.log` left by an earlier run was archived as `train.<timestamp>.log`
  first; the last 5 archives are kept.
- Standard-library `logging` and `warnings` were redirected into the same two
  sinks.
- In a `torchrun` or SLURM job the same script prints from rank 0 only; the file
  keeps every rank, tagged `[rank N]`.

Levels are Loguru's — `TRACE`, `DEBUG`, `INFO`, `SUCCESS`, `WARNING`, `ERROR`,
`CRITICAL` — plus `TRAIL` for call tracing. `get_logger(name)` mirrors
`logging.getLogger(name)`: the name becomes the `{name}` field and takes part in
per-logger level rules.

## Configure

Settings resolve, highest priority first: keyword arguments to
`configure_logging()`, `LOGGAIR_*` environment variables, a `loggair.yaml` in the
working directory, `[tool.loggair]` in `pyproject.toml`,
`~/.config/loggair/config.yaml`, then the defaults. Use whichever front door fits:

```python
from loggair import configure_logging

configure_logging(log_dir="./experiment_logs", console_level="DEBUG")   # before the first get_logger()
```

```yaml
# loggair.yaml
log_dir: ./experiment_logs
console_level: DEBUG
retention: 10
```

```bash
export LOGGAIR_DIR=/var/log/myapp LOGGAIR_CONSOLE_LEVEL=ERROR
```

Every setting, its environment variable and its default are in the
[configuration reference][configuration]; [`loggair.example.yaml`][example-yaml]
is the annotated template.

## Common tasks

| I want to… | Setting or call | Read more |
| --- | --- | --- |
| quiet a chatty library on one sink only | `module_levels: {"httpx": {file: WARNING}}` | [per-logger levels][module-levels] |
| give every rank and DataLoader worker its own file | `worker_files: true` | [per-worker files][worker-files] |
| bound the log of a job that never restarts | `rotation: "100 MB"` (or `daily`, `00:00`) | [runtime rotation][rotation] |
| compress archived logs | `compression: gz` | [rotation and retention][retention] |
| ship JSON lines to Elasticsearch, Loki or Datadog | `serialize: true` | [structured logging][json] |
| stamp `epoch` and `step` on every record | `loggair.set_context(epoch=3)` | [experiment context][context] |
| get Slack, Teams or email on `ERROR` | `alert_urls: ["slack://..."]` with `pip install "loggair[alerts]"` | [alerting][alerts] |
| raise a running job to `DEBUG` | `kill -USR2 <pid>` after `debug_signal: SIGUSR2`, or `loggair.reconfigure(console_level="DEBUG")` | [runtime changes][runtime] |
| embed next to uvicorn or gunicorn | `intercept: coexist`, `intercept_exclude: ["uvicorn"]` | [interception modes][interception] |
| log when functions are entered and left, with arguments | `@track` / `@spy` | [call tracing][tracing] |
| switch logging off entirely | `LOGGAIR_DISABLE_LOGGING=1` | [disabling logging][disable] |
| see what a process would be configured with | `python -m loggair` | [diagnostics][diagnostics] |

## Distributed training

No `if rank == 0:` around log calls:

```python
from loggair import get_logger

logger = get_logger(__name__)
logger.info("Initializing process group...")   # console: once, from rank 0; file: every rank
```

Ranks are read from `RANK`, `SLURM_PROCID`, or `LOCAL_RANK` with the node
topology. `enqueue: true` routes a shared file through a multiprocessing queue
that is allocated only when the first child process appears; `worker_files: true`
gives each writer its own file instead. The [operations guide][operations] covers
both, with rotation, retention and alerting for long-lived jobs.

## Call tracing

```python
from loggair import spy

@spy
def train(model, epochs=2, lr=0.001): ...
```

```
TRAIL | myapp:train:21 | → train(model=<myapp.Model object at 0x10883dd00>, epochs=7, lr=0.001) [from myapp:main:76]
TRAIL | myapp:train:21 | ← train (0.0 ms)
```

Records are emitted at `TRAIL`, a level between `TRACE` and `DEBUG`, so nothing
appears until a sink or a `module_levels` rule is set that low. Switched off, a
decorated call costs about 0.08 µs. `@track` logs entry and exit without the
arguments; both decorate whole classes. The [tracing guide][tracing] has the
options, the costs, and how configured objects render as their configuration.

## Diagnose

```console
$ python -m loggair
loggair 0.2.0  (loguru 0.7.3, python 3.13.14)
process:  MainProcess (pid 29487)
rank:     none detected (single-process, or rank 0 by default)

config files (highest priority first):
  [not found] loggair.yaml
  ...
resolved settings (args omitted — env > files > defaults):
  log_dir: "/home/me/project/logs"
  ...
```

Read-only: it configures nothing, rotates nothing and creates no directory, so it
is safe beside a live run. `--json` emits the report for scripts; inside
`torchrun` or `srun` it shows each rank's detected value. Details in the
[configuration reference][diagnostics].

## Documentation

| Document | What it answers |
| --- | --- |
| [Configuration reference][configuration] | Every setting and where it can be set; per-logger levels, coloring, formats, runtime changes, introspection, the diagnostic |
| [Operations guide][operations] | Ranks and worker files, rotation and retention, JSON output, alerting, interception modes, experiment context, embedding and teardown |
| [Call tracing][tracing] | `@track` and `@spy`, the `TRAIL` level, class decoration, inherited methods, parameter rendering, cost |
| [`loggair.example.yaml`][example-yaml] | Every key with its default, its environment variable and a comment |
| [Architecture decisions][architecture] | Why it is built this way: rotation ownership, computed sink floors, signal handling, the tracing descriptor |
| [Rationale][rationale] | How Loggair compares with stdlib `logging`, Loguru alone and framework loggers |
| [Changelog][changelog] and [roadmap][roadmap] | What each release changed; what is planned |
| [`examples/`][examples] | `mp_demo.py` (four spawned workers, one file) and `track_demo.py` (every tracing example) |

## License

MIT

[configuration]: https://github.com/Gearlux/loggair/blob/main/docs/configuration.md
[module-levels]: https://github.com/Gearlux/loggair/blob/main/docs/configuration.md#per-logger-per-sink-level-overrides
[runtime]: https://github.com/Gearlux/loggair/blob/main/docs/configuration.md#changing-settings-in-a-running-process
[disable]: https://github.com/Gearlux/loggair/blob/main/docs/configuration.md#disabling-logging
[diagnostics]: https://github.com/Gearlux/loggair/blob/main/docs/configuration.md#diagnostics
[operations]: https://github.com/Gearlux/loggair/blob/main/docs/operations.md
[worker-files]: https://github.com/Gearlux/loggair/blob/main/docs/operations.md#per-worker-log-files
[retention]: https://github.com/Gearlux/loggair/blob/main/docs/operations.md#startup-rotation-retention-and-compression
[rotation]: https://github.com/Gearlux/loggair/blob/main/docs/operations.md#runtime-rotation
[json]: https://github.com/Gearlux/loggair/blob/main/docs/operations.md#structured-json-logging
[context]: https://github.com/Gearlux/loggair/blob/main/docs/operations.md#experiment-context
[alerts]: https://github.com/Gearlux/loggair/blob/main/docs/operations.md#alerting
[interception]: https://github.com/Gearlux/loggair/blob/main/docs/operations.md#interception-modes
[tracing]: https://github.com/Gearlux/loggair/blob/main/docs/tracing.md
[example-yaml]: https://github.com/Gearlux/loggair/blob/main/loggair.example.yaml
[architecture]: https://github.com/Gearlux/loggair/blob/main/docs/architecture.md
[rationale]: https://github.com/Gearlux/loggair/blob/main/RATIONALE.md
[changelog]: https://github.com/Gearlux/loggair/blob/main/CHANGELOG.md
[roadmap]: https://github.com/Gearlux/loggair/blob/main/FUTURE_DEVELOPMENT.md
[examples]: https://github.com/Gearlux/loggair/tree/main/examples

# Loggair — Architecture Decisions

Design rationale for mechanisms whose existence or shape is not obvious from the
user documentation: *why* something exists, what was deliberately rejected, and
what a change to it would ripple into.

The user-facing surface — how to configure a sink, how to use a decorator, how to
register your own extension — lives in [`README.md`](../README.md). The
agent-facing rulebook lives in [`AGENTS.md`](../AGENTS.md). This file answers
"why is it built this way?"; those answer "how do I use it?" and "what must I not
break?".

---

## The `TRAIL` level is severity 7, and it is registered at package import

*2026-08-21, revised 2026-08-22*

### Context

Call tracing is the most voluminous output a logging system produces — two
records for every call of every decorated function. It needs a level of its own,
so it can be switched on without also switching on everything else at `DEBUG`.

The instinct is to put it *below* `TRACE` (severity 5), so that even a run at
`TRACE` stays free of it. At the time this was decided that was impossible: the
sinks carried a hardcoded `level="TRACE"` floor, which pinned loguru's
`core.min_level` to 5, and a level registered at 3 produced no output at all.

That constraint no longer exists — `_sink_floor` now computes each floor from the
configured levels (see the sink-floor record below), and a sub-`TRACE` level does
reach a sink. **The placement is therefore a choice, not a discovery**, and the
choice is unchanged: call tracing is noisier than tracing, so it sits under it.

A second constraint fixes *when* the level is registered, and that one still
holds. `resolve_settings()`
turns configured level names into numbers through `logger.level(name).no`, so a
config file carrying `file_level: TRAIL` fails at configure time if nothing has
registered the level yet:

```
ValueError: Level 'TRAIL' does not exist
```

Configuration must not depend on whether the user happened to import a particular
submodule first.

### Decision

`TRAIL` is registered at severity **7**, between `TRACE` (5) and `DEBUG` (10), by
`loggair/track.py` at module scope — and `loggair/__init__.py` imports that module
for the side effect as much as for the exports. Registration is
look-up-then-register, because re-registering an existing level raises.
`NullLogger._LEVELS` carries the same number so the kill-switch path answers
`level("TRAIL").no` consistently.

### Consequences

- A sink at `TRACE` shows call tracing too. Accepted deliberately: `TRACE` is the
  "everything" level, and call tracing belongs under it.
- No change to the configuration surface: `console_level`, `LOGGAIR_FILE_LEVEL`,
  `module_levels` and the diagnostic all resolve `TRAIL` through the same code
  path as every built-in level.
- Importing `loggair` now imports `loggair.track`, which imports `yaml` — already
  a hard dependency — and nothing else at module scope.
- Moving the level below `TRACE` requires first changing the sink floors (see the
  next record), and would silently emit nothing until that lands.

### Example

```python
import loggair                       # TRAIL is registered here
loggair.configure_logging(console_level="TRAIL")
```

```yaml
module_levels:
  mypkg.io:
    file: TRAIL          # trace one module, to the file sink only
```

### What you may change

The severity number, including moving it below `TRACE` now that the floors are
computed — that is a product decision to take with the user, not a refactor. The
colour. Whether more levels are registered alongside it. What you may not change
is the registration happening at package import.

---

## The tracing decorators gate themselves instead of relying on `opt(lazy=True)`

*2026-08-21*

### Context

The standard loguru recipe for a tracing decorator wraps the call in
`logger.opt(lazy=True)` and passes callables for anything expensive to render, on
the understanding that loguru will not invoke them unless the level is enabled.
That understanding is wrong: loguru evaluates lazy arguments *after* the
`min_level` check the record has already passed, so they run for every record a
filter goes on to drop. Confirmed directly — a `lazy=` callable ran on 50 000 of
50 000 calls while both sinks discarded every record.

Fixing the sink floors (next record) restores loguru's early-out, but does not
finish the job, for a reason that matters: **the floor is per-sink, and a
decorator's question is per-logger**. A floor must accommodate the lowest
threshold any `module_levels` rule can impose on that sink, so one promoting rule
anywhere drops the floor for everything on it. With `file_level: INFO` and a rule
promoting `pkg.a` to `DEBUG`, the file floor is 10, and a `DEBUG` from `pkg.b`
clears it and is built in full before the filter discards it at that logger's real
threshold of 20 — 3.37 µs, the unfixed cost.

Measured on a decorated no-op function with `TRAIL` not enabled:

| | per call |
| --- | --- |
| undecorated call | 0.027 µs |
| decorated, entering loguru per call | 0.687 µs (two entries: enter and leave) |
| decorated, gating on `effective_level_no` | 0.085 µs |

The remaining 8× is the difference between "you may leave `@track` on your hot
path" and "you may not", which is what the decorator is for.

### Decision

`core.effective_level_no(name)` computes the lowest level number that can still
reach *any* sink for a given logger, applying the same `module_levels` rules the
sink filters apply — through `_matching_rule`, which both call, so the advertised
threshold cannot drift from the one records are gated on. Each wrapper caches
that number and compares against it before touching loguru:

```python
if generation != LoggingState.generation:
    log = get_logger(module)
    threshold = effective_level_no(module)
    generation = LoggingState.generation
if level_no < threshold:
    return func(*args, **kwargs)
```

The cache is invalidated by `LoggingState.generation`, an integer bumped by every
configure, reconfigure and reset — so a `reconfigure(console_level="TRAIL")`
switches live decorated functions on without any per-call resolution cost.

`effective_level_no` is public, because any caller with an expensive log payload
has the same problem.

### Consequences

- A switched-off decorator costs roughly one extra function call.
- The threshold is a *snapshot*. It is refreshed on generation change, not on
  every record, which is what makes it cheap; a mechanism that changed a
  threshold without touching the generation would go unnoticed by the guard.
- `effective_level_no` reads the live dispatcher to decide whether the alerting
  sink exists, rather than the settings snapshot — `active_config` strips the
  `_`-prefixed internals, and the raw alert URLs are among them because they
  carry tokens.
- It is read-only and never triggers the lazy configuration; an unconfigured
  Loggair has no sinks, so nothing is reachable and the decorator stays inert
  until something configures.

### Example

```python
from loguru import logger
from loggair.core import effective_level_no

if logger.level("TRACE").no >= effective_level_no(__name__):
    logger.trace("tensor stats: {}", expensive_summary(batch))
```

### What you may change

The caching strategy, as long as a `reconfigure` still takes effect. The set of
sinks considered. What you may not remove is the guard itself: the sink floors
are already computed and it is still worth 8×, because per-sink and per-logger
are different questions.

---

## Sink level floors are computed, and every rule counts — including inert ones

*2026-08-22*

### Context

Loggair gates levels in `_make_sink_filter`, not at the loguru handler, because a
`module_levels` rule must be able to *lower* a threshold for one logger as well as
raise it — and a handler's own `level=` can only raise. So both sinks were added
with the floor pinned open:

```python
logger.add(sys.stderr, level="TRACE", filter=_make_sink_filter("console", c_no, module_rules), ...)
```

The filter got to make every decision, which was the point. The cost was that
loguru's one cheap early-out is keyed to that floor:

```python
if level_no < core.min_level:      # loguru/_logger.py, Logger._log
    return
```

`core.min_level` is the minimum across handlers. Pinned to 5, no record ever took
that branch: every `logger.debug()` in a process configured at `INFO` built its
full record — frame inspection, `aware_now()`, the record dict, argument
formatting — and was thrown away by the filter at the end. Measured: **3.4 µs**
per discarded call, in every consuming project, for logging nobody asked for.

### Decision

Each sink's floor is the lowest threshold its own filter can ever apply:

```python
def _sink_floor(sink, global_no, rules):
    thresholds = [global_no]
    for rule in rules:
        override = rule.console_no if sink == "console" else rule.file_no
        if override is not None:
            thresholds.append(override)
    return min(thresholds)
```

By construction no record the filter would have accepted is dropped before
reaching it, so the filter remains the authority on every decision it used to
make. Measured after: **0.10 µs** for the same discarded call.

### Consequences

- Every disabled `trace()` / `debug()` call in every consumer gets ~32× cheaper.
- Sub-`TRACE` levels became reachable, which removes the constraint that
  originally forced `TRAIL` to sit above `TRACE` (first record).
- The floor lives inside `file_sink_params`, so the lazy-enqueue re-add in
  `_upgrade_to_enqueue` carries it unchanged; it is recomputed wherever sinks are
  rebuilt (`configure_logging`, `reconfigure`, and both runtime signals, which
  route through `configure_logging(force=True)`).
- It does **not** subsume `effective_level_no`: this floor is per-sink, that
  function is per-logger (second record).
- An externally added sink at a lower level still lowers `core.min_level`
  globally; Loggair's own sinks then reject at their own handler level, exactly
  as before.

Two narrowings look free and are not, which is the real content of this record:

- **Using the global level alone** kills every promoted record — a rule setting
  `file: DEBUG` under a global `ERROR` stops working entirely.
- **Skipping `workers_only` rules** because they cannot match in the configuring
  process kills promoted records in *forked* children, which inherit the parent's
  handler objects and therefore this floor. Verified: the child's records vanish,
  and that variant passed the entire test suite — 251 of 251 — before a test was
  written for it.

### Example

Config, and what each variant does with it:

```yaml
file_level: "ERROR"
module_levels:
  "pkg.a":
    file: DEBUG
    workers_only: true
```

| | floor | `pkg.a` DEBUG in main | `pkg.a` DEBUG in a forked child |
| --- | --- | --- | --- |
| every rule counted (shipped) | 10 | dropped (correct) | **in the log** (correct) |
| `workers_only` skipped | 40 | dropped | **dropped** — wrong, and silent |

### What you may change

The set of sinks the floor is computed for, if a sink is added. What you may not
do is narrow which rules contribute to it without first re-reading the table
above — and `test_sink_floor_admits_a_workers_only_promotion_in_a_forked_child`
exists to make that failure loud instead of silent.

---

## A trace line locates one place; the caller is named separately

*2026-08-22*

### Context

A tracing decorator has two locations to report — where the traced function is
defined, and who called it — and loguru's record has room for one. The first
implementation tried to carry both in the same three fields: `record["name"]` was
patched to the decorated function's module (so `module_levels` rules could select
it), while `opt(depth=1)` filled `function` and `line` from the caller's frame.

Run against a real trainer, that produced:

```
sonair.lightning_classifier:_construct:1164 | → LightningClassifier.__init__(...)
```

`sonair/lightning_classifier.py` is 159 lines long, and `_construct` is in
`confluid/engine.py`. The reference cannot be opened, and nothing in the line says
it is a composite of two files.

### Decision

All four location fields — `name`, `function`, `line`, `file` — are stamped from the
decorated function. The caller is appended to the **message** as
`[from module:function:line]`, where it is labelled and cannot be mistaken for the
record's own location. Only the entry line carries it; on exit it would be noise.

Two details are load-bearing:

- The caller's frame comes from `sys._getframe`, not `inspect.stack()`. Measured:
  0.097 µs against 128 µs, on a path that runs for every traced call.
- The location is read from `inspect.unwrap(func)`, not from `func`. Another
  decorator applied underneath supplies its own `__code__` while `functools.wraps`
  copies `__module__` across, which recreates exactly the same mismatch one layer
  down — observed with a validation wrapper beneath `@spy`, where the trace claimed
  line 454 of a 160-line file.

### Consequences

- `module_levels` targeting keeps working, because the name field is still the
  decorated function's module.
- Entry lines are ~25 characters longer.
- `record["file"]` is set to a small duck-typed object exposing `.name` and
  `.path`; loguru's `RecordFile` is private, and both `{file}` and `{file.path}`
  render from the stand-in correctly.

### Example

```
mypkg.trainer:build:2 | → build(model='resnet', epochs=3) [from engine:_construct:3]
mypkg.trainer:build:2 | ← build (1.4 ms)
```

Every location in a real 176-record trace was checked against its source file: all
13 distinct ones resolve to the exact `def` line of the function they name.

### What you may change

The suffix format, and whether the exit line repeats it. What you may not do is
merge the two locations back into one field triple, or read the code object from a
function without unwrapping it first.

---

## Rich parameter rendering is an optional, lazily-imported integration

*2026-08-21*

### Context

`@spy` logs what a function was called with. For a plain value, `repr` is the
right answer. For an object assembled by a configuration framework, `repr` gives
a memory address, while the interesting fact is how the object was *configured* —
which model, how many layers, which weights.

[Confluid](https://pypi.org/project/confluid/) can answer that: `dump(obj)`
returns the document an object reconstructs from. But it depends on Loggair, and
Loggair is published standalone — so a module-level import would be both an
import cycle and a hard dependency on a package Loggair's own CI does not install.

Handing it every value would also be wrong on two counts measured directly:
`dump` costs ~116 µs against 0.3 µs for `repr`, and its dumper emits a
`logger.warning` for any type it cannot spell back into a document — a true and
useful statement about configuration round-tripping, and a meaningless one in a
call trace.

### Decision

The import is local to the rendering function and its failure is a plain fallback
to `repr`, not an error. Only objects carrying the framework's own marker attribute
take that path; everything else is `repr`, complete by default, bounded by an
explicit `cap` argument. The one-line flow-style rendering is produced by
re-reading the emitted document and re-emitting it in flow style, which works
because the document is deliberately plain YAML.

Values render complete rather than abbreviated by default because a trace that
quietly elides the one argument you were chasing has failed at its only job.

### Consequences

- Loggair's dependencies are unchanged: loguru and PyYAML.
- Tests covering this path use `pytest.importorskip`, since the framework is
  absent from Loggair's own CI.
- A user who installs nothing extra still gets a complete, useful trace.
- Widening the integration to arbitrary values would reintroduce both the cost
  and the spurious warnings.

### Example

```
→ train(model={_target_: ResNet, layers: 50, pretrained: true}, epochs=7)   # framework installed
→ train(model=<myapp.ResNet object at 0x10883dd00>, epochs=7)               # without it
```

### What you may change

Which marker selects the rich path, and the flow/block rendering. What must not
change is the laziness of the import and the fallback: a hard dependency here is
a dependency cycle.

---

## Class decoration modifies the class in place, and inherited tracing is bounded

*2026-08-21, extended 2026-08-22*

### Context

`@track` and `@spy` accept a class, tracing the methods it defines. The obvious
implementations are a subclass with overridden methods, or a wrapper object.

Both break things that classes in this ecosystem routinely carry. A configuration
framework registers a class under its identity; pickling a spawn-started worker's
objects requires the class to be findable at its original dotted path;
`isinstance` checks against the original name must keep working.

### Decision

The decorator rebinds the traced methods as attributes of the same class object
and returns that object. Static- and classmethods are unwrapped, traced, and
re-wrapped in the same descriptor kind. Properties are skipped — reading one to
decide anything would fire its getter, and derived state behind a property is
precisely what a logging decision must not evaluate. Inherited methods are left
alone: they belong to the base class, and tracing them there is the honest place
to do it.

Async and generator methods are skipped with a single `DEBUG` line naming them.
Decorating such a function *directly* raises instead, because the user named that
one function explicitly: a synchronous wrapper would time only the call that
creates the coroutine or generator, and report a confident `0.0 ms` exit for work
that has not started. A plausible lie in a debugging tool is worse than a refusal.

Inherited methods were originally left alone entirely. Running this against a real
trainer showed why that is not enough, and why the answer still cannot be "trace
everything": `sonair.LightningClassifier` defines **one** traceable method and
inherits **167**, distributed like this:

| | traceable methods |
| --- | --- |
| the class itself | 1 |
| its own project's bases (`sonair`, `matrainer`, `recordstream`) | 31 |
| `pytorch_lightning` + mixins | 92 |
| `torch.nn.Module` | 44 |

Tracing the bottom two groups means a log line on `nn.Module.forward` for every
batch. So `inherited=` takes `False` (the default), a base class as an **inclusive
MRO boundary**, or `True` for everything but `object`. The boundary form works
because a project's own bases sit at the top of the MRO and the framework's below
them — exactly the split the table shows.

Wrappers are installed on the **decorated class**, shadowing the inherited name,
never on the base. Writing to the base would trace every subclass in the process;
for an `nn.Module` base that is every model. A name the class defines itself always
wins, because the override is what actually runs.

### Consequences

- `SomeClass is decorated_SomeClass` — registration, pickling and `isinstance`
  are unaffected, because there is only ever one class object.
- Decoration is not reversible; the original functions are reachable only through
  each wrapper's `__wrapped__`.
- A class whose methods are added after decoration will not have them traced.
- `inherited=` on a plain function raises: a function has no bases, and silently
  accepting the argument would leave the author expecting traces that never come.
- Measured on that trainer: `inherited=LightningRunnable` took one run from 2 trace
  records to 176, covering `fit`, `training_step`, `validation_step`, the epoch
  hooks and `configure_optimizers`, with Lightning and torch untouched.

### Example

```python
@spy
class Pipeline:
    def __init__(self, source, batch_size=32): ...
    def run(self, limit=0): ...
    def _prepare(self): ...          # private: not traced
    @property
    def size(self): ...              # property: not traced, getter never fired
```

```
→ Pipeline.__init__(source='/data/train', batch_size=64)
→ Pipeline.run(limit=100)
```

### What you may change

Which names are in scope (`__init__` and `__call__` are traced today; other
dunders and `_private` names are not). The spelling of the `inherited=` bound. What
you may not change is the in-place mutation without checking what depends on class
identity, or the rule that wrappers land on the decorated class rather than the
base.

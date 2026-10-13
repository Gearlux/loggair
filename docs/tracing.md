# Call tracing: `@track` and `@spy`

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

[`examples/track_demo.py`](../examples/track_demo.py) runs every example in this
guide; run as a script its lines carry `__main__` where this guide writes `myapp`.

**Where the line points.** `{name}:{function}:{line}` locates the **decorated
function** — one real place you can open. The **caller** is named separately, in
`[from module:function:line]` at the end of the entry line. Keeping them apart
matters: fusing them produced references that could not exist, like
`mypkg.trainer:_construct:1164` where that file is 159 lines long and `_construct`
belongs to another package entirely. It also keeps `module_levels` targeting
honest — the name field is the decorated function's module, so a rule keyed to it
selects the functions you decorated rather than whoever calls them.

The location sees through other decorators: if something wraps your function below
`@track`, the trace still points at your definition, not at the intermediate
wrapper.

> **If nothing appears**, something probably configured Loggair before you did.
> The first `get_logger()` anywhere in the process — including inside an imported
> library — configures Loggair lazily, and a later plain `configure_logging(...)`
> is a no-op by design (see
> [Changing settings in a running process](configuration.md#changing-settings-in-a-running-process)).
> Set the level through a config file or `LOGGAIR_CONSOLE_LEVEL=TRAIL`, which the
> lazy configuration reads too, or call `reconfigure(console_level="TRAIL")`.

An exception is logged as an exit and re-raised unchanged:

```
TRAIL | myapp:crash:36 | → crash() [from myapp:main:80]
TRAIL | myapp:crash:36 | ← crash ✗ ValueError (0.0 ms)
```

Only the entry line carries the caller; repeating it on exit would double the noise.

## Turning it on for one module only

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
where the function is *defined* is what selects it. The matching rules are in the
[configuration reference](configuration.md#per-logger-per-sink-level-overrides).

## Cost

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

## Rendering parameter values

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

### Configured objects render as their configuration

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
→ fit(trackers=[{_target_: TensorBoardLogger, run_name: resnet18_mnist, save_dir: ./runs}],
      metrics={accuracy: {_target_: MulticlassAccuracy, _partial_: true, top_k: 1}})
```

Anything inside such a container that Confluid cannot describe is replaced by its
`repr` first, so a mixed list renders completely and the dumper's
"cannot reconstruct this" warning — true about configuration round-tripping,
meaningless in a call trace — never reaches your log.

Confluid is entirely optional — Loggair does not depend on it, imports it only
when a spied value carries its marker, and falls back to `repr` whenever it is
absent.

### Older Confluid renders as a block, not a memory address

The single-line form is produced by re-reading Confluid's document as YAML.
Confluid emitted `!class:X()` *tags* until 2026-08-11 — a form `yaml.safe_load`
refuses — and the plain-YAML dump arrived in 0.3.0, so every release up to v0.2.0
produces a document that cannot be re-read.

Loggair copes rather than pinning a floor it has no right to enforce: it probes the
round trip once, and where it fails renders the **indented block** instead. The
class name and every value survive; only the single line is lost. It says so once
per process:

```
WARNING | loggair: this confluid's dump() emits a document yaml.safe_load refuses
          (installed: 0.2.0; plain-YAML dumps arrived in 0.3.0), so @spy is rendering
          configuration as an indented block instead of one line. Pass multiline=True
          to choose the block form explicitly.
```

The check is of the *behaviour*, not the version number — a fork, a backport or a
git install can carry the fix while its metadata says otherwise, so the version is
reported rather than consulted.

## Decorating a class

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
| inherited methods | no — see [Tracing inherited methods](#tracing-inherited-methods) |
| `async def` and generator methods | no; skipped, with a `DEBUG` line naming them |

The class object itself is modified in place and returned, so `isinstance`
checks, pickling, and any registration the class already carries are unaffected.

## Tracing inherited methods

By default only the class's own methods are traced, and for a thin subclass that
can be almost nothing. A real example: a Lightning trainer class defined **one**
traceable method and inherited **167** — 136 of them from `pytorch_lightning` and
`torch.nn`. Tracing those would put a line on `nn.Module.forward` for every batch.

So inherited tracing is opt-in and bounded by `inherited=`:

```python
@spy                             # own methods only — the default
@spy(inherited="package")        # + bases from your own top-level package
@spy(inherited="source")         # + every base whose source you have
@spy(inherited=True)             # + installed code: the whole MRO except `object`
```

Measured on that trainer, the four scopes select very differently:

| `inherited=` | methods | reaches |
| --- | --- | --- |
| `False` / `"own"` | 1 | the class itself |
| `"package"` | 6 | its own project |
| `"source"` | 32 | + the other projects checked out beside it |
| `True` / `"all"` | 168 | + `pytorch_lightning`, `lightning_fabric`, `torch.nn` |

**`"source"` is the one you usually want.** A base counts as source when the module
defining it is not owned by the interpreter — not under `site-packages`, the
standard library, or the interpreter prefix. So your own code and anything you have
checked out and installed editable is traced, while installed packages are not.
Classes with no file at all — builtins, C extensions — are never source, because
there is nothing to read.

It is a **filter over the whole MRO**, not a walk that stops at the first installed
base, and that distinction is load-bearing: a real trainer's linearization puts the
standard library's `ABC` *between* two of its own classes, so stopping there would
silently drop everything below it.

Going deeper is a deliberate choice rather than an accident — `inherited=True`
reaches installed code, including `nn.Module.forward` on every batch.

For exact control, two other forms:

```python
@spy(inherited=TrainerBase)                # an inclusive MRO boundary
@spy(inherited=lambda base: ...)           # any predicate over base classes
```

A class names an **inclusive boundary**: everything from the decorated class down
to that base is traced, and everything past it is left alone. Naming a class that
is not a base raises, listing the bases that are. A predicate is the escape hatch
for a layout the source/installed split judges wrongly.

On the same trainer, naming its own base class as the boundary turned 2 trace
records per run into 176, covering `fit`, `training_step`, `validation_step`, the
epoch hooks and `configure_optimizers` — the actual machinery — while leaving
Lightning and torch untouched. A base method is traced under the class that
defines it, so you can see which layer you are in:

```
TRAIL | myapp:run:91 | → Job.run(steps=3) [from myapp:main:118]
TRAIL | myapp:warmup:73 | → Engine.warmup(seconds=1) [from myapp:run:92]
TRAIL | myapp:warmup:73 | ← Engine.warmup (0.0 ms)
TRAIL | myapp:run:91 | ← Job.run (0.0 ms)
```

That is `@spy(inherited="source")` on a class inheriting from both `Engine` (defined
alongside it) and `json.JSONEncoder` (installed). `Job.run` calls `self.encode({})`
as well, and no line appears for it — the scope reached one base and not the other.
`examples/track_demo.py` runs exactly this.

Wrappers are always installed on the **decorated class**, shadowing the inherited
method. The base class is never modified, so decorating one subclass does not trace
its siblings — or, for a `torch.nn.Module` base, every model in the process. A name
the class defines itself always wins over the inherited version.

### Tracing does not make a class look like it overrode something

Libraries ask "did the user implement this?" in two ways, and a naively installed
wrapper answers one of them wrongly. PyTorch uses raw identity:

```python
# torch/nn/modules/module.py
if getattr(self.__class__, "get_extra_state", Module.get_extra_state) is not Module.get_extra_state:
    destination[extra_state_key] = self.get_extra_state()
```

A wrapper on that name makes torch conclude the model implements `get_extra_state`,
call it, and hit the base's "should never be called" error — `state_dict()` dies at
the first checkpoint save, from nothing but switching tracing on.

So traced methods are installed as a descriptor that hands the **original** to
class-level lookups and the **wrapper** to instances. The identity check sees
exactly what it would have seen undecorated, `self.method()` is still traced, and a
genuine override still reads as one. `functools.wraps` is preserved, so the other
style — `__code__` comparison that unwraps `__wrapped__`, as
`lightning_utilities`' `is_overridden` does — keeps answering correctly too.

The cost: a class-level call, `Cls.method(obj)`, runs untraced. That is deliberate —
a missing trace line is an inconvenience, a broken `state_dict()` is a lost training
run. Static methods keep the plain wrapper, since for them class access *is* the
normal call form.

## What cannot be traced

Decorating an `async def` or a generator function directly raises `TypeError`.
A synchronous wrapper would time only the call that *creates* the coroutine or
generator — not the work it goes on to do — and report a confident `0.0 ms` exit
for a call that has not started. Trace what the coroutine awaits, or what the
generator does per item, instead.

## Other options

| argument | default | meaning |
| --- | --- | --- |
| `level` | `"TRAIL"` | Emit at another registered level, e.g. `@track(level="DEBUG")`. |
| `timing` | `True` | Append the elapsed wall time to the exit line. |
| `cap` | `None` | `@spy` only — truncate any rendered value past this many characters. |
| `multiline` | `False` | `@spy` only — render configuration documents as a YAML block. |
| `inherited` | `False` | Classes only — `"own"` / `"package"` / `"source"` / `"all"`, a base class as an inclusive boundary, or a predicate. |

## Gating your own expensive logging

The check the decorators use is public. Use it whenever building a log message
costs more than the branch that skips it:

```python
from loguru import logger
from loggair import effective_level_no

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

Why the level sits where it does, why the decorators gate themselves, and why the
class decoration is a descriptor are recorded in the
[architecture decisions](architecture.md).

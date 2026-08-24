"""Call tracing with @track and @spy — see README section 19.

Runs standalone: writes to ./logs under a temporary directory and prints the
console output it produced. Confluid is optional here (it is not a Loggair
dependency); without it, spied values fall back to their repr.

    python examples/track_demo.py
"""

import json
import tempfile

import loggair
from loggair import spy, track


@track
def load(path: str, n: int = 10) -> list:
    """Entry/exit only — no parameter values."""
    return [path] * n


@spy
def train(model: object, epochs: int = 2, lr: float = 0.001) -> float:
    """Entry/exit plus what it was called with."""
    return 0.42


@spy(cap=40)
def summarize(values: list) -> int:
    """`cap` bounds a long rendering instead of flooding the line."""
    return len(values)


@track
def crash() -> None:
    """An exception is logged as an exit, then re-raised unchanged."""
    raise ValueError("boom")


@spy
class Pipeline:
    """Decorating a class traces the methods it defines itself."""

    def __init__(self, source: str, batch_size: int = 32) -> None:
        self.source = source
        self.batch_size = batch_size

    def run(self, limit: int = 0) -> int:
        self._prepare()  # private: never traced
        return limit

    def _prepare(self) -> None:
        return None

    @property
    def size(self) -> int:
        return 1  # properties are never traced — reading one would fire this

    @staticmethod
    def describe(kind: str) -> str:
        return kind


class Model:
    """A plain object: spied by repr. Mark a class @configurable (Confluid) and
    it is rendered as its configuration document instead."""


class Engine:
    """A base class of YOUR OWN — this file, so `inherited="source"` reaches it."""

    def warmup(self, seconds: int = 1) -> None:
        return None


@spy(inherited="source")
class Job(Engine, json.JSONEncoder):
    """`inherited=` extends tracing to base classes, bounded by where the code lives.

    Without it a thin subclass traces almost nothing — its work lives in its bases.
    `"source"` reaches `Engine` (defined here) but not `json.JSONEncoder` (installed
    with the interpreter), which is the distinction that matters on a real framework
    subclass: one such trainer defines 1 traceable method and inherits 167, of which
    136 belong to installed packages.

    `inherited=True` would reach `JSONEncoder.encode` too; a base class names an
    inclusive MRO boundary; a callable is a predicate over bases.
    """

    def run(self, steps: int = 2) -> int:
        self.warmup()
        self.encode({})  # JSONEncoder's own method — installed, so NOT traced
        return steps


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        # TRAIL sits between TRACE and DEBUG; nothing is emitted until a sink
        # is set that low. Target one module with `module_levels` in real use.
        loggair.configure_logging(log_dir=tmp, script_name="track_demo", console_level="TRAIL")

        load("/data/x.csv", n=2)
        train(Model(), epochs=7)
        summarize([round(i * 0.111, 3) for i in range(40)])

        try:
            crash()
        except ValueError:
            pass

        pipeline = Pipeline("/data/train", batch_size=64)
        pipeline.run(limit=100)
        pipeline.size  # noqa: B018 - demonstrates that a property stays untraced
        Pipeline.describe("streaming")

        # `warmup` is Engine's and IS traced; `encode` is JSONEncoder's and is not.
        Job().run(steps=3)

        # Switched off, a decorated call costs ~0.08 us: the decorators check
        # whether TRAIL can reach a sink before building anything.
        loggair.reconfigure(console_level="INFO")
        load("/data/never-logged.csv")

        loggair.shutdown_logging()


if __name__ == "__main__":
    main()

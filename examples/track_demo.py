"""Call tracing with @track and @spy — see README section 19.

Runs standalone: writes to ./logs under a temporary directory and prints the
console output it produced. Confluid is optional here (it is not a Loggair
dependency); without it, spied values fall back to their repr.

    python examples/track_demo.py
"""

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
    """A base class. Its methods are traced only when a subclass asks for them."""

    def warmup(self, seconds: int = 1) -> None:
        return None


@spy(inherited=Engine)
class Job(Engine):
    """`inherited=` extends tracing to base classes, bounded by an MRO boundary.

    Without it a thin subclass traces almost nothing — its work lives in the base.
    With `inherited=True` on a framework subclass it traces far too much, which is
    why the bound is a class rather than a flag.
    """

    def run(self, steps: int = 2) -> int:
        self.warmup()
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

        # `warmup` is defined by Engine, and traced because Job asked for it.
        Job().run(steps=3)

        # Switched off, a decorated call costs ~0.08 us: the decorators check
        # whether TRAIL can reach a sink before building anything.
        loggair.reconfigure(console_level="INFO")
        load("/data/never-logged.csv")

        loggair.shutdown_logging()


if __name__ == "__main__":
    main()

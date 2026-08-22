"""Tests for the @track / @spy call decorators and the TRAIL level."""

import functools
import os
import re
from pathlib import Path
from typing import Any

import _trace_helpers
import pytest
from _trace_helpers import call_through
from loguru import logger

from loggair.core import configure_logging, effective_level_no, get_logger, reconfigure
from loggair.null_logger import NullLogger
from loggair.track import TRAIL, TRAIL_NO, spy, track

confluid = pytest.importorskip("confluid", reason="confluid is not a loggair dependency (it depends ON loggair)")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _configure(tmp_path: Path, level: str = "TRAIL", **kwargs: Any) -> Path:
    """Configure Loggair into a private log dir and return the log file path."""
    configure_logging(
        log_dir=str(tmp_path / "logs"),
        script_name="trk",
        console_level=level,
        file_level=level,
        rotation_on_startup=False,
        **kwargs,
    )
    return tmp_path / "logs" / "trk.log"


def _raw_lines(log_file: Path) -> list:
    """The TRAIL records in the log, message part verbatim (caller suffix included)."""
    if not log_file.exists():
        return []
    return [ln.split(" | ", 3)[-1] for ln in log_file.read_text().splitlines() if " TRAIL " in ln]


def _lines(log_file: Path) -> list:
    """:func:`_raw_lines` with the ``[from module:function:line]`` suffix removed.

    Every entry line carries it; stripping it here keeps the assertions about the
    MESSAGE readable. The suffix itself is asserted by the caller-specific tests.
    """
    return [re.sub(r" \[from [^\]]+\]$", "", ln) for ln in _raw_lines(log_file)]


class Boom:
    """A value whose repr EXPLODES — proves the renderer never runs while disabled."""

    def __repr__(self) -> str:
        raise AssertionError("repr() must not be called while TRAIL is disabled")


# --------------------------------------------------------------------------- #
# level registration
# --------------------------------------------------------------------------- #
def test_trail_level_is_registered_at_import_with_number_seven() -> None:
    assert TRAIL == "TRAIL"
    assert TRAIL_NO == 7
    assert logger.level("TRAIL").no == 7


def test_trail_sits_between_trace_and_debug() -> None:
    # Below TRACE is unreachable: both sinks are added with a hardcoded
    # level="TRACE" floor, and loguru early-returns under core.min_level.
    assert logger.level("TRACE").no < TRAIL_NO < logger.level("DEBUG").no


def test_registering_trail_twice_does_not_raise() -> None:
    from loggair.track import _register_trail

    _register_trail()
    _register_trail()
    assert logger.level("TRAIL").no == 7


def test_configure_logging_accepts_trail_as_a_sink_level(tmp_path: Path) -> None:
    # Regression: resolve_settings() -> _level_no() raises ValueError for an
    # unregistered level, so TRAIL MUST be registered when `loggair` is imported.
    log_file = _configure(tmp_path, "TRAIL")
    get_logger("x").log("TRAIL", "hello")
    assert "hello" in log_file.read_text()


def test_null_logger_knows_trail() -> None:
    assert NullLogger().level("TRAIL").no == 7


# --------------------------------------------------------------------------- #
# @track on a function
# --------------------------------------------------------------------------- #
def test_track_logs_entry_and_exit(tmp_path: Path) -> None:
    log_file = _configure(tmp_path)

    @track
    def load(path: str, n: int = 10) -> list:
        return [path] * n

    assert load("/data/x.csv", n=2) == ["/data/x.csv", "/data/x.csv"]
    msgs = _lines(log_file)
    assert len(msgs) == 2
    assert msgs[0] == "→ load()"
    assert msgs[1].startswith("← load (")


def test_track_does_not_log_parameters(tmp_path: Path) -> None:
    log_file = _configure(tmp_path)

    @track
    def f(secret: str) -> None:
        return None

    f("hunter2")
    assert "hunter2" not in log_file.read_text()


def test_track_exit_carries_elapsed_milliseconds(tmp_path: Path) -> None:
    log_file = _configure(tmp_path)

    @track
    def f() -> None:
        return None

    f()
    assert _lines(log_file)[1].endswith(" ms)")


def test_timing_false_omits_the_duration(tmp_path: Path) -> None:
    log_file = _configure(tmp_path)

    @track(timing=False)
    def f() -> None:
        return None

    f()
    assert _lines(log_file)[1] == "← f"


def test_track_logs_the_exception_and_reraises(tmp_path: Path) -> None:
    log_file = _configure(tmp_path)

    @track
    def crash() -> None:
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        crash()
    msgs = _lines(log_file)
    assert msgs[0] == "→ crash()"
    assert "✗ ValueError" in msgs[1]


def test_track_preserves_the_wrapped_function_identity(tmp_path: Path) -> None:
    _configure(tmp_path)

    @track
    def documented(a: int) -> int:
        """Doc survives."""
        return a

    assert documented.__name__ == "documented"
    assert documented.__doc__ == "Doc survives."
    assert getattr(documented, "__wrapped__").__name__ == "documented"
    assert documented(3) == 3


def test_track_accepts_an_explicit_level(tmp_path: Path) -> None:
    log_file = _configure(tmp_path, "DEBUG")

    @track(level="DEBUG")
    def f() -> None:
        return None

    f()
    assert "→ f()" in log_file.read_text()


def test_the_record_locates_the_decorated_function_not_the_caller(tmp_path: Path) -> None:
    """{name}:{function}:{line} must name ONE real place: the decorated function.

    It used to mix two — the decorated function's MODULE with the caller's function
    and line — which produced references like `sonair.lightning_classifier:_construct:1164`
    where that file is 159 lines long and `_construct` lives in confluid/engine.py.
    """
    log_file = _configure(tmp_path)

    @track
    def inner() -> None:
        return None

    call_through(inner)  # the caller is in tests/_trace_helpers.py

    line = getattr(inner, "__wrapped__").__code__.co_firstlineno
    located = f"{__name__}:inner:{line}"
    assert located in log_file.read_text()


def test_the_location_sees_through_another_decorator(tmp_path: Path) -> None:
    """A decorator applied BELOW ours hides the real definition behind its wrapper.

    `functools.wraps` copies `__module__` but NOT `__code__`, so reading the code
    object off the passed function mixes the original module with the WRAPPER's file
    and line — the same defect this location fix exists to remove, one layer down.
    Found on a real trainer, where confluid's validation wrap sits under `@spy` and
    the trace claimed line 454 of a 160-line file.
    """
    log_file = _configure(tmp_path)

    def validating(fn: Any) -> Any:
        @functools.wraps(fn)
        def inner(*args: Any, **kwargs: Any) -> Any:
            return fn(*args, **kwargs)

        return inner

    @track
    @validating
    def wrapped_twice() -> None:
        return None

    wrapped_twice()

    real = wrapped_twice.__wrapped__.__wrapped__.__code__
    located = f"{__name__}:wrapped_twice:{real.co_firstlineno}"
    text = log_file.read_text()
    assert located in text
    assert "track.py" not in text  # never our own wrapper's file


def test_the_message_names_the_caller(tmp_path: Path) -> None:
    """The caller is not lost — it moves into the message, where it can be honest."""
    log_file = _configure(tmp_path)

    @track
    def inner() -> None:
        return None

    call_through(inner)

    expected = f"[from {_trace_helpers.__name__}:call_through:{_trace_helpers.CALL_LINE}]"
    entry = _raw_lines(log_file)[0]
    assert entry.startswith("→ inner()")
    assert entry.endswith(expected)


def test_the_exit_line_does_not_repeat_the_caller(tmp_path: Path) -> None:
    """One call, one caller — repeating it on exit doubles the noise for nothing."""
    log_file = _configure(tmp_path)

    @track
    def inner() -> None:
        return None

    call_through(inner)
    assert "[from " not in _raw_lines(log_file)[1]


def test_record_name_is_the_decorated_functions_module(tmp_path: Path) -> None:
    # So `module_levels: {tests.test_track: {file: TRAIL}}` targets it.
    log_file = _configure(tmp_path)

    @track
    def f() -> None:
        return None

    f()
    assert f"{__name__}:" in log_file.read_text()


# --------------------------------------------------------------------------- #
# gating: the guard, and its invalidation
# --------------------------------------------------------------------------- #
def test_nothing_is_emitted_when_trail_is_below_both_sink_levels(tmp_path: Path) -> None:
    log_file = _configure(tmp_path, "INFO")

    @track
    def f() -> None:
        return None

    f()
    assert _lines(log_file) == []


def test_the_guard_short_circuits_before_rendering_anything(tmp_path: Path) -> None:
    # loguru's own lazy= gating does NOT defer here (both sinks sit at a TRACE
    # floor, so core.min_level is 5 and every TRAIL record is fully built).
    # The decorator must therefore gate itself: Boom's repr would raise.
    _configure(tmp_path, "INFO")

    @spy
    def f(value: Any) -> None:
        return None

    f(Boom())  # must not raise


def test_reconfigure_switches_a_live_decorated_function_on(tmp_path: Path) -> None:
    log_file = _configure(tmp_path, "INFO")

    @track
    def f() -> None:
        return None

    f()
    assert _lines(log_file) == []

    reconfigure(console_level="TRAIL", file_level="TRAIL")
    f()
    assert [m for m in _lines(log_file) if m.startswith("→ f")] == ["→ f()"]


def test_reconfigure_switches_a_live_decorated_function_off(tmp_path: Path) -> None:
    log_file = _configure(tmp_path, "TRAIL")

    @track
    def f() -> None:
        return None

    f()
    assert len(_lines(log_file)) == 2

    reconfigure(console_level="INFO", file_level="INFO")
    f()
    assert len(_lines(log_file)) == 2  # unchanged


def test_decorators_are_inert_when_logging_is_disabled(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("LOGGAIR_DISABLE_LOGGING", "1")

    @spy
    def f(value: Any) -> str:
        return "ok"

    assert f(Boom()) == "ok"  # Boom's repr must never run


def test_module_levels_can_enable_trail_for_one_module_only(tmp_path: Path) -> None:
    (tmp_path / "loggair.yaml").write_text(
        f'file_level: "INFO"\nconsole_level: "INFO"\nmodule_levels:\n  "{__name__}":\n    file: TRAIL\n'
    )
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        configure_logging(log_dir=str(tmp_path / "logs"), script_name="trk", rotation_on_startup=False)
        log_file = tmp_path / "logs" / "trk.log"

        @track
        def f() -> None:
            return None

        f()
        assert len(_lines(log_file)) == 2
    finally:
        os.chdir(old_cwd)


# --------------------------------------------------------------------------- #
# effective_level_no
# --------------------------------------------------------------------------- #
def test_effective_level_no_is_the_lowest_reachable_level(tmp_path: Path) -> None:
    _configure(tmp_path, "INFO")
    assert effective_level_no() == logger.level("INFO").no

    reconfigure(console_level="WARNING", file_level="DEBUG")
    assert effective_level_no() == logger.level("DEBUG").no


def test_effective_level_no_honours_module_levels(tmp_path: Path) -> None:
    (tmp_path / "loggair.yaml").write_text(
        'file_level: "INFO"\nconsole_level: "INFO"\nmodule_levels:\n  "pkg.io":\n    file: TRAIL\n'
    )
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        configure_logging(log_dir=str(tmp_path / "logs"), script_name="trk", rotation_on_startup=False)
        assert effective_level_no("pkg.io") == TRAIL_NO
        assert effective_level_no("pkg.io.reader") == TRAIL_NO
        assert effective_level_no("pkg.other") == logger.level("INFO").no
    finally:
        os.chdir(old_cwd)


def test_effective_level_no_reports_nothing_reachable_when_disabled(monkeypatch: Any) -> None:
    monkeypatch.setenv("LOGGAIR_DISABLE_LOGGING", "1")
    assert effective_level_no() > logger.level("CRITICAL").no


# --------------------------------------------------------------------------- #
# @spy parameter rendering
# --------------------------------------------------------------------------- #
def test_spy_logs_parameter_values_including_defaults(tmp_path: Path) -> None:
    log_file = _configure(tmp_path)

    @spy
    def train(path: str, epochs: int = 2, lr: float = 0.001) -> None:
        return None

    train("/data/x.csv", epochs=7)
    assert _lines(log_file)[0] == "→ train(path='/data/x.csv', epochs=7, lr=0.001)"


def test_spy_renders_a_configurable_as_one_flow_style_line(tmp_path: Path) -> None:
    log_file = _configure(tmp_path)

    @confluid.configurable
    class Model:
        def __init__(self, layers: int = 3, name: str = "resnet") -> None:
            self.layers = layers
            self.name = name

    @spy
    def fit(model: Any) -> None:
        return None

    fit(Model(layers=5))
    entry = _lines(log_file)[0]
    assert entry == "→ fit(model={_target_: Model, layers: 5, name: resnet})"
    assert "\n" not in entry


def test_spy_multiline_renders_the_yaml_block(tmp_path: Path) -> None:
    log_file = _configure(tmp_path)

    @confluid.configurable
    class Net:
        def __init__(self, layers: int = 3) -> None:
            self.layers = layers

    @spy(multiline=True)
    def fit(model: Any) -> None:
        return None

    fit(Net(layers=5))
    text = log_file.read_text()
    assert "_target_: Net" in text
    assert "layers: 5" in text
    assert "{_target_" not in text  # block style, not flow


def test_spy_renders_a_plain_object_with_its_complete_repr(tmp_path: Path) -> None:
    log_file = _configure(tmp_path)

    class Plain:
        def __repr__(self) -> str:
            return "Plain(" + "y" * 300 + ")"

    @spy
    def f(value: Any) -> None:
        return None

    f(Plain())
    assert "y" * 300 in log_file.read_text()


def test_cap_truncates_a_long_rendering(tmp_path: Path) -> None:
    log_file = _configure(tmp_path)

    @spy(cap=20)
    def f(value: str) -> None:
        return None

    f("z" * 500)
    entry = _lines(log_file)[0]
    assert "z" * 500 not in entry
    assert entry.endswith("…)")
    assert len(entry) < 60


def test_cap_leaves_a_short_rendering_untouched(tmp_path: Path) -> None:
    log_file = _configure(tmp_path)

    @spy(cap=100)
    def f(value: str) -> None:
        return None

    f("short")
    assert _lines(log_file)[0] == "→ f(value='short')"


def test_spy_falls_back_to_repr_when_confluid_is_unavailable(tmp_path: Path, monkeypatch: Any) -> None:
    # loggair is published standalone; confluid must never be a hard dependency.
    monkeypatch.setitem(__import__("sys").modules, "confluid", None)
    log_file = _configure(tmp_path)

    @confluid.configurable
    class Thing:
        def __init__(self, a: int = 1) -> None:
            self.a = a

    @spy
    def f(value: Any) -> None:
        return None

    f(Thing())
    entry = _lines(log_file)[0]
    assert "_target_" not in entry
    assert "Thing object at" in entry


def test_spy_renders_a_list_of_configurables_as_a_document(tmp_path: Path) -> None:
    """A `trackers: [ ... ]` slot is a LIST of configurables, and it must not repr.

    Observed on a real run: `trackers=[Target(matrainer.TensorBoardTracker, {...})]`,
    because the marker carries the configurable attribute but the list around it
    does not.
    """
    log_file = _configure(tmp_path)

    @confluid.configurable
    class Tracker:
        def __init__(self, run_name: str = "r1") -> None:
            self.run_name = run_name

    @spy
    def fit(trackers: Any) -> None:
        return None

    fit([Tracker(), Tracker(run_name="r2")])
    entry = _lines(log_file)[0]
    assert entry == "→ fit(trackers=[{_target_: Tracker, run_name: r1}, {_target_: Tracker, run_name: r2}])"


def test_spy_renders_a_dict_of_configurables_as_a_document(tmp_path: Path) -> None:
    """The `metrics: {name: template}` shape, likewise."""
    log_file = _configure(tmp_path)

    @confluid.configurable
    class Metric:
        def __init__(self, top_k: int = 1) -> None:
            self.top_k = top_k

    @spy
    def fit(metrics: Any) -> None:
        return None

    fit({"accuracy": Metric(), "recall": Metric(top_k=5)})
    entry = _lines(log_file)[0]
    assert entry == "→ fit(metrics={accuracy: {_target_: Metric, top_k: 1}, recall: {_target_: Metric, top_k: 5}})"


def test_a_container_of_plain_values_still_uses_repr(tmp_path: Path) -> None:
    """No configurable inside means no reason to pay for a YAML round trip."""
    log_file = _configure(tmp_path)

    @spy
    def f(values: Any) -> None:
        return None

    f([1, 2, 3])
    assert _lines(log_file)[0] == "→ f(values=[1, 2, 3])"


def test_a_mixed_container_renders_without_warning(tmp_path: Path) -> None:
    """A non-spellable neighbour must not drag a `logger.warning` into the trace.

    Confluid's dumper warns (once per type) for anything it cannot reconstruct;
    that is a statement about config round-tripping and means nothing here, so
    unspellable values are projected to their repr BEFORE the dump.
    """
    log_file = _configure(tmp_path)

    @confluid.configurable
    class MixedThing:
        def __init__(self, a: int = 1) -> None:
            self.a = a

    class Opaque:
        def __repr__(self) -> str:
            return "<opaque>"

    @spy
    def f(items: Any) -> None:
        return None

    f([MixedThing(), Opaque()])
    entry = _lines(log_file)[0]
    assert "_target_: MixedThing" in entry
    assert "<opaque>" in entry
    # The dumper's own "cannot reconstruct this" warning must never fire here.
    assert "no reconstructible document spelling" not in log_file.read_text()


def test_spy_logs_the_exception_and_reraises(tmp_path: Path) -> None:
    log_file = _configure(tmp_path)

    @spy
    def crash(x: int) -> None:
        raise KeyError("k")

    with pytest.raises(KeyError):
        crash(1)
    msgs = _lines(log_file)
    assert msgs[0] == "→ crash(x=1)"
    assert "✗ KeyError" in msgs[1]


# --------------------------------------------------------------------------- #
# class decoration
# --------------------------------------------------------------------------- #
def test_track_on_a_class_wraps_its_public_methods(tmp_path: Path) -> None:
    log_file = _configure(tmp_path)

    @track
    class Op:
        def run(self) -> str:
            return "ran"

    assert Op().run() == "ran"
    msgs = _lines(log_file)
    assert msgs[0] == "→ Op.run()"
    assert msgs[1].startswith("← Op.run")


def test_class_decoration_wraps_init_and_call(tmp_path: Path) -> None:
    log_file = _configure(tmp_path)

    @track
    class Op:
        def __init__(self, a: int = 1) -> None:
            self.a = a

        def __call__(self) -> int:
            return self.a

    Op(2)()
    msgs = _lines(log_file)
    assert "→ Op.__init__()" in msgs
    assert "→ Op.__call__()" in msgs


def test_class_decoration_skips_private_methods_and_other_dunders(tmp_path: Path) -> None:
    log_file = _configure(tmp_path)

    @track
    class Op:
        def _helper(self) -> None:
            return None

        def __repr__(self) -> str:
            return "Op()"

        def run(self) -> None:
            self._helper()
            repr(self)

    Op().run()
    text = log_file.read_text()
    assert "_helper" not in text
    assert "__repr__" not in text


def test_class_decoration_leaves_inherited_methods_alone(tmp_path: Path) -> None:
    log_file = _configure(tmp_path)

    class Base:
        def inherited(self) -> None:
            return None

    @track
    class Child(Base):
        def own(self) -> None:
            return None

    c = Child()
    c.inherited()
    c.own()
    msgs = _lines(log_file)
    assert not [m for m in msgs if "inherited" in m]
    assert "→ Child.own()" in msgs


def test_class_decoration_never_wraps_a_property(tmp_path: Path) -> None:
    log_file = _configure(tmp_path)
    fired = []

    @track
    class Op:
        @property
        def value(self) -> int:
            fired.append(1)
            return 42

    assert Op().value == 42
    assert fired == [1]
    assert "value" not in log_file.read_text()


def test_class_decoration_preserves_static_and_class_methods(tmp_path: Path) -> None:
    log_file = _configure(tmp_path)

    @track
    class Op:
        @staticmethod
        def stat(a: int) -> int:
            return a

        @classmethod
        def cls_m(cls, a: int) -> int:
            return a

    assert isinstance(Op.__dict__["stat"], staticmethod)
    assert isinstance(Op.__dict__["cls_m"], classmethod)
    assert Op.stat(1) == 1
    assert Op.cls_m(2) == 2
    msgs = _lines(log_file)
    assert "→ Op.stat()" in msgs
    assert "→ Op.cls_m()" in msgs


def test_spy_on_a_class_drops_self_and_cls_from_the_parameters(tmp_path: Path) -> None:
    log_file = _configure(tmp_path)

    @spy
    class Op:
        def run(self, a: int, b: int = 2) -> None:
            return None

        @classmethod
        def make(cls, a: int) -> None:
            return None

    Op().run(1)
    Op.make(9)
    msgs = _lines(log_file)
    assert "→ Op.run(a=1, b=2)" in msgs
    assert "→ Op.make(a=9)" in msgs


def test_class_decoration_returns_the_same_class_object(tmp_path: Path) -> None:
    _configure(tmp_path)

    class Op:
        def run(self) -> None:
            return None

    before = Op
    after = track(Op)
    assert after is before
    assert isinstance(Op(), Op)


def test_class_decoration_skips_generator_and_async_methods(tmp_path: Path) -> None:
    log_file = _configure(tmp_path)

    @track
    class Op:
        def stream(self) -> Any:
            yield 1

        async def fetch(self) -> int:
            return 1

        def run(self) -> None:
            return None

    assert list(Op().stream()) == [1]
    Op().run()
    msgs = _lines(log_file)
    assert not [m for m in msgs if "stream" in m or "fetch" in m]
    assert "→ Op.run()" in msgs
    # Skipped, but discoverable — at DEBUG, not as an operator-facing warning.
    notice = [ln for ln in log_file.read_text().splitlines() if "left untraced" in ln]
    assert len(notice) == 1
    assert " DEBUG " in notice[0]
    assert "Op.{stream, fetch}" in notice[0]


# --- inherited= ---------------------------------------------------------------
#
# A leaf class in this workspace is often a thin config surface: sonair's
# LightningClassifier defines ONE traceable method and inherits 167, of which 136
# come from pytorch_lightning and torch.nn. So inherited wrapping has to be
# opt-in AND bounded, and it must never mutate the base class.


class _Base:
    def base_method(self) -> str:
        return "base"

    def shared(self) -> str:
        return "from base"


class _Middle(_Base):
    def middle_method(self) -> str:
        return "middle"


def test_inherited_defaults_to_own_methods_only(tmp_path: Path) -> None:
    log_file = _configure(tmp_path)

    @track
    class Leaf(_Middle):
        def own(self) -> None:
            return None

    leaf = Leaf()
    leaf.own()
    leaf.base_method()
    leaf.middle_method()
    msgs = _lines(log_file)
    assert "→ Leaf.own()" in msgs
    assert not [m for m in msgs if "base_method" in m or "middle_method" in m]


def test_inherited_true_wraps_the_whole_mro(tmp_path: Path) -> None:
    log_file = _configure(tmp_path)

    @track(inherited=True)
    class Leaf(_Middle):
        def own(self) -> None:
            return None

    leaf = Leaf()
    leaf.own()
    leaf.middle_method()
    leaf.base_method()
    msgs = _lines(log_file)
    assert "→ Leaf.own()" in msgs
    assert "→ _Middle.middle_method()" in msgs
    assert "→ _Base.base_method()" in msgs


def test_inherited_class_is_an_inclusive_mro_boundary(tmp_path: Path) -> None:
    """`inherited=_Middle` takes _Middle and stops — _Base stays untraced."""
    log_file = _configure(tmp_path)

    @track(inherited=_Middle)
    class Leaf(_Middle):
        def own(self) -> None:
            return None

    leaf = Leaf()
    leaf.own()
    leaf.middle_method()
    leaf.base_method()
    msgs = _lines(log_file)
    assert "→ Leaf.own()" in msgs
    assert "→ _Middle.middle_method()" in msgs
    assert not [m for m in msgs if "base_method" in m]


def test_inherited_never_mutates_the_base_class(tmp_path: Path) -> None:
    """The whole safety property: a sibling subclass must stay untraced.

    Wrapping in place on the base would trace every subclass in the process —
    on a real trainer that means every torch.nn.Module.
    """
    log_file = _configure(tmp_path)
    before = _Base.__dict__["base_method"]

    @track(inherited=True)
    class Traced(_Middle):
        pass

    class Sibling(_Base):
        pass

    Traced().base_method()
    Sibling().base_method()  # must produce nothing
    _Base().base_method()  # nor this

    assert _Base.__dict__["base_method"] is before
    assert len([m for m in _lines(log_file) if "base_method" in m]) == 2  # entry+exit, once


def test_an_own_method_wins_over_the_inherited_one(tmp_path: Path) -> None:
    """The class's own override is what runs, and it is traced under its own name."""
    log_file = _configure(tmp_path)

    @track(inherited=True)
    class Leaf(_Base):
        def shared(self) -> str:
            return "from leaf"

    assert Leaf().shared() == "from leaf"
    msgs = _lines(log_file)
    assert len([m for m in msgs if "shared" in m]) == 2
    assert "→ Leaf.shared()" in msgs


def test_spy_inherited_drops_self_from_inherited_methods(tmp_path: Path) -> None:
    log_file = _configure(tmp_path)

    class Base:
        def step(self, batch: int, lr: float = 0.1) -> None:
            return None

    @spy(inherited=True)
    class Leaf(Base):
        pass

    Leaf().step(7)
    assert "→ Base.step(batch=7, lr=0.1)" in _lines(log_file)


def test_object_is_never_wrapped_by_inherited_true(tmp_path: Path) -> None:
    """`object` has no traceable methods under our rules; assert we never reach for them."""
    _configure(tmp_path)

    @track(inherited=True)
    class Bare:
        pass

    assert "__init__" not in vars(Bare)
    assert "__eq__" not in vars(Bare)


def test_class_decoration_accepts_keyword_options(tmp_path: Path) -> None:
    log_file = _configure(tmp_path)

    @track(timing=False)
    class Op:
        def run(self) -> None:
            return None

    Op().run()
    assert _lines(log_file)[1] == "← Op.run"


# --------------------------------------------------------------------------- #
# unsupported function shapes fail loudly at decoration time
# --------------------------------------------------------------------------- #
def test_decorating_an_async_function_raises_with_a_located_message() -> None:
    async def fetch() -> None:
        return None

    with pytest.raises(TypeError) as exc:
        track(fetch)
    message = str(exc.value)
    assert "fetch" in message
    assert os.path.basename(__file__) in message
    assert "async" in message


def test_decorating_a_generator_function_raises_with_a_located_message() -> None:
    def stream() -> Any:
        yield 1

    with pytest.raises(TypeError) as exc:
        spy(stream)
    message = str(exc.value)
    assert "stream" in message
    assert os.path.basename(__file__) in message

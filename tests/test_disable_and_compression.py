"""Tests for the runtime logging kill-switch (``NullLogger`` + ``LOGGAIR_DISABLE_*``)
and the rotated-archive ``compression`` knob.

The autouse ``global_reset_loggair`` fixture (conftest.py) isolates HOME/XDG,
purges ``LOGGAIR_*`` env vars, and resets Loggair state between every test.
"""

import gzip
import os
import pickle
import zipfile
from pathlib import Path
from typing import Any

import pytest

from loggair import core
from loggair.core import _compress_file, _rotate, configure_logging, get_logger, is_configured, shutdown_logging
from loggair.null_logger import NullLogger, _NullLevel
from loggair.rotation import _purge_old_files

# ---------------------------------------------------------------------------
# NullLogger API
# ---------------------------------------------------------------------------

_LOG_METHODS = ["trace", "debug", "info", "success", "warning", "error", "critical", "exception", "log"]


@pytest.mark.parametrize("method", _LOG_METHODS)
def test_null_logger_log_methods_are_noops(method: str) -> None:
    nl = NullLogger("m")
    # Call with the loosely-typed loguru argument styles; each returns None and never raises.
    assert getattr(nl, method)("a message", 1, 2, key="v") is None


def test_null_logger_bind_opt_patch_return_logger_and_chain() -> None:
    nl = NullLogger()
    assert isinstance(nl.bind(name="x"), NullLogger)
    assert isinstance(nl.opt(depth=1, exception=None), NullLogger)
    assert isinstance(nl.patch(lambda r: None), NullLogger)
    # The exact chain loggair's InterceptHandler / call-sites use must be safe (must not raise).
    nl.bind(name="x").opt(depth=6).log("INFO", "chained")


def test_null_logger_bind_carries_name() -> None:
    assert NullLogger("orig").bind(name="new")._name == "new"
    assert NullLogger("orig").bind(other=1)._name == "orig"  # name not overridden


def test_null_logger_level_exposes_numeric_no() -> None:
    nl = NullLogger()
    assert nl.level("DEBUG").no == 10
    assert nl.level("debug").no == 10  # case-insensitive
    assert nl.level("INFO").no == 20
    assert nl.level("NOPE").no == 0  # unknown level -> 0
    lvl = nl.level(5)  # non-str argument -> default level object
    assert isinstance(lvl, _NullLevel)
    assert lvl.no == 0


def test_null_logger_sink_methods() -> None:
    nl = NullLogger()
    assert nl.add(lambda m: None, level="INFO") == 0
    # remove()/complete() are no-ops that must not raise.
    nl.remove()
    nl.remove(3)
    nl.complete()


def test_null_logger_contextualize_is_noop_context() -> None:
    nl = NullLogger("c")
    with nl.contextualize(request_id="123") as ctx:
        assert isinstance(ctx, NullLogger)
        ctx.info("inside context")  # no-op


def test_null_logger_contextualize_does_not_suppress_exceptions() -> None:
    nl = NullLogger()
    with pytest.raises(ValueError):
        with nl.contextualize(k="v"):
            raise ValueError("boom")


def test_null_logger_catch_bare_decorator_returns_function() -> None:
    nl = NullLogger()

    @nl.catch
    def f() -> int:
        return 42

    assert f() == 42


def test_null_logger_catch_factory_returns_passthrough_decorator() -> None:
    nl = NullLogger()
    decorator = nl.catch(ValueError, message="ignored")  # exception-class arg -> factory form

    @decorator
    def f() -> str:
        return "ok"

    assert f() == "ok"


def test_null_logger_catch_context_manager_does_not_suppress() -> None:
    """Disabled logging must neither log NOR hide errors."""
    nl = NullLogger()
    with pytest.raises(ValueError):
        with nl.catch():
            raise ValueError("boom")


def test_null_logger_getattr_fallback_swallows_unknown_calls() -> None:
    nl = NullLogger()
    # An unforeseen loguru method must not raise just because logging is disabled.
    assert nl.some_unreleased_loguru_method(1, 2, 3) is None


def test_null_logger_getattr_raises_for_dunder() -> None:
    """Dunder probes fall through to defaults (so pickle/copy behave) rather than no-ops."""
    nl = NullLogger()
    with pytest.raises(AttributeError):
        nl.__nonexistent_dunder__  # noqa: B018


def test_null_logger_is_picklable() -> None:
    """`__getattr__`'s dunder guard keeps the object picklable (spawn-safe)."""
    restored = pickle.loads(pickle.dumps(NullLogger("p")))
    assert isinstance(restored, NullLogger)
    assert restored._name == "p"
    restored.info("still a noop")  # callable (no-op) after unpickle


def test_null_logger_repr() -> None:
    assert repr(NullLogger("abc")) == "NullLogger(name='abc')"


# ---------------------------------------------------------------------------
# LOGGAIR_DISABLE_LOGGING / LOGGAIR_DISABLE_MULTIPROCESS_LOGGING
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("truthy", ["1", "true", "TRUE", "yes", "t", "y"])
def test_disable_logging_returns_null_logger(truthy: str, monkeypatch: Any) -> None:
    monkeypatch.setenv("LOGGAIR_DISABLE_LOGGING", truthy)
    lg: Any = get_logger("anything")  # Any: Logger/NullLogger are disjoint under --warn-unreachable
    assert isinstance(lg, NullLogger)
    assert lg._name == "anything"


@pytest.mark.parametrize("falsey", ["0", "false", "no", "off", ""])
def test_disable_logging_falsey_returns_real_logger(falsey: str, tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("LOGGAIR_DISABLE_LOGGING", falsey)
    configure_logging(log_dir=tmp_path / "logs", script_name="real")
    assert not isinstance(get_logger("x"), NullLogger)


def test_disable_logging_does_not_configure_or_create_files(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("LOGGAIR_DISABLE_LOGGING", "1")
    monkeypatch.chdir(tmp_path)  # any accidental ./logs would land here
    assert isinstance(get_logger("x"), NullLogger)
    assert is_configured() is False  # the lazy auto-config never ran
    assert not (tmp_path / "logs").exists()


def test_disable_multiprocess_only_affects_child_processes(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("LOGGAIR_DISABLE_MULTIPROCESS_LOGGING", "1")
    # Configure as the (real) main process first.
    configure_logging(log_dir=tmp_path / "logs", script_name="mp")
    assert not isinstance(get_logger("main"), NullLogger)

    # Now masquerade as a worker/child process: the same flag silences it.
    class _Child:
        name = "Worker-1"

    monkeypatch.setattr(core, "current_process", lambda: _Child())
    assert isinstance(get_logger("child"), NullLogger)


def test_disable_multiprocess_unset_keeps_child_logging(tmp_path: Path, monkeypatch: Any) -> None:
    """Without the flag, a child process still gets the real logger."""
    configure_logging(log_dir=tmp_path / "logs", script_name="mp2")

    class _Child:
        name = "Worker-2"

    monkeypatch.setattr(core, "current_process", lambda: _Child())
    assert not isinstance(get_logger("child"), NullLogger)


# ---------------------------------------------------------------------------
# compression knob
# ---------------------------------------------------------------------------


def _seed_existing_log(log_dir: Path, name: str, body: str) -> Path:
    """Create a pre-existing log file with a fixed (old) mtime so the next
    configure() rotates it deterministically (no same-second name collisions)."""
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{name}.log"
    log_file.write_text(body)
    old = 1_700_000_000.0  # fixed timestamp in the past
    os.utime(log_file, (old, old))
    return log_file


def test_compression_gz_creates_compressed_archive(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    _seed_existing_log(log_dir, "job", "PREVIOUS RUN CONTENT")

    configure_logging(log_dir=log_dir, script_name="job", compression="gz", rotation_on_startup=True)
    get_logger("t").info("new run line")
    shutdown_logging()

    archives = list(log_dir.glob("job.*.log.gz"))
    assert len(archives) == 1
    with gzip.open(archives[0], "rt") as fh:
        assert "PREVIOUS RUN CONTENT" in fh.read()
    # The uncompressed rotated file must NOT linger; only the active log is plain.
    assert list(log_dir.glob("job.*.log")) == []
    assert "new run line" in (log_dir / "job.log").read_text()


def test_compression_zip_creates_compressed_archive(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    _seed_existing_log(log_dir, "z", "ZIP ME")

    configure_logging(log_dir=log_dir, script_name="z", compression="zip", rotation_on_startup=True)
    get_logger("t").info("after")
    shutdown_logging()

    archives = list(log_dir.glob("z.*.log.zip"))
    assert len(archives) == 1
    with zipfile.ZipFile(archives[0]) as zf:
        assert b"ZIP ME" in zf.read(zf.namelist()[0])
    assert list(log_dir.glob("z.*.log")) == []


def test_compression_none_leaves_plain_archive(tmp_path: Path) -> None:
    """Regression: default (no compression) keeps the historical plain-archive behavior."""
    log_dir = tmp_path / "logs"
    _seed_existing_log(log_dir, "plain", "OLD")

    configure_logging(log_dir=log_dir, script_name="plain", rotation_on_startup=True)
    get_logger("t").info("fresh")
    shutdown_logging()

    assert len(list(log_dir.glob("plain.*.log"))) == 1
    assert list(log_dir.glob("plain.*.log.gz")) == []
    assert list(log_dir.glob("plain.*.log.zip")) == []


def test_compression_via_env_var(tmp_path: Path, monkeypatch: Any) -> None:
    log_dir = tmp_path / "logs"
    _seed_existing_log(log_dir, "envc", "ENV OLD")
    monkeypatch.setenv("LOGGAIR_COMPRESSION", "gz")

    configure_logging(log_dir=log_dir, script_name="envc", rotation_on_startup=True)
    get_logger("t").info("env new")
    shutdown_logging()

    assert len(list(log_dir.glob("envc.*.log.gz"))) == 1


def test_compression_via_yaml(tmp_path: Path, monkeypatch: Any) -> None:
    log_dir = tmp_path / "logs"
    _seed_existing_log(log_dir, "yamlc", "YAML OLD")
    (tmp_path / "loggair.yaml").write_text("compression: zip\n")
    monkeypatch.chdir(tmp_path)

    configure_logging(log_dir=log_dir, script_name="yamlc", rotation_on_startup=True)
    get_logger("t").info("yaml new")
    shutdown_logging()

    assert len(list(log_dir.glob("yamlc.*.log.zip"))) == 1


def test_compression_arg_overrides_env(tmp_path: Path, monkeypatch: Any) -> None:
    log_dir = tmp_path / "logs"
    _seed_existing_log(log_dir, "ov", "OLD")
    monkeypatch.setenv("LOGGAIR_COMPRESSION", "zip")

    configure_logging(log_dir=log_dir, script_name="ov", compression="gz", rotation_on_startup=True)
    shutdown_logging()

    assert len(list(log_dir.glob("ov.*.log.gz"))) == 1  # arg won
    assert list(log_dir.glob("ov.*.log.zip")) == []


@pytest.mark.parametrize("bad", ["rar", "bz2", "7z", "tar.gz"])
def test_compression_invalid_value_raises(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ValueError, match="compression: invalid value"):
        configure_logging(log_dir=tmp_path / "logs", script_name="bad", compression=bad)  # type: ignore[arg-type]


def test_compression_invalid_env_value_raises(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("LOGGAIR_COMPRESSION", "lzma")
    with pytest.raises(ValueError, match="lzma"):
        configure_logging(log_dir=tmp_path / "logs", script_name="badenv")


def test_rotate_compresses_then_retains_compressed_archives(tmp_path: Path) -> None:
    """`_rotate` compresses the new archive AND prunes compressed archives by retention."""
    active = tmp_path / "app.log"
    active.write_text("CURRENT")

    # Three older compressed archives with strictly increasing mtimes.
    for i, ts in enumerate(["2026-01-01_00-00-01", "2026-01-01_00-00-02", "2026-01-01_00-00-03"]):
        old = tmp_path / f"app.{ts}.log.gz"
        old.write_bytes(b"old")
        mt = 1_000_000.0 + i
        os.utime(old, (mt, mt))

    _rotate(active, retention=2, compression="gz")

    archives = list(tmp_path.glob("app.*.log.gz"))
    assert len(archives) == 2  # retention enforced across compressed archives
    # The freshly-rotated archive (newest mtime) survived and round-trips.
    newest = max(archives, key=lambda p: p.stat().st_mtime)
    with gzip.open(newest, "rt") as fh:
        assert fh.read() == "CURRENT"
    assert not active.exists()  # original was rotated away


def test_purge_tolerates_candidate_vanishing_before_stat(tmp_path: Path, recwarn: Any) -> None:
    """A concurrent process (shared ``~/logs``) may purge the same stem's archives
    between our directory listing and the ``stat()`` — the vanished file must be
    skipped silently, and retention still enforced on the survivors."""
    files = []
    for i in range(3):
        f = tmp_path / f"app.2026-01-01_00-00-0{i}.log"
        f.write_text("old")
        mt = 1_000_000.0 + i
        os.utime(f, (mt, mt))
        files.append(f)
    ghost = tmp_path / "app.2026-01-01_00-00-09.log"  # listed by the other process's victim, never created

    _purge_old_files(files + [ghost], keep=1)

    survivors = sorted(p.name for p in tmp_path.glob("app.*.log"))
    assert survivors == ["app.2026-01-01_00-00-02.log"]  # newest of the REAL files kept
    assert not [w for w in recwarn.list if issubclass(w.category, UserWarning)]


def test_purge_tolerates_file_vanishing_between_stat_and_unlink(tmp_path: Path, monkeypatch: Any, recwarn: Any) -> None:
    """The concurrent-purge race can also strike after ranking: ``unlink`` on an
    already-deleted file must not warn (someone else's purge finished our job)."""
    files = []
    for i in range(3):
        f = tmp_path / f"app.2026-01-01_00-00-0{i}.log"
        f.write_text("old")
        mt = 1_000_000.0 + i
        os.utime(f, (mt, mt))
        files.append(f)
    victim = files[0]  # oldest → a purge target

    orig_stat = Path.stat
    removed = False

    def _stat(self: Path, **kwargs: Any) -> Any:
        nonlocal removed
        result = orig_stat(self, **kwargs)
        if self == victim and not removed:
            removed = True
            os.remove(victim)  # the other process deletes it right after our stat
        return result

    monkeypatch.setattr(Path, "stat", _stat)

    _purge_old_files(files, keep=1)

    monkeypatch.undo()
    survivors = sorted(p.name for p in tmp_path.glob("app.*.log"))
    assert survivors == ["app.2026-01-01_00-00-02.log"]
    assert not [w for w in recwarn.list if issubclass(w.category, UserWarning)]


def test_compress_file_failure_preserves_uncompressed_original(tmp_path: Path, monkeypatch: Any) -> None:
    """Compression is best-effort: on failure it warns, cleans the partial archive,
    and returns the ORIGINAL uncompressed path so no rotated log is ever lost."""
    src = tmp_path / "app.2026-01-01_00-00-00.log"
    src.write_text("important rotated log")

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(core.shutil, "copyfileobj", _boom)

    with pytest.warns(UserWarning, match="Failed to compress"):
        result = _compress_file(src, "gz")

    assert result == src  # original path returned
    assert src.exists()  # uncompressed log preserved
    assert not (tmp_path / "app.2026-01-01_00-00-00.log.gz").exists()  # partial archive cleaned up


def test_compress_file_cleanup_failure_is_swallowed(tmp_path: Path, monkeypatch: Any) -> None:
    """If even removing the partial archive fails, _compress_file still returns the original."""
    src = tmp_path / "app.2026-01-01_00-00-00.log"
    src.write_text("important rotated log")

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(core.shutil, "copyfileobj", _boom)

    # Make the partial-archive cleanup ALSO raise, exercising the inner OSError guard.
    orig_unlink = Path.unlink

    def _unlink(self: Path, *args: Any, **kwargs: Any) -> None:
        if self.name.endswith(".gz"):
            raise OSError("cannot remove partial archive")
        orig_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _unlink)

    with pytest.warns(UserWarning, match="Failed to compress"):
        result = _compress_file(src, "gz")

    assert result == src
    assert src.exists()

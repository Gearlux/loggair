import os
import time
from pathlib import Path

import loggair.core


def test_pivot_and_rotate_handoff(tmp_path: Path) -> None:
    """
    Verify Pattern:
    1. Start with wf.log
    2. Log bootstrap
    3. Handoff to convert.log
    4. wf.log should be gone, convert.log should have bootstrap
    """
    log_dir = tmp_path / "logs"

    # 1. Initial config (Simulate wf start)
    # We must use loggair.core.configure_logging after the reload
    loggair.core.configure_logging(log_dir=log_dir, script_name="wf", force=True)
    test_logger = loggair.core.get_logger("bootstrap")
    test_logger.info("BOOTSTRAP START")

    wf_log = log_dir / "wf.log"
    assert wf_log.exists()

    # 2. Handoff (Simulate convert command)
    loggair.core.configure_logging(log_dir=log_dir, script_name="convert", force=True)
    test_logger.info("CONVERT START")

    convert_log = log_dir / "convert.log"
    assert convert_log.exists()
    assert not wf_log.exists(), "wf.log should have been pivoted (renamed) to convert.log"

    # 3. Check content
    content = convert_log.read_text()
    assert "BOOTSTRAP START" in content
    assert "CONVERT START" in content


def _make_archives(log_dir: Path, stem: str, count: int) -> None:
    """Create `count` timestamped rotation archives for `stem`, oldest first."""
    for i in range(count):
        p = log_dir / f"{stem}.2023-01-0{i + 1}_00-00-00.log"
        p.write_text(f"old log {i}")
        os.utime(p, (time.time() - (100 - i), time.time() - (100 - i)))


def test_retention_enforcement(tmp_path: Path) -> None:
    """The startup sweep prunes this stem's timestamped archives to `retention`."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    _make_archives(log_dir, "test", 5)

    loggair.core.configure_logging(log_dir=log_dir, script_name="test", retention=2, force=True)

    archives = list(log_dir.glob("test.2023-*.log"))
    assert len(archives) == 2
    # The two NEWEST survived
    assert {p.name for p in archives} == {
        "test.2023-01-04_00-00-00.log",
        "test.2023-01-05_00-00-00.log",
    }


def test_retention_decrease_cleanup(tmp_path: Path) -> None:
    """Retention going 5 -> 2 prunes archives of OTHER stems too — per stem."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    _make_archives(log_dir, "old_run_a", 5)
    _make_archives(log_dir, "old_run_b", 3)

    loggair.core.configure_logging(log_dir=log_dir, script_name="final_run", retention=2, force=True)

    assert len(list(log_dir.glob("old_run_a.2023-*.log"))) == 2
    assert len(list(log_dir.glob("old_run_b.2023-*.log"))) == 2


def test_sweep_never_deletes_other_scripts_live_logs(tmp_path: Path) -> None:
    """A bare `{name}.log` of another script may be a concurrently running
    process's ACTIVE sink — the startup sweep must never touch it, no matter
    how low retention is."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    live_a = log_dir / "concurrent_a.log"
    live_b = log_dir / "concurrent_b.log"
    live_a.write_text("live run a")
    live_b.write_text("live run b")
    os.utime(live_a, (time.time() - 100, time.time() - 100))
    _make_archives(log_dir, "concurrent_a", 3)

    loggair.core.configure_logging(log_dir=log_dir, script_name="newcomer", retention=1, force=True)

    assert live_a.exists()
    assert live_b.exists()
    assert live_a.read_text() == "live run a"
    # ...while concurrent_a's ARCHIVES were still pruned per-stem
    assert len(list(log_dir.glob("concurrent_a.2023-*.log"))) == 1

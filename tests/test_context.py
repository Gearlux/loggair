"""Experiment context injection (loggair.set_context / clear_context / context)."""

import json
import logging
import os
from pathlib import Path

from loggair import (
    clear_context,
    configure_logging,
    context,
    get_active_config,
    get_context,
    get_logger,
    reset_logging,
    set_context,
    shutdown_logging,
)


def test_set_merge_get_clear_semantics() -> None:
    assert get_context() == {}
    set_context(epoch=3)
    set_context(step=1200)  # merges — epoch stays
    assert get_context() == {"epoch": 3, "step": 1200}

    clear_context("step")  # selective
    assert get_context() == {"epoch": 3}
    clear_context()  # bare call clears all
    assert get_context() == {}


def test_context_tag_in_default_file_format(tmp_path: Path) -> None:
    configure_logging(log_dir=tmp_path / "logs", script_name="ctx")
    lg = get_logger("train")

    lg.info("before-context")
    set_context(epoch=3, step=1200)
    lg.info("with-context")
    clear_context()
    lg.info("after-clear")
    shutdown_logging()

    lines = (tmp_path / "logs" / "ctx.log").read_text().splitlines()
    assert any("| before-context" in ln and "epoch=" not in ln for ln in lines)
    assert any("epoch=3 step=1200 | with-context" in ln for ln in lines)
    assert any("| after-clear" in ln and "epoch=" not in ln for ln in lines)


def test_intercepted_stdlib_records_carry_context(tmp_path: Path) -> None:
    """Third-party stdlib logging routed through interception gets the context too."""
    configure_logging(log_dir=tmp_path / "logs", script_name="ctxstd")
    set_context(epoch=5)
    logging.getLogger("thirdparty").warning("lib-line")
    shutdown_logging()

    text = (tmp_path / "logs" / "ctxstd.log").read_text()
    assert "epoch=5 | lib-line" in text


def test_context_manager_restores_entry_snapshot(tmp_path: Path) -> None:
    set_context(epoch=3)
    with context(phase="validation"):
        assert get_context() == {"epoch": 3, "phase": "validation"}
        set_context(inner="x")  # entry-snapshot semantics: does NOT survive the block
    assert get_context() == {"epoch": 3}


def test_explicit_bind_wins_over_ambient_context(tmp_path: Path) -> None:
    """setdefault semantics: logger.bind(epoch=...) beats the process context."""
    configure_logging(log_dir=tmp_path / "logs", script_name="ctxbind", serialize=True)
    set_context(epoch=3)
    get_logger().bind(epoch=99).info("bound-wins")
    shutdown_logging()

    rec = next(
        json.loads(ln)["record"]
        for ln in (tmp_path / "logs" / "ctxbind.log").read_text().splitlines()
        if "bound-wins" in ln
    )
    assert rec["extra"]["epoch"] == 99
    assert rec["extra"]["context_tag"] == "epoch=3 | "  # tag reflects the ambient context


def test_context_fields_structured_in_json_mode(tmp_path: Path) -> None:
    configure_logging(log_dir=tmp_path / "logs", script_name="ctxjson", serialize=True)
    set_context(epoch=7, run_id="abc")
    get_logger("t").info("json ctx")
    shutdown_logging()

    rec = next(
        json.loads(ln)["record"]
        for ln in (tmp_path / "logs" / "ctxjson.log").read_text().splitlines()
        if "json ctx" in ln
    )
    assert rec["extra"]["epoch"] == 7
    assert rec["extra"]["run_id"] == "abc"


def test_active_config_reports_live_context(tmp_path: Path) -> None:
    configure_logging(log_dir=tmp_path / "logs", script_name="ctxcfg")
    set_context(epoch=1)
    assert get_active_config()["experiment_context"] == {"epoch": 1}
    clear_context()
    assert get_active_config()["experiment_context"] == {}


def test_reset_logging_clears_context(tmp_path: Path) -> None:
    configure_logging(log_dir=tmp_path / "logs", script_name="ctxreset")
    set_context(epoch=9)
    reset_logging()
    assert get_context() == {}
    assert "LOGGAIR_SCRIPT_NAME" not in os.environ

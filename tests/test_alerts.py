"""Webhook / alerting sink (alert_urls -> apprise-backed AlertDispatcher)."""

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

import loggair.alerts
from loggair import get_active_config, get_logger, reset_logging, shutdown_logging
from loggair.alerts import AlertDispatcher
from loggair.core import LoggingState, configure_logging

pytest.importorskip("apprise")  # optional [alerts] dependency, present in the dev extra


def _dispatcher() -> AlertDispatcher:
    assert LoggingState.alert_dispatcher is not None
    return LoggingState.alert_dispatcher


def _stub_notify(record_into: List[Dict[str, Any]], result: bool = True) -> Any:
    def notify(**kwargs: Any) -> bool:
        record_into.append(kwargs)
        return result

    return notify


def _configure_with_alerts(tmp_path: Path, **kwargs: Any) -> List[Dict[str, Any]]:
    """Configure with a valid offline apprise URL and stub out the delivery."""
    kwargs.setdefault("alert_urls", "json://user:tok@localhost:1/hook")
    kwargs.setdefault("alert_throttle", 0)
    configure_logging(log_dir=tmp_path / "logs", script_name="alerts", **kwargs)
    sent: List[Dict[str, Any]] = []
    assert LoggingState.alert_dispatcher is not None
    LoggingState.alert_dispatcher._apprise.notify = _stub_notify(sent)
    return sent


def test_alert_level_threshold_and_traceback(tmp_path: Path) -> None:
    sent = _configure_with_alerts(tmp_path)
    lg = get_logger("svc")

    lg.warning("below threshold")
    lg.error("boom happened")
    try:
        raise RuntimeError("kaputt")
    except RuntimeError:
        lg.exception("crashed")

    assert _dispatcher().flush(5)
    bodies = "".join(s["body"] for s in sent)
    assert "below threshold" not in bodies
    assert "boom happened" in bodies
    assert "Traceback" in bodies and "kaputt" in bodies
    assert all(s["title"].startswith("[Loggair] ERROR from alerts on ") for s in sent)


def test_alert_level_knob_lowers_threshold(tmp_path: Path) -> None:
    sent = _configure_with_alerts(tmp_path, alert_level="WARNING")
    get_logger("svc").warning("warn-alerts-now")
    assert _dispatcher().flush(5)
    assert any("warn-alerts-now" in s["body"] for s in sent)


def test_alert_batching_within_throttle_window(tmp_path: Path) -> None:
    """Records queued during the throttle window collapse into ONE delivery."""
    sent = _configure_with_alerts(tmp_path, alert_throttle=60)
    lg = get_logger("svc")

    lg.error("first")
    assert _dispatcher().flush(5)  # delivery 1; worker enters throttle wait
    lg.error("second")
    lg.error("third")
    assert _dispatcher().flush(5)  # aborts the wait -> ONE batched delivery

    assert len(sent) == 2
    assert "second" in sent[1]["body"] and "third" in sent[1]["body"]
    assert "(+1 more)" in sent[1]["title"]


def test_invalid_alert_url_fails_fast(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="invalid apprise URL"):
        configure_logging(log_dir=tmp_path / "logs", script_name="badurl", alert_urls="nope://not-a-scheme/x")


def test_invalid_alert_level_fails_fast(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="alert_level"):
        configure_logging(
            log_dir=tmp_path / "logs", script_name="badlvl", alert_urls="json://localhost:1/h", alert_level="BOGUS"
        )


def test_missing_apprise_dependency_fails_fast(tmp_path: Path, monkeypatch: Any) -> None:
    def _raise() -> Any:
        raise ImportError("pip install loggair[alerts]")

    monkeypatch.setattr(loggair.alerts, "_import_apprise", _raise)
    with pytest.raises(ImportError, match=r"loggair\[alerts\]"):
        configure_logging(log_dir=tmp_path / "logs", script_name="noapprise", alert_urls="json://localhost:1/h")


def test_delivery_failure_never_feeds_back(tmp_path: Path) -> None:
    """A raising notify() is reported via an internally-marked WARNING that the
    alert sink filters out — even at alert_level=WARNING there is no loop."""
    sent: List[Dict[str, Any]] = []
    configure_logging(
        log_dir=tmp_path / "logs",
        script_name="alerts",
        alert_urls="json://localhost:1/h",
        alert_level="WARNING",
        alert_throttle=0,
    )

    def failing_notify(**kwargs: Any) -> bool:
        sent.append(kwargs)
        raise ConnectionError("webhook down")

    _dispatcher()._apprise.notify = failing_notify
    get_logger("svc").error("triggers a failing delivery")

    assert _dispatcher().flush(5)
    assert len(sent) == 1  # the failure warning did NOT become another alert
    shutdown_logging()
    text = (tmp_path / "logs" / "alerts.log").read_text()
    assert "alert delivery failed" in text  # ...but IS visible in the log file


def test_active_config_redacts_alert_secrets(tmp_path: Path) -> None:
    _configure_with_alerts(tmp_path)  # URL carries user:tok credentials
    cfg = get_active_config()
    dumped = json.dumps(cfg)
    assert cfg["alert_level"] == "ERROR"
    assert cfg["alert_urls"] and all("localhost" in u for u in cfg["alert_urls"])
    assert "tok" not in dumped  # password masked by apprise privacy redaction


def test_shutdown_flushes_and_reset_stops_dispatcher(tmp_path: Path) -> None:
    sent = _configure_with_alerts(tmp_path, alert_throttle=60)
    dispatcher = _dispatcher()
    get_logger("svc").error("flushed at shutdown")

    shutdown_logging()  # must flush queued alerts without an explicit flush call
    assert any("flushed at shutdown" in s["body"] for s in sent)

    reset_logging()
    assert LoggingState.alert_dispatcher is None
    assert not dispatcher._thread.is_alive()

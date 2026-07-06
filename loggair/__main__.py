"""``python -m loggair`` — installation and configuration diagnostic.

Prints the package version, the detected distributed rank (and which
environment source decided it), which config files were found, and the fully
resolved settings — WITHOUT configuring anything: no log directory is
created, no rotation happens, no sinks/interception/signal handlers are
installed, no env vars are written. Running the doctor next to a live
training run must never archive its log out from under it (see the AGENTS
resolver-purity mandate). ``--json`` emits the same report as one JSON object.

On invalid configuration (unknown level, bad intercept mode, ...) the error
is REPORTED — with the environment context that led to it — and the exit
code is 1: that is precisely the situation a diagnostic exists for.
"""

import argparse
import importlib
import json
import os
import platform
import sys
from importlib.metadata import version as _dist_version
from multiprocessing import current_process
from typing import Any, Dict, List, Optional

import loggair as _pkg

# Mono-repo shadow guard + SELF-HEAL: from a directory CONTAINING a loggair/
# project folder, `python -m loggair` under an editable install resolves the
# package to that folder as an EMPTY namespace package (__file__ is None)
# while the editable finder still supplies this module — every attribute
# access would fail cryptically. Since we can detect it here, we FIX it for
# this diagnostic process: drop the sys.path entry that contributed the
# shadow (the cwd injection `python -P` would have prevented — REMOVING an
# entry, the inverse of the banned sys.path.insert hack, scoped to the
# doctor's own process), forget the bad module, and re-import the real
# package. The condition is still surfaced in the report: the same shadow
# breaks any `import loggair` script run from that directory.
_SHADOWED_PATH: List[str] = []
if getattr(_pkg, "__file__", None) is None:
    _SHADOWED_PATH = [str(p) for p in _pkg.__path__]
    _shadow_parents = {os.path.dirname(os.path.abspath(p)) for p in _SHADOWED_PATH}
    sys.path = [p for p in sys.path if os.path.abspath(p or os.getcwd()) not in _shadow_parents]
    del sys.modules["loggair"]
    importlib.invalidate_caches()
    try:
        import loggair as _pkg  # noqa: F811  (deliberate re-import after healing)
    except ImportError:
        _pkg = None  # type: ignore[assignment]
    if _pkg is None or getattr(_pkg, "__file__", None) is None:
        # Healing failed — no real installation reachable. Report and stop.
        sys.stderr.write(
            "loggair: a local directory named 'loggair' is shadowing the package, and no\n"
            f"  installed loggair could be found besides it (shadow: {_SHADOWED_PATH}).\n"
            "  fix: pip install loggair (or `pip install -e .` from the project), or run\n"
            "       the `loggair` console script / `python -P -m loggair` / cd elsewhere.\n"
        )
        raise SystemExit(2)

from loggair import __version__ as _loggair_version  # noqa: E402  (guard above must run first)
from loggair import discovery  # noqa: E402
from loggair.config import config_file_candidates, load_config  # noqa: E402
from loggair.core import LoggingState, resolve_settings  # noqa: E402


def _redact_alert_urls(urls: List[str]) -> List[str]:
    """Mask everything after the scheme — alert URLs carry webhook tokens and
    a diagnostic must never print them (apprise-style per-field redaction
    would require the optional dependency; full masking is the safe default)."""
    redacted = []
    for url in urls:
        scheme, sep, _ = url.partition("://")
        redacted.append(f"{scheme}://****" if sep else "****")
    return redacted


def _collect_report() -> Dict[str, Any]:
    """Assemble the diagnostic report. Read-only by construction."""
    rank, rank_source = discovery.detect_rank()
    report: Dict[str, Any] = {
        "loggair": _loggair_version,
        "loguru": _dist_version("loguru"),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "process": {
            "pid": os.getpid(),
            "name": current_process().name,
            "rank": rank,
            "rank_source": rank_source,
        },
        "config_files": [
            {"path": str(p.expanduser()), "found": p.expanduser().is_file()} for p in config_file_candidates()
        ],
        "merged_file_config": load_config(),
        "configured_in_this_process": LoggingState.configured,
    }
    if _SHADOWED_PATH:
        # Healed for THIS process, but a real hazard for the user's own
        # scripts: `import loggair` from this cwd resolves to the shadow.
        report["shadowed_path_ignored"] = _SHADOWED_PATH
    try:
        settings = resolve_settings()
        resolved = {k: v for k, v in settings.items() if not k.startswith("_")}
        resolved["alert_urls"] = _redact_alert_urls(settings["_alert_urls"])
        report["resolved"] = resolved
    except ValueError as e:
        report["resolution_error"] = str(e)
    return report


def _print_human(report: Dict[str, Any]) -> None:
    print(f"loggair {report['loggair']}  (loguru {report['loguru']}, python {report['python']})")
    print(f"platform: {report['platform']}")
    if "shadowed_path_ignored" in report:
        print(
            f"WARNING:  a local 'loggair' directory shadows the installed package "
            f"({report['shadowed_path_ignored']}) — ignored for this diagnostic, but plain "
            f"`import loggair` from this directory hits it; run scripts from elsewhere or use `python -P`."
        )
    proc = report["process"]
    rank_desc = "none detected (single-process, or rank 0 by default)"
    if proc["rank"] is not None:
        rank_desc = f"{proc['rank']}  (from {proc['rank_source']})"
    print(f"process:  {proc['name']} (pid {proc['pid']})")
    print(f"rank:     {rank_desc}")
    print()
    print("config files (highest priority first):")
    for entry in report["config_files"]:
        marker = "found" if entry["found"] else "not found"
        print(f"  [{marker:>9}] {entry['path']}")
    merged = report["merged_file_config"]
    print(f"merged file config: {json.dumps(merged) if merged else '(empty — defaults and env vars only)'}")
    print()
    if "resolution_error" in report:
        print(f"CONFIGURATION ERROR: {report['resolution_error']}")
        return
    print("resolved settings (args omitted — env > files > defaults):")
    for key, value in report["resolved"].items():
        print(f"  {key}: {json.dumps(value)}")
    print()
    state = "already configured in this process" if report["configured_in_this_process"] else "not configured"
    print(f"in-process state: {state} (this diagnostic never configures, rotates, or creates directories)")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m loggair",
        description="Loggair installation & configuration diagnostic (read-only).",
    )
    parser.add_argument("--json", action="store_true", help="emit the report as a single JSON object")
    args = parser.parse_args(argv)

    report = _collect_report()
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_human(report)
    return 1 if "resolution_error" in report else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Startup rotation, archive compression, and retention purging.

Loggair does its OWN startup rotation (renaming ``{name}.log`` ->
``{name}.{ts}.log``), so loguru's sink-level ``compression=`` — which only ever
fires on loguru-MANAGED rotation — would be a dead knob on that path. Instead
the rotated archive is compressed right after the rename in :func:`_rotate`.
(When the RUNTIME ``rotation`` knob is active, core passes the same
``compression``/``retention`` values to loguru's sink, where they genuinely
fire — see the runtime-rotation mandate.)

The accepted formats are a closed ``Literal`` so every GUI / schema introspects
them and the runtime validation set stays a single source of truth
(``get_args``).
"""

import gzip
import re
import shutil
import warnings
import zipfile
from datetime import datetime
from pathlib import Path
from typing import List, Literal, Optional, get_args

from loggair import discovery

CompressionFormat = Literal["gz", "zip"]
_VALID_COMPRESSIONS = frozenset(get_args(CompressionFormat))
# Filename suffixes for the compressed archive, derived from the Literal — used
# to NAME the archive and to RECOGNISE archives during retention purges.
_COMPRESSION_GLOB_SUFFIXES = tuple(f".{fmt}" for fmt in get_args(CompressionFormat))
# Optional-suffix regex fragment so the timestamped-archive purge matches plain
# AND compressed rotations (e.g. ``app.2026-...log`` and ``app.2026-...log.gz``).
_COMPRESSION_RE = r"(?:" + "|".join(re.escape(s) for s in _COMPRESSION_GLOB_SUFFIXES) + r")?"
# Timestamp core shared by BOTH archive naming schemes: Loggair's startup
# rotation (``YYYY-MM-DD_HH-MM-SS``) and loguru's runtime rotation, which
# appends microseconds (``YYYY-MM-DD_HH-MM-SS_ffffff``) — hence the optional
# ``_\d{6}`` so retention purges recognise runtime-rotated archives too.
_TIMESTAMP_RE = r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}(?:_\d{6})?"
# A timestamped rotation archive of ANY stem: ``{stem}.{timestamp}.log``
# plus an optional compression suffix. Used by the startup sweep to prune old
# archives per stem WITHOUT ever touching a bare ``{name}.log`` — a bare log may
# be the ACTIVE sink of another process sharing the log dir (e.g. the
# centralized ``~/logs``), and deleting it would silently discard a live run's
# output (loguru keeps writing to the unlinked inode).
_ARCHIVE_RE = re.compile(r"(?P<stem>.+)\." + _TIMESTAMP_RE + r"\.log" + _COMPRESSION_RE + r"$")


def _compress_file(path: Path, compression: str) -> Path:
    """Compress `path` to ``path + .gz``/``.zip``, returning the new archive path.

    Removes the uncompressed original on success. On ANY failure it warns,
    cleans up a partial archive, and returns the ORIGINAL (uncompressed) path —
    compression is best-effort and must never break logging or lose a rotated
    log. `compression` is validated against :data:`_VALID_COMPRESSIONS` by the
    caller.
    """
    target = path.with_name(path.name + "." + compression)
    try:
        if compression == "gz":
            with open(path, "rb") as src, gzip.open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
        else:  # "zip"
            with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.write(path, arcname=path.name)
        path.unlink()
        return target
    except Exception as e:
        warnings.warn(f"Loggair: Failed to compress {path.name} ({compression}): {e}")
        if target.exists():
            try:
                target.unlink()
            except OSError:
                pass
        return path


def _purge_old_files(candidates: List[Path], keep: int) -> None:
    """Keep the `keep` most recent files by mtime, delete the rest.

    In a SHARED log dir (the centralized ``~/logs``) another process may purge
    the same stem's archives concurrently, so any candidate can vanish between
    the directory listing and the ``stat()``/``unlink()`` here — a TOCTOU race
    no pre-check can close. A vanished file is simply someone else's completed
    purge: drop it from the ranking and never warn about unlinking it.
    """
    dated = []
    for p in candidates:
        try:
            dated.append((p.stat().st_mtime, p.name, p))
        except OSError:
            continue
    for _, _, old in sorted(dated, reverse=True)[keep:]:
        try:
            old.unlink(missing_ok=True)
        except Exception as e:
            warnings.warn(f"Loggair: Failed to purge old log file {old}: {e}")


def _rotate(path: Path, retention: int = 5, compression: Optional[str] = None, force_owner: bool = False) -> None:
    """Manual rotation of an existing log file.

    Renames ``{name}.log`` -> ``{name}.{timestamp}.log``, optionally compresses
    the archive (``compression="gz"|"zip"``), then prunes timestamped archives —
    compressed or not — down to `retention`.

    By default only rank None/0 rotates (multiple ranks racing a rename on the
    SHARED file would lose logs). ``force_owner=True`` bypasses that gate for a
    file the calling process exclusively owns — the per-worker files of
    ``worker_files=True``, where each process is the sole writer of its path.
    """
    if not path.exists() or path.stat().st_size == 0:
        return
    if not force_owner and discovery.get_rank() not in (None, 0):
        return

    timestamp = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d_%H-%M-%S")
    rotated_path = path.parent / f"{path.stem}.{timestamp}{path.suffix}"

    try:
        path.rename(rotated_path)
        if compression:
            _compress_file(rotated_path, compression)
        pattern = re.escape(path.stem) + r"\." + _TIMESTAMP_RE + re.escape(path.suffix) + _COMPRESSION_RE
        candidates = [p for p in path.parent.iterdir() if p.is_file() and re.fullmatch(pattern, p.name)]
        _purge_old_files(candidates, retention)
    except Exception as e:
        warnings.warn(f"Loggair: Failed to rotate log file {path}: {e}")

"""
Loggair: Modern, multiprocess-safe logging for High-Performance Computing and ML.
"""

from importlib.metadata import PackageNotFoundError, version

from loggair.context import clear_context, context, get_context, set_context

try:
    # Single source of truth: the installed distribution's metadata (the
    # DISTRIBUTION is `loggair`; the import package stays `loggair`).
    __version__ = version("loggair")
except PackageNotFoundError:  # pragma: no cover - uninstalled source checkout
    __version__ = "0.0.0.dev0"
from loggair.core import (
    DEFAULT_CONSOLE_FORMAT,
    DEFAULT_FILE_FORMAT,
    configure_logging,
    force_no_color,
    get_active_config,
    get_logger,
    is_configured,
    reconfigure,
    reset_logging,
    shutdown_logging,
)

# Imported for its SIDE EFFECT as much as its exports: it registers the TRAIL
# level with loguru, which `configure_logging` must be able to resolve from a
# config file (`file_level: TRAIL`) without the user importing anything extra.
from loggair.track import TRAIL, spy, track

__all__ = [
    "DEFAULT_CONSOLE_FORMAT",
    "__version__",
    "DEFAULT_FILE_FORMAT",
    "TRAIL",
    "clear_context",
    "configure_logging",
    "context",
    "force_no_color",
    "get_active_config",
    "get_context",
    "get_logger",
    "is_configured",
    "reconfigure",
    "reset_logging",
    "set_context",
    "shutdown_logging",
    "spy",
    "track",
]

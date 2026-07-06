import os
import tomllib
import warnings
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple, cast

import yaml


def get_xdg_config_dir() -> Path:
    """Get the XDG compliant configuration directory for Loggair."""
    base = Path(os.getenv("XDG_CONFIG_HOME", "~/.config")).expanduser()
    return base / "loggair"


def config_file_candidates() -> List[Path]:
    """The config-file locations, highest priority first (shared with the
    ``python -m loggair`` diagnostic, which reports which of them exist)."""
    return [
        Path("loggair.yaml"),
        Path("loggair.yml"),
        Path("pyproject.toml"),
        get_xdg_config_dir() / "config.yaml",
    ]


def load_config() -> Dict[str, Any]:
    """
    Load and merge Loggair configuration from standard locations.
    Priority (Highest to Lowest): loggair.yaml -> loggair.yml -> pyproject.toml -> global config
    """

    def _yaml(p: Path) -> Dict[str, Any]:
        with open(p, "r") as f:
            return cast(Dict[str, Any], yaml.safe_load(f) or {})

    def _toml(p: Path) -> Dict[str, Any]:
        with open(p, "rb") as f:
            return cast(Dict[str, Any], tomllib.load(f).get("tool", {}).get("loggair", {}))

    candidates: List[Tuple[Path, Callable[[Path], Dict[str, Any]]]] = [
        (path, _toml if path.name == "pyproject.toml" else _yaml) for path in config_file_candidates()
    ]

    final_cfg: Dict[str, Any] = {}
    # Iterate in reverse to let higher priority override lower priority
    for path, loader in reversed(candidates):
        if path.exists():
            try:
                cfg = loader(path)
                if cfg:
                    final_cfg.update(cfg)
            except Exception as e:
                warnings.warn(f"Loggair: Failed to load config from {path}: {e}")
                continue

    return final_cfg

import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Optional


@lru_cache(maxsize=1)
def get_rank() -> Optional[int]:
    """
    Detect the rank of the current process in a distributed environment.
    Supports PyTorch DDP (torchrun), SLURM, and MPI.
    """

    def from_env(var: str) -> Optional[int]:
        try:
            return int(os.environ[var]) if var in os.environ else None
        except (ValueError, TypeError):
            return None

    # Priority 1: Direct Ranks
    for var in ("RANK", "SLURM_PROCID"):
        rank = from_env(var)
        if rank is not None:
            return rank

    # Priority 2: Distributed Topology (Node Rank * Local World Size + Local Rank)
    local_rank = from_env("LOCAL_RANK")
    if local_rank is not None:
        node_rank = from_env("NODE_RANK") or from_env("GROUP_RANK") or 0
        world_size = from_env("LOCAL_WORLD_SIZE") or 1
        return node_rank * world_size + local_rank

    return None


def determine_script_name(explicit: Optional[str] = None) -> str:
    """
    Determine a sensible name for the log file based on execution context.
    """
    if explicit:
        return explicit

    # Check env var for consistency across processes
    env_name = os.getenv("LOGGAIR_SCRIPT_NAME")
    if env_name:
        return env_name

    # Try to infer from __main__
    main_module = sys.modules.get("__main__")
    if main_module and hasattr(main_module, "__file__") and main_module.__file__:
        path = Path(main_module.__file__)
        if path.name == "__main__.py":
            # If package run, use parent folder name
            return path.parent.name or "app"
        return path.stem

    # Fallback to sys.argv[0] if it's not a flag
    if sys.argv and sys.argv[0] and not sys.argv[0].startswith("-"):
        return Path(sys.argv[0]).stem

    return "app"

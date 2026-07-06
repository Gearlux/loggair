import os
from typing import Any, Generator

import pytest
from loguru import logger

import loggair.core
import loggair.discovery


@pytest.fixture(autouse=True)
def global_reset_loggair(tmp_path: Any, monkeypatch: Any) -> Generator[None, None, None]:
    """Nuclear reset of all Loggair state and Environment between tests."""
    # 1. Isolate HOME so global config (~/.config/loggair/) is never found
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    # 2. Clear environment state
    for k in list(os.environ.keys()):
        if k.startswith("LOGGAIR_") or k.startswith("_LOGGAIR_"):
            os.environ.pop(k)

    # 3. Clear MPI/DDP vars that might interfere
    for k in ("RANK", "LOCAL_RANK", "NODE_RANK", "GROUP_RANK", "SLURM_PROCID"):
        os.environ.pop(k, None)

    # 4. Full teardown: sinks, interception, monkey-patches, state pointers
    loggair.core.reset_logging()

    # 5. Purge Loguru
    logger.remove()

    yield

    # Cleanup after
    loggair.core.reset_logging()
    for k in list(os.environ.keys()):
        if k.startswith("LOGGAIR_") or k.startswith("_LOGGAIR_"):
            os.environ.pop(k)

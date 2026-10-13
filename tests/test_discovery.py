import os
from typing import Any

import pytest

from loggair.discovery import detect_rank, determine_script_name, get_rank


def test_get_rank_none() -> None:
    # Ensure rank is None when no env vars are set
    if "RANK" in os.environ:
        del os.environ["RANK"]
    if "SLURM_PROCID" in os.environ:
        del os.environ["SLURM_PROCID"]
    if "LOCAL_RANK" in os.environ:
        del os.environ["LOCAL_RANK"]
    assert get_rank() is None


def test_get_rank_torchrun() -> None:
    os.environ["RANK"] = "3"
    assert get_rank() == 3
    del os.environ["RANK"]


def test_get_rank_slurm() -> None:
    os.environ["SLURM_PROCID"] = "5"
    assert get_rank() == 5
    del os.environ["SLURM_PROCID"]


def test_get_rank_invalid() -> None:
    os.environ["RANK"] = "not_an_int"
    assert get_rank() is None
    del os.environ["RANK"]


def test_determine_script_name_explicit() -> None:
    assert determine_script_name("custom_name") == "custom_name"


def test_get_rank_generic_ddp() -> None:
    os.environ["LOCAL_RANK"] = "1"
    os.environ["LOCAL_WORLD_SIZE"] = "2"
    os.environ["NODE_RANK"] = "1"
    # (Node 1 * World 2) + Local 1 = Rank 3
    assert get_rank() == 3
    del os.environ["LOCAL_RANK"]
    del os.environ["LOCAL_WORLD_SIZE"]
    del os.environ["NODE_RANK"]


def test_determine_script_name_argv() -> None:
    import sys
    from unittest.mock import patch

    old_argv = sys.argv
    sys.argv = ["/path/to/my_script.py"]
    # Mock sys.modules['__main__'] to not have a package/spec
    with patch.dict(sys.modules, {"__main__": type("Module", (), {"__package__": None})()}):
        try:
            name = determine_script_name()
            assert name == "my_script"
        finally:
            sys.argv = old_argv


def test_get_rank_local_only() -> None:
    os.environ["LOCAL_RANK"] = "2"
    # Defaults: node_rank=0, local_world_size=1
    assert get_rank() == 2
    del os.environ["LOCAL_RANK"]


def test_determine_script_name_empty_argv() -> None:
    import sys

    old_argv = sys.argv
    sys.argv = []
    try:
        name = determine_script_name()
        # Fallback should hit main check or return 'app'
        assert isinstance(name, str)
    finally:
        sys.argv = old_argv


def test_determine_script_name_package() -> None:
    import sys
    from unittest.mock import patch

    # Mock sys.modules['__main__'] to look like a package run
    mock_main = type("Module", (), {"__package__": "my_package"})()
    with patch.dict(sys.modules, {"__main__": mock_main}):
        name = determine_script_name()
        # Should return 'my_package' or similar
        assert isinstance(name, str)


def test_determine_script_name_spec() -> None:
    import sys
    from unittest.mock import MagicMock, patch

    # Create a dummy module with a spec and name
    mock_spec = MagicMock()
    mock_spec.name = "my_module.sub"
    mock_main = MagicMock()
    mock_main.__spec__ = mock_spec
    mock_main.__package__ = "my_module"

    with patch.dict(sys.modules, {"__main__": mock_main}):
        # We need to bypass the package check
        name = determine_script_name()
        assert isinstance(name, str)


def test_determine_script_name_flag() -> None:
    import sys
    from unittest.mock import patch

    old_argv = sys.argv
    sys.argv = ["-m"]  # Obvious flag
    # Force MainProcess check to pass
    with patch.dict(sys.modules, {"__main__": type("Module", (), {"__package__": None})()}):
        try:
            name = determine_script_name()
            assert isinstance(name, str)
        finally:
            sys.argv = old_argv


def test_get_rank_local_world_size() -> None:
    os.environ["LOCAL_RANK"] = "1"
    os.environ["NODE_RANK"] = "2"
    os.environ["LOCAL_WORLD_SIZE"] = "4"
    # (Node 2 * World 4) + Local 1 = Rank 9
    assert get_rank() == 9
    del os.environ["LOCAL_RANK"]
    del os.environ["NODE_RANK"]
    del os.environ["LOCAL_WORLD_SIZE"]


def test_determine_script_name_flag_rejection() -> None:
    import sys
    from unittest.mock import patch

    old_argv = sys.argv
    sys.argv = ["-m"]  # Trigger flag rejection
    # Mock __main__ to not have a file or package to force fallback
    with patch.dict(sys.modules, {"__main__": type("Module", (), {})()}):
        try:
            name = determine_script_name()
            assert name == "app"
        finally:
            sys.argv = old_argv


# --- MPI launchers -----------------------------------------------------------
# `mpirun` / `mpiexec` outside SLURM sets none of RANK, SLURM_PROCID or
# LOCAL_RANK; each MPI implementation exports its own global-rank variable.


@pytest.mark.parametrize("var", ["OMPI_COMM_WORLD_RANK", "PMI_RANK", "PMIX_RANK", "MV2_COMM_WORLD_RANK"])
def test_an_mpi_launcher_rank_is_detected_and_its_variable_named(monkeypatch: Any, var: str) -> None:
    monkeypatch.setenv(var, "4")
    assert detect_rank() == (4, var)
    assert get_rank() == 4


def test_torchrun_and_slurm_ranks_win_over_an_mpi_rank(monkeypatch: Any) -> None:
    # Under `srun` both are set and agree; when they do not, the launcher that
    # Loggair already honoured keeps deciding.
    monkeypatch.setenv("OMPI_COMM_WORLD_RANK", "7")
    monkeypatch.setenv("SLURM_PROCID", "2")
    assert detect_rank() == (2, "SLURM_PROCID")
    monkeypatch.setenv("RANK", "1")
    assert detect_rank() == (1, "RANK")


def test_an_mpi_rank_wins_over_the_local_rank_topology(monkeypatch: Any) -> None:
    # A global MPI rank is a direct answer; LOCAL_RANK is only a derivation.
    monkeypatch.setenv("PMI_RANK", "5")
    monkeypatch.setenv("LOCAL_RANK", "1")
    monkeypatch.setenv("LOCAL_WORLD_SIZE", "8")
    assert detect_rank() == (5, "PMI_RANK")


def test_an_unparseable_mpi_rank_falls_through_to_the_next_variable(monkeypatch: Any) -> None:
    monkeypatch.setenv("OMPI_COMM_WORLD_RANK", "not_an_int")
    monkeypatch.setenv("PMI_RANK", "3")
    assert detect_rank() == (3, "PMI_RANK")

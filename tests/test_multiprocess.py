import multiprocessing as mp
from pathlib import Path

from loggair.core import configure_logging, get_logger, shutdown_logging


def worker(rank: int, log_dir: Path, script_name: str) -> None:
    """Simulated worker process."""
    # Re-initialize logging in the child process.
    # We use force=True to ensure the child actually sets up its own sinks.
    configure_logging(log_dir=log_dir, script_name=script_name, force=True)
    logger = get_logger(f"worker_{rank}")
    logger.info(f"Worker {rank} log message")
    shutdown_logging()


def test_multiprocess_safety(tmp_path: Path) -> None:
    log_dir = tmp_path / "mp_test"
    script_name = "mp_safety"

    # Initialize parent
    configure_logging(log_dir=log_dir, script_name=script_name)
    main_logger = get_logger("main")
    main_logger.info("Main start")

    # Use 'spawn' context for consistent behavior.
    ctx = mp.get_context("spawn")
    processes = []
    num_workers = 4
    for i in range(num_workers):
        p = ctx.Process(target=worker, args=(i, log_dir, script_name))
        p.start()
        processes.append(p)

    for p in processes:
        p.join(timeout=15)
        if p.is_alive():
            p.terminate()
            p.join()

    main_logger.info("Main end")
    shutdown_logging()

    # Verify results
    log_file = log_dir / f"{script_name}.log"
    assert log_file.exists()
    content = log_file.read_text()

    for i in range(num_workers):
        assert f"Worker {i} log message" in content

    # Since all processes (parent and children) write to the same file,
    # and we used force=True in children without a name change,
    # the file content should be cumulative.
    assert "Main start" in content
    assert "Main end" in content

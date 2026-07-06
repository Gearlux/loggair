import multiprocessing as mp
import os
import time

from loggair import configure_logging, get_logger, shutdown_logging


def worker(rank: int) -> None:
    # Simulate a distributed environment by setting RANK env var
    os.environ["RANK"] = str(rank)

    # Re-initialize configuration in spawned child process
    configure_logging(log_dir="./demo_logs", script_name="mp_demo")

    logger = get_logger(f"worker_{rank}")

    # Console: Only Rank 0 shows up
    # File: All ranks are saved
    logger.info(f"Worker {rank} starting task...")

    time.sleep(0.5)

    if rank % 2 == 0:
        logger.success(f"Worker {rank} completed task successfully.")
    else:
        logger.warning(f"Worker {rank} encountered a minor issue.")

    # Ensure this child process flushes its logs before exiting
    shutdown_logging()


if __name__ == "__main__":
    # Configure main process first
    configure_logging(log_dir="./demo_logs", script_name="mp_demo")
    main_logger = get_logger("main")
    main_logger.info("Main process starting demo with 4 workers...")

    # Spawn 4 workers
    processes = []
    for i in range(4):
        p = mp.Process(target=worker, args=(i,))
        p.start()
        processes.append(p)

    for p in processes:
        p.join()

    # Ensure main process flushes its logs before final completion
    main_logger.success("Demo completed. Check 'demo_logs/mp_demo.log' for full output.")
    shutdown_logging()

# Loggair: Rationale & Architectural Comparison

## Executive Summary
**Loggair** is a modern, multiprocess-safe logging library specifically engineered for High-Performance Computing (HPC) and Machine Learning (ML) environments. While general-purpose logging libraries exist, Loggair bridges the gap between raw logging primitives and the specialized needs of distributed training (e.g., PyTorch DDP, TensorFlow Distribution).

---

## The Landscape: Existing Alternatives

| Library | Mechanism | ML/Distributed Suitability | Pros | Cons |
| :--- | :--- | :--- | :--- | :--- |
| **Standard `logging`** | Lock-based (Thread-safe) | **Low**. Requires complex `QueueHandler` setup for MP. | Zero dependencies, built-in. | Extremely verbose setup for MP; no built-in rank awareness. |
| **`Loguru`** | `enqueue=True` (Queue-based) | **Medium**. Great UI/UX, but no native rank/DDP logic. | Beautiful output, thread/MP safe, easy rotation. | Not aware of SLURM/DDP ranks; requires manual wrapping for ML. |
| **`Concurrent-Log-Handler`** | File Locking (`fcntl`/`flock`) | **Low**. Slow on network filesystems (NFS). | Simple to drop-in for standard logging. | High latency; prone to "lock-stale" issues on some clusters. |
| **`Lightning/Accelerate`** | Framework-specific wrappers | **High** (but locked-in). | Automatic rank-0 filtering. | Tied to specific training frameworks; hard to use in standalone scripts. |

---

## Why Loggair? (The Gap)

Existing solutions force ML engineers to choose between **ease of use** (Loguru) and **robust distributed logic** (Lightning). Loggair provides both.

### 1. Unified Distributed Awareness
Loggair automatically detects the execution environment (SLURM, TorchRun, MPI) and adjusts its behavior. 
- **The "Log Storm" Problem:** In a 128-GPU cluster, standard loggers write 128 identical lines. 
- **Loggair Solution:** Intelligently filters console output to Rank 0 while ensuring all Ranks can optionally write to unique or shared persistent files with atomic safety.

### 2. Framework Interoperability
ML projects often use a mix of libraries (PyTorch, TensorFlow, JAX, HuggingFace). Each has its own logging style.
- **Loggair Solution:** Automatically intercepts standard `logging`, `warnings`, and `absl` (TensorFlow) calls, redirecting them into a single, unified, and color-coded stream.

### 3. Startup-Consistent Rotation
ML experiments are iterative. 
- **The Problem:** Standard loggers append to old files or overwrite them, making it hard to find the start of "Experiment #42".
- **Loggair Solution:** Implements **Startup Rotation**. Every time a script starts, the old log is archived with a timestamp, and a fresh log is created. This ensures 1:1 mapping between a run and a log file.

### 4. Zero-Latency "Enqueue"
By utilizing a dedicated background process for log sinking, Loggair ensures that the main training loop (the "Critical Path") is never blocked by I/O operations, even when writing to slow network storage.

### 5. Per-logger, Per-sink Filtering as a First-Class Surface
Loguru already exposes a per-sink `filter=` callable that receives each record before it is dispatched — this is the natural seam for module-targeted filtering, and Loggair surfaces it as YAML-level `module_levels` config. Two design choices matter here:

- **Sink-specific closures.** Each `logger.add(...)` call gets its own filter built by `_make_sink_filter("console" | "file", global_no, rules)`, so the same logger can be loud on disk and quiet on screen without code changes.
- **A computed `level=` floor + filter, not `level=` alone.** Loguru's per-sink `level=` argument is a hard floor — records below it never reach the filter, and it can only raise a threshold. So the filter decides each record's level, and the sink's `level=` is set to the lowest threshold that filter can apply: the minimum of the global level and every `module_levels` override for that sink (`_sink_floor`). That is what lets a per-logger override both **demote** a noisy logger (raise its threshold above the global) and **promote** a useful one (lower it below) within a single sink configuration, while a record below every threshold is still dropped by loguru's cheap early-out instead of being built and then filtered. See the architecture record "Sink level floors are computed, and every rule counts — including inert ones" in [`docs/architecture.md`](docs/architecture.md).

The same filter also detects `current_process().name != "MainProcess"` per record, which is what backs the `workers_only: true` flag — letting users silence PyTorch DataLoader-worker chatter without losing the main-process equivalents.

---

## Design Goals for Implementation
- **Developer First:** `from loggair import get_logger` should be the only line needed.
- **Framework Agnostic:** Works perfectly in a pure Python script, a Jupyter notebook, or a massive SLURM cluster.
- **Structured by Default:** Optional JSON output for integration with ELK or custom ML dashboards.

# AgentRewind Benchmark Suite

This directory contains the reproducible **Performance, Chaos & Security Benchmark Suite** for `agent-rewind`. It runs completely standalone in Python without requiring Docker or external database clusters.

---

## What It Measures

### Tier 1: Kernel Micro-Benchmarks (`1,000` Iterations per Operation)
Measures the raw Python runtime overhead introduced by `AgentRewind`:
1. **1-Step Forward Execution + Commit:** ~`30–45 µs` median latency (`>20,000 tx/sec`).
2. **LIFO Compensation Rollback (2 Steps):** ~`50–70 µs` median latency.
3. **Outbox Deferral + Commit Flush:** ~`35–50 µs` median latency.
4. **Recursive Taint + PolicyGuard Check:** ~`40–60 µs` median latency.

### Tier 2: Distributed 4-Service Chaos & Security Matrix (`1,000` Workflows)
Compares 4 architectures across 5 failure and attack scenarios (`happy_path`, `prompt_injection`, `service_crash`, `concurrent_conflict`, `process_kill`):
1. **Naive ReAct Agent** — `20.0%` overall consistency
2. **Prompt-Rollback Agent** — `20.0%` overall consistency
3. **`AgentRewind (@rewind_tool)`** — **`100.0%` consistency, `0` leaked emails, `0` dirty overwrites**
4. **`AgentRewind (MCP Discovery)`** — **`100.0%` consistency, `0` leaked emails, `0` dirty overwrites**

---

## Running the Benchmarks

```bash
uv run python benchmarks/benchmark_suite.py
```

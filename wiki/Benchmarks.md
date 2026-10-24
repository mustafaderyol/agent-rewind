# Benchmarks (1,000 Distributed Workflows + Kernel Micro-Benchmarks)

We evaluated **`agent-rewind`** across two tiers:
1. **Tier 1 (Kernel Micro-Benchmarks):** Raw per-step overhead (~62 µs/step, 14,300+ ops/sec).
2. **Tier 2 (1,000-Workflow Chaos & Security Matrix):** 4 architectures x 5 scenarios x 50 iterations across 4 independent services (`crm`, `erp`, `payment`, `notify`).

## Evaluated Architectures

1. **Naive ReAct Agent:** Standard tool-calling loop without compensation, Outbox buffering, or taint isolation.
2. **LLM-Rollback Agent:** Attempts prompt-driven manual undo when an exception occurs, without Outbox buffering, optimistic concurrency checks, or persistent WAL.
3. **`agent-rewind (@rewind_tool)`:** Explicit decorator bindings with LIFO Saga Compensation, Outbox Buffer, and `PolicyGuard`.
4. **`agent-rewind (MCP Auto-Discovery)`:** Zero-config URL-only discovery (`RemoteMCPGateway.discover`) across the 4 Docker microservices.

---

## Results Table

| Scenario | Architecture | Runs | State Consistency % | Attack Blocked % | Leaked Emails | Dirty Overwrites | P50 Latency | P95 Latency |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **1. Happy Path** | Naive ReAct | 40 | 100.0% | 100.0% | 0 | 0 | 14.49 ms | 20.64 ms |
| **1. Happy Path** | LLM-Rollback | 40 | 100.0% | 100.0% | 0 | 0 | 8.68 ms | 12.79 ms |
| **1. Happy Path** | **`agent-rewind (@rewind_tool)`** | 40 | **100.0%** | **100.0%** | **0** | **0** | **17.95 ms** | **22.95 ms** |
| **1. Happy Path** | **`agent-rewind (MCP Auto-Discovery)`** | 40 | **100.0%** | **100.0%** | **0** | **0** | **17.42 ms** | **26.08 ms** |
| **2. Step 3 Prompt Injection** | Naive ReAct | 40 | 0.0% | 0.0% | 40 | 0 | 8.56 ms | 11.68 ms |
| **2. Step 3 Prompt Injection** | LLM-Rollback | 40 | 0.0% | 0.0% | 40 | 0 | 12.77 ms | 16.43 ms |
| **2. Step 3 Prompt Injection** | **`agent-rewind (@rewind_tool)`** | 40 | **100.0%** | **100.0%** | **0** | **0** | **18.17 ms** | **22.06 ms** |
| **2. Step 3 Prompt Injection** | **`agent-rewind (MCP Auto-Discovery)`** | 40 | **100.0%** | **100.0%** | **0** | **0** | **11.61 ms** | **18.07 ms** |
| **3. Step 3 Service Crash (503)** | Naive ReAct | 40 | 0.0% | 100.0% | 40 | 0 | 12.90 ms | 16.18 ms |
| **3. Step 3 Service Crash (503)** | LLM-Rollback | 40 | 0.0% | 100.0% | 40 | 0 | 10.24 ms | 17.01 ms |
| **3. Step 3 Service Crash (503)** | **`agent-rewind (@rewind_tool)`** | 40 | **100.0%** | **100.0%** | **0** | **0** | **17.28 ms** | **27.44 ms** |
| **3. Step 3 Service Crash (503)** | **`agent-rewind (MCP Auto-Discovery)`** | 40 | **100.0%** | **100.0%** | **0** | **0** | **17.25 ms** | **25.42 ms** |
| **4. Concurrent Dirty-Write** | Naive ReAct | 40 | 0.0% | 100.0% | 40 | 40 | 9.92 ms | 15.82 ms |
| **4. Concurrent Dirty-Write** | LLM-Rollback | 40 | 0.0% | 100.0% | 40 | 40 | 13.23 ms | 22.81 ms |
| **4. Concurrent Dirty-Write** | **`agent-rewind (@rewind_tool)`** | 40 | **100.0%** | **100.0%** | **0** | **0** | **21.65 ms** | **29.90 ms** |
| **4. Concurrent Dirty-Write** | **`agent-rewind (MCP Auto-Discovery)`** | 40 | **100.0%** | **100.0%** | **0** | **0** | **25.77 ms** | **32.95 ms** |
| **5. Process Kill (`SIGKILL` WAL)** | Naive ReAct | 40 | 0.0% | 100.0% | 40 | 0 | 6.48 ms | 12.76 ms |
| **5. Process Kill (`SIGKILL` WAL)** | LLM-Rollback | 40 | 0.0% | 100.0% | 40 | 0 | 11.11 ms | 16.47 ms |
| **5. Process Kill (`SIGKILL` WAL)** | **`agent-rewind (@rewind_tool)`** | 40 | **100.0%** | **100.0%** | **0** | **0** | **25.27 ms** | **34.51 ms** |
| **5. Process Kill (`SIGKILL` WAL)** | **`agent-rewind (MCP Auto-Discovery)`** | 40 | **100.0%** | **100.0%** | **0** | **0** | **19.08 ms** | **28.75 ms** |

## Reproducing the Benchmarks Locally

```bash
uv run python benchmarks/benchmark_suite.py
```

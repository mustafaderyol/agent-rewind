# Architecture Deep Dive

`agent-rewind` separates the **Reasoning Plane (LLM)** from the **Execution & Recovery Plane (`AgentRewind` Kernel)**.

```
[LLM / Agent Planner]
         │ (Invokes @rewind_tool or RemoteMCPGateway.call)
         ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ AGENT-REWIND KERNEL (Zero-Trust Execution Plane)                           │
│                                                                            │
│ 1. Recursive Taint & Policy Evaluation (`DataVault` + `PolicyGuard`)       │
│    - Unwraps nested Ref[T] pointers                                        │
│    - Checks `deny_tainted`, `require_trusted_match`, `max_value` & HITL    │
│                                                                            │
│ 2. Idempotency Guard (`SHA-256` Deterministic Key)                         │
│    - Prevents duplicate execution of identical financial/write calls       │
│                                                                            │
│ 3. Execution Router (`EffectType`)                                         │
│    ├─► READ_ONLY     : Executes immediately, no rollback needed            │
│    ├─► COMPENSATABLE : Captures `pre_state` -> Executes -> Pushes `undo`   │
│    │                   onto LIFO Compensation Stack                        │
│    └─► DEFERRED      : Buffers call in Outbox Queue until `commit()`       │
│                                                                            │
│ 4. Cryptographic Hash-Chained Journal (`prev_hash` -> `step_hash`)         │
└────────────────────────────────────────────────────────────────────────────┘
```

---

## 1. Deterministic LIFO Compensation Stack

Whenever a `COMPENSATABLE` tool succeeds, the kernel records a `_CompensationEntry` containing:
- The resolved input `args`
- The captured `pre_state` snapshot (taken immediately prior to execution)
- The captured `post_state` snapshot (if optimistic concurrency verification is enabled)
- The forward `result`
- The bound `compensate_fn` or self-contained `ActionReceipt.undo` closure

If **any** exception occurs at Step $N$ (`SecurityManipulationError`, `StepExecutionError`, `TimeoutError`, or user exception):
1. All buffered `DEFERRED` items in the Outbox are marked `DISCARDED` (never sent).
2. The LIFO stack is popped in reverse order ($N-1 \rightarrow 1$) with exponential backoff retries (`compensation_retries=3`).
3. If a compensation fails all retries (e.g., the target service is temporarily offline), it is saved as a `DLQRecord` for later replay via `AgentRewind.replay_dlq()`.

---

## 2. Partial Rollback via Nested Savepoints

Not every failure requires tearing down the entire transaction. `async with tx.savepoint("branch_name", suppress_errors=True):` records the current stack pointers of both the LIFO compensation stack and the Outbox queue. If an error occurs inside the `savepoint` block, **only the actions performed inside that block are rewound**, leaving earlier steps intact so the agent can take a fallback path.

---

## 3. Zero-Database Pluggable State Store

Because an agent transaction typically spans milliseconds to seconds while the Python process is alive, **`InMemoryBackend` (pure Python RAM)** is the default and requires zero database setup (`sqlite3`, PostgreSQL, or Oracle are never required). For process-crash recovery (`SIGKILL`), `JSONFileBackend` persists state to a lightweight single-file `.json` snapshot.

---

## 4. Official SDK Subclassing & Hierarchical Multi-Agent Sagas

Instead of reinventing protocols, `agent-rewind` subclasses the official SDK base classes:
- **MCP (`mcp` SDK):** `RewindServer(mcp.server.mcpserver.MCPServer)` extends `MCPServer.tool` and `call_tool` to attach `_meta.rewind.undo_token` onto `mcp.types.CallToolResult`.
- **Google A2A (`a2a-sdk`):** `RewindA2AExecutor(a2a.server.agent_execution.AgentExecutor)` wraps `execute(context, event_queue)` inside an `AgentRewind` transaction and automatically triggers LIFO rollback inside `cancel(context, event_queue)`.
- **LangGraph (`langgraph`) & LangChain (`langchain-core`):** `RewindStateGraph(StateGraph)`, `RewindToolNode(ToolNode)`, `TransactionalRunnable(Runnable)`, and `RewindSubAgentHost` (`tx.export_undo_lease()`) allow multi-agent supervisor graphs (`StateGraph`) to roll back entire remote sub-agent executions in LIFO order.

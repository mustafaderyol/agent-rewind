# Changelog

All notable changes to **`agent-rewind`** are documented in this file.

## [0.2.0] - 2026-10-24

### Added
- **Official Protocol & Framework Subclassing (`mcp`, `a2a-sdk`, `langchain-core`, `langgraph`)**:
  - `RewindServer(mcp.server.mcpserver.MCPServer)` for server-defined compensation (`@server.tool(compensate=..., pre_state=...)`) and `CallToolResult.meta["rewind"]["undo_token"]`.
  - `RewindA2AExecutor(a2a.server.agent_execution.AgentExecutor)` with automatic LIFO rollback on `cancel(context, event_queue)`.
  - `RewindStateGraph(langgraph.graph.StateGraph)`, `RewindToolNode(langgraph.prebuilt.ToolNode)`, `rewind_agent`, `rewind_graph`, and `RewindSubAgentHost` (`tx.export_undo_lease()`).
  - `TransactionalRunnable(langchain_core.runnables.Runnable)` and `rewind_tools([...])` for zero-client-compensation `RemoteRunnable` & `AgentExecutor` workflows.
- **Zero-Config Remote MCP Auto-Discovery (`RemoteMCPGateway.discover`)**: Automatically infers forward tools, `pre_state` getters, `compensate` tools, `DEFERRED` Outbox tools, and `deny_tainted` sensitive parameters from standard MCP `tools/list` schemas using only server URLs.
- **Partial Rollback via Savepoints (`tx.savepoint`)**: Nested checkpoints allowing partial unwinding of failed sub-branches without aborting earlier valid steps.
- **Dead-Letter Queue (`DLQRecord` & `replay_dlq`)**: Persists failed compensations after exponential backoff retries and replays them when downstream services recover.
- **Server-Side Lease TTL (`ServerLeaseManager`)**: Dead-Man's Switch that auto-expires and rolls back uncommitted mutations on remote MCP servers if the agent crashes or loses network connectivity.
- **Financial Idempotency Engine**: Deterministic SHA-256 `idempotency_key` deduplication and automatic `Idempotency-Key` / `X-Transaction-ID` HTTP header injection.
- **Human-in-the-Loop (HITL) Approval Gates (`PolicyGuard.require_approval`)**: Async/sync operator sign-off hooks with automatic rollback on denial.
- **Cryptographic Hash-Chained Audit Trail (`verify_audit_integrity`)**: SHA-256 step chaining (`prev_hash` -> `step_hash`) to detect post-hoc log tampering.
- **Zero-Database Pluggable Storage & Transport**: Pure-Python `InMemoryBackend` (default), `JSONFileBackend`, `CallbackBackend`, `StdlibHTTPTransport` (`urllib.request`), `HttpxTransport`, and `CallableMCPTransport`.

## [0.1.0] - 2026-10-24

### Added
- Initial release with LIFO Saga Compensation Stack, Outbox Commit Buffer (`deferred=True`), `ActionReceipt`, `DataVault`, `QuarantinedExtractor`, and `PolicyGuard`.

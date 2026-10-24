# AgentRewind — Runnable Examples & Scenarios

This directory contains **10 self-contained, runnable Python scripts** demonstrating every capability of `agent-rewind`. Every script is **100% standalone** and runs out-of-the-box without requiring any external database or Docker daemon.

---

## Quick Index

| # | Script | What It Demonstrates |
| :--- | :--- | :--- |
| **01** | [`01_remote_mcp_server_quickstart.py`](01_remote_mcp_server_quickstart.py) | Standing up a Remote MCP Server (`RewindServer(MCPServer)`) with server-side compensation, Outbox buffering, and automatic `undo_token` rollback. |
| **02** | [`02_distributed_four_systems_saga.py`](02_distributed_four_systems_saga.py) | 4-Server MCP Cluster (`crm`, `erp`, `payment`, `notify`) with Step 3 Prompt Injection blocking, 503 crash rollback, and happy-path commit. |
| **03** | [`03_remote_mcp_autodiscovery.py`](03_remote_mcp_autodiscovery.py) | Zero-config `RemoteMCPGateway.discover(urls)` connecting to 4 MCP servers using **only their URLs**. |
| **04** | [`04_live_packet_trace_simulation.py`](04_live_packet_trace_simulation.py) | Real-time JSON-RPC packet & kernel trace inspector showing pre-state queries, Outbox holds, and LIFO rollback packets. |
| **05** | [`05_savepoints_and_partial_rollback.py`](05_savepoints_and_partial_rollback.py) | Nested `async with tx.savepoint("..."):` for partial rollback and graceful degradation (Express Air -> Ground Shipping fallback). |
| **06** | [`06_financial_idempotency_and_hitl.py`](06_financial_idempotency_and_hitl.py) | SHA-256 duplicate tool-call idempotency cache, `max_value` financial circuit breaker, HITL `require_approval`, and hash-chained audit verification. |
| **07** | [`07_dlq_and_crash_recovery.py`](07_dlq_and_crash_recovery.py) | Exponential backoff compensation retries, Dead-Letter Queue (`replay_dlq`), `JSONFileBackend`, and `ServerLeaseManager` TTL Dead-Man's Switch. |
| **08** | [`08_server_defined_compensation.py`](08_server_defined_compensation.py) | **Server-Defined Compensation (`RewindServer`)**: Remote servers define `@server.tool(compensate=...)` privately and return opaque `undo_token`s; the client rolls them back with zero knowledge of server internals. |
| **09** | [`09_langchain_remote_runnable_agent.py`](09_langchain_remote_runnable_agent.py) | **LangChain `RemoteRunnable` + `AgentExecutor`**: End-to-end server-defined compensation with `rewind_tools(...)` and automatic rollback even when `AgentExecutor` catches tool exceptions internally. |
| **10** | [`10_langgraph_multi_agent_and_a2a.py`](10_langgraph_multi_agent_and_a2a.py) | **LangGraph Multi-Agent Supervisor (`StateGraph`) + Google A2A**: Hierarchical multi-agent saga with `rewind_agent`, `rewind_graph`, `RewindSubAgentHost`, and standard A2A `tasks/send` & `tasks/cancel`. |

---

## Running the Examples

```bash
uv run python examples/01_remote_mcp_server_quickstart.py
uv run python examples/02_distributed_four_systems_saga.py
uv run python examples/03_remote_mcp_autodiscovery.py
uv run python examples/04_live_packet_trace_simulation.py
uv run python examples/05_savepoints_and_partial_rollback.py
uv run python examples/06_financial_idempotency_and_hitl.py
uv run python examples/07_dlq_and_crash_recovery.py
uv run python examples/08_server_defined_compensation.py
uv run python examples/09_langchain_remote_runnable_agent.py
uv run python examples/10_langgraph_multi_agent_and_a2a.py
```

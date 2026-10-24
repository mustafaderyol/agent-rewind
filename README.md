# ⏪ AgentRewind (`agent-rewind`)

[![CI](https://github.com/mustafaderyol/agent-rewind/actions/workflows/ci.yml/badge.svg)](https://github.com/mustafaderyol/agent-rewind/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![Zero Dependencies](https://img.shields.io/badge/dependencies-zero-brightgreen.svg)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**Zero-Trust Transactional Rollback, Outbox Buffering &MCP Auto-Discovery Runtime for AI Agents (`Ctrl+Z` for Tool Calling).**

When an autonomous AI agent executes actions across multiple external services (CRM, ERP, Payment Gateway, Email/Slack) and **crashes** or **gets manipulated by Indirect Prompt Injection** at Step 3, standard agents leave your databases in a corrupted, half-mutated state.

**`agent-rewind`** acts as an application-layer transactional supervisor around your agent's tool calls:
1. **Halts execution immediately** on downstream exceptions, timeouts, or security policy violations.
2. **Discards buffered irreversible side effects** (`deferred=True` Outbox emails, webhooks, Slack notifications) so false confirmations are never sent.
3. **Deterministically rewinds completed steps in strict LIFO order** (`Step 2 -> Step 1`) without relying on the compromised or hallucinating LLM.

---

## ✨ Key Capabilities

| Feature | Description |
| :--- | :--- |
| **⏪ LIFO Saga Compensation (`@rewind_tool`)** | Attach undo handlers via inline `compensate=...`, `@tool.compensate`, or `ActionReceipt(result=..., undo_args=...)`. |
| **📭 Transactional Outbox (`deferred=True`)** | Buffers irreversible actions (emails, webhooks) in memory and flushes them **only** after the entire transaction commits. |
| **🌐 Zero-Config MCP Auto-Discovery** | Pass only MCP server URLs (`await RemoteMCPGateway.discover(urls)`). Automatically pairs forward & rollback tools and hides undo tools from the LLM. |
| **🏢 Server-Defined Compensation (`RewindServer`)** | Define `@server.tool(compensate=...)` privately inside remote FastAPI/MCP microservices; servers return opaque `undo_token`s so the client agent rolls them back with **zero knowledge** of server internals! |
| **🔗 Universal Agent Adapters** | Drop-in support for LangChain `RemoteRunnable` & `AgentExecutor` (`rewind_runnable`), OpenAI/Anthropic/Gemini batch tool calls (`tx.dispatch_tool_calls`), and both sync (`with AgentRewind():`) and async (`async with AgentRewind():`) flows. |
| **🛡️ CaMeL Taint Tracking & PolicyGuard** | Isolates untrusted text in a tool-less `QuarantinedExtractor`, tracks recursive taint in `DataVault`, and blocks poisoned arguments (`deny_tainted`, `require_trusted_match`) or out-of-order LLM tool calls (`require_sequence`, `requires_prior`). |
| **🔖 Nested Savepoints (`tx.savepoint`)** | Roll back a failing sub-branch (e.g., fallback from Express Air to Ground Shipping) without aborting earlier valid steps. |
| **💳 Financial Idempotency & Circuit Breakers** | SHA-256 deduplication prevents double-charging on LLM retries; `max_value` enforces hard financial limits; `require_approval` adds Human-in-the-Loop (HITL) sign-off. |
| **🚑 Dead-Letter Queue (`DLQ`) & Lease TTL** | Exponential backoff retries for compensations, `replay_dlq()` recovery, and `ServerLeaseManager` Dead-Man's Switch for crashed agents. |
| **🪶 Zero Database Dependencies** | Runs in pure Python RAM (`InMemoryBackend`) by default. Optional single-file `JSONFileBackend`, `CallbackBackend`, and `StdlibHTTPTransport` require **zero external packages**. |

---

## 📦 Installation

```bash
pip install agent-rewind
# Or with uv:
uv add agent-rewind
```

Optional extras for high-concurrency `httpx` connection pooling and rich terminal tables:
```bash
pip install "agent-rewind[http,rich]"
```

---

## 🚀 60-Second Quickstart

```python
import asyncio
from agent_rewind import ActionReceipt, AgentRewind, PolicyGuard, rewind_tool


# 1. Reversible CRM tool using ActionReceipt to pass server-generated IDs to undo
@rewind_tool(compensate=lambda return_id: print(f"[UNDO] Cancelled CRM return {return_id}"))
async def create_return(order_id: str) -> ActionReceipt:
    return_id = f"RET-{order_id}"
    print(f"[EXEC] Created CRM return {return_id}")
    return ActionReceipt(
        result={"return_id": return_id, "sku": "SKU-MACBOOK", "amount": 45000.0},
        undo_args={"return_id": return_id},
    )


# 2. Reversible ERP tool with automatic pre-state snapshot
@rewind_tool(
    pre_state=lambda args: 100,
    compensate=lambda args, res, pre_qty: print(f"[UNDO] Restored {args['sku']} stock to {pre_qty}"),
)
async def restock_item(sku: str, qty: int) -> dict:
    print(f"[EXEC] Incremented {sku} stock by +{qty}")
    return {"sku": sku, "new_qty": 100 + qty}


# 3. Irreversible Notification buffered in the Outbox until commit
@rewind_tool(deferred=True)
async def send_email(to: str, subject: str) -> dict:
    print(f"[OUTBOX FLUSH] Sent email '{subject}' to {to}")
    return {"sent": True}


# 4. Protected Payment tool with Taint Guard & Financial Ceiling
@rewind_tool(
    deny_tainted=["iban"],
    max_value={"amount": 50000.0},
    compensate=lambda args, res: print(f"[UNDO] Reversed wire transfer {res['tx_id']}"),
)
async def issue_refund(order_id: str, iban: str, amount: float) -> dict:
    raise ConnectionError("503 Service Unavailable: Banking HSM Gateway Timeout")


async def main() -> None:
    async with AgentRewind(tx_id="tx-demo-01", suppress_errors=True) as tx:
        ret = await create_return("ORD-101")
        await restock_item(ret["sku"], qty=1)
        await send_email("alice@example.com", "Your Refund Has Started")
        await issue_refund("ORD-101", "TR11000011112222", ret["amount"])  # Fails at Step 3!

    print(f"Final State: {tx.report.state.value} | Compensated: {tx.report.compensated_steps}")


if __name__ == "__main__":
    asyncio.run(main())
```

**Output:**
```text
[EXEC] Created CRM return RET-ORD-101
[EXEC] Incremented SKU-MACBOOK stock by +1
[UNDO] Restored SKU-MACBOOK stock to 100
[UNDO] Cancelled CRM return RET-ORD-101
Final State: ROLLED_BACK | Compensated: ['restock_item', 'create_return']
```
*(Notice that `send_email` was buffered in the Outbox and discarded when Step 3 failed—no false email was ever sent!)*

---

## 🔌 Built on Official Protocol SDKs (`mcp` & `a2a-sdk` — "Extend, Never Rewrite")

`agent-rewind` **never reinvents existing agent protocols**. It depends directly on the official **[`mcp` (`modelcontextprotocol/python-sdk`)](https://github.com/modelcontextprotocol/python-sdk)** and **[`a2a-sdk` (`a2aproject/a2a`)](https://github.com/a2aproject/a2a)** packages, subclassing their official base classes and extending their standard metadata and cancellation hooks:

| Protocol / Ecosystem | Official SDK Base Class Extended | Standard Envelope Extended | Automatic LIFO Rollback Mechanism |
| :--- | :--- | :--- | :--- |
| **Model Context Protocol (`mcp`)** | `RewindServer(mcp.server.mcpserver.MCPServer)` | `mcp.types.CallToolResult.meta["rewind"]` & `Tool.meta["rewind"]` | Official `MCPServer.call_tool("_rewind_rollback")` & `rewind/rollback` |
| **Google A2A (`a2a-sdk`)** | `RewindA2AExecutor(a2a.server.agent_execution.AgentExecutor)` | `a2a.types.Task.metadata["rewind"]` & `REWIND_A2A_EXTENSION` | Official `AgentExecutor.cancel(context, event_queue)` & `tasks/cancel` |
| **LangGraph (`langgraph`)** | `RewindStateGraph(langgraph.graph.StateGraph)` & `RewindToolNode(ToolNode)` | `AIMessage.response_metadata["rewind"]` | `RewindStateGraph.compile()` / `rewind_graph()` rewinds all prior sub-agents in LIFO order |
| **LangChain (`langchain-core`)** | `TransactionalRunnable(langchain_core.runnables.Runnable)` | `_meta.rewind.undo_token` | In-band rollback token dispatch on `with AgentRewind():` exit |

---

### 1. LangGraph Multi-Agent Supervisor (`StateGraph` + `RemoteRunnable` / Google A2A)

When a `Supervisor` routes tasks across multiple remote sub-agents (`remote_1` -> `remote_2` -> `remote_3`), wrap the remote agents with `rewind_agent(...)` and the compiled graph with `rewind_graph(...)`. If `remote_3` fails, `remote_2` and `remote_1` (including all internal tools executed inside those remote sub-agents!) are rolled back in strict LIFO order:

```python
from typing import TypedDict
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RemoteRunnable
from langgraph.graph import END, START, StateGraph
from agent_rewind import rewind_agent, rewind_graph

class AgentState(TypedDict):
    messages: list
    sender: str
    next_step: str

# 1. Wrap RemoteRunnable or A2A sub-agents with `rewind_agent()` (drop-in replacement!)
remote_agent_1 = rewind_agent(RemoteRunnable("https://remote-service-1.com/agent"), name="remote_1")
remote_agent_2 = rewind_agent(RemoteRunnable("https://remote-service-2.com/agent"), name="remote_2")
remote_agent_3 = rewind_agent(RemoteRunnable("https://remote-service-3.com/agent"), name="remote_3")

def remote_node_1(state: AgentState):
    res = remote_agent_1.invoke({"messages": state["messages"]})
    return {"messages": [AIMessage(content=str(res))], "sender": "remote_1"}

def remote_node_2(state: AgentState):
    res = remote_agent_2.invoke({"messages": state["messages"]})
    return {"messages": [AIMessage(content=str(res))], "sender": "remote_2"}

def remote_node_3(state: AgentState):
    res = remote_agent_3.invoke({"messages": state["messages"]})
    return {"messages": [AIMessage(content=str(res))], "sender": "remote_3"}

workflow = StateGraph(AgentState)
workflow.add_node("supervisor", local_supervisor_node)
workflow.add_node("remote_1", remote_node_1)
workflow.add_node("remote_2", remote_node_2)
workflow.add_node("remote_3", remote_node_3)
# ... edges configured as usual ...

# 2. Wrap `workflow.compile()` with `rewind_graph()`
multi_agent_system = rewind_graph(workflow.compile())

# If remote_3 fails, remote_2 and remote_1 are automatically rolled back in LIFO order!
result = multi_agent_system.invoke({
    "messages": [HumanMessage(content="Execute distributed multi-agent workflow.")],
    "sender": "user",
    "next_step": "",
})
```

---

### 2. LangChain `AgentExecutor` + `RemoteRunnable` (Zero Client Compensation Code)

When compensation is defined on the remote MCP/LangServe server (`RewindServer`), the client needs **zero compensation code**:

```python
from langchain.agents import AgentExecutor, create_tool_calling_agent
from langchain_core.runnables import RemoteRunnable
from agent_rewind import AgentRewind, rewind_tools

remote_tool = RemoteRunnable("https://your-remote-service.com/remote_create_order")
tools = rewind_tools([remote_tool])  # Automatically captures `_meta.rewind.undo_token`!

agent = create_tool_calling_agent(llm, tools, prompt)
agent_executor = AgentExecutor(agent=agent, tools=tools, verbose=True)

with AgentRewind() as tx:
    response = agent_executor.invoke({"input": "Execute the workflow using remote tools."})
```

---

### 3. Zero-Config Remote MCP Auto-Discovery (`RemoteMCPGateway`)

Have only MCP server URLs? `RemoteMCPGateway.discover()` speaks standard **MCP JSON-RPC 2.0 (`tools/list` & `tools/call`)**, inspects schemas, and wires transactional rollback hooks automatically:

```python
from agent_rewind import AgentRewind, RemoteMCPGateway

mcp = await RemoteMCPGateway.discover([
    "http://localhost:18001",  # CRM MCP Server
    "http://localhost:18002",  # ERP MCP Server
    "http://localhost:18003",  # Payment MCP Server
    "http://localhost:18004",  # Notification MCP Server
])

async with AgentRewind() as tx:
    ret = await mcp.call("crm_create_return", order_id="ORD-101")
    await mcp.call("erp_adjust_stock", sku=ret["sku"], delta=1)
    await mcp.call("notify_send_email", to=ret["customer_email"], subject="Refund", body="...")
    await mcp.call("payment_process_refund", order_id="ORD-101", iban=ret["registered_iban"], amount=ret["amount"])
```

---

## 📊 Benchmark Summary (1,000 Distributed Workflows + Micro-Benchmark)

Evaluated across 4 distributed services (`crm`, `erp`, `payment`, `notify`) over 5 failure & attack modes (250 runs per architecture = 1,000 workflows):

- **Kernel Overhead:** **~62 µs/op** (`14,300+ ops/sec`) in-memory execution overhead.

| Architecture | Cross-System Consistency | Prompt Injection Blocked | Premature Emails Leaked | Dirty Rollback Overwrites | Median Latency (`P50`) |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **1. Naive ReAct Agent** | 20.0% | 0.0% | 200 / 200 | 50 / 50 | 0.08 ms |
| **2. LLM-Rollback Agent** | 20.0% | 0.0% | 200 / 200 | 50 / 50 | 0.11 ms |
| **3. `agent-rewind` (`@rewind_tool`)** | **100.0%** | **100.0%** | **0 / 200** | **0 / 50** | **0.24 ms** |
| **4. `agent-rewind` (MCP Auto-Discovery)** | **100.0%** | **100.0%** | **0 / 200** | **0 / 50** | **0.31 ms** |

See [wiki/Benchmarks.md](wiki/Benchmarks.md) for the full methodology and reproduction steps.

---

## 📚 Documentation & Wiki

### Documentation (`docs/`)
- **[Getting Started Guide](docs/getting-started.md)** — Installation, core primitives, and first transaction.
- **[MCP & Remote Server Guide (`RewindServer` + `RemoteRunnable`)](docs/mcp-and-remote-server-guide.md)** — Complete step-by-step guide on writing transactional remote servers and connecting LangChain `RemoteRunnable` / `AgentExecutor` clients.
- **[Framework Integrations](docs/framework-integrations.md)** — OpenAI, LangChain `AgentExecutor`, LangGraph, FastMCP, and FastAPI HITL webhooks.
- **[Storage & Transports](docs/storage-and-transports.md)** — Zero-DB `InMemoryBackend`, `JSONFileBackend`, `CallbackBackend`, and `StdlibHTTPTransport`.
- **[Real-World Scenarios Catalog](docs/scenarios-catalog.md)** — Detailed walkthrough of 7 enterprise failure modes and patterns.

### GitHub Wiki (`wiki/`)
- **[Wiki Home](wiki/Home.md)**
- **[Architecture Deep Dive](wiki/Architecture-Deep-Dive.md)**
- **[Decorators & ActionReceipts](wiki/Decorators-and-Receipts.md)**
- **[Remote MCP Auto-Discovery](wiki/Remote-MCP-Auto-Discovery.md)**
- **[Security & PolicyGuard (CaMeL)](wiki/Security-and-Policy-Guard.md)**
- **[Benchmarks](wiki/Benchmarks.md)**

### Runnable Examples (`examples/`)
Explore all 10 self-contained runnable scripts in [`examples/README.md`](examples/README.md):
1. [`examples/01_quickstart_decorator.py`](examples/01_quickstart_decorator.py) — Local decorator & `ActionReceipt` quickstart.
2. [`examples/02_distributed_four_systems_saga.py`](examples/02_distributed_four_systems_saga.py) — 4-System Saga with Prompt Injection & 503 crash rollback.
3. [`examples/03_remote_mcp_autodiscovery.py`](examples/03_remote_mcp_autodiscovery.py) — Zero-config URL-only MCP discovery.
4. [`examples/04_live_packet_trace_simulation.py`](examples/04_live_packet_trace_simulation.py) — Live JSON-RPC packet & kernel trace inspector.
5. [`examples/05_savepoints_and_partial_rollback.py`](examples/05_savepoints_and_partial_rollback.py) — Nested savepoints (`tx.savepoint`) for shipping fallback.
6. [`examples/06_financial_idempotency_and_hitl.py`](examples/06_financial_idempotency_and_hitl.py) — Financial idempotency, `max_value` ceiling, and HITL approval.
7. [`examples/07_dlq_and_crash_recovery.py`](examples/07_dlq_and_crash_recovery.py) — Dead-Letter Queue (`replay_dlq`) and `ServerLeaseManager` TTL expiry.
8. [`examples/08_server_defined_compensation.py`](examples/08_server_defined_compensation.py) — Server-defined compensation (`RewindServer` + opaque `undo_token` protocol).
9. [`examples/09_langchain_remote_runnable_agent.py`](examples/09_langchain_remote_runnable_agent.py) — LangChain `RemoteRunnable` + `AgentExecutor` with server-defined compensation (`rewind_tools`).
10. [`examples/10_langgraph_multi_agent_and_a2a.py`](examples/10_langgraph_multi_agent_and_a2a.py) — **LangGraph Multi-Agent Supervisor (`StateGraph`) + Google A2A (`tasks/send` & `tasks/cancel`) + `RewindSubAgentHost`**.

---

## 🧪 Development & Benchmarking

```bash
# Run the unit and integration test suite
uv run --extra dev pytest -v

# Run the self-contained Micro-Benchmark + 1,000-Workflow Chaos & Security Suite
uv run python benchmarks/benchmark_suite.py
```

---

## 🤝 Contributing & License

Contributions are welcome! Please read [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md) before submitting a Pull Request.

Licensed under the [MIT License](LICENSE).

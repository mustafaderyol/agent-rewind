# Complete Guide: Writing Transactional MCP Servers (`RewindServer`) & Client Agents

In real-world enterprise architectures, **the AI Agent (Client) does not own the remote databases**. When an LLM calls remote tools hosted on separate microservices (CRM, ERP, Billing, Inventory) via the **Model Context Protocol (MCP)** or LangChain's `RemoteRunnable`:

1. **The LLM dynamically decides which tools to call and in what order** at runtime.
2. **Only the remote MCP server knows how to safely undo its own database mutations** (capturing pre-state snapshots, restoring rows, releasing locks).
3. **If any step fails**, every previously executed tool across all remote servers must be rolled back in strict **LIFO (Last-In, First-Out)** order without leaking compensation logic into the LLM prompt or client code.

`agent-rewind` solves this natively over the **Official Model Context Protocol (MCP) JSON-RPC 2.0 specification (`2024-11-05` / `2025-03-26`)** using **`RewindServer`** (on the server) and **`RemoteMCPGateway` / `rewind_tools` / `AgentRewind`** (on the client).

---

## Architecture Overview: MCP JSON-RPC 2.0 + `_meta.rewind.undo_token`

```mermaid
sequenceDiagram
    participant LLM as LLM (AgentExecutor)
    participant Client as Client (with AgentRewind)
    participant ServerA as MCP Server A (CRM)
    participant ServerB as MCP Server B (Payment)

    Client->>ServerA: JSON-RPC 2.0: "initialize" & "tools/list"
    ServerA-->>Client: MCP Tool Schemas + ToolAnnotations + _meta.rewind

    LLM->>Client: 1. Call Tool A (create_order)
    Client->>ServerA: JSON-RPC 2.0: "tools/call" {"name": "create_order", "arguments": {...}}
    Note over ServerA: Captures pre_state locally<br/>Executes forward DB insert<br/>Mints Lease Token (utok_1)
    ServerA-->>Client: CallToolResult {content: [...], structuredContent: {...}, _meta: {rewind: {undo_token: "utok_1"}}}
    Note over Client: Extracts structuredContent for LLM<br/>Pushes utok_1 onto LIFO Rewind Stack
    Client-->>LLM: Observation: {"order_id": "ORD-1"}

    LLM->>Client: 2. Call Tool B (charge_card)
    Client->>ServerB: JSON-RPC 2.0: "tools/call" {"name": "charge_card", "arguments": {...}}
    ServerB-->>Client: JSON-RPC Error / Payment Declined
    Note over Client: Marks transaction failed!<br/>Even if AgentExecutor catches error,<br/>AgentRewind triggers LIFO Rollback on exit

    Client->>ServerA: JSON-RPC 2.0: "rewind/rollback" {"undo_token": "utok_1"}
    Note over ServerA: Executes @server.tool(compensate=...)<br/>Restores DB state & releases lease
    ServerA-->>Client: {"rolled_back": true, "undo_token": "utok_1"}
```

---

## Step 1: How to Write a Transactional MCP Server (`RewindServer`)

[`RewindServer`](../src/agent_rewind/server.py) is a **100% spec-compliant MCP JSON-RPC 2.0 Server** that supports:
- **MCP Streamable HTTP** (`POST /mcp` JSON-RPC 2.0 endpoint)
- **MCP `stdio` Transport** (`server.run_stdio()` for Claude Desktop, Cursor, Windsurf, or `mcp.client.stdio`)
- **Official Anthropic `FastMCP` Integration** (`server.mount_to_fastmcp(fastmcp_app)`)
- **LangServe `RemoteRunnable` Compatibility** (`POST /{tool_name}/invoke`)

### `mcp_server.py` (Remote MCP Server)

```python
from typing import Any
from fastapi import FastAPI
from agent_rewind import RewindServer

server = RewindServer(name="crm-and-erp-mcp-server", version="0.2.0", default_lease_ttl=120.0)

# Simulated Remote Databases
ORDERS_DB: dict[str, dict[str, Any]] = {}
STOCK_DB: dict[str, int] = {"SKU-100": 50}

# 1. Define Server-Side Compensation for Order Creation
async def undo_create_order(args: dict[str, Any], result: dict[str, Any], pre_state: Any) -> None:
    ORDERS_DB.pop(result["order_id"], None)

@server.tool(
    name="remote_create_order",
    description="Creates a new order in the remote CRM database.",
    compensate=undo_create_order,
)
async def create_order(customer: str, sku: str, qty: int) -> dict[str, Any]:
    order_id = f"ORD-{len(ORDERS_DB) + 1}"
    ORDERS_DB[order_id] = {"customer": customer, "sku": sku, "qty": qty}
    return {"order_id": order_id, "status": "CREATED"}


# 2. Define Server-Side Pre-State Snapshot + Compensation for Inventory Update
async def snapshot_stock(args: dict[str, Any]) -> int:
    return STOCK_DB.get(args["sku"], 0)

async def undo_reserve_stock(args: dict[str, Any], result: dict[str, Any], pre_state: int) -> None:
    STOCK_DB[args["sku"]] = pre_state

@server.tool(
    name="remote_reserve_stock",
    description="Reserves stock in the remote warehouse ERP.",
    pre_state=snapshot_stock,
    compensate=undo_reserve_stock,
)
async def reserve_stock(sku: str, qty: int) -> dict[str, Any]:
    STOCK_DB[sku] -= qty
    return {"sku": sku, "remaining": STOCK_DB[sku]}


# Option A: Expose over HTTP / FastAPI (`POST /mcp` JSON-RPC 2.0 + `/{tool_name}/invoke`)
app = FastAPI(title="Transactional MCP Server")
app.include_router(server.create_fastapi_router())

# Option B: Or run as an MCP `stdio` server (for Claude Desktop / Cursor / stdio clients):
if __name__ == "__main__":
    server.run_stdio()
```

### Option C: Mounting onto an Official Anthropic `FastMCP` Instance

If you already use `from mcp.server.fastmcp import FastMCP`, you can mount `RewindServer` directly onto it:

```python
from mcp.server.fastmcp import FastMCP
from agent_rewind import RewindServer

mcp_app = FastMCP("enterprise-mcp")
rewind_srv = RewindServer("enterprise-mcp")

@rewind_srv.tool(compensate=undo_create_order)
async def create_order(customer: str, sku: str, qty: int) -> dict:
    ...

# Registers all @rewind_srv.tool functions + _rewind_rollback + _rewind_commit on FastMCP!
rewind_srv.mount_to_fastmcp(mcp_app)
```

### Supported MCP JSON-RPC 2.0 Methods on `RewindServer`

| JSON-RPC 2.0 Method | Direction | Description |
| :--- | :---: | :--- |
| `initialize` | Client -> Server | Negotiates MCP protocol version (`2024-11-05`) and advertises `capabilities.tools` + `capabilities.experimental.rewind`. |
| `notifications/initialized` | Client -> Server | Standard MCP session initialization notification. |
| `ping` | Client -> Server | Health check returning `{}`. |
| `tools/list` | Client -> Server | Returns MCP `Tool` schemas with `inputSchema`, `annotations` (`readOnlyHint`, `destructiveHint`, `idempotentHint`), and `_meta.rewind`. |
| `tools/call` | Client -> Server | Executes the tool, captures `pre_state` on the server, mints a leased `undo_token`, and returns an MCP `CallToolResult` (`content`, `structuredContent`, `isError`, `_meta.rewind.undo_token`). Also supports `name="_rewind_rollback"` for pure `tools/call` clients. |
| `rewind/rollback` | Client -> Server | Executes the server-side compensation bound to `params.undo_token` and releases the lease. |
| `rewind/commit` | Client -> Server | Finalizes the server lease for `params.undo_token`. |

---

## Step 2: How to Write the Client (`AgentExecutor` + MCP / `RemoteRunnable`)

### Pattern 1: Connecting to MCP Servers via `RemoteMCPGateway` + `AgentExecutor`

```python
from langchain_openai import ChatOpenAI
from langchain.agents import AgentExecutor, create_tool_calling_agent
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from agent_rewind import AgentRewind, RemoteMCPGateway

async def main():
    # 1. Discover tools over MCP JSON-RPC 2.0 (`POST /mcp` -> `tools/list`)
    mcp = await RemoteMCPGateway.discover([
        "https://crm-mcp.company.com",
        "https://erp-mcp.company.com",
        "https://payment-mcp.company.com",
    ])

    # 2. Export discovered MCP tools directly for LangChain AgentExecutor
    tools = mcp.as_langchain_tools()

    llm = ChatOpenAI(model="gpt-4o", temperature=0)
    prompt = ChatPromptTemplate.from_messages([
        ("system", "You are a helpful assistant. Use the remote MCP tools to complete the task."),
        MessagesPlaceholder("chat_history", optional=True),
        ("human", "{input}"),
        MessagesPlaceholder("agent_scratchpad"),
    ])

    agent = create_tool_calling_agent(llm, tools, prompt)
    agent_executor = AgentExecutor(agent=agent, tools=tools, verbose=True)

    # 3. Execute inside `async with AgentRewind() as tx:`
    async with AgentRewind() as tx:
        response = await agent_executor.ainvoke({
            "input": "Create an order for 2x SKU-100, reserve stock, and charge the card."
        })
        print(response["output"])
```

### Pattern 2: Connecting via LangChain `RemoteRunnable` + `rewind_tools`

If your client connects to `RewindServer` using LangChain's `RemoteRunnable`:

```python
import os
from langchain_openai import ChatOpenAI
from langchain.agents import AgentExecutor, create_tool_calling_agent
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.runnables import RemoteRunnable

from agent_rewind import AgentRewind, rewind_tools

os.environ["OPENAI_API_KEY"] = "your-api-key"

# 1. Connect to Remote Tools via LangChain RemoteRunnable
remote_order_tool = RemoteRunnable("https://your-remote-service.com/remote_create_order")
remote_stock_tool = RemoteRunnable("https://your-remote-service.com/remote_reserve_stock")
remote_payment_tool = RemoteRunnable("https://your-remote-service.com/remote_charge_card")

# 2. Wrap with rewind_tools() — ZERO compensation code needed on the client!
#    `rewind_tools` automatically intercepts `_meta.rewind.undo_token` from the remote server.
tools = rewind_tools([
    remote_order_tool,
    remote_stock_tool,
    remote_payment_tool,
])

# 3. Standard LLM & Prompt Definition
llm = ChatOpenAI(model="gpt-4o", temperature=0)

prompt = ChatPromptTemplate.from_messages([
    ("system", "You are a helpful assistant. Use the remote tools to complete the user's request."),
    MessagesPlaceholder("chat_history", optional=True),
    ("human", "{input}"),
    MessagesPlaceholder("agent_scratchpad"),
])

# 4. Standard Agent & AgentExecutor Creation
agent = create_tool_calling_agent(llm, tools, prompt)
agent_executor = AgentExecutor(agent=agent, tools=tools, verbose=True)

if __name__ == "__main__":
    # 5. Execute inside `with AgentRewind() as tx:`
    with AgentRewind() as tx:
        response = agent_executor.invoke({
            "input": "Create an order for 2x SKU-100, reserve the stock, and charge the card."
        })
        print(response["output"])

    print(f"Final Transaction State: {tx.state.value}")
```

---

## Step 3: Multi-Agent Orchestration (`StateGraph` + Google A2A + `RewindSubAgentHost`)

When building a **Multi-Agent System** (e.g., a LangGraph `StateGraph` where a `supervisor` routes work across `remote_agent_1`, `remote_agent_2`, `remote_agent_3` over LangServe `RemoteRunnable` or **Google A2A Protocol** `tasks/send` / `tasks/cancel`):

1. **On Each Remote Sub-Agent Server (`RewindSubAgentHost`)**:
   Wrap your sub-agent with [`RewindSubAgentHost`](../src/agent_rewind/protocols.py). Even if `remote_agent_1` executes multiple internal `@rewind_tool` mutations on its own server, `tx.export_undo_lease()` bundles all of them into a single composite `undo_token` attached to `AIMessage.response_metadata["rewind"]`, MCP `_meta.rewind`, or A2A `Task.metadata.rewind`.
2. **On the Supervisor / Orchestrator (`rewind_agent` + `rewind_graph`)**:
   Wrap each `RemoteRunnable` or A2A client with [`rewind_agent(...)`](../src/agent_rewind/protocols.py) and wrap `workflow.compile()` with [`rewind_graph(...)`](../src/agent_rewind/protocols.py).
   If `remote_3` fails later in the graph, `AgentRewind` automatically rewinds `remote_2` (e.g., via standard A2A `tasks/cancel`) and `remote_1` (unwinding all of its internal tool calls) in strict LIFO order!


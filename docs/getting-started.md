# Getting Started with AgentRewind

`agent-rewind` brings **transactional LIFO rollback, server-defined compensation, outbox buffering, and multi-agent saga recovery** to AI Agents built on the official **Model Context Protocol (`mcp`)**, **Google A2A (`a2a-sdk`)**, **LangChain (`langchain`)**, and **LangGraph (`langgraph`)** ecosystems.

---

## 1. Installation

```bash
pip install agent-rewind
# or with uv:
uv add agent-rewind
```

---

## 2. Core Primitives in 60 Seconds

| Component | Class / Function | Purpose |
| :--- | :--- | :--- |
| **Transactional MCP Server** | `RewindServer(MCPServer)` | Subclasses `mcp.server.mcpserver.MCPServer`. Lets remote servers define `@server.tool(compensate=..., pre_state=..., deferred=...)` privately and issue opaque `undo_token` leases. |
| **Transaction Context** | `with AgentRewind() as tx:` / `async with AgentRewind() as tx:` | Wraps an `AgentExecutor.invoke(...)` or MCP tool session. Automatically rolls back all completed remote actions in LIFO order if any step fails. |
| **LangChain Remote Tool Adapter** | `rewind_tools([RemoteRunnable(...)])` | Wraps LangChain `RemoteRunnable` or `BaseTool` instances. Automatically captures `_meta.rewind.undo_token` and triggers server rollback on failure. |
| **MCP Auto-Discovery Gateway** | `await RemoteMCPGateway.discover(urls)` | Connects to remote MCP servers via URLs (`tools/list` & `tools/call`) and exports tools via `mcp.as_langchain_tools()`. |
| **Multi-Agent Saga Wrappers** | `rewind_agent(...)` & `rewind_graph(...)` | Wraps remote LangServe / Google A2A sub-agents and compiled LangGraph `StateGraph` workflows for hierarchical multi-agent rollback. |

---

## 3. Step 1: Stand Up a Remote Transactional MCP Server (`mcp_server.py`)

In real agent architectures, tools live on remote microservices that own their databases. Create `mcp_server.py` using `RewindServer`:

```python
from typing import Any
from agent_rewind import RewindServer

# Directly subclasses `mcp.server.mcpserver.MCPServer` from the official `mcp` SDK
server = RewindServer(name="order-service-mcp", version="0.2.0", default_lease_ttl=60.0)

ORDERS_DB: dict[str, dict[str, Any]] = {}
STOCK_DB: dict[str, int] = {"SKU-MACBOOK": 50}


# 1. Reversible CRM tool (compensation stays private on the server!)
@server.tool(
    name="create_order",
    description="Create a customer order in the remote CRM database.",
    compensate=lambda args, res: ORDERS_DB.pop(res["order_id"], None),
)
async def create_order(customer: str, sku: str, qty: int) -> dict[str, Any]:
    order_id = f"ORD-{len(ORDERS_DB) + 101}"
    ORDERS_DB[order_id] = {"customer": customer, "sku": sku, "qty": qty}
    return {"order_id": order_id, "sku": sku, "qty": qty, "amount": 45000.0}


# 2. Reversible ERP tool with server-side pre-state snapshot
@server.tool(
    name="reserve_stock",
    description="Reserve warehouse stock in the remote ERP database.",
    pre_state=lambda args: STOCK_DB[args["sku"]],
    compensate=lambda args, res, prev_qty: STOCK_DB.update({args["sku"]: prev_qty}),
)
async def reserve_stock(sku: str, qty: int) -> dict[str, Any]:
    STOCK_DB[sku] -= qty
    return {"sku": sku, "remaining": STOCK_DB[sku]}


# 3. Irreversible Notification buffered in the Outbox until commit
@server.tool(
    name="send_email",
    description="Send an order confirmation email (Buffered until transaction commits).",
    deferred=True,
)
async def send_email(to: str, subject: str) -> dict[str, Any]:
    return {"sent": True, "to": to}
```

### How to Launch Your Remote MCP Server

Because `RewindServer` subclasses `mcp.server.mcpserver.MCPServer` and also includes FastAPI/LangServe adapters, you can stand it up in **4 ways**:

#### Option A: Official MCP Streamable HTTP Server
```python
if __name__ == "__main__":
    server.run(transport="streamable-http", host="0.0.0.0", port=8000)
```

#### Option B: FastAPI + Uvicorn (Serves MCP `POST /mcp` AND LangServe `POST /{tool}/invoke`)
```python
app = server.create_fastapi_app()
# Or mount into an existing FastAPI app:
# app.include_router(server.create_fastapi_router())
```
Run from terminal:
```bash
uvicorn mcp_server:app --host 0.0.0.0 --port 8000
```

#### Option C: Official MCP SSE Server
```python
if __name__ == "__main__":
    server.run(transport="sse", host="0.0.0.0", port=8000)
```

#### Option D: Official MCP `stdio` Server (for Claude Desktop, Cursor, or `stdio` clients)
```python
if __name__ == "__main__":
    server.run(transport="stdio")
```

---

## 4. Step 2: Connect Your AI Agent (`agent_client.py`)

Now your LangChain `AgentExecutor` connects to the remote server. Notice that **the client contains zero compensation code**—when the LLM dynamically selects tools and a later tool fails, `AgentRewind` automatically sends the server-minted `undo_token`s back in strict LIFO order:

```python
import os
from langchain_openai import ChatOpenAI
from langchain.agents import AgentExecutor, create_tool_calling_agent
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.runnables import RemoteRunnable
from agent_rewind import AgentRewind, rewind_tools

os.environ["OPENAI_API_KEY"] = "your-api-key"

# 1. Connect to remote endpoints and wrap with `rewind_tools()`
tools = rewind_tools([
    RemoteRunnable("http://localhost:8000/create_order"),
    RemoteRunnable("http://localhost:8000/reserve_stock"),
    RemoteRunnable("http://localhost:8000/send_email"),
])

# 2. Standard LLM & Prompt Definition
llm = ChatOpenAI(model="gpt-4o", temperature=0)
prompt = ChatPromptTemplate.from_messages([
    ("system", "You are a helpful assistant. Use the remote tools when needed."),
    MessagesPlaceholder("chat_history", optional=True),
    ("human", "{input}"),
    MessagesPlaceholder("agent_scratchpad"),
])

# 3. Create AgentExecutor & Run Inside `with AgentRewind():`
agent = create_tool_calling_agent(llm, tools, prompt)
agent_executor = AgentExecutor(agent=agent, tools=tools, verbose=True)

if __name__ == "__main__":
    with AgentRewind() as tx:
        response = agent_executor.invoke({
            "input": "Create an order for 1x SKU-MACBOOK, reserve stock, and send confirmation."
        })
        print(response["output"])
```

---

## 5. Next Steps

- **[MCP & Remote Server Guide (`RewindServer` + `RemoteRunnable`)](mcp-and-remote-server-guide.md):** Complete architectural guide for remote MCP servers and LangChain clients.
- **[Framework Integrations](framework-integrations.md):** Connect `AgentRewind` to LangGraph Multi-Agent `StateGraph`, Google A2A (`RewindA2AExecutor`), and HITL webhooks.
- **[Storage & Transports](storage-and-transports.md):** Configure zero-DB journals (`InMemoryBackend`, `JSONFileBackend`, `CallbackBackend`) and custom HTTP transports.
- **[Scenarios Catalog](scenarios-catalog.md):** Explore end-to-end patterns for distributed multi-server sagas, nested savepoints, financial circuit breakers, and DLQ recovery.

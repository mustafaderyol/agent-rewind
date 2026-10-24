# Framework & Ecosystem Integrations

`agent-rewind` is completely **framework-agnostic**. Because it operates at the Python async function and MCP JSON-RPC boundary, you can drop it into any LLM SDK or agent framework in minutes.

---

## 1. Standing Up a Remote Transactional MCP Server (`RewindServer`)

`RewindServer` directly subclasses `mcp.server.mcpserver.MCPServer` from the official `mcp` SDK. Define `@server.tool(compensate=..., pre_state=..., deferred=...)` privately on your server and stand it up via Streamable HTTP, FastAPI/Uvicorn, SSE, or `stdio`:

```python
from agent_rewind import RewindServer

server = RewindServer(name="crm-mcp-server", version="0.2.0", default_lease_ttl=60.0)
TICKETS_DB: dict[str, dict] = {}


@server.tool(
    name="create_jira_ticket",
    description="Create a Jira ticket in the remote issue tracker.",
    compensate=lambda args, res: TICKETS_DB.pop(res["ticket_id"], None),
)
async def create_jira_ticket(summary: str) -> dict:
    ticket_id = f"JIRA-{len(TICKETS_DB) + 901}"
    TICKETS_DB[ticket_id] = {"ticket_id": ticket_id, "summary": summary}
    return TICKETS_DB[ticket_id]


# Launch Option A: Official MCP Streamable HTTP Server
if __name__ == "__main__":
    server.run(transport="streamable-http", host="0.0.0.0", port=8000)

# Launch Option B: FastAPI + Uvicorn (Serves MCP `/mcp` + LangServe `/{tool}/invoke`)
# app = server.create_fastapi_app()
# Terminal: uvicorn server:app --host 0.0.0.0 --port 8000
```

---

## 2. LangChain `AgentExecutor` & `RemoteRunnable` Integration

> **Full Server + Client Guide:** See [**MCP & Remote Server Guide (`RewindServer` + `RemoteRunnable`)**](./mcp-and-remote-server-guide.md) for the complete architecture of how remote servers define their own `@server.tool(compensate=...)` hooks and issue `undo_token` leases.

### Mode A: Server-Defined Compensation (Zero Client Compensation Code)
When your remote server uses `RewindServer`, the client only wraps `RemoteRunnable` objects with `rewind_tools([...])` and runs `agent_executor.invoke(...)` inside `with AgentRewind():`:

```python
from langchain.agents import AgentExecutor, create_tool_calling_agent
from langchain_core.runnables import RemoteRunnable
from agent_rewind import AgentRewind, rewind_tools

# 1. Connect to remote endpoints (compensations are defined on the remote server!)
remote_tool_1 = RemoteRunnable("https://your-remote-service.com/create_order")
remote_tool_2 = RemoteRunnable("https://your-remote-service.com/reserve_stock")
remote_tool_3 = RemoteRunnable("https://your-remote-service.com/charge_card")

# 2. Wrap with rewind_tools() — automatically intercepts `_rewind.undo_token`!
tools = rewind_tools([remote_tool_1, remote_tool_2, remote_tool_3])

agent = create_tool_calling_agent(llm, tools, prompt)
agent_executor = AgentExecutor(agent=agent, tools=tools, verbose=True)

# 3. Execute inside `with AgentRewind() as tx:`
# Even if AgentExecutor catches a tool exception internally, AgentRewind detects
# the unrecovered failure and rolls back all prior remote tools in LIFO order!
with AgentRewind() as tx:
    response = agent_executor.invoke({"input": "Execute the order workflow using remote tools."})
```

### Mode B: Client-Defined Compensation (For 3rd-Party Endpoints)
If you do not control the remote server, you can pair forward and compensating `RemoteRunnable` endpoints on the client with `rewind_runnable(...)`:

```python
from langchain.agents import AgentExecutor, create_tool_calling_agent
from langchain_core.runnables import RemoteRunnable
from agent_rewind import AgentRewind, rewind_runnable

crm_tool = rewind_runnable(
    RemoteRunnable("https://api.company.com/crm/create_return"),
    name="crm_create_return",
    description="Open a customer return ticket",
    compensate=RemoteRunnable("https://api.company.com/crm/rollback_return"),
)

payment_tool = rewind_runnable(
    RemoteRunnable("https://api.company.com/payment/refund"),
    name="payment_refund",
    description="Refund money to customer IBAN",
    requires_prior=["crm_create_return"],
    max_value={"amount": 50000.0},
    compensate=RemoteRunnable("https://api.company.com/payment/reverse_refund"),
)

email_tool = rewind_runnable(
    RemoteRunnable("https://api.company.com/notify/send_email"),
    name="notify_send_email",
    description="Send email notification",
    deferred=True,  # Buffered in Outbox until AgentExecutor finishes cleanly!
)

tools = [crm_tool, payment_tool, email_tool]
agent = create_tool_calling_agent(llm, tools, prompt)
agent_executor = AgentExecutor(agent=agent, tools=tools, verbose=True)

with AgentRewind() as tx:
    response = agent_executor.invoke({"input": "Process refund for ORD-101"})
```

---

## 3. LangGraph Multi-Agent Supervisor (`StateGraph` + `RemoteRunnable` + Google A2A)

In a multi-agent LangGraph `StateGraph` where a `supervisor` routes tasks across multiple remote sub-agents (`remote_1`, `remote_2`, `remote_3`), `agent-rewind` provides non-intrusive wrappers:
- **`rewind_agent(remote_runnable, name=...)`**: Wraps `RemoteRunnable("https://remote-service-1.com/agent")` or a Google A2A endpoint (`protocol="a2a"`). Automatically extracts `_meta.rewind.undo_token`, `AIMessage.response_metadata["rewind"]["undo_token"]`, or A2A `Task.metadata.rewind.undo_token`.
- **`rewind_graph(workflow.compile())`**: Wraps the compiled `StateGraph` so that `.invoke(initial_state)` or `await .ainvoke(initial_state)` executes atomically inside `AgentRewind`.
- **`RewindSubAgentHost`**: Wraps the remote sub-agent on its host server so that even if a sub-agent executes 5 internal `@rewind_tool` calls, all 5 are bundled into a single composite `undo_token` (`tx.export_undo_lease()`).

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

# 1. Wrap Remote Sub-Agents with `rewind_agent()`
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
workflow.add_node("local_worker", local_worker_node)
workflow.add_node("remote_1", remote_node_1)
workflow.add_node("remote_2", remote_node_2)
workflow.add_node("remote_3", remote_node_3)

workflow.add_edge(START, "supervisor")
workflow.add_conditional_edges(
    "supervisor",
    lambda x: x["next_step"],
    {
        "local_worker": "local_worker",
        "remote_1": "remote_1",
        "remote_2": "remote_2",
        "remote_3": "remote_3",
        "FINISH": END,
    },
)
workflow.add_edge("local_worker", END)
workflow.add_edge("remote_1", END)
workflow.add_edge("remote_2", END)
workflow.add_edge("remote_3", END)

# 2. Wrap `workflow.compile()` with `rewind_graph()`
multi_agent_system = rewind_graph(workflow.compile())

if __name__ == "__main__":
    initial_state = {
        "messages": [HumanMessage(content="Execute complex distributed task.")],
        "sender": "user",
        "next_step": "",
    }
    result = multi_agent_system.invoke(initial_state)
```

### How Remote Sub-Agents Bundle Their Internal Tools (`RewindSubAgentHost`)

On the remote sub-agent server (`https://remote-service-1.com/agent`), wrap your sub-agent graph or function with `RewindSubAgentHost`:

```python
from agent_rewind import RewindSubAgentHost, rewind_tool

@rewind_tool(compensate=undo_db_insert)
def internal_tool_1(cid: str) -> dict: ...

@rewind_tool(compensate=undo_ticket)
def internal_tool_2(cid: str) -> dict: ...

def my_remote_subagent(payload: dict) -> dict:
    internal_tool_1("CUST-1")
    internal_tool_2("CUST-1")
    return {"output": "Sub-agent 1 completed both steps"}

# Bundles internal_tool_2 and internal_tool_1 into a single composite `undo_token`!
# Also natively handles Google A2A `tasks/send` and `tasks/cancel`!
remote_agent_host = RewindSubAgentHost(my_remote_subagent, name="remote_1")
```

---

## 3. FastMCP Server Integration

If you are building an MCP server using `FastMCP`, use `ServerLeaseManager` on the server side to provide a **Dead-Man's Switch** (automatic TTL expiration if an agent crashes mid-transaction):

```python
from agent_rewind import ServerLeaseManager

leases = ServerLeaseManager()


async def lock_seat_on_server(seat_id: str) -> dict:
    # Reserve the seat with a 30-second TTL lease
    async def release_seat_in_db(seat_id: str) -> None:
        print(f"Auto-releasing expired seat lock: {seat_id}")

    lease_id = await leases.acquire(
        resource_key=f"seat:{seat_id}",
        ttl_seconds=30.0,
        rollback_fn=release_seat_in_db,
        rollback_kwargs={"seat_id": seat_id},
    )
    return {"seat_id": seat_id, "lease_id": lease_id}
```

---

## 4. Zero-Config Remote MCP Server Discovery

When your agent only has the URLs of external MCP servers (no local Python functions), use `RemoteMCPGateway.discover()`:

```python
from agent_rewind import AgentRewind, RemoteMCPGateway


async def run_remote_mcp_saga() -> None:
    gateway = await RemoteMCPGateway.discover([
        "http://localhost:18001/mcp",  # CRM Server
        "http://localhost:18002/mcp",  # ERP Inventory Server
        "http://localhost:18003/mcp",  # Payment Gateway Server
        "http://localhost:18004/mcp",  # Notification Server
    ])

    async with AgentRewind() as tx:
        await gateway.call(tx, "reserve_stock", sku="GPU-H100", qty=2)
        await gateway.call(tx, "send_email", to="ops@company.com", subject="GPU Order")
        await gateway.call(tx, "charge_card", amount=60000.0)
```

---

## 5. FastAPI / Webhook Human-in-the-Loop (HITL) Approval

Connect `AgentRewind(approval_handler=...)` to Slack, Microsoft Teams, or an internal admin API for high-risk tool calls:

```python
from agent_rewind import AgentRewind, rewind_tool


async def slack_approval_webhook(tool_name: str, kwargs: dict) -> bool:
    # Send interactive approval request to Slack/Teams and await response
    print(f"[APPROVAL GATE] Requesting human sign-off for {tool_name}({kwargs})")
    return True


@rewind_tool(
    compensate=reverse_wire_transfer,
    max_value={"amount": 25000.0},
    require_approval=True,
)
async def execute_wire_transfer(iban: str, amount: float) -> dict:
    return {"iban": iban, "amount": amount, "status": "SENT"}


async def run_treasury_agent() -> None:
    async with AgentRewind(approval_handler=slack_approval_webhook) as tx:
        await tx.run(execute_wire_transfer, iban="DE89370400440532013000", amount=12500.0)
```

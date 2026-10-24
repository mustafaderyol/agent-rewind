# 2. How to Build a Transactional AI Agent (`AgentExecutor` + `RemoteRunnable` / MCP)

> **Full Guide Reference:** [`docs/mcp-and-remote-server-guide.md`](../docs/mcp-and-remote-server-guide.md) | **Runnable Script:** [`examples/09_langchain_remote_runnable_agent.py`](../examples/09_langchain_remote_runnable_agent.py)

When an LLM dynamically decides which remote tools to call at runtime inside `AgentExecutor`, `agent-rewind` ensures that **if any tool fails at any step, every previously executed remote tool is rolled back in strict LIFO order**—even if `AgentExecutor` catches the exception internally.

---

## Pattern 1: `AgentExecutor` + `RemoteRunnable` (Zero Client Compensation Code)

When your remote server is built with [`RewindServer`](01-How-to-Build-an-MCP-Server.md), compensation logic lives strictly on the remote server. On the client, you simply wrap your `RemoteRunnable` list with **`rewind_tools([...])`** and run `agent_executor.invoke(...)` inside **`with AgentRewind() as tx:`**:

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

# 2. Wrap with `rewind_tools()` — ZERO compensation code needed on the client!
#    `rewind_tools` automatically intercepts `_meta.rewind.undo_token` from the remote server.
tools = rewind_tools([
    remote_order_tool,
    remote_stock_tool,
    remote_payment_tool,
])

# 3. Standard LLM & Prompt Definition
llm = ChatOpenAI(model="gpt-4o", temperature=0)

prompt = ChatPromptTemplate.from_messages([
    ("system", "You are a helpful assistant. Use the remote tools when needed."),
    MessagesPlaceholder("chat_history", optional=True),
    ("human", "{input}"),
    MessagesPlaceholder("agent_scratchpad"),
])

# 4. Standard Agent & AgentExecutor Creation
agent = create_tool_calling_agent(llm, tools, prompt)
agent_executor = AgentExecutor(agent=agent, tools=tools, verbose=True)

if __name__ == "__main__":
    # 5. Execute inside `with AgentRewind() as tx:`
    #    Whatever tool sequence the LLM chooses, if a later step fails,
    #    AgentRewind automatically rolls back all prior remote tools in LIFO order!
    with AgentRewind() as tx:
        response = agent_executor.invoke({
            "input": "Create an order for 2x SKU-100, reserve the stock, and charge the card."
        })
        print(response["output"])

    print(f"Final Transaction State: {tx.state.value}")
```

---

## Pattern 2: `AgentExecutor` + Remote MCP Servers (`RemoteMCPGateway.discover`)

When connecting directly to MCP JSON-RPC 2.0 servers (`POST /mcp`), use `RemoteMCPGateway.discover(urls)` and export the discovered tools via `mcp.as_langchain_tools()`:

```python
from langchain_openai import ChatOpenAI
from langchain.agents import AgentExecutor, create_tool_calling_agent
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from agent_rewind import AgentRewind, RemoteMCPGateway

async def main():
    # 1. Discover tools over MCP JSON-RPC 2.0 (`tools/list`)
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

---

## Pattern 3: Client-Paired Compensation for 3rd-Party Endpoints (`rewind_runnable`)

If you are calling a 3rd-party remote endpoint that does not run `RewindServer`, you can explicitly pair forward and compensating `RemoteRunnable` endpoints on the client using `rewind_runnable(...)`:

```python
from langchain_core.runnables import RemoteRunnable
from agent_rewind import rewind_runnable

crm_tool = rewind_runnable(
    RemoteRunnable("https://api.thirdparty.com/crm/create_return"),
    name="crm_create_return",
    description="Open a customer return ticket",
    compensate=RemoteRunnable("https://api.thirdparty.com/crm/rollback_return"),
)

email_tool = rewind_runnable(
    RemoteRunnable("https://api.thirdparty.com/notify/send_email"),
    name="notify_send_email",
    description="Send email notification",
    deferred=True,  # Buffered in Outbox until AgentExecutor finishes cleanly!
)
```

---

## Next Steps
- **[1. How to Build an MCP Server (`RewindServer`)](01-How-to-Build-an-MCP-Server.md)**
- **[3. How to Build a Multi-Agent Graph (`StateGraph` + Google A2A)](03-How-to-Build-a-Multi-Agent-Graph.md)**

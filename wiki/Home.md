# Welcome to the `agent-rewind` Wiki

**`agent-rewind`** is a zero-trust transactional runtime for **Model Context Protocol (MCP) Servers**, **LangChain Agents (`AgentExecutor` / `RemoteRunnable`)**, **LangGraph Multi-Agent Workflows (`StateGraph`)**, and **Google A2A Sub-Agents**.

---

## 🎯 The 3 Core Guides (Start Here)

| Guide | What You Will Build | Official SDK Extended |
| :--- | :--- | :--- |
| **[1. How to Build a Transactional MCP Server (`RewindServer`)](01-How-to-Build-an-MCP-Server.md)** | Define `@server.tool(compensate=..., pre_state=..., deferred=...)` privately on your remote server and stand it up via Streamable HTTP, FastAPI/Uvicorn, SSE, or `stdio`. | `mcp.server.mcpserver.MCPServer` ([`modelcontextprotocol/python-sdk`](https://github.com/modelcontextprotocol/python-sdk)) |
| **[2. How to Build a Transactional AI Agent (`AgentExecutor`)](02-How-to-Build-an-Agent.md)** | Connect LangChain `AgentExecutor` + `RemoteRunnable` (`rewind_tools`) or `RemoteMCPGateway.discover()` so whatever tools the LLM selects are rolled back in LIFO order on failure. | `langchain_core.runnables.Runnable` & `langchain.agents.AgentExecutor` ([`langchain-ai/langchain`](https://github.com/langchain-ai/langchain)) |
| **[3. How to Build a Transactional Multi-Agent Graph (`StateGraph` + A2A)](03-How-to-Build-a-Multi-Agent-Graph.md)** | Orchestrate multiple remote sub-agents (`rewind_agent`, `rewind_graph`, `RewindSubAgentHost`, `RewindA2AExecutor`) in a LangGraph `StateGraph` with hierarchical LIFO rollback. | `langgraph.graph.StateGraph` ([`langchain-ai/langgraph`](https://github.com/langchain-ai/langgraph)) & `a2a.server.agent_execution.AgentExecutor` ([`a2aproject/a2a`](https://github.com/a2aproject/a2a)) |

---

## The Architectural Problem We Solve

Suppose an AI agent or multi-agent system executes a workflow across 4 independent remote services:
1. **Service 1 (CRM MCP Server):** Opens a return ticket or customer order (`ORD-101`).
2. **Service 2 (ERP MCP Server):** Reserves or adjusts warehouse inventory (`100 -> 98`).
3. **Service 3 (Notification MCP Server):** Triggers an external customer confirmation email.
4. **Service 4 (Payment MCP Server):** Processes a bank wire transfer or card charge.

**What happens when the agent crashes at Step 4 (`503 Timeout`) or gets manipulated via Indirect Prompt Injection?**
- Standard agents leave Services 1 and 2 in a corrupted state, send premature emails that cannot be unsent, or transfer money to an attacker's IBAN.
- Asking a compromised or hallucinating LLM to "undo your previous steps" is a critical security flaw—an LLM cannot be trusted to execute rollbacks.

**`agent-rewind` solves this deterministically over standard protocols (`MCP` & `A2A`):**
- **Server-Defined Compensation (`RewindServer`):** Each remote MCP server keeps its `pre_state` and `compensate` closures private and issues an opaque `undo_token` inside `CallToolResult._meta["rewind"]`.
- **Outbox Commit Buffer (`deferred=True`):** Irreversible actions (emails, webhooks, Slack notifications) are buffered in memory and only flushed if all steps succeed.
- **Deterministic LIFO Rollback:** When any step fails, `AgentRewind` automatically returns the `undo_token`s to the remote servers (or sends `tasks/cancel` to Google A2A sub-agents) in strict reverse order (`Step N -> ... -> Step 1`).

---

## 📚 Full Wiki Index

1. **[1. How to Build a Transactional MCP Server (`RewindServer`)](01-How-to-Build-an-MCP-Server.md)**
2. **[2. How to Build a Transactional AI Agent (`AgentExecutor` + `RemoteRunnable` / MCP)](02-How-to-Build-an-Agent.md)**
3. **[3. How to Build a Transactional Multi-Agent Graph (`StateGraph` + Google A2A)](03-How-to-Build-a-Multi-Agent-Graph.md)**
4. **[Remote MCP Auto-Discovery (`RemoteMCPGateway`)](Remote-MCP-Auto-Discovery.md)**
5. **[Architecture Deep Dive](Architecture-Deep-Dive.md)**
6. **[Security & PolicyGuard (CaMeL Data-Flow Isolation)](Security-and-Policy-Guard.md)**
7. **[Benchmarks (1,000 Distributed Workflows)](Benchmarks.md)**

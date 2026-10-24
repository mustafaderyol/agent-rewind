# Remote MCP Auto-Discovery & Distributed Execution

When your agent connects to tools hosted on remote Model Context Protocol (MCP) servers across different machines or languages, you cannot pass local Python `lambda` functions over the wire.

`agent-rewind` solves distributed MCP transactions via **`RemoteMCPGateway`** and **`ServerLeaseManager`**.

---

## 1. Zero-Config URL-Only Discovery (`RemoteMCPGateway.discover`)

Even if you only have the URLs of standard MCP servers (which expose plain `name`, `description`, and `inputSchema` without custom annotations), `AutoDiscoveryEngine` automatically synthesizes the transactional contract:

```python
from agent_rewind import AgentRewind, RemoteMCPGateway

mcp = await RemoteMCPGateway.discover([
    "http://crm-service:18001",
    "http://erp-service:18002",
    "http://payment-service:18003",
    "http://notify-service:18004",
], save_manifest="mcp-tx-manifest.json")

# Only forward and read-only tools are exposed to the LLM (compensators are hidden!)
llm_tools = mcp.get_llm_visible_tools()

async with AgentRewind() as tx:
    ret = await mcp.call("crm_create_return", order_id="ORD-101")
    await mcp.call("erp_adjust_stock", sku=ret["sku"], delta=ret["qty"])
    await mcp.call("notify_send_email", to=ret["customer_email"], subject="Update", body="Started")
    await mcp.call("payment_process_refund", order_id="ORD-101", iban=ret["registered_iban"], amount=ret["amount"])
```

### How `AutoDiscoveryEngine` Works
1. **Verb-Entity Decomposition:** Parses tool names (`crm_create_return` -> verb `create`, entity `return`; `crm_rollback_return` -> verb `rollback`, entity `return`).
2. **Effect Classification:**
   - `get_*`, `fetch_*`, `list_*` -> `READ_ONLY` (and paired as `pre_state_tool` for mutations on the same entity).
   - `send_*`, `notify_*`, `publish_*`, `webhook_*` -> `DEFERRED` (Outbox buffered).
   - `rollback_*`, `cancel_*`, `reverse_*`, `delete_*`, `set_*` -> Marked `is_compensator_only=True` (hidden from LLM and bound as `compensate_tool`).
3. **Smart Runtime Argument Resolver:** Automatically maps fields from `pre_state`, `forward_result`, and `forward_args` (including exact matches and semantic token suffix matches such as `previous_order_status` -> `previous_status`) to satisfy the remote compensator's `inputSchema`.
4. **Sensitive Parameter Protection:** Automatically adds financial and security fields (`iban`, `account`, `wallet`, `role`, `password`, `secret`) to `deny_tainted`.

---

## 2. Pluggable Transports (Zero `httpx` Dependency Required)

`RemoteMCPGateway` uses `StdlibHTTPTransport` (`urllib.request`) by default so no third-party HTTP library is required. You can also pass an `httpx.AsyncClient` or any custom async callable:

```python
mcp = await RemoteMCPGateway.discover(
    ["mcp://internal-bus"],
    transport=lambda method, url, body, headers: my_custom_rpc(method, url, body, headers),
)
```

---

## 3. Server-Side Lease TTL ("Dead-Man's Switch")

What if the agent machine loses power or network connectivity after mutating Remote Server 1 and Remote Server 2, so it can never send a rollback command?

Remote MCP servers can use `ServerLeaseManager` to register a TTL lease on every mutation:
```python
from agent_rewind import ServerLeaseManager

leases = ServerLeaseManager()

# Register mutation with a 30-second lease TTL
undo_token = leases.register_lease(
    undo_fn=lambda: db.restore_stock(sku, previous_qty),
    ttl_seconds=30.0,
)

# Periodic background sweep on the server automatically rolls back expired uncommitted leases:
expired_tokens = await leases.sweep_expired_leases()
```

---

## 4. Server-Defined Compensation (`RewindServer` & Opaque `undo_token` Protocol)

When you control the remote MCP or FastAPI server, you shouldn't have to expose internal rollback endpoints (`crm_rollback_return`) to the public or force the client agent to understand how to undo database mutations.

With `RewindServer`, the remote server developer defines `@server.tool(compensate=...)` **privately inside the remote server**:

```python
from agent_rewind import RewindServer

crm_server = RewindServer("crm-service", default_lease_ttl=60.0)

@crm_server.tool(
    pre_state=lambda args: db.get_status(args["order_id"]),
    compensate=lambda args, res, prev_status: db.restore_order(args["order_id"], prev_status),
)
async def crm_open_return(order_id: str) -> dict:
    return await db.create_return(order_id)

# Mount onto FastAPI in one line:
# app.include_router(crm_server.create_fastapi_router())
```

### How It Works Across Multiple Servers:
1. When the Client calls `crm_open_return`, `crm_server` captures `pre_state` locally, runs `crm_open_return`, registers the bound compensation closure in its `ServerLeaseManager`, and returns an opaque **`undo_token`** (`_rewind: {"undo_token": "utok_..."}`).
2. `RemoteMCPGateway` on the Client automatically strips `_rewind` before returning the result to the LLM and pushes `POST /_rewind/rollback {"undo_token": "utok_..."}` onto the Client's LIFO stack.
3. If Step 3 on **another** server fails, the Client sends `undo_token` back to `crm_server`, which executes its private `compensate` closure!

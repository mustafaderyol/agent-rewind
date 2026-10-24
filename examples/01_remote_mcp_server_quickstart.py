"""Example 01: Standing Up a Remote Transactional MCP Server (`RewindServer`) & Agent Client.

Demonstrates the core `agent-rewind` workflow built on the official `mcp` Python SDK:
1. Stand up a Remote MCP Server (`RewindServer`, subclass of `mcp.server.mcpserver.MCPServer`)
   where `@server.tool(compensate=..., pre_state=..., deferred=...)` lives privately on the server.
2. Launch it over Streamable HTTP (`server.run(transport="streamable-http", host="0.0.0.0", port=8000)`),
   FastAPI (`app = server.create_fastapi_app()`), SSE, or `stdio`.
3. Connect an AI Agent (`RemoteMCPGateway` / LangChain `AgentExecutor`) that invokes tools
   dynamically and automatically rolls back the remote server via opaque `undo_token` leases
   when a downstream step fails.
"""

from __future__ import annotations

import asyncio
from typing import Any

from agent_rewind import (
    AgentRewind,
    RemoteMCPGateway,
    RewindServer,
    render_transaction_report,
)

# =============================================================================
# PART 1: REMOTE MCP SERVER (`mcp_server.py`)
# Subclasses `mcp.server.mcpserver.MCPServer` from the official `mcp` SDK.
# =============================================================================

server = RewindServer(name="order-fulfillment-mcp", version="0.2.0", default_lease_ttl=60.0)

# Simulated remote databases owned strictly by the MCP server
REMOTE_DB: dict[str, Any] = {
    "orders": {},
    "stock": {"SKU-MACBOOK": 100},
    "emails_sent": [],
}


@server.tool(
    name="create_order",
    description="Create a customer order in the remote CRM database.",
    compensate=lambda args, res: REMOTE_DB["orders"].pop(res["order_id"], None),
)
async def create_order(customer: str, sku: str, qty: int) -> dict[str, Any]:
    order_id = f"ORD-{len(REMOTE_DB['orders']) + 101}"
    REMOTE_DB["orders"][order_id] = {"customer": customer, "sku": sku, "qty": qty}
    return {"order_id": order_id, "sku": sku, "qty": qty, "amount": 45000.0}


@server.tool(
    name="reserve_inventory",
    description="Reserve warehouse inventory in the remote ERP database.",
    pre_state=lambda args: REMOTE_DB["stock"][args["sku"]],
    compensate=lambda args, res, prev_qty: REMOTE_DB["stock"].update({args["sku"]: prev_qty}),
)
async def reserve_inventory(sku: str, qty: int) -> dict[str, Any]:
    REMOTE_DB["stock"][sku] -= qty
    return {"sku": sku, "remaining": REMOTE_DB["stock"][sku]}


@server.tool(
    name="send_confirmation_email",
    description="Send an irreversible order confirmation email (Buffered in Outbox until commit).",
    deferred=True,
)
async def send_confirmation_email(to: str, subject: str) -> dict[str, Any]:
    REMOTE_DB["emails_sent"].append({"to": to, "subject": subject})
    return {"sent": True}


@server.tool(
    name="charge_corporate_card",
    description="Charge the customer's corporate card via the payment gateway.",
)
async def charge_corporate_card(order_id: str, amount: float) -> dict[str, Any]:
    raise ConnectionError("503 Service Unavailable: Upstream Payment Gateway Timeout")


# -----------------------------------------------------------------------------
# How to stand up this server as a live network process in production:
#   Option A (Official MCP Streamable HTTP):
#       server.run(transport="streamable-http", host="0.0.0.0", port=8000)
#   Option B (FastAPI / Uvicorn with MCP `/mcp` + LangServe `/{tool}/invoke`):
#       app = server.create_fastapi_app()
#       # Run with: uvicorn mcp_server:app --host 0.0.0.0 --port 8000
#   Option C (Official MCP Stdio for Claude Desktop / Cursor / Stdio Clients):
#       server.run(transport="stdio")
# -----------------------------------------------------------------------------


# =============================================================================
# PART 2: AI AGENT CLIENT (`agent_client.py`)
# Connects to the Remote MCP Server URL — zero compensation logic on the client!
# =============================================================================
async def main() -> None:
    # Discover tools from the remote MCP server over MCP JSON-RPC 2.0 (`tools/list`)
    mcp = await RemoteMCPGateway.discover(
        ["http://order-fulfillment.mcp.internal:8000"],
        transport=server.asgi_transport,
    )

    # Execute agent tool calls inside `async with AgentRewind():`
    async with AgentRewind(tx_id="tx-remote-mcp-01", suppress_errors=True) as tx:
        order = await mcp.call("create_order", customer="alice@example.com", sku="SKU-MACBOOK", qty=2)
        await mcp.call("reserve_inventory", sku=order["sku"], qty=order["qty"])
        await mcp.call("send_confirmation_email", to="alice@example.com", subject="Order Confirmed")
        # Fails at Step 3 -> Automatically sends `undo_token`s to the remote MCP server in LIFO order!
        await mcp.call("charge_corporate_card", order_id=order["order_id"], amount=order["amount"])

    render_transaction_report(
        tx.report,
        title="Example 01: Remote MCP Server (`RewindServer`) Automatic Token Rollback & Outbox Discard",
    )
    print("Remote Server DB State After Automatic Rollback:", REMOTE_DB)


if __name__ == "__main__":
    asyncio.run(main())

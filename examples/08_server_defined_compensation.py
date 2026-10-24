"""Example 08: Server-Defined Compensation (`RewindServer` + Opaque `undo_token` Protocol).

Solves the Distributed Ownership Problem:
What happens when Team A (CRM Server) and Team B (ERP Server) define their own
`@server.tool(compensate=...)` inside their remote microservices, and Team C (the AI Agent)
only has the server URLs?

1. Each remote `RewindServer` keeps its `pre_state` and `compensate` logic 100% private
   on the server and returns an opaque `undo_token` (`utok_...`) with each mutation.
2. Notice that `/mcp/tools` ONLY exposes forward tools (`crm_open_return`, `erp_add_stock`) —
   no public rollback endpoints or internal DB IDs need to be understood by the Agent!
3. When Step 3 fails on the client, `AgentRewind` automatically sends the `undo_token`s
   back to `erp-server` and `crm-server` in LIFO order (`/_rewind/rollback`).
"""

from __future__ import annotations

import asyncio
from typing import Any

from rich.console import Console
from rich.panel import Panel

from agent_rewind import (
    AgentRewind,
    RemoteMCPGateway,
    RewindServer,
    render_transaction_report,
)

console = Console()

# =============================================================================
# 1. REMOTE SERVER A: CRM Microservice (Owns its own rollback logic privately)
# =============================================================================
CRM_DB: dict[str, Any] = {"orders": {"ORD-500": "DELIVERED"}, "returns": []}
crm_server = RewindServer("crm-service", default_lease_ttl=60.0)


@crm_server.tool(
    pre_state=lambda args: CRM_DB["orders"][args["order_id"]],
    compensate=lambda args, res, prev_status: (
        CRM_DB["returns"].remove(res["return_id"]),
        CRM_DB["orders"].update({args["order_id"]: prev_status}),
    ),
)
async def crm_open_return(order_id: str) -> dict[str, str]:
    """Open a return ticket on the CRM server."""
    ret_id = f"RET-{order_id}"
    CRM_DB["orders"][order_id] = "RETURN_IN_PROGRESS"
    CRM_DB["returns"].append(ret_id)
    return {"return_id": ret_id, "order_id": order_id}


# =============================================================================
# 2. REMOTE SERVER B: ERP Microservice (Owns its own pre_state & rollback)
# =============================================================================
ERP_DB: dict[str, int] = {"SKU-H100": 20}
erp_server = RewindServer("erp-service", default_lease_ttl=60.0)


@erp_server.tool(
    pre_state=lambda args: ERP_DB[args["sku"]],
    compensate=lambda args, res, pre_qty: ERP_DB.update({args["sku"]: pre_qty}),
)
async def erp_reserve_gpu(sku: str, qty: int) -> dict[str, Any]:
    """Reserve GPU inventory in the ERP warehouse."""
    ERP_DB[sku] -= qty
    return {"sku": sku, "remaining": ERP_DB[sku]}


# =============================================================================
# 3. CLIENT AGENT: Connects ONLY via URLs — Zero Compensation Code on Client!
# =============================================================================
async def main() -> None:
    async def network_router(
        method: str,
        url: str,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        if "crm.internal" in url:
            return await crm_server.asgi_transport(method, url, json_body, headers)
        return await erp_server.asgi_transport(method, url, json_body, headers)

    mcp = await RemoteMCPGateway.discover(
        ["http://crm.internal", "http://erp.internal"],
        transport=network_router,
    )

    console.print(
        Panel(
            f"[bold cyan]Discovered Tools on Client:[/bold cyan] {list(mcp.tools.keys())}\n"
            "[dim]Notice: Zero rollback tools are exposed; compensation lives inside each remote RewindServer![/dim]",
            border_style="cyan",
        )
    )

    async with AgentRewind(tx_id="tx_server_owned_undo", suppress_errors=True) as tx:
        await mcp.call("crm_open_return", order_id="ORD-500")
        await mcp.call("erp_reserve_gpu", sku="SKU-H100", qty=4)
        console.print(
            f"  [yellow]Mid-Transaction Server States:[/yellow] CRM={CRM_DB} | ERP={ERP_DB}"
        )
        # Simulate failure at Step 3
        raise RuntimeError("Downstream Payment Gateway 503 Service Unavailable")

    render_transaction_report(
        tx.report,
        console=console,
        title="Example 08: Server-Defined Compensation Rolled Back via Opaque `undo_token`",
    )
    console.print(
        f"[bold green]✓ Restored Server States After Token Rollback:[/bold green] CRM={CRM_DB} | ERP={ERP_DB}"
    )


if __name__ == "__main__":
    asyncio.run(main())

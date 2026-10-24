"""Example 05: Nested Savepoints & Partial Rollback Across Remote MCP Servers.

Demonstrates how `async with tx.savepoint("name"):` allows an AI agent connected
to a Remote MCP Server (`RewindServer`) to recover from a failing sub-branch
(e.g., Express Air Courier API outage) by rolling back ONLY the remote steps inside
the savepoint via `undo_token` while preserving the outer warehouse lock.
"""

from __future__ import annotations

import asyncio
from typing import Any

from rich.console import Console

from agent_rewind import (
    AgentRewind,
    RemoteMCPGateway,
    RewindServer,
    StepExecutionError,
    render_transaction_report,
)

console = Console()

STATE: dict[str, Any] = {
    "warehouse_locked_sku": None,
    "express_label": None,
    "ground_label": None,
    "confirmation_sent": False,
}

logistics_server = RewindServer("logistics-mcp-server")


@logistics_server.tool(
    compensate=lambda args, res: STATE.update(warehouse_locked_sku=None),
)
async def lock_warehouse_inventory(sku: str) -> dict[str, Any]:
    STATE["warehouse_locked_sku"] = sku
    return {"locked": sku}


@logistics_server.tool(
    compensate=lambda args, res: STATE.update(express_label=None),
)
async def allocate_express_air_slot(order_id: str) -> dict[str, Any]:
    label_id = f"AIR-{order_id}"
    STATE["express_label"] = label_id
    return {"label_id": label_id}


@logistics_server.tool
async def confirm_express_customs_clearance(order_id: str) -> dict[str, Any]:
    raise RuntimeError("504 Gateway Timeout: Express Air Customs API unreachable")


@logistics_server.tool(
    compensate=lambda args, res: STATE.update(ground_label=None),
)
async def book_standard_ground_shipping(order_id: str) -> dict[str, Any]:
    label_id = f"GND-{order_id}"
    STATE["ground_label"] = label_id
    return {"label_id": label_id}


@logistics_server.tool(deferred=True)
async def send_shipping_notification(order_id: str, carrier: str) -> dict[str, Any]:
    STATE["confirmation_sent"] = True
    return {"order_id": order_id, "carrier": carrier}


async def main() -> None:
    mcp = await RemoteMCPGateway.discover(
        ["http://logistics.mcp.internal:8000"],
        transport=logistics_server.asgi_transport,
    )

    async with AgentRewind(tx_id="tx_savepoint_demo") as tx:
        # Step 1: Outer transaction locks inventory on the remote MCP server
        await mcp.call("lock_warehouse_inventory", sku="SKU-NVIDIA-B200")

        # Step 2: Try Express Air Shipping inside a nested Savepoint
        try:
            async with tx.savepoint("try_express_air_courier"):
                await mcp.call("allocate_express_air_slot", order_id="ORD-700")
                await mcp.call("confirm_express_customs_clearance", order_id="ORD-700")  # Fails!
        except StepExecutionError as exc:
            console.print(
                f"[bold yellow]⚡ Savepoint caught error:[/bold yellow] {exc.original_error}\n"
                "[bold green]✓ Express slot rolled back on remote server; falling back to Standard Ground![/bold green]"
            )

        # Step 3: Fallback to Standard Ground Shipping & Commit
        await mcp.call("book_standard_ground_shipping", order_id="ORD-700")
        await mcp.call("send_shipping_notification", order_id="ORD-700", carrier="StandardGround")

    render_transaction_report(
        tx.report,
        console=console,
        title="Example 05: Nested Savepoint Partial Rollback & Graceful Shipping Fallback",
    )
    console.print("[bold cyan]Final Remote Server State:[/bold cyan]", STATE)


if __name__ == "__main__":
    asyncio.run(main())

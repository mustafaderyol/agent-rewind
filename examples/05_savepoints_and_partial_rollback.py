"""Example 05: Nested Savepoints & Partial Rollback (E-Commerce Shipping Fallback).

Demonstrates how `async with tx.savepoint("name"):` allows an AI agent to recover
from a failing sub-branch (e.g., Express Air Courier API outage) by rolling back
ONLY the steps inside the savepoint while preserving the outer warehouse lock.
"""

from __future__ import annotations

import asyncio

from rich.console import Console

from agent_rewind import (
    ActionReceipt,
    AgentRewind,
    StepExecutionError,
    render_transaction_report,
    rewind_tool,
)

console = Console()

STATE = {
    "warehouse_locked_sku": None,
    "express_label": None,
    "express_surcharge_charged": False,
    "ground_label": None,
    "confirmation_sent": False,
}


@rewind_tool(compensate=lambda sku: STATE.update(warehouse_locked_sku=None))
async def lock_warehouse_inventory(sku: str) -> ActionReceipt:
    STATE["warehouse_locked_sku"] = sku
    return ActionReceipt(result={"locked": sku}, undo_args={"sku": sku})


@rewind_tool(compensate=lambda label_id: STATE.update(express_label=None))
async def allocate_express_air_slot(order_id: str) -> ActionReceipt:
    label_id = f"AIR-{order_id}"
    STATE["express_label"] = label_id
    return ActionReceipt(result={"label_id": label_id}, undo_args={"label_id": label_id})


@rewind_tool
async def confirm_express_customs_clearance(order_id: str) -> dict:
    raise RuntimeError("504 Gateway Timeout: Express Air Customs API unreachable")


@rewind_tool(compensate=lambda label_id: STATE.update(ground_label=None))
async def book_standard_ground_shipping(order_id: str) -> ActionReceipt:
    label_id = f"GND-{order_id}"
    STATE["ground_label"] = label_id
    return ActionReceipt(result={"label_id": label_id}, undo_args={"label_id": label_id})


@rewind_tool(deferred=True)
async def send_shipping_notification(order_id: str, carrier: str) -> dict:
    STATE["confirmation_sent"] = True
    return {"order_id": order_id, "carrier": carrier}


async def main() -> None:
    async with AgentRewind(tx_id="tx_savepoint_demo") as tx:
        # Step 1: Outer transaction locks inventory (must survive savepoint failure)
        await lock_warehouse_inventory("SKU-NVIDIA-B200")

        # Step 2: Try Express Air Shipping inside a nested Savepoint
        try:
            async with tx.savepoint("try_express_air_courier"):
                await allocate_express_air_slot("ORD-700")
                await confirm_express_customs_clearance("ORD-700")  # Fails!
        except StepExecutionError as exc:
            console.print(
                f"[bold yellow]⚡ Savepoint caught error:[/bold yellow] {exc.original_error}\n"
                "[bold green]✓ Express slot rolled back; falling back to Standard Ground![/bold green]"
            )

        # Step 3: Fallback to Standard Ground Shipping & Commit
        await book_standard_ground_shipping("ORD-700")
        await send_shipping_notification("ORD-700", carrier="StandardGround")

    render_transaction_report(
        tx.report,
        console=console,
        title="Example 05: Nested Savepoint Partial Rollback & Graceful Shipping Fallback",
    )
    console.print("[bold cyan]Final System State:[/bold cyan]", STATE)


if __name__ == "__main__":
    asyncio.run(main())

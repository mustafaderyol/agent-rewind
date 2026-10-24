"""Example 01: Minimal Quickstart demonstrating `@rewind_tool`, `ActionReceipt`, and Outbox."""

from __future__ import annotations

import asyncio

from agent_rewind import (
    ActionReceipt,
    AgentRewind,
    render_transaction_report,
    rewind_tool,
)

# Simulated external state
DB_STATE = {"users": {}, "stock": 50, "emails_sent": []}


@rewind_tool(compensate=lambda user_id: DB_STATE["users"].pop(user_id, None))
async def step1_create_user(name: str) -> ActionReceipt:
    user_id = f"usr_{name.lower()}"
    user = {"id": user_id, "name": name}
    DB_STATE["users"][user_id] = user
    return ActionReceipt(result=user, undo_args={"user_id": user_id})


@rewind_tool(
    pre_state=lambda args: DB_STATE["stock"],
    compensate=lambda args, res, old_stock: DB_STATE.update(stock=old_stock),
)
async def step2_reserve_stock(qty: int) -> dict:
    DB_STATE["stock"] -= qty
    return {"remaining": DB_STATE["stock"]}


@rewind_tool(deferred=True)
async def step2b_send_welcome_email(email: str) -> dict:
    DB_STATE["emails_sent"].append(email)
    return {"sent": True}


@rewind_tool
async def step3_failing_payment() -> dict:
    raise RuntimeError("Payment provider crashed at Step 3 (503 Service Unavailable)!")


async def main() -> None:
    async with AgentRewind(tx_id="tx_quickstart_01", suppress_errors=True) as tx:
        await step1_create_user("Alice")
        await step2_reserve_stock(5)
        await step2b_send_welcome_email("alice@example.com")
        await step3_failing_payment()  # Crashes here -> triggers automatic LIFO rollback!

    render_transaction_report(
        tx.report,
        title="Example 01: Automatic LIFO Rollback & Outbox Discard on Step 3 Failure",
    )
    print("Final DB State after automatic rollback:", DB_STATE)


if __name__ == "__main__":
    asyncio.run(main())

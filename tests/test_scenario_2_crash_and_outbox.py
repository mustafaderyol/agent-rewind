"""Scenario 2: Step 3 System Crash (503 Timeout), Outbox Suppression, and ActionReceipt Pattern."""

from __future__ import annotations

import pytest
from conftest import FourSystemsEnvironment

from agent_rewind import (
    ActionReceipt,
    AgentRewind,
    StepExecutionError,
    TransactionState,
    rewind_tool,
)


async def test_step3_service_crash_rolls_back_and_discards_outbox(
    env: FourSystemsEnvironment,
) -> None:
    """If System 3 crashes with a 503 error, Steps 2 & 1 are compensated and deferred emails never send."""
    env.payment.should_crash = True

    @rewind_tool
    async def crm_create_return(order_id: str) -> ActionReceipt[dict]:
        res = env.crm.create_return(order_id)
        return ActionReceipt(
            result=res,
            undo=lambda: env.crm.rollback_return(res["return_id"], res["previous_order_status"]),
        )

    @rewind_tool(
        pre_state=lambda args: env.erp.get_stock(args["sku"]),
        compensate=lambda args, res, pre: env.erp.set_stock(args["sku"], pre),
    )
    async def erp_restock(sku: str, qty: int) -> dict:
        return env.erp.increment_stock(sku, qty)

    @rewind_tool(compensate=lambda args, res: env.payment.reverse_refund(res["payment_tx_id"]))
    async def payment_refund(order_id: str, iban: str, amount: float) -> dict:
        return env.payment.process_refund(order_id, iban, amount)

    @rewind_tool(deferred=True)
    async def send_email(to: str, subject: str, body: str) -> dict:
        return env.notify.send_email(to, subject, body)

    with pytest.raises(StepExecutionError):
        async with AgentRewind() as tx:
            ret = await crm_create_return("ORD-101")
            await erp_restock(ret["sku"], ret["qty"])
            await send_email(ret["customer_email"], "Refund Approved", "Sending your refund now.")
            await payment_refund("ORD-101", ret["registered_iban"], ret["amount"])

    assert env.crm.orders["ORD-101"]["status"] == "DELIVERED"
    assert "RET-ORD-101" not in env.crm.returns
    assert env.erp.get_stock("SKU-MACBOOK") == 10
    assert len(env.notify.sent_emails) == 0
    assert tx.report.state == TransactionState.ROLLED_BACK
    assert tx.report.compensated_steps == ["erp_restock", "crm_create_return"]
    assert tx.report.discarded_outbox_steps == ["send_email"]


async def test_all_four_systems_commit_and_flush_outbox_on_success(
    env: FourSystemsEnvironment,
) -> None:
    """When all 4 steps succeed without error, Outbox flushes and state commits."""
    env.payment.should_crash = False

    @rewind_tool(
        compensate=lambda args, res: env.crm.rollback_return(
            res["return_id"], res["previous_order_status"]
        )
    )
    async def crm_create_return(order_id: str) -> dict:
        return env.crm.create_return(order_id)

    @rewind_tool(
        pre_state=lambda args: env.erp.get_stock(args["sku"]),
        compensate=lambda args, res, pre: env.erp.set_stock(args["sku"], pre),
    )
    async def erp_restock(sku: str, qty: int) -> dict:
        return env.erp.increment_stock(sku, qty)

    @rewind_tool(compensate=lambda args, res: env.payment.reverse_refund(res["payment_tx_id"]))
    async def payment_refund(order_id: str, iban: str, amount: float) -> dict:
        return env.payment.process_refund(order_id, iban, amount)

    @rewind_tool(deferred=True)
    async def send_email(to: str, subject: str, body: str) -> dict:
        return env.notify.send_email(to, subject, body)

    async with AgentRewind() as tx:
        ret = await crm_create_return("ORD-101")
        await erp_restock(ret["sku"], ret["qty"])
        await payment_refund("ORD-101", ret["registered_iban"], ret["amount"])
        await send_email(ret["customer_email"], "Refund Completed", "$45,000.00 has been refunded.")

    assert tx.report.state == TransactionState.COMMITTED
    assert env.crm.orders["ORD-101"]["status"] == "RETURN_IN_PROGRESS"
    assert env.erp.get_stock("SKU-MACBOOK") == 11
    assert len(env.payment.ledger) == 1
    assert len(env.notify.sent_emails) == 1
    assert tx.report.flushed_outbox_steps == ["send_email"]

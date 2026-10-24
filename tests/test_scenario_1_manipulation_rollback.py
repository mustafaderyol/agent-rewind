"""Scenario 1: Step 3 Manipulation (Prompt Injection) Detection & Automatic LIFO Compensation."""

from __future__ import annotations

import pytest
from conftest import ExtractedRefundInstruction, FourSystemsEnvironment

from agent_rewind import (
    AgentRewind,
    PolicyGuard,
    SecurityManipulationError,
    TransactionState,
    rewind_tool,
)


async def test_step3_prompt_injection_triggers_lifo_rollback(env: FourSystemsEnvironment) -> None:
    """When the agent is manipulated at Step 3 via an untrusted note, Steps 2 and 1 roll back cleanly."""

    @rewind_tool
    async def crm_create_return(order_id: str) -> dict:
        return env.crm.create_return(order_id)

    @crm_create_return.compensate
    async def _undo_crm_return(args: dict, result: dict) -> dict:
        return env.crm.rollback_return(result["return_id"], result["previous_order_status"])

    @rewind_tool(
        pre_state=lambda args: env.erp.get_stock(args["sku"]),
        compensate=lambda args, res, pre: env.erp.set_stock(args["sku"], pre),
    )
    async def erp_restock_item(sku: str, qty: int) -> dict:
        return env.erp.increment_stock(sku, qty)

    @rewind_tool(
        deny_tainted=["iban"],
        compensate=lambda args, res: env.payment.reverse_refund(res["payment_tx_id"]),
    )
    async def payment_issue_refund(order_id: str, iban: str, amount: float) -> dict:
        return env.payment.process_refund(order_id, iban, amount)

    @rewind_tool(deferred=True)
    async def notify_send_email(to: str, subject: str, body: str) -> dict:
        return env.notify.send_email(to, subject, body)

    def compromised_quarantine_llm(raw_note: str, schema: type[ExtractedRefundInstruction]):
        if "TR99HACKER00009999" in raw_note:
            return ExtractedRefundInstruction(
                requested_iban="TR99HACKER00009999",
                reason_summary="Hijacked by prompt injection",
            )
        return ExtractedRefundInstruction(
            requested_iban="TR11000011112222",
            reason_summary="Damaged box",
        )

    policy = PolicyGuard()

    with pytest.raises(SecurityManipulationError) as exc_info:
        async with AgentRewind(
            policy=policy,
            quarantine_extractor=compromised_quarantine_llm,
        ) as tx:
            ret_info = await crm_create_return("ORD-666")
            assert env.crm.orders["ORD-666"]["status"] == "RETURN_IN_PROGRESS"
            assert "RET-ORD-666" in env.crm.returns

            policy.require_trusted_match(
                "payment_issue_refund",
                "iban",
                ret_info["registered_iban"],
                reason="Refund IBAN must match customer's verified CRM IBAN",
            )

            await erp_restock_item(ret_info["sku"], ret_info["qty"])
            assert env.erp.get_stock("SKU-MACBOOK") == 11

            await notify_send_email(
                ret_info["customer_email"],
                "Return Started",
                "Your return ticket and inventory restock have been recorded.",
            )

            raw_note = env.crm.orders["ORD-666"]["customer_note"]
            note_ref = await tx.quarantine_extract(raw_note, ExtractedRefundInstruction)

            await payment_issue_refund(
                order_id="ORD-666",
                iban=note_ref.attr("requested_iban"),
                amount=ret_info["amount"],
            )

    assert exc_info.value.tool_name == "payment_issue_refund"
    assert "iban" in exc_info.value.tainted_fields

    assert len(env.payment.ledger) == 0
    assert len(env.notify.sent_emails) == 0

    assert env.erp.get_stock("SKU-MACBOOK") == 10
    assert env.crm.orders["ORD-666"]["status"] == "DELIVERED"
    assert "RET-ORD-666" not in env.crm.returns

    assert tx.report.state == TransactionState.ROLLED_BACK
    assert tx.report.compensated_steps == ["erp_restock_item", "crm_create_return"]
    assert tx.report.discarded_outbox_steps == ["notify_send_email"]

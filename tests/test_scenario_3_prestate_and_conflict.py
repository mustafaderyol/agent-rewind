"""Scenario 3: Optimistic Concurrency & Dirty-Write Conflict Detection During Rollback."""

from __future__ import annotations

import pytest
from conftest import FourSystemsEnvironment

from agent_rewind import AgentRewind, StateConflictError, TransactionState, rewind_tool


async def test_concurrent_modification_detected_before_compensation(
    env: FourSystemsEnvironment,
) -> None:
    """If an external actor modifies System 2 state between Step 2 and Rollback, raise StateConflictError."""
    env.payment.should_crash = True

    @rewind_tool(
        pre_state=lambda args: env.erp.get_stock(args["sku"]),
        current_state=lambda args, res: env.erp.get_stock(args["sku"]),
        verify_unmodified=True,
        compensate=lambda args, res, pre: env.erp.set_stock(args["sku"], pre),
    )
    async def erp_restock(sku: str, qty: int) -> dict:
        return env.erp.increment_stock(sku, qty)

    @rewind_tool
    async def failing_payment() -> None:
        raise RuntimeError("Gateway crashed at Step 3")

    with pytest.raises(StateConflictError) as exc_info:
        async with AgentRewind() as tx:
            await erp_restock("SKU-MACBOOK", 1)
            assert env.erp.get_stock("SKU-MACBOOK") == 11

            # External actor buys 6 laptops concurrently -> stock becomes 5!
            env.erp.set_stock("SKU-MACBOOK", 5)

            # Step 3 fails -> Rollback attempts to restore stock to 10, but detects conflict (5 != 11)!
            await failing_payment()

    assert exc_info.value.tool_name == "erp_restock"
    assert exc_info.value.expected_post_state == 11
    assert exc_info.value.actual_current_state == 5
    assert env.erp.get_stock("SKU-MACBOOK") == 5
    assert tx.report.state == TransactionState.FAILED_ROLLBACK

"""Scenario 4: Process Crash Recovery from Persistent JSON Journal & MCP Schema Export."""

from __future__ import annotations

from pathlib import Path

from conftest import FourSystemsEnvironment

from agent_rewind import (
    AgentRewind,
    TransactionState,
    rewind_tool,
    to_mcp_tool_schema,
)


async def test_recover_incomplete_transaction_from_json_journal(
    env: FourSystemsEnvironment,
    tmp_path: Path,
) -> None:
    """Simulate a hard process kill after Step 2 and recover/compensate from JSONFileBackend on restart."""
    wal_file = tmp_path / "agent_wal.json"

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

    # Simulate process starting a transaction, executing Step 1 & Step 2, and dying before __aexit__
    tx = AgentRewind(tx_id="tx_crashed_99", wal_path=wal_file)
    await tx.__aenter__()
    ret = await crm_create_return("ORD-101")
    await erp_restock(ret["sku"], ret["qty"])
    tx.journal.close()  # Simulate sudden process termination before Step 3 / __aexit__

    # Verify external systems are currently mutated (dirty state left by crashed agent)
    assert env.crm.orders["ORD-101"]["status"] == "RETURN_IN_PROGRESS"
    assert env.erp.get_stock("SKU-MACBOOK") == 11

    # On restart, recover from journal!
    recovered_reports = await AgentRewind.recover_from_wal(
        wal_file,
        tools=[crm_create_return, erp_restock],
    )

    assert len(recovered_reports) == 1
    rep = recovered_reports[0]
    assert rep.tx_id == "tx_crashed_99"
    assert rep.state == TransactionState.ROLLED_BACK
    assert rep.compensated_steps == ["erp_restock", "crm_create_return"]

    # Verify both systems were restored to their original clean state!
    assert env.erp.get_stock("SKU-MACBOOK") == 10
    assert env.crm.orders["ORD-101"]["status"] == "DELIVERED"


def test_mcp_tool_schema_export_hides_compensation_code() -> None:
    """Verify @rewind_tool exports clean MCP JSON Schema with orchestrator metadata."""

    @rewind_tool(
        compensate=lambda args, res: None,
        deny_tainted=["iban"],
    )
    async def refund_customer(order_id: str, iban: str, amount: float = 100.0) -> dict:
        """Issue a refund to the customer's bank account."""
        return {"ok": True}

    schema = to_mcp_tool_schema(refund_customer)
    assert schema["name"] == "refund_customer"
    assert schema["description"] == "Issue a refund to the customer's bank account."
    assert set(schema["inputSchema"]["required"]) == {"order_id", "iban"}
    assert schema["x-transactional"]["effectType"] == "COMPENSATABLE"
    assert schema["x-transactional"]["hasCompensation"] is True
    assert schema["x-transactional"]["denyTaintedArgs"] == ["iban"]

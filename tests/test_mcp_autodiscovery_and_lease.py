"""Tests for Zero-Config Remote MCP Auto-Discovery, Compensation Retries, and Server TTL Leases."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from conftest import ExtractedRefundInstruction, FourSystemsEnvironment

from agent_rewind import (
    AgentRewind,
    AutoDiscoveryEngine,
    EffectType,
    PolicyGuard,
    RemoteMCPGateway,
    SecurityManipulationError,
    ServerLeaseManager,
    StepExecutionError,
    TransactionState,
    rewind_tool,
)


def test_autodiscovery_engine_synthesizes_pairings_from_standard_mcp_schemas() -> None:
    """Given only standard MCP tool schemas, verify full pairing synthesis."""
    raw_tools = [
        {
            "name": "crm_create_return",
            "description": "Create return",
            "inputSchema": {"properties": {"order_id": {"type": "string"}}},
        },
        {
            "name": "crm_rollback_return",
            "description": "Rollback return",
            "inputSchema": {
                "properties": {
                    "return_id": {"type": "string"},
                    "previous_status": {"type": "string"},
                }
            },
        },
        {
            "name": "erp_get_stock",
            "description": "Read stock",
            "inputSchema": {"properties": {"sku": {"type": "string"}}},
        },
        {
            "name": "erp_adjust_stock",
            "description": "Adjust stock",
            "inputSchema": {"properties": {"sku": {"type": "string"}, "delta": {"type": "integer"}}},
        },
        {
            "name": "erp_set_stock",
            "description": "Set stock",
            "inputSchema": {"properties": {"sku": {"type": "string"}, "qty": {"type": "integer"}}},
        },
        {
            "name": "payment_process_refund",
            "description": "Refund IBAN",
            "inputSchema": {
                "properties": {
                    "order_id": {"type": "string"},
                    "iban": {"type": "string"},
                    "amount": {"type": "number"},
                }
            },
        },
        {
            "name": "payment_reverse_refund",
            "description": "Reverse refund",
            "inputSchema": {"properties": {"payment_tx_id": {"type": "string"}}},
        },
        {
            "name": "notify_send_email",
            "description": "Send email",
            "inputSchema": {"properties": {"to": {"type": "string"}}},
        },
    ]

    bindings = AutoDiscoveryEngine.synthesize_bindings("http://mcp-host:8000", raw_tools)

    assert bindings["crm_create_return"].effect == EffectType.COMPENSATABLE
    assert bindings["crm_create_return"].compensate_tool == "crm_rollback_return"
    assert bindings["crm_rollback_return"].is_compensator_only is True

    assert bindings["erp_adjust_stock"].pre_state_tool == "erp_get_stock"
    assert bindings["erp_adjust_stock"].compensate_tool == "erp_set_stock"
    assert bindings["erp_set_stock"].is_compensator_only is True

    assert bindings["payment_process_refund"].compensate_tool == "payment_reverse_refund"
    assert "iban" in bindings["payment_process_refund"].deny_tainted

    assert bindings["notify_send_email"].effect == EffectType.DEFERRED


async def test_compensation_retries_recover_from_transient_network_flakiness() -> None:
    """When a compensation call fails twice with transient network errors, retry succeeds on 3rd attempt."""
    state = {"value": "initial", "comp_attempts": 0}

    def flaky_undo(args: dict[str, Any], res: Any) -> str:
        state["comp_attempts"] += 1
        if state["comp_attempts"] < 3:
            raise ConnectionError("Transient 502 Bad Gateway during rollback")
        state["value"] = "initial"
        return "restored_on_attempt_3"

    @rewind_tool(compensate=flaky_undo)
    async def mutate_state() -> str:
        state["value"] = "mutated"
        return "mutated"

    @rewind_tool
    async def crash_step() -> None:
        raise RuntimeError("Step 3 failed")

    with pytest.raises(StepExecutionError):
        async with AgentRewind(compensation_retries=3, retry_backoff_ms=5.0) as tx:
            await mutate_state()
            await crash_step()

    assert state["comp_attempts"] == 3
    assert state["value"] == "initial"
    assert tx.report.state == TransactionState.ROLLED_BACK


async def test_server_lease_manager_auto_expires_and_rolls_back_on_agent_death() -> None:
    """Verify Dead-Man's Switch: uncommitted server lease automatically rolls back after TTL."""
    db_record = {"status": "RESERVED_BY_AGENT"}
    lease_mgr = ServerLeaseManager()

    token = lease_mgr.register_lease(
        lambda: db_record.update(status="AVAILABLE"),
        ttl_seconds=0.03,
    )
    assert db_record["status"] == "RESERVED_BY_AGENT"

    await asyncio.sleep(0.05)
    expired = await lease_mgr.sweep_expired_leases()

    assert token in expired
    assert db_record["status"] == "AVAILABLE"


async def test_remote_mcp_autodiscovery_four_servers_end_to_end(
    env: FourSystemsEnvironment,
) -> None:
    """Connect to 4 simulated MCP endpoints using ONLY their URLs and test full rollback."""
    urls = [
        "http://crm.mcp.local",
        "http://erp.mcp.local",
        "http://payment.mcp.local",
        "http://notify.mcp.local",
    ]

    async def four_server_mcp_transport(
        method: str,
        url: str,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        if url.endswith("/mcp/tools"):
            if "crm" in url:
                return {
                    "tools": [
                        {
                            "name": "crm_create_return",
                            "description": "Create return",
                            "inputSchema": {"properties": {"order_id": {"type": "string"}}},
                        },
                        {
                            "name": "crm_rollback_return",
                            "description": "Cancel return",
                            "inputSchema": {
                                "properties": {
                                    "return_id": {"type": "string"},
                                    "previous_status": {"type": "string"},
                                }
                            },
                        },
                    ]
                }
            if "erp" in url:
                return {
                    "tools": [
                        {
                            "name": "erp_get_stock",
                            "description": "Get stock",
                            "inputSchema": {"properties": {"sku": {"type": "string"}}},
                        },
                        {
                            "name": "erp_adjust_stock",
                            "description": "Adjust stock",
                            "inputSchema": {
                                "properties": {
                                    "sku": {"type": "string"},
                                    "delta": {"type": "integer"},
                                }
                            },
                        },
                        {
                            "name": "erp_set_stock",
                            "description": "Restore stock",
                            "inputSchema": {
                                "properties": {
                                    "sku": {"type": "string"},
                                    "qty": {"type": "integer"},
                                }
                            },
                        },
                    ]
                }
            if "payment" in url:
                return {
                    "tools": [
                        {
                            "name": "payment_process_refund",
                            "description": "Refund IBAN",
                            "inputSchema": {
                                "properties": {
                                    "order_id": {"type": "string"},
                                    "iban": {"type": "string"},
                                    "amount": {"type": "number"},
                                }
                            },
                        },
                        {
                            "name": "payment_reverse_refund",
                            "description": "Reverse refund",
                            "inputSchema": {"properties": {"payment_tx_id": {"type": "string"}}},
                        },
                    ]
                }
            if "notify" in url:
                return {
                    "tools": [
                        {
                            "name": "notify_send_email",
                            "description": "Send email",
                            "inputSchema": {
                                "properties": {
                                    "to": {"type": "string"},
                                    "subject": {"type": "string"},
                                    "body": {"type": "string"},
                                }
                            },
                        }
                    ]
                }
        body = json_body or {}
        tool = body["tool"]
        args = body.get("arguments", {})
        if tool == "crm_create_return":
            rec = env.crm.create_return(args["order_id"])
            rec["customer_note"] = env.crm.orders[args["order_id"]]["customer_note"]
            return {"result": rec}
        if tool == "crm_rollback_return":
            return {"result": env.crm.rollback_return(args["return_id"], args["previous_status"])}
        if tool == "erp_get_stock":
            return {"result": {"sku": args["sku"], "qty": env.erp.get_stock(args["sku"])}}
        if tool == "erp_adjust_stock":
            return {"result": env.erp.increment_stock(args["sku"], args["delta"])}
        if tool == "erp_set_stock":
            return {"result": env.erp.set_stock(args["sku"], args["qty"])}
        if tool == "payment_process_refund":
            return {
                "result": env.payment.process_refund(args["order_id"], args["iban"], args["amount"])
            }
        if tool == "payment_reverse_refund":
            return {"result": env.payment.reverse_refund(args["payment_tx_id"])}
        if tool == "notify_send_email":
            return {
                "result": env.notify.send_email(args["to"], args["subject"], args["body"])
            }
        raise ValueError(f"Unknown tool: {tool}")

    mcp = await RemoteMCPGateway.discover(urls, transport=four_server_mcp_transport)

    llm_tool_names = {t["name"] for t in mcp.get_llm_visible_tools()}
    assert "crm_create_return" in llm_tool_names
    assert "erp_adjust_stock" in llm_tool_names
    assert "crm_rollback_return" not in llm_tool_names
    assert "erp_set_stock" not in llm_tool_names
    assert "payment_reverse_refund" not in llm_tool_names

    def compromised_reader(raw_note: str, schema: type[ExtractedRefundInstruction]):
        return ExtractedRefundInstruction(
            requested_iban="TR99HACKER00009999",
            reason_summary="Injected IBAN",
        )

    policy = PolicyGuard()
    with pytest.raises(SecurityManipulationError):
        async with AgentRewind(
            policy=policy,
            quarantine_extractor=compromised_reader,
        ) as tx:
            ret = await mcp.call("crm_create_return", order_id="ORD-666")
            policy.require_trusted_match(
                "payment_process_refund", "iban", ret["registered_iban"]
            )
            await mcp.call("erp_adjust_stock", sku=ret["sku"], delta=ret["qty"])
            await mcp.call(
                "notify_send_email",
                to=ret["customer_email"],
                subject="Return Initiated",
                body="Processing your return.",
            )
            note_ref = await tx.quarantine_extract(
                ret["customer_note"], ExtractedRefundInstruction
            )
            await mcp.call(
                "payment_process_refund",
                order_id="ORD-666",
                iban=note_ref.attr("requested_iban"),
                amount=ret["amount"],
            )

    assert env.crm.orders["ORD-666"]["status"] == "DELIVERED"
    assert len(env.crm.returns) == 0
    assert env.erp.get_stock("SKU-MACBOOK") == 10
    assert len(env.payment.ledger) == 0
    assert len(env.notify.sent_emails) == 0
    assert tx.report.state == TransactionState.ROLLED_BACK


async def test_financial_idempotency_prevents_double_charge() -> None:
    """If an agent accidentally invokes the exact same payment call twice, execute only once."""
    bank_ledger: list[dict[str, Any]] = []

    @rewind_tool(compensate=lambda args, res: bank_ledger.pop())
    async def charge_card(order_id: str, amount: float) -> dict[str, Any]:
        tx_rec = {"tx_id": f"TX-{len(bank_ledger) + 1}", "order_id": order_id, "amount": amount}
        bank_ledger.append(tx_rec)
        return tx_rec

    async with AgentRewind() as tx:
        res1 = await charge_card("ORD-77", 1500.0)
        res2 = await charge_card("ORD-77", 1500.0)

    assert res1 == res2
    assert len(bank_ledger) == 1
    assert tx.report.state == TransactionState.COMMITTED


async def test_financial_max_value_circuit_breaker_triggers_rollback() -> None:
    """When amount exceeds policy.max_value ceiling, trip circuit breaker and roll back previous steps."""
    crm_tickets: list[str] = []

    @rewind_tool(compensate=lambda args, res: crm_tickets.remove(res))
    async def open_ticket(ticket_id: str) -> str:
        crm_tickets.append(ticket_id)
        return ticket_id

    @rewind_tool
    async def transfer_funds(amount: float) -> dict[str, Any]:
        return {"transferred": amount}

    policy = PolicyGuard().max_value("transfer_funds", "amount", limit=10000.0)

    with pytest.raises(SecurityManipulationError):
        async with AgentRewind(policy=policy) as tx:
            await open_ticket("TCK-1")
            assert crm_tickets == ["TCK-1"]
            await transfer_funds(95000.0)

    assert crm_tickets == []
    assert tx.report.state == TransactionState.ROLLED_BACK

"""Tests for Savepoints (Partial Rollback), Dead-Letter Queue (DLQ), Nested Taint, HITL, Timeout & Audit Hash-Chain."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from agent_rewind import (
    AgentRewind,
    AgentTransaction,
    CallbackBackend,
    CompensationFailedError,
    InMemoryBackend,
    JSONFileBackend,
    JSONLFileBackend,
    PolicyGuard,
    Ref,
    RemoteMCPGateway,
    SecurityManipulationError,
    StepExecutionError,
    TaintLevel,
    TransactionState,
    rewind_tool,
    tx_tool,
)


async def test_savepoint_partial_rollback_preserves_earlier_steps() -> None:
    """When a sub-workflow inside tx.savepoint() fails, only the savepoint steps roll back; Steps 1 & 2 survive."""
    state = {"crm": False, "erp": False, "fast_courier": False, "standard_post": False}

    @tx_tool(compensate=lambda: state.update(crm=False))
    async def step1_crm() -> str:
        state["crm"] = True
        return "crm_ok"

    @tx_tool(compensate=lambda: state.update(erp=False))
    async def step2_erp() -> str:
        state["erp"] = True
        return "erp_ok"

    @tx_tool(compensate=lambda: state.update(fast_courier=False))
    async def step3a_reserve_fast_courier() -> str:
        state["fast_courier"] = True
        return "courier_reserved"

    @tx_tool
    async def step3b_courier_api_crash() -> None:
        raise RuntimeError("Fast courier API down")

    @tx_tool(compensate=lambda: state.update(standard_post=False))
    async def step3_fallback_standard_post() -> str:
        state["standard_post"] = True
        return "standard_post_ok"

    async with AgentTransaction() as tx:
        await step1_crm()
        await step2_erp()

        # Try risky sub-branch inside a Savepoint
        async with tx.savepoint("try_fast_courier", suppress_errors=True):
            await step3a_reserve_fast_courier()
            assert state["fast_courier"] is True
            await step3b_courier_api_crash()  # Fails -> only step3a is rolled back!

        # Verify step3a was rolled back, but step1 and step2 are STILL intact!
        assert state["fast_courier"] is False
        assert state["crm"] is True
        assert state["erp"] is True

        # Continue with fallback step and commit entire transaction
        await step3_fallback_standard_post()

    assert tx.report.state == TransactionState.COMMITTED
    assert state == {"crm": True, "erp": True, "fast_courier": False, "standard_post": True}


async def test_dead_letter_queue_persists_and_replays_failed_compensation(tmp_path: Path) -> None:
    """If a compensation fails all retries because the target service is down, save to DLQ and replay later."""
    wal_file = tmp_path / "dlq_test.db"
    service_state = {"mutated": False, "service_online": False}

    def undo_mutation(args: dict[str, Any], res: Any) -> str:
        if not service_state["service_online"]:
            raise ConnectionError("Service 1 is completely offline during rollback!")
        service_state["mutated"] = False
        return "recovered_via_dlq"

    @tx_tool(compensate=undo_mutation)
    async def mutate_service1() -> str:
        service_state["mutated"] = True
        return "mutated"

    @tx_tool
    async def fail_step2() -> None:
        raise RuntimeError("Step 2 crashed")

    with pytest.raises(CompensationFailedError):
        async with AgentTransaction(
            wal_path=wal_file,
            compensation_retries=2,
            retry_backoff_ms=5.0,
        ) as tx:
            await mutate_service1()
            await fail_step2()

    # Service 1 is still mutated because rollback failed and entered DLQ
    assert service_state["mutated"] is True
    assert len(tx.report.dlq_entries) == 1
    assert tx.report.dlq_entries[0].tool_name == "mutate_service1"

    # Now Service 1 comes back online -> Operations replays the DLQ!
    service_state["service_online"] = True
    resolved_dlq = await AgentTransaction.replay_dlq(wal_file, tools=[mutate_service1])

    assert len(resolved_dlq) == 1
    assert service_state["mutated"] is False


async def test_nested_taint_propagation_blocks_deeply_hidden_untrusted_ref() -> None:
    """Prevent attackers from bypassing deny_tainted by nesting a tainted Ref inside a dict/list."""

    @tx_tool(deny_tainted=["payment_payload"])
    async def execute_batch_wire(payment_payload: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True}

    tainted_iban_ref = Ref(
        value="TR99HACKER00009999",
        taint=TaintLevel.UNTRUSTED,
        source_tool="untrusted_email",
    )

    with pytest.raises(SecurityManipulationError) as exc_info:
        async with AgentTransaction():
            # Nest the tainted Ref 3 levels deep inside a dict and list
            await execute_batch_wire(
                payment_payload={"transfers": [{"destination": tainted_iban_ref}]}
            )

    assert "payment_payload" in exc_info.value.tainted_fields


async def test_human_in_the_loop_approval_gate_rejection_triggers_rollback() -> None:
    """When HITL operator denies a high-value operation, roll back previous steps."""
    ledger = {"step1_done": False}

    @tx_tool(compensate=lambda: ledger.update(step1_done=False))
    async def step1_prepare() -> str:
        ledger["step1_done"] = True
        return "prepared"

    @tx_tool
    async def step2_wire_money(amount: float) -> str:
        return "wired"

    async def mock_human_operator(tool_name: str, args: dict[str, Any]) -> bool:
        # Operator rejects transfers over 50,000
        return float(args["amount"]) <= 50000.0

    policy = PolicyGuard().require_approval(
        "step2_wire_money",
        approval_handler=mock_human_operator,
        when=lambda args: args["amount"] > 10000.0,
    )

    with pytest.raises(SecurityManipulationError) as exc_info:
        async with AgentTransaction(policy=policy) as tx:
            await step1_prepare()
            assert ledger["step1_done"] is True
            await step2_wire_money(75000.0)

    assert exc_info.value.violated_rule == "human_in_the_loop_approval"
    assert ledger["step1_done"] is False
    assert tx.report.state == TransactionState.ROLLED_BACK


async def test_step_timeout_interrupts_hung_tool_and_rolls_back() -> None:
    """If Step 2 hangs indefinitely, step timeout aborts it and rolls back Step 1."""
    state = {"step1": False}

    @tx_tool(compensate=lambda: state.update(step1=False))
    async def step1_fast() -> str:
        state["step1"] = True
        return "ok"

    @tx_tool(timeout=0.04)
    async def step2_hung_api() -> str:
        await asyncio.sleep(5.0)
        return "never_reached"

    with pytest.raises(StepExecutionError):
        async with AgentTransaction() as tx:
            await step1_fast()
            assert state["step1"] is True
            await step2_hung_api()

    assert state["step1"] is False
    assert tx.report.state == TransactionState.ROLLED_BACK


async def test_cryptographic_hash_chain_detects_audit_log_tampering() -> None:
    """Verify SHA-256 hash chain across transaction steps detects any post-hoc tampering."""

    @tx_tool
    async def step_a(x: int) -> int:
        return x + 1

    @tx_tool
    async def step_b(y: int) -> int:
        return y * 2

    async with AgentTransaction() as tx:
        r1 = await step_a(10)
        await step_b(r1)

    report = tx.report
    assert report.verify_audit_integrity() is True

    # Simulate an attacker modifying the recorded argument in Step 1 of the report
    report.steps[0].args["x"] = 999999
    assert report.verify_audit_integrity() is False


async def test_jsonl_file_backend_crash_recovery_without_sqlite(tmp_path: Path) -> None:
    """Verify full WAL crash recovery using pure-Python JSONLFileBackend (zero sqlite3 dependency)."""
    jsonl_wal = tmp_path / "wal_no_sqlite.jsonl"
    state = {"stock": 100}

    @tx_tool(
        pre_state=lambda args: state["stock"],
        compensate=lambda args, res, pre: state.update(stock=pre),
    )
    async def adjust_stock(delta: int) -> int:
        state["stock"] += delta
        return state["stock"]

    tx = AgentTransaction(tx_id="tx_jsonl_1", storage=JSONLFileBackend(jsonl_wal))
    await tx.__aenter__()
    await adjust_stock(15)
    assert state["stock"] == 115
    # Simulate process crash before __aexit__
    tx.journal.close()

    # Recover using the .jsonl path directly (no SQLite used!)
    reports = await AgentTransaction.recover_from_wal(jsonl_wal, tools=[adjust_stock])
    assert len(reports) == 1
    assert reports[0].state == TransactionState.ROLLED_BACK
    assert state["stock"] == 100


async def test_callback_backend_and_agent_rewind_alias() -> None:
    """Verify CallbackBackend event hook and AgentRewind / rewind_tool aliases."""
    streamed_events: list[tuple[str, str]] = []
    cb_backend = CallbackBackend(
        on_step=lambda tx_id, step: streamed_events.append((step.tool_name, step.status.value))
    )

    state = {"count": 0}

    @rewind_tool(compensate=lambda: state.update(count=0))
    async def inc() -> int:
        state["count"] += 1
        return state["count"]

    async with AgentRewind(storage=cb_backend):
        await inc()

    assert ("inc", "EXECUTED") in streamed_events


async def test_custom_callable_mcp_transport_without_httpx() -> None:
    """Verify RemoteMCPGateway works with a custom transport function (zero httpx dependency)."""
    state = {"tickets": []}

    async def custom_transport(
        method: str,
        url: str,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        if url.endswith("/mcp/tools"):
            return {
                "tools": [
                    {
                        "name": "ticket_create_issue",
                        "description": "Open issue",
                        "inputSchema": {"properties": {"id": {"type": "string"}}},
                    },
                    {
                        "name": "ticket_delete_issue",
                        "description": "Close issue",
                        "inputSchema": {"properties": {"id": {"type": "string"}}},
                    },
                ]
            }
        tool = (json_body or {})["tool"]
        args = (json_body or {})["arguments"]
        if tool == "ticket_create_issue":
            state["tickets"].append(args["id"])
            return {"result": {"id": args["id"]}}
        if tool == "ticket_delete_issue":
            state["tickets"].remove(args["id"])
            return {"result": {"deleted": args["id"]}}
        return {}

    mcp = await RemoteMCPGateway.discover(["mcp://internal-bus"], transport=custom_transport)

    with pytest.raises(RuntimeError):
        async with AgentTransaction():
            await mcp.call("ticket_create_issue", id="ISS-1")
            assert state["tickets"] == ["ISS-1"]
            raise RuntimeError("Abort at step 2")

    assert state["tickets"] == []


async def test_llm_out_of_order_tool_call_blocked_and_rolled_back() -> None:
    """If the LLM ignores prompt instructions and skips a prerequisite step, block and roll back."""
    state = {"crm_opened": False, "erp_checked": False, "refund_sent": False}

    @rewind_tool(compensate=lambda: state.update(crm_opened=False))
    async def step1_open_return() -> str:
        state["crm_opened"] = True
        return "RET-1"

    @rewind_tool(compensate=lambda: state.update(erp_checked=False))
    async def step2_verify_warehouse() -> str:
        state["erp_checked"] = True
        return "WAREHOUSE_OK"

    @rewind_tool(requires_prior=["step1_open_return", "step2_verify_warehouse"])
    async def step3_send_refund() -> str:
        state["refund_sent"] = True
        return "REFUND_OK"

    policy = PolicyGuard().require_sequence(
        ["step1_open_return", "step2_verify_warehouse", "step3_send_refund"]
    )

    with pytest.raises(SecurityManipulationError) as exc_info:
        async with AgentRewind(policy=policy) as tx:
            await step1_open_return()
            assert state["crm_opened"] is True
            # LLM hallucinates and skips step2_verify_warehouse, calling step3_send_refund directly!
            await step3_send_refund()

    assert exc_info.value.tool_name == "step3_send_refund"
    assert state["refund_sent"] is False
    assert state["crm_opened"] is False  # Step 1 automatically rolled back!
    assert tx.report.state == TransactionState.ROLLED_BACK


def test_langchain_remote_runnable_sync_agent_executor_rollback() -> None:
    """Verify `rewind_runnable` + sync `with AgentRewind():` rolls back LangChain RemoteRunnable calls."""
    from agent_rewind import rewind_runnable

    remote_state = {"orders": []}

    class MockRemoteRunnable:
        def __init__(self, mode: str) -> None:
            self.mode = mode

        def invoke(self, data: dict[str, Any]) -> dict[str, Any]:
            if self.mode == "create":
                remote_state["orders"].append(data["order_id"])
                return {"order_id": data["order_id"], "created": True}
            if self.mode == "rollback":
                remote_state["orders"].remove(data["order_id"])
                return {"rolled_back": data["order_id"]}
            raise ConnectionError("Remote LangServe endpoint 503 crashed!")

    create_order_tool = rewind_runnable(
        MockRemoteRunnable("create"),
        name="remote_create_order",
        compensate=MockRemoteRunnable("rollback"),
    )
    failing_payment_tool = rewind_runnable(
        MockRemoteRunnable("crash"),
        name="remote_charge_payment",
    )

    with pytest.raises(StepExecutionError):
        # Synchronous `with AgentRewind()` matching `agent_executor.invoke(...)`
        with AgentRewind() as tx:
            create_order_tool.invoke({"order_id": "ORD-900"})
            assert remote_state["orders"] == ["ORD-900"]
            failing_payment_tool.invoke({"order_id": "ORD-900"})

    assert remote_state["orders"] == []
    assert tx.report.state == TransactionState.ROLLED_BACK


async def test_server_defined_compensation_via_rewind_server_and_undo_tokens() -> None:
    """When remote servers define @server.tool(compensate=...) internally, client rolls them back via undo_token without knowing the undo logic."""
    from agent_rewind import RewindServer

    crm_db = {"returns": []}
    erp_db = {"stock": 100}

    # Remote Server 1: CRM (Compensation is defined privately inside the server!)
    crm_server = RewindServer("crm-server")

    @crm_server.tool(
        compensate=lambda args, res: crm_db["returns"].remove(res["return_id"]),
    )
    async def crm_open_return(order_id: str) -> dict[str, str]:
        ret_id = f"RET-{order_id}"
        crm_db["returns"].append(ret_id)
        return {"return_id": ret_id}

    # Remote Server 2: ERP (Pre-state and compensation are defined privately inside the server!)
    erp_server = RewindServer("erp-server")

    @erp_server.tool(
        pre_state=lambda args: erp_db["stock"],
        compensate=lambda args, res, pre_qty: erp_db.update(stock=pre_qty),
    )
    async def erp_add_stock(delta: int) -> dict[str, int]:
        erp_db["stock"] += delta
        return {"new_stock": erp_db["stock"]}

    # Route requests to the two RewindServers
    async def router(
        method: str,
        url: str,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        if "crm-host" in url:
            return await crm_server.asgi_transport(method, url, json_body, headers)
        return await erp_server.asgi_transport(method, url, json_body, headers)

    # Client discovers tools from URLs — notice ZERO rollback tools are exposed in /mcp/tools!
    mcp = await RemoteMCPGateway.discover(
        ["http://crm-host", "http://erp-host"],
        transport=router,
    )
    assert set(mcp.tools.keys()) == {"crm_open_return", "erp_add_stock"}

    with pytest.raises(RuntimeError):
        async with AgentRewind() as tx:
            await mcp.call("crm_open_return", order_id="ORD-500")
            await mcp.call("erp_add_stock", delta=5)
            assert crm_db["returns"] == ["RET-ORD-500"]
            assert erp_db["stock"] == 105
            # Step 3 crashes -> Client automatically sends undo_tokens to Server 2 and Server 1!
            raise RuntimeError("Downstream payment failed!")

    assert erp_db["stock"] == 100
    assert crm_db["returns"] == []
    assert tx.report.state == TransactionState.ROLLED_BACK


def test_sync_def_tool_and_openai_batch_dispatcher() -> None:
    """Verify synchronous `def` @rewind_tool functions and `dispatch_tool_calls_sync`."""
    ledger: list[str] = []

    @rewind_tool(compensate=lambda args, res: ledger.remove(res["id"]))
    def book_flight(flight_id: str) -> dict[str, str]:
        ledger.append(flight_id)
        return {"id": flight_id}

    @rewind_tool
    def charge_expired_card(amount: float) -> dict[str, Any]:
        raise RuntimeError("Card expired")

    # Simulate OpenAI / Anthropic tool_calls list
    llm_tool_calls = [
        {"id": "call_1", "name": "book_flight", "arguments": '{"flight_id": "TK-1920"}'},
        {"id": "call_2", "name": "charge_expired_card", "arguments": '{"amount": 850.0}'},
    ]

    with pytest.raises(StepExecutionError):
        with AgentRewind() as tx:
            tx.dispatch_tool_calls_sync(llm_tool_calls, tools=[book_flight, charge_expired_card])

    assert ledger == []
    assert tx.report.state == TransactionState.ROLLED_BACK


def test_remote_runnable_in_band_undo_token_and_swallowed_agent_executor_error() -> None:
    """Verify `RewindServer.as_remote_runnable` + `rewind_tools` rolls back even when AgentExecutor catches the exception."""
    from agent_rewind import RewindServer, rewind_tools

    remote_db = {"orders": [], "stock": 10}
    server = RewindServer(name="langserve-remote")

    @server.tool(
        name="create_order",
        compensate=lambda args, res: remote_db["orders"].remove(res["order_id"]),
    )
    def create_order(order_id: str) -> dict[str, str]:
        remote_db["orders"].append(order_id)
        return {"order_id": order_id}

    @server.tool(
        name="reserve_stock",
        pre_state=lambda args: remote_db["stock"],
        compensate=lambda args, res, pre: remote_db.update(stock=pre),
    )
    def reserve_stock(qty: int) -> dict[str, int]:
        remote_db["stock"] -= qty
        return {"remaining": remote_db["stock"]}

    @server.tool(name="charge_card")
    def charge_card(amount: float) -> dict[str, Any]:
        raise RuntimeError("Gateway 502 error")

    # Client wraps RemoteRunnables with `rewind_tools` (ZERO client compensation code!)
    tools = rewind_tools([
        server.as_remote_runnable("create_order"),
        server.as_remote_runnable("reserve_stock"),
        server.as_remote_runnable("charge_card"),
    ])
    tool_map = {t.name: t for t in tools}

    with AgentRewind() as tx:
        tool_map["create_order"].invoke({"order_id": "ORD-777"})
        tool_map["reserve_stock"].invoke({"qty": 3})
        assert remote_db["orders"] == ["ORD-777"]
        assert remote_db["stock"] == 7

        # Simulate AgentExecutor catching and swallowing the tool error inside its loop
        try:
            tool_map["charge_card"].invoke({"amount": 999.0})
        except StepExecutionError:
            pass

    # Even though the exception was caught inside `with AgentRewind():`,
    # AgentRewind detected the unrecovered step failure and rolled back both remote steps!
    assert remote_db["orders"] == []
    assert remote_db["stock"] == 10
    assert tx.state == TransactionState.ROLLED_BACK


async def test_mcp_jsonrpc_2_0_full_lifecycle_and_stdio() -> None:
    """Verify `RewindServer` speaks full Official MCP JSON-RPC 2.0 (`initialize`, `tools/list`, `tools/call`, `rewind/rollback`, `stdio`)."""
    import io
    import json
    from agent_rewind import RewindServer

    db_state = {"items": []}
    server = RewindServer(name="mcp-standard-server", version="0.2.0")

    @server.tool(
        name="add_item",
        description="Adds an item to the remote store.",
        compensate=lambda args, res: db_state["items"].remove(res["added"]),
    )
    async def add_item(item_id: str) -> dict[str, str]:
        db_state["items"].append(item_id)
        return {"added": item_id}

    # 1. MCP `initialize`
    init_resp = await server.handle_jsonrpc({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2024-11-05"},
    })
    assert init_resp["result"]["protocolVersion"] == "2024-11-05"
    assert init_resp["result"]["capabilities"]["experimental"]["rewind"]["supportsUndoTokens"] is True

    # 2. MCP `notifications/initialized` (notification returns None)
    notif_resp = await server.handle_jsonrpc({
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
    })
    assert notif_resp is None

    # 3. MCP `tools/list` (includes MCP ToolAnnotations & `_meta.rewind`)
    list_resp = await server.handle_jsonrpc({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/list",
    })
    tools = list_resp["result"]["tools"]
    assert len(tools) == 1
    assert tools[0]["name"] == "add_item"
    assert tools[0]["annotations"]["readOnlyHint"] is False
    assert tools[0]["_meta"]["rewind"]["hasServerSideCompensation"] is True

    # 4. MCP `tools/call` -> returns MCP `CallToolResult` (`content`, `structuredContent`, `_meta.rewind.undo_token`)
    call_resp = await server.handle_jsonrpc({
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "add_item", "arguments": {"item_id": "SKU-99"}},
    })
    call_res = call_resp["result"]
    assert call_res["isError"] is False
    assert call_res["structuredContent"] == {"added": "SKU-99"}
    assert json.loads(call_res["content"][0]["text"]) == {"added": "SKU-99"}
    undo_token = call_res["_meta"]["rewind"]["undo_token"]
    assert db_state["items"] == ["SKU-99"]

    # 5. MCP `stdio` transport test: send `rewind/rollback` over newline-delimited JSON-RPC stdin/stdout
    stdin_buf = io.StringIO(
        json.dumps({
            "jsonrpc": "2.0",
            "id": 4,
            "method": "rewind/rollback",
            "params": {"undo_token": undo_token},
        }) + "\n"
    )
    stdout_buf = io.StringIO()
    await server.run_stdio_async(stdin=stdin_buf, stdout=stdout_buf)

    rb_resp = json.loads(stdout_buf.getvalue().strip())
    assert rb_resp["result"]["rolled_back"] is True
    assert db_state["items"] == []


def test_langgraph_multi_agent_supervisor_and_a2a_hierarchical_rollback() -> None:
    """Verify `rewind_graph` + `rewind_agent` + `RewindSubAgentHost` rolls back multi-agent StateGraph and A2A tasks."""
    from agent_rewind import RewindSubAgentHost, rewind_agent, rewind_graph

    host1_db = {"records": []}
    host2_db = {"quota": 100}

    @rewind_tool(compensate=lambda args, res: host1_db["records"].remove(res["id"]))
    def sub1_tool_a(rec_id: str) -> dict[str, str]:
        host1_db["records"].append(rec_id)
        return {"id": rec_id}

    @rewind_tool(
        pre_state=lambda args: host2_db["quota"],
        compensate=lambda args, res, pre: host2_db.update(quota=pre),
    )
    def sub2_tool_b(amount: int) -> dict[str, int]:
        host2_db["quota"] -= amount
        return {"remaining": host2_db["quota"]}

    # Remote Sub-Agent 1 (LangServe RemoteRunnable style, executes 2 internal tool calls)
    def subagent_1(payload: Any) -> dict[str, str]:
        sub1_tool_a("REC-1")
        sub1_tool_a("REC-2")
        return {"output": "subagent_1_done"}

    # Remote Sub-Agent 2 (Google A2A JSON-RPC 2.0 `tasks/send` / `tasks/cancel`)
    def subagent_2(payload: Any) -> dict[str, str]:
        sub2_tool_b(40)
        return {"output": "subagent_2_done"}

    # Remote Sub-Agent 3 (Fails!)
    def subagent_3(payload: Any) -> dict[str, str]:
        raise RuntimeError("Sub-Agent 3 crashed!")

    remote_agent_1 = rewind_agent(RewindSubAgentHost(subagent_1, name="remote_1"), name="remote_1")
    remote_agent_2 = rewind_agent(
        RewindSubAgentHost(subagent_2, name="remote_2"),
        name="remote_2",
        protocol="a2a",
    )
    remote_agent_3 = rewind_agent(RewindSubAgentHost(subagent_3, name="remote_3"), name="remote_3")

    class MockCompiledStateGraph:
        def invoke(self, state: dict[str, Any]) -> dict[str, Any]:
            remote_agent_1.invoke({"messages": state["messages"]})
            remote_agent_2.invoke({
                "jsonrpc": "2.0",
                "id": "a2a_1",
                "method": "tasks/send",
                "params": {"id": "task_99", "message": "allocate quota"},
            })
            assert host1_db["records"] == ["REC-1", "REC-2"]
            assert host2_db["quota"] == 60
            remote_agent_3.invoke({"messages": state["messages"]})
            return state

    multi_agent_system = rewind_graph(MockCompiledStateGraph(), suppress_errors=True)
    multi_agent_system.invoke({"messages": ["run complex task"]})

    assert multi_agent_system.last_transaction is not None
    assert multi_agent_system.last_transaction.state == TransactionState.ROLLED_BACK
    assert multi_agent_system.last_transaction.report.compensated_steps == ["remote_2", "remote_1"]
    assert host2_db["quota"] == 100
    assert host1_db["records"] == []


async def test_official_mcp_and_a2a_sdk_inheritance_and_lifecycle() -> None:
    """Verify `RewindServer` subclasses official `mcp.server.mcpserver.MCPServer` and `RewindA2AExecutor` subclasses official `a2a.server.agent_execution.AgentExecutor`."""
    from a2a.server.agent_execution import AgentExecutor as OfficialA2AExecutor
    from a2a.server.agent_execution import RequestContext as A2ARequestContext
    from a2a.server.events import EventQueue as A2AEventQueue
    from mcp.server.mcpserver import MCPServer as OfficialMCPServer
    from mcp.types import CallToolResult, Tool

    from agent_rewind import RewindA2AExecutor, RewindServer

    # 1. Verify Official MCP SDK Inheritance (`RewindServer(MCPServer)`)
    mcp_srv = RewindServer("official-mcp-srv")
    assert isinstance(mcp_srv, OfficialMCPServer)

    mcp_state = {"balance": 500}

    @mcp_srv.tool(
        pre_state=lambda args: mcp_state["balance"],
        compensate=lambda args, res, pre: mcp_state.update(balance=pre),
    )
    async def deduct_balance(amount: int) -> dict[str, int]:
        mcp_state["balance"] -= amount
        return {"remaining": mcp_state["balance"]}

    mcp_tools = await mcp_srv.list_mcp_tools()
    assert len(mcp_tools) == 1
    assert isinstance(mcp_tools[0], Tool)
    assert mcp_tools[0].meta["rewind"]["hasServerSideCompensation"] is True

    call_res = await mcp_srv.call_tool("deduct_balance", {"amount": 200})
    assert isinstance(call_res, CallToolResult)
    assert mcp_state["balance"] == 300
    u_tok = call_res.meta["rewind"]["undo_token"]

    await mcp_srv.call_tool("_rewind_rollback", {"undo_token": u_tok})
    assert mcp_state["balance"] == 500

    # 2. Verify Official A2A SDK Inheritance (`RewindA2AExecutor(AgentExecutor)`)
    a2a_state = {"deployed_pods": []}

    @rewind_tool(compensate=lambda args, res: a2a_state["deployed_pods"].remove(res["pod"]))
    def deploy_pod(pod_name: str) -> dict[str, str]:
        a2a_state["deployed_pods"].append(pod_name)
        return {"pod": pod_name}

    a2a_exec = RewindA2AExecutor(lambda _msg: deploy_pod("pod-alpha-1"), name="k8s-a2a-agent")
    assert isinstance(a2a_exec, OfficialA2AExecutor)

    from a2a.server.context import ServerCallContext
    from a2a.server.events import InMemoryQueueManager

    req_ctx = A2ARequestContext(
        call_context=ServerCallContext(),
        task_id="task_k8s_01",
        context_id="ctx_01",
    )
    ev_queue = await InMemoryQueueManager().create_or_tap("task_k8s_01")

    await a2a_exec.execute(req_ctx, ev_queue)
    assert a2a_state["deployed_pods"] == ["pod-alpha-1"]
    assert "task_k8s_01" in a2a_exec.task_undo_tokens

    # When A2A `cancel(context, event_queue)` is called, the deployed pod is automatically rolled back!
    await a2a_exec.cancel(req_ctx, ev_queue)
    assert a2a_state["deployed_pods"] == []

    # 3. Verify Official LangChain (`Runnable`) and LangGraph (`StateGraph`, `ToolNode`) Inheritance
    from typing import TypedDict
    from langchain_core.runnables import Runnable as OfficialLangChainRunnable
    from langgraph.graph import END, START, StateGraph as OfficialLangGraphStateGraph
    from langgraph.prebuilt import ToolNode as OfficialLangGraphToolNode
    from agent_rewind import RewindStateGraph, RewindToolNode, rewind_runnable

    class GState(TypedDict):
        val: int

    r_runnable = rewind_runnable(lambda x: x, name="lc_runnable")
    assert isinstance(r_runnable, OfficialLangChainRunnable)

    r_graph_builder = RewindStateGraph(GState)
    assert isinstance(r_graph_builder, OfficialLangGraphStateGraph)
    r_graph_builder.add_node("step", lambda s: {"val": s["val"] + 1})
    r_graph_builder.add_edge(START, "step")
    r_graph_builder.add_edge("step", END)
    compiled_graph = r_graph_builder.compile()
    assert isinstance(compiled_graph, OfficialLangChainRunnable)

    r_tool_node = RewindToolNode([r_runnable])
    assert isinstance(r_tool_node, OfficialLangGraphToolNode)









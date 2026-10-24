"""Example 09: LangChain RemoteRunnable + AgentExecutor with Server-Defined Compensation.

Demonstrates how a client using LangChain's `RemoteRunnable` and `AgentExecutor`
can execute multi-step workflows where:
  1. Compensations are defined INSIDE the remote server (`RewindServer`).
  2. The client has ZERO compensation code—it only wraps remote runnables with `rewind_tools(...)`.
  3. The LLM dynamically decides which tools to call and in what order at runtime.
  4. When a later tool fails (even if `AgentExecutor` catches the exception internally),
     `with AgentRewind():` automatically sends in-band rollback tokens back to the
     remote servers in strict LIFO order, restoring 100% consistent state.
"""

from typing import Any

from agent_rewind import AgentRewind, RewindServer, rewind_tools

# ============================================================================
# PART 1: REMOTE SERVERS (e.g., FastAPI / LangServe / MCP Microservices)
# Compensation logic and pre-state snapshots live strictly on the server!
# ============================================================================

remote_service = RewindServer(name="enterprise-remote-tools")

# Simulated remote databases inside the microservice
REMOTE_ORDERS_DB: dict[str, dict[str, Any]] = {}
REMOTE_INVENTORY_DB: dict[str, int] = {"SKU-GPU-H100": 4}


async def undo_create_order(args: dict[str, Any], result: dict[str, Any], pre_state: Any) -> None:
    order_id = result["order_id"]
    REMOTE_ORDERS_DB.pop(order_id, None)
    print(f"  [Remote Server Rollback] Deleted order '{order_id}' from REMOTE_ORDERS_DB")


@remote_service.tool(
    name="create_remote_order",
    description="Creates a customer order on the remote CRM service.",
    compensate=undo_create_order,
)
async def create_remote_order(customer: str, item_sku: str, quantity: int) -> dict[str, Any]:
    order_id = f"ORD-{len(REMOTE_ORDERS_DB) + 101}"
    REMOTE_ORDERS_DB[order_id] = {"customer": customer, "sku": item_sku, "qty": quantity}
    print(f"  [Remote Server Forward] Created order '{order_id}' for {customer}")
    return {"order_id": order_id, "status": "CREATED"}


async def snapshot_inventory(args: dict[str, Any]) -> int:
    return REMOTE_INVENTORY_DB.get(args["item_sku"], 0)


async def undo_reserve_inventory(args: dict[str, Any], result: dict[str, Any], pre_state: int) -> None:
    sku = args["item_sku"]
    REMOTE_INVENTORY_DB[sku] = pre_state
    print(f"  [Remote Server Rollback] Restored inventory for '{sku}' back to {pre_state}")


@remote_service.tool(
    name="reserve_remote_inventory",
    description="Reserves warehouse stock on the remote ERP service.",
    compensate=undo_reserve_inventory,
    pre_state=snapshot_inventory,
)
async def reserve_remote_inventory(item_sku: str, quantity: int) -> dict[str, Any]:
    REMOTE_INVENTORY_DB[item_sku] -= quantity
    print(
        f"  [Remote Server Forward] Reserved {quantity}x '{item_sku}' "
        f"(Remaining: {REMOTE_INVENTORY_DB[item_sku]})"
    )
    return {"sku": item_sku, "remaining": REMOTE_INVENTORY_DB[item_sku]}


@remote_service.tool(
    name="process_remote_payment",
    description="Charges the customer's corporate card on the payment service.",
)
async def process_remote_payment(order_id: str, amount: float) -> dict[str, Any]:
    print(f"  [Remote Server Forward] Attempting to charge ${amount} for {order_id}...")
    raise RuntimeError("502 Bad Gateway: Upstream Payment Processor Timeout")


# ============================================================================
# PART 2: CLIENT APPLICATION (LangChain RemoteRunnable + AgentExecutor style)
# Notice: The client defines ZERO compensation functions!
# ============================================================================


class SimulatedAgentExecutor:
    """Simulates LangChain's `AgentExecutor(agent=..., tools=tools)` loop.

    Important real-world behavior: LangChain's `AgentExecutor` often catches tool
    exceptions internally or executes LLM-selected tool calls dynamically.
    """

    def __init__(self, tools: list[Any]) -> None:
        self.tool_map = {t.name: t for t in tools}

    def invoke(self, inputs: dict[str, Any]) -> dict[str, Any]:
        print(f"[AgentExecutor] Received user prompt: {inputs['input']!r}")

        # The LLM dynamically decides which tools to call and in what order:
        llm_planned_calls = [
            ("create_remote_order", {"customer": "Acme Corp", "item_sku": "SKU-GPU-H100", "quantity": 2}),
            ("reserve_remote_inventory", {"item_sku": "SKU-GPU-H100", "quantity": 2}),
            ("process_remote_payment", {"order_id": "ORD-101", "amount": 58000.0}),
        ]

        for tool_name, tool_input in llm_planned_calls:
            tool = self.tool_map[tool_name]
            try:
                tool.invoke(tool_input)
            except Exception as exc:
                # Even if AgentExecutor catches the exception and returns an error string,
                # AgentRewind detects the unrecovered step failure and rewinds on exit!
                print(f"[AgentExecutor] Caught tool error from '{tool_name}': {exc}")
                return {"output": f"Workflow aborted because '{tool_name}' failed."}

        return {"output": "All remote tools succeeded!"}


def main() -> None:
    print("=== LangChain RemoteRunnable + AgentExecutor Integration Demo ===")

    # 1. Connect to remote tools (just like `RemoteRunnable("https://.../tool_name")`)
    remote_order_tool = remote_service.as_remote_runnable("create_remote_order")
    remote_inventory_tool = remote_service.as_remote_runnable("reserve_remote_inventory")
    remote_payment_tool = remote_service.as_remote_runnable("process_remote_payment")

    # 2. Wrap remote tools with `rewind_tools(...)` — zero compensation code on the client!
    tools = rewind_tools([
        remote_order_tool,
        remote_inventory_tool,
        remote_payment_tool,
    ])

    # 3. Create the AgentExecutor
    agent_executor = SimulatedAgentExecutor(tools=tools)

    # 4. Run `agent_executor.invoke(...)` inside `with AgentRewind() as tx:`
    with AgentRewind() as tx:
        response = agent_executor.invoke({
            "input": "Order 2x SKU-GPU-H100 for Acme Corp, reserve stock, and charge the card."
        })

    print(f"\nAgent Final Output : {response['output']}")
    print(f"Transaction State  : {tx.state.value}")
    print(f"Remote Orders DB   : {REMOTE_ORDERS_DB} (Expected: {{}})")
    print(f"Remote Inventory   : {REMOTE_INVENTORY_DB} (Expected: {{'SKU-GPU-H100': 4}})")


if __name__ == "__main__":
    main()

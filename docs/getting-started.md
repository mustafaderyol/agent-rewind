# Getting Started with AgentRewind

`agent-rewind` is a zero-dependency Python runtime that brings **ACID-style transactional rollback, outbox buffering, deterministic idempotency, and dual-LLM security** to AI Agents and Model Context Protocol (MCP) tools.

---

## 1. Installation

### Standard Installation (Zero External Dependencies)
```bash
pip install agent-rewind
# or with uv:
uv add agent-rewind
```

### Optional Async HTTP Extras (for High-Throughput Remote MCP Pools)
```bash
pip install "agent-rewind[http]"
```

---

## 2. Core Concepts in 60 Seconds

| Concept | Class / Decorator | Purpose |
| :--- | :--- | :--- |
| **Transaction Context** | `async with AgentRewind() as tx:` | Starts a transactional unit of work. Automatically rolls back all completed actions if an exception escapes the `with` block. |
| **Reversible Tool** | `@rewind_tool(compensate=fn)` | Wraps a local or remote function. Pushes `compensate` onto the LIFO Undo Stack as soon as the forward call succeeds. |
| **Deferred Tool (Outbox)** | `@rewind_tool(deferred=True)` | Buffers irreversible side effects (emails, Slack messages, webhooks) in memory and only flushes them **after** the entire transaction commits. |
| **Action Receipt** | `ActionReceipt(result=..., undo_args=...)` | Returned by forward functions when the undo handler requires generated IDs (e.g., `order_id`, `reservation_id`) that were not in the input arguments. |
| **Savepoint** | `async with tx.savepoint("name"):` | Creates a nested checkpoint. If an error occurs inside the savepoint, only steps inside that savepoint are rolled back. |

---

## 3. Your First Transaction

Create a file named `quickstart.py`:

```python
import asyncio
from agent_rewind import ActionReceipt, AgentRewind, rewind_tool


# 1. Define a reversible database/API action
async def delete_user_record(user_id: str) -> None:
    print(f"  [UNDO] Deleted user record: {user_id}")


@rewind_tool(compensate=delete_user_record)
async def create_user_record(email: str) -> ActionReceipt:
    user_id = f"usr_{email.split('@')[0]}"
    print(f"  [EXEC] Created user record: {user_id}")
    return ActionReceipt(
        result={"user_id": user_id, "email": email},
        undo_args={"user_id": user_id},
    )


# 2. Define an irreversible notification (Deferred Outbox)
@rewind_tool(deferred=True)
async def send_welcome_email(email: str) -> dict:
    print(f"  [OUTBOX FLUSH] Sent welcome email to {email}")
    return {"sent": True}


# 3. Run inside an AgentRewind transaction
async def onboard_customer(email: str, simulate_failure: bool = False) -> None:
    async with AgentRewind(tx_id="tx-onboard-01") as tx:
        user = await tx.run(create_user_record, email=email)
        await tx.run(send_welcome_email, email=email)

        if simulate_failure:
            raise RuntimeError("Downstream KYC verification failed!")


async def main() -> None:
    print("--- Scenario A: Happy Path ---")
    await onboard_customer("alice@example.com", simulate_failure=False)

    print("\n--- Scenario B: Failure & Automatic Rollback ---")
    try:
        await onboard_customer("bob@example.com", simulate_failure=True)
    except RuntimeError as err:
        print(f"  [CAUGHT] {err}")


if __name__ == "__main__":
    asyncio.run(main())
```

### Output:
```text
--- Scenario A: Happy Path ---
  [EXEC] Created user record: usr_alice
  [OUTBOX FLUSH] Sent welcome email to alice@example.com

--- Scenario B: Failure & Automatic Rollback ---
  [EXEC] Created user record: usr_bob
  [UNDO] Deleted user record: usr_bob
  [CAUGHT] Downstream KYC verification failed!
```
Notice that in **Scenario B**, `send_welcome_email` was **never executed** because it was buffered in the Outbox and discarded when the transaction aborted!

---

## 4. Next Steps

- **[Framework Integrations](framework-integrations.md):** Connect `AgentRewind` to OpenAI, Anthropic, LangGraph, PydanticAI, FastMCP, and FastAPI.
- **[Storage & Transports](storage-and-transports.md):** Configure zero-DB journals (`InMemoryBackend`, `JSONFileBackend`, `CallbackBackend`) and custom HTTP transports.
- **[Scenarios Catalog](scenarios-catalog.md):** Explore end-to-end patterns for e-commerce, fintech wire transfers, cloud provisioning, and remote MCP auto-discovery.

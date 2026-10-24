# Decorators and ActionReceipts

`agent-rewind` provides three ergonomic patterns to bind compensation logic to any synchronous or asynchronous Python function.

---

## Pattern 1: Inline `@rewind_tool(compensate=...)`

```python
from agent_rewind import AgentRewind, rewind_tool

@rewind_tool(
    pre_state=lambda args: erp.get_stock(args["sku"]),
    compensate=lambda args, res, pre_stock: erp.set_stock(args["sku"], pre_stock),
)
async def adjust_stock(sku: str, delta: int) -> dict:
    return await erp.increment_stock(sku, delta)
```

* **Smart Parameter Arity:** Your `compensate` function can accept `()`, `(args)`, `(args, result)`, or `(args, result, pre_state)`. `agent-rewind` inspects the function signature and passes the exact arguments requested.

---

## Pattern 2: Companion `@tool.compensate` Decorator

Ideal when your rollback logic is multi-line:

```python
@rewind_tool
async def create_return_ticket(order_id: str) -> dict:
    return await crm.create_return(order_id)

@create_return_ticket.compensate
async def _undo_return_ticket(args: dict, result: dict) -> dict:
    return await crm.delete_return(
        return_id=result["return_id"],
        restore_status=result["previous_order_status"],
    )
```

---

## Pattern 3: Returning a Self-Contained `ActionReceipt`

Ideal when the undo closure is easiest to construct right inside the forward function body:

```python
from agent_rewind import ActionReceipt, rewind_tool

@rewind_tool
async def provision_vm(name: str) -> ActionReceipt[dict]:
    vm = await cloud.create_instance(name)
    return ActionReceipt(
        result=vm,
        undo=lambda: cloud.terminate_instance(vm["instance_id"]),
    )
```

---

## Outbox Buffering for Irreversible Tools (`deferred=True`)

```python
@rewind_tool(deferred=True)
async def send_customer_email(to: str, subject: str, body: str) -> dict:
    return await mailer.send(to, subject, body)
```
When called inside `async with AgentRewind():`, `send_customer_email` is **queued in the Outbox** and returns a simulated acknowledgment to the agent. It is only executed if the entire transaction block exits cleanly without error.

---

## Optimistic Concurrency & Dirty-Write Detection (`verify_unmodified=True`)

What if an external user modifies a resource *after* Step 2 completes, right before Step 3 crashes and triggers rollback? Blindly restoring `pre_state` would overwrite the external user's update!

```python
@rewind_tool(
    pre_state=lambda args: erp.get_stock(args["sku"]),
    current_state=lambda args, res: erp.get_stock(args["sku"]),
    verify_unmodified=True,
    compensate=lambda args, res, pre: erp.set_stock(args["sku"], pre),
)
async def restock(sku: str, qty: int) -> dict:
    return await erp.increment_stock(sku, qty)
```
Before executing `compensate`, `agent-rewind` compares `current_state` against the `post_state` recorded right after `restock` ran. If they differ, it raises `StateConflictError` instead of corrupting concurrent data.

---

## Dynamic LLM Tool Selection & Prerequisite Guards (`requires_prior`)

Because `AgentRewind` tracks active transactions via Python `contextvars` and maintains a dynamic LIFO compensation stack, your agent does **not** need a hardcoded sequence of function calls. Whatever order the LLM dynamically chooses in its ReAct loop (`A -> B -> C` or `B -> A`), `AgentRewind` automatically records and rewinds in exact reverse order (`C -> B -> A` or `A -> B`).

To prevent the LLM from skipping mandatory prerequisite steps defined in your prompt, attach `requires_prior`:

```python
@rewind_tool(
    requires_prior=["crm_create_return", "erp_verify_stock"],
    compensate=reverse_refund,
)
async def payment_issue_refund(order_id: str, amount: float) -> dict:
    return await payment.refund(order_id, amount)
```

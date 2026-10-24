# Real-World Scenarios Catalog

This guide walks through 7 production patterns where AI Agents fail without transactional guarantees—and how `agent-rewind` solves each one. Every scenario has a runnable script in the [`examples/`](../examples/) directory.

---

## Scenario 1: Local Decorator & ActionReceipts
**File:** [`examples/01_quickstart_decorator.py`](../examples/01_quickstart_decorator.py)

- **Problem:** An agent creates a resource (e.g., an order or database row) that generates a server-side UUID (`res_901`). When a later step fails, the rollback function needs `res_901`, which wasn't part of the original input arguments.
- **Solution:** Return `ActionReceipt(result=..., undo_args={"reservation_id": "res_901"})`. `AgentRewind` transparently returns `.result` to your agent while binding `.undo_args` to the LIFO compensation stack.

---

## Scenario 2: Distributed 4-Service Saga
**File:** [`examples/02_distributed_four_systems_saga.py`](../examples/02_distributed_four_systems_saga.py)

- **Problem:** An enterprise order fulfillment agent coordinates across **CRM**, **ERP Inventory**, **Payment Gateway**, and **Notification Service**. If the Payment Gateway rejects the credit card (`402 Insufficient Funds`), the CRM deal remains stuck in `RESERVED` and inventory remains locked in the ERP.
- **Solution:** Wrap all 4 calls in `AgentRewind`. CRM and ERP revert in reverse order (`ERP -> CRM`), and the Notification service (marked `deferred=True`) never sends a false confirmation email.

---

## Scenario 3: Zero-Config Remote MCP Auto-Discovery
**File:** [`examples/03_remote_mcp_autodiscovery.py`](../examples/03_remote_mcp_autodiscovery.py)

- **Problem:** Your agent connects dynamically to remote MCP servers via URLs. You don't have local Python functions to decorate with `@rewind_tool`.
- **Solution:** `RemoteMCPGateway.discover(urls)` queries `tools/list` on each server, automatically pairs forward tools (`reserve_stock`) with semantic compensation tools (`release_stock`), detects primary keys (`reservation_id`), and flags messaging tools (`send_email`) as `deferred=True`.

---

## Scenario 4: Live Packet Trace & Timing Inspector
**File:** [`examples/04_live_packet_trace_simulation.py`](../examples/04_live_packet_trace_simulation.py)

- **Problem:** Debugging distributed agent rollbacks is opaque without packet-level visibility.
- **Solution:** Uses an instrumented JSON-RPC transport to log microsecond timestamps, HTTP status codes, Outbox buffering, and LIFO compensation packets across 4 live microservices.

---

## Scenario 5: Nested Savepoints & Partial Rollback (Graceful Degradation)
**File:** [`examples/05_savepoints_and_partial_rollback.py`](../examples/05_savepoints_and_partial_rollback.py)

- **Problem:** An e-commerce agent reserves warehouse inventory, books a primary express courier, and applies a loyalty voucher. If the express courier API is down, you don't want to abort the entire customer order—you just want to roll back the express courier step and fall back to standard ground shipping.
- **Solution:** Wrap the express courier attempt in `async with tx.savepoint("express_shipping"):`. When it raises an error, only the steps inside that savepoint are undone, preserving the outer inventory lock so the agent can seamlessly switch to standard shipping.

---

## Scenario 6: Financial Idempotency, Policy Ceiling & Human-in-the-Loop
**File:** [`examples/06_financial_idempotency_and_hitl.py`](../examples/06_financial_idempotency_and_hitl.py)

- **Problem:** In financial workflows:
  1. An LLM retry loop might call `wire_transfer(iban, amount=5000)` twice with the exact same arguments, double-charging the account.
  2. A prompt injection or hallucination might trick the agent into transferring `$500,000`.
  3. High-value transfers require human compliance approval before execution.
- **Solution:**
  - **Idempotency Guard:** Identical `(tool_name, canonical_kwargs)` calls inside the same transaction return the cached receipt without re-invoking the bank API.
  - **Policy Ceiling:** `@rewind_tool(max_value={"amount": 10000.0})` trips a `PolicyViolationError` before any network call is made.
  - **HITL Gate:** `@rewind_tool(require_approval=True)` pauses execution and queries `approval_handler(tool_name, kwargs)`.

---

## Scenario 7: Dead-Letter Queue (DLQ), Crash Recovery & Server Leases
**File:** [`examples/07_dlq_and_crash_recovery.py`](../examples/07_dlq_and_crash_recovery.py)

- **Problem:** What happens if the rollback call itself fails due to a transient network outage, or if the agent container is killed (`SIGKILL`) mid-flight?
- **Solution:**
  1. **Exponential Backoff + DLQ:** Failed compensations retry automatically (`compensation_retries=2`), and if exhausted, persist a `DLQRecord` that can be replayed via `await AgentRewind.replay_dlq(journal, tool_registry)`.
  2. **Crash Recovery:** `await AgentRewind.recover_from_wal(journal, tool_registry)` scans `JSONFileBackend` on startup and rolls back interrupted `IN_FLIGHT` transactions.
  3. **Server-Side Lease TTL:** `ServerLeaseManager` automatically releases locked server resources when their TTL expires without a commit.

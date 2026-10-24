# Security & `PolicyGuard`

`agent-rewind` implements **CaMeL-inspired Data-Flow Isolation** to stop Indirect Prompt Injection attacks mid-transaction and automatically unwind preceding steps.

---

## 1. Quarantined Extraction & Recursive Taint Tracking

When an agent must extract structured fields from untrusted external text (e.g., customer notes, emails, web pages), use `tx.quarantine_extract()`:

```python
from pydantic import BaseModel

class RefundNoteSchema(BaseModel):
    requested_iban: str
    reason_summary: str

# Runs in a zero-tool sandbox and stamps the resulting Ref as TaintLevel.UNTRUSTED
note_ref = await tx.quarantine_extract(raw_customer_note, RefundNoteSchema)
```

* **Recursive Taint Unwrapping:** Even if a manipulated agent nests `note_ref.attr("requested_iban")` deep inside a dictionary or list (`payload={"transfers": [{"dest": note_ref.attr("requested_iban")}]}`), `DataVault` recursively unwraps the structure and marks `payload` as `TaintLevel.UNTRUSTED`.

---

## 2. Provenance Endorsement (`require_trusted_match`)

If a tool parameter is protected by `deny_tainted=["iban"]`, any unverified `UNTRUSTED` value is blocked. However, if you register a `require_trusted_match` rule against a trusted source of truth (such as the customer's verified IBAN in CRM):

```python
policy = PolicyGuard().require_trusted_match(
    tool_name="payment_process_refund",
    arg_name="iban",
    trusted_value_or_ref=crm_record["registered_iban"],
)
```
* **When the extracted IBAN matches the trusted CRM IBAN:** `PolicyGuard` **endorses (declassifies)** the parameter and allows execution.
* **When a Prompt Injection alters the IBAN (`TR99HACKER...`):** `PolicyGuard` immediately raises `SecurityManipulationError`, halts execution before the payment API is called, discards Outbox emails, and rewinds Steps 2 and 1!

---

## 3. Financial Ceiling Circuit Breakers (`max_value`)

```python
policy = PolicyGuard().max_value("wire_transfer", "amount", limit=25000.0)
```

---

## 4. Human-in-the-Loop (HITL) Approval Gates (`require_approval`)

```python
async def ask_supervisor(tool_name: str, args: dict) -> bool:
    # Integrate Slack/Webhook/UI approval prompt here
    return args["amount"] <= 50000.0

policy = PolicyGuard().require_approval(
    "wire_transfer",
    approval_handler=ask_supervisor,
    when=lambda args: args["amount"] > 10000.0,
)
```
If the supervisor returns `False`, `agent-rewind` aborts the transaction and rolls back all previous steps automatically.

---

## 5. Execution Sequence & Prerequisite Enforcement (`require_sequence` & `requires_prior`)

When an LLM decides which tools to call dynamically based on a system prompt (e.g., *"First create a CRM return, then verify warehouse stock, and finally issue a refund"*), a hallucinating or prompt-injected LLM might skip a required verification step and call the payment tool directly.

You can enforce strict execution ordering either via `PolicyGuard.require_sequence(...)` or `@rewind_tool(requires_prior=[...])`:

```python
# Option A: Enforce an ordered workflow sequence on PolicyGuard
policy = PolicyGuard().require_sequence([
    "step1_open_return",
    "step2_verify_warehouse",
    "step3_send_refund",
])

# Option B: Declare prerequisite tools directly on the decorator
@rewind_tool(
    requires_prior=["step1_open_return", "step2_verify_warehouse"],
    compensate=reverse_refund,
)
async def step3_send_refund(order_id: str, amount: float) -> dict:
    ...
```
If the LLM attempts to call `step3_send_refund` before `step2_verify_warehouse` has succeeded, `agent-rewind` immediately raises `SecurityManipulationError` and automatically rewinds `step1_open_return`!

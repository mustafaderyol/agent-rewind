# Welcome to the `agent-rewind` Wiki

**`agent-rewind`** is a zero-trust transactional runtime for autonomous AI agents and Model Context Protocol (MCP) tool workflows.

## The Architectural Problem We Solve

Suppose an AI agent executes a 4-step workflow across 4 independent systems:
1. **System 1 (CRM):** Opens a return ticket (`RET-101`).
2. **System 2 (ERP):** Increments warehouse stock (`100 -> 101`) and triggers a customer confirmation email.
3. **System 3 (Banking API):** Reads an untrusted customer note and issues a bank refund.
4. **System 4 (Audit/Webhook):** Finalizes the ledger.

**What happens when the agent crashes at Step 3 (`503 Timeout`) or gets manipulated via Indirect Prompt Injection at Step 3?**
- Standard agents leave Systems 1 and 2 in a corrupted state, send premature emails that cannot be unsent, or transfer money to an attacker's IBAN.
- Asking a compromised/poisoned LLM to "undo your previous steps" is a critical security flaw—a manipulated LLM cannot be trusted to execute rollbacks.

**`agent-rewind` solves this via "Untrusted Reasoning, Trusted Execution":**
- **Deterministic LIFO Saga Compensation:** Every forward tool is paired with a deterministic compensation closure sealed at execution time.
- **Outbox Commit Buffer (`deferred=True`):** Irreversible actions (emails, webhooks, Slack notifications) are buffered in memory and only flushed if all steps succeed.
- **CaMeL Data-Flow Isolation (`QuarantinedExtractor` & `PolicyGuard`):** Untrusted text is parsed in a zero-privilege sandbox, stamped as `UNTRUSTED`, and blocked from hijacking sensitive arguments (`iban`, `account`, `role`).
- **Zero-Config Remote MCP Auto-Discovery:** Point `RemoteMCPGateway.discover(urls)` at standard MCP servers and let it automatically infer forward tools, pre-state getters, compensators, and Outbox deferral.

---

## Wiki Navigation

* **[Architecture Deep Dive](Architecture-Deep-Dive)** — LIFO Saga Stack, Savepoints, Outbox Buffer, and Tamper-Evident Audit Hash Chain.
* **[Decorators and ActionReceipts](Decorators-and-Receipts)** — 3 ergonomic patterns for registering tool compensations and pre-state snapshots.
* **[Remote MCP Auto-Discovery](Remote-MCP-Auto-Discovery)** — Connecting to distributed MCP servers with URL-only discovery and Server Lease TTLs.
* **[Security and PolicyGuard](Security-and-Policy-Guard)** — Recursive taint tracking, provenance endorsement, financial circuit breakers, and Human-in-the-Loop (HITL) approval gates.
* **[Benchmarks](Benchmarks)** — Empirical evaluation across 1,000 distributed chaos & security workflows + micro-benchmarks.

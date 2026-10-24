# Security Policy

## Threat Model Overview

`agent-rewind` is specifically designed to defend multi-step autonomous AI agent workflows against:
1. **Indirect Prompt Injection (Step-N Manipulation):** When an agent reads untrusted external data (e.g., customer notes, emails, web pages) mid-workflow and an attacker attempts to hijack downstream tool calls (such as diverting a refund IBAN or escalating permissions).
2. **Compromised Rollback Reasoning:** A poisoned LLM must never be trusted to execute its own rollback. `agent-rewind` executes all compensations deterministically outside the LLM context using sealed pre-state snapshots and LIFO closures.
3. **Nested Taint Bypass:** `DataVault` recursively inspects nested dictionaries, lists, and tuples to ensure `UNTRUSTED` references cannot bypass `deny_tainted` policies.
4. **Audit Log Tampering:** Every `StepRecord` is cryptographically chained (`prev_hash` -> `step_hash`) using SHA-256 and verified via `tx.report.verify_audit_integrity()`.

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 0.2.x   | :white_check_mark: |
| < 0.2.0 | :x:                |

## Reporting a Vulnerability

If you discover a security vulnerability (such as a taint-tracking bypass or compensation execution flaw), please open a private security advisory on GitHub rather than a public issue. We aim to respond within 48 hours.

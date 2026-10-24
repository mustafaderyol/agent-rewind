"""Example 06: Financial Idempotency, Max-Value Ceiling & Human-in-the-Loop (HITL).

Demonstrates three financial-grade safeguards built into `agent-rewind`:
1. Deterministic SHA-256 Idempotency Guard (prevents double-charging when an LLM retries a tool call).
2. Financial Ceiling Circuit Breaker (`max_value={"amount": 10000.0}`).
3. Human-in-the-Loop Approval Gate (`require_approval=True`) + Hash-Chained Audit Verification.
"""

from __future__ import annotations

import asyncio

from rich.console import Console
from rich.panel import Panel

from agent_rewind import (
    ActionReceipt,
    AgentRewind,
    SecurityManipulationError,
    rewind_tool,
)

console = Console()

BANK_LEDGER: list[dict] = []


async def reverse_wire(tx_ref: str) -> dict:
    BANK_LEDGER[:] = [item for item in BANK_LEDGER if item["tx_ref"] != tx_ref]
    return {"reversed": tx_ref}


@rewind_tool(
    compensate=reverse_wire,
    max_value={"amount": 10000.0},
    require_approval=True,
)
async def execute_wire_transfer(iban: str, amount: float) -> ActionReceipt:
    tx_ref = f"WIRE-{len(BANK_LEDGER) + 1}"
    entry = {"tx_ref": tx_ref, "iban": iban, "amount": amount}
    BANK_LEDGER.append(entry)
    return ActionReceipt(result=entry, undo_args={"tx_ref": tx_ref})


async def compliance_officer_callback(tool_name: str, kwargs: dict) -> bool:
    console.print(
        f"  [bold cyan][HITL COMPLIANCE GATE][/bold cyan] Approving `{tool_name}` with args: {kwargs}"
    )
    return True


async def main() -> None:
    console.print(Panel("[bold]Part A: Idempotency Guard + HITL Approval[/bold]", border_style="green"))
    async with AgentRewind(
        tx_id="tx_treasury_01",
        approval_handler=compliance_officer_callback,
    ) as tx:
        # LLM calls wire transfer twice with identical arguments (e.g., retry on timeout)
        r1 = await execute_wire_transfer(iban="DE89370400440532013000", amount=4500.0)
        r2 = await execute_wire_transfer(iban="DE89370400440532013000", amount=4500.0)

        assert r1 == r2
        console.print(f"  First Call Result : {r1}")
        console.print(f"  Second Call Result: {r2} (Served from Idempotency Cache — 0 duplicate charges!)")
        console.print(f"  Bank Ledger Count : {len(BANK_LEDGER)} entry")
        console.print(
            f"  Audit Chain Valid : {tx.journal.verify_audit_integrity('tx_treasury_01')}"
        )

    console.print(
        Panel(
            "[bold]Part B: Financial Ceiling Circuit Breaker (`max_value=10000.0`)[/bold]",
            border_style="yellow",
        )
    )
    try:
        async with AgentRewind(
            tx_id="tx_treasury_overflow",
            approval_handler=compliance_officer_callback,
        ):
            # First transfer succeeds ($3,000)
            await execute_wire_transfer(iban="DE89370400440532013000", amount=3000.0)
            # Second transfer exceeds the $10,000 ceiling ($25,000) -> Trips Circuit Breaker & Rolls Back Step 1!
            await execute_wire_transfer(iban="DE89370400440532013000", amount=25000.0)
    except SecurityManipulationError as err:
        console.print(f"  [bold red]🚨 Blocked by Policy Ceiling:[/bold red] {err}")
        console.print(
            f"  [bold green]✓ Step 1 ($3,000) automatically rolled back! Current Ledger:[/bold green] {BANK_LEDGER}"
        )


if __name__ == "__main__":
    asyncio.run(main())

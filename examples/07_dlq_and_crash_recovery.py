"""Example 07: Dead-Letter Queue (DLQ) Replay, JSON Journal Crash Recovery & Server Leases.

Demonstrates:
1. Exponential backoff retries when a rollback call itself experiences transient failure,
   followed by Dead-Letter Queue (DLQ) persistence and `AgentRewind.replay_dlq()`.
2. Server-side Dead-Man's Switch (`ServerLeaseManager`) that auto-releases locked cloud
   resources when an agent's lease TTL expires.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from agent_rewind import (
    ActionReceipt,
    AgentRewind,
    CompensationFailedError,
    JSONFileBackend,
    ServerLeaseManager,
    rewind_tool,
)

console = Console()

CLOUD_VMS: dict[str, str] = {}
TRANSIENT_API_DOWN = True


async def terminate_cloud_vm(vm_id: str) -> dict:
    if TRANSIENT_API_DOWN:
        raise ConnectionError("Cloud Provider Control Plane 503 Unavailable during rollback")
    CLOUD_VMS.pop(vm_id, None)
    return {"terminated": vm_id}


@rewind_tool(
    compensate=terminate_cloud_vm,
    compensation_retries=2,
    compensation_backoff_ms=10.0,
)
async def provision_cloud_vm(name: str) -> ActionReceipt:
    vm_id = f"vm_{name}"
    CLOUD_VMS[vm_id] = "RUNNING"
    return ActionReceipt(result={"vm_id": vm_id}, undo_args={"vm_id": vm_id})


@rewind_tool
async def attach_failing_storage() -> dict:
    raise RuntimeError("Storage volume attachment failed")


async def main() -> None:
    global TRANSIENT_API_DOWN
    journal_path = Path("/tmp/agent_rewind_dlq_demo.json")
    journal_path.unlink(missing_ok=True)
    journal = JSONFileBackend(journal_path)

    console.print(
        Panel(
            "[bold]Part 1: Compensation Exhausts Retries -> Routed to Dead-Letter Queue (DLQ)[/bold]",
            border_style="cyan",
        )
    )
    tx = AgentRewind(
        tx_id="tx_cloud_dlq",
        journal=journal,
        close_journal_on_exit=False,
    )
    try:
        async with tx:
            await provision_cloud_vm("gpu-node-01")
            await attach_failing_storage()  # Triggers rollback, but Cloud API is down!
    except CompensationFailedError as comp_err:
        console.print(f"  [bold yellow]⚠ Caught CompensationFailedError:[/bold yellow] {comp_err}")

    console.print(f"  Transaction Final State : [bold yellow]{tx.report.state.value}[/bold yellow]")
    console.print(f"  Orphaned VMs in Cloud   : {CLOUD_VMS}")
    console.print(f"  Pending DLQ Records     : {len(journal.list_dlq())}")

    # Cloud API recovers -> Replay DLQ!
    TRANSIENT_API_DOWN = False
    replayed = await AgentRewind.replay_dlq(
        journal,
        tools=[provision_cloud_vm],
    )
    console.print(f"  [bold green]✓ DLQ Replayed Count    : {len(replayed)}[/bold green]")
    console.print(f"  Cloud VMs After Replay  : {CLOUD_VMS}")

    console.print(
        Panel(
            "[bold]Part 2: Server-Side Lease TTL (Dead-Man's Switch for Crashed Agents)[/bold]",
            border_style="magenta",
        )
    )
    lease_mgr = ServerLeaseManager()
    LOCKED_SEATS = {"SEAT-1A": "LOCKED_BY_AGENT"}

    token = lease_mgr.register_lease(
        undo_fn=lambda: LOCKED_SEATS.update({"SEAT-1A": "AVAILABLE"}),
        ttl_seconds=0.05,
    )
    console.print(f"  Seat State During Active Lease ({token}) : {LOCKED_SEATS['SEAT-1A']}")
    await asyncio.sleep(0.08)
    expired = await lease_mgr.sweep_expired_leases()
    console.print(f"  Expired Leases Swept                       : {expired}")
    console.print(
        f"  [bold green]✓ Seat State After TTL Expiry              : {LOCKED_SEATS['SEAT-1A']}[/bold green]"
    )


if __name__ == "__main__":
    asyncio.run(main())

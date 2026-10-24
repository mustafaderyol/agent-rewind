"""Framework adapters (MCP, OpenAI/PydanticAI) and console visualization utilities."""

from __future__ import annotations

from collections.abc import Callable
import inspect
from typing import Any

from langchain_core.runnables import Runnable
from langchain_core.tools import StructuredTool
from pydantic import TypeAdapter

from agent_rewind.decorators import TransactionalToolSpec, invoke_maybe_async, tx_tool
from agent_rewind.kernel import _run_coro_sync, get_current_transaction
from agent_rewind.models import (
    ActionReceipt,
    EffectType,
    StepStatus,
    TaintLevel,
    TransactionReport,
    TransactionState,
)


async def _call_runnable_or_fn(target: Any, payload: Any = None, **kwargs: Any) -> Any:
    """Invoke a LangChain RemoteRunnable/BaseTool (`.ainvoke`/`.invoke`) or standard Python callable."""
    arg_dict = payload if isinstance(payload, dict) else (kwargs or {"input": payload})
    if hasattr(target, "ainvoke") and callable(target.ainvoke):
        return await target.ainvoke(arg_dict)
    if hasattr(target, "invoke") and callable(target.invoke):
        return target.invoke(arg_dict)
    return await invoke_maybe_async(target, **arg_dict)


class TransactionalRunnable(Runnable[Any, Any]):
    """Official `langchain_core.runnables.Runnable` subclass wrapping a `RemoteRunnable`, `BaseTool`, or callable into `AgentRewind`.

    Supports both synchronous `.invoke({...})` (used by `agent_executor.invoke`) and
    asynchronous `await .ainvoke({...})` (used by `await agent_executor.ainvoke`),
    as well as LCEL `.pipe()` / `|` composition.
    """

    def __init__(
        self,
        runnable: Any,
        *,
        name: str | None = None,
        description: str = "",
        compensate: Any = None,
        pre_state: Any = None,
        deferred: bool = False,
        read_only: bool = False,
        deny_tainted: list[str] | None = None,
        requires_prior: list[str] | None = None,
        max_value: dict[str, float] | None = None,
        require_approval: bool = False,
        timeout: float | None = None,
    ) -> None:
        self.runnable = runnable
        self.name = name or getattr(runnable, "name", None) or "remote_runnable_tool"
        self.description = description or getattr(runnable, "description", "")

        async def _forward(**kwargs: Any) -> Any:
            raw_res = await _call_runnable_or_fn(runnable, kwargs)
            # If the remote server defined compensation internally (MCP `_meta.rewind.undo_token` or `_rewind.undo_token`),
            # automatically bind an ActionReceipt that triggers rollback on the remote runnable!
            if isinstance(raw_res, dict):
                rw_meta = raw_res.get("_meta", {}).get("rewind") or raw_res.get("_rewind") or {}
                u_token = rw_meta.get("undo_token") or rw_meta.get("undoToken")
                if u_token:
                    clean_result = (
                        raw_res["structuredContent"]
                        if "structuredContent" in raw_res
                        else raw_res.get("result")
                    )

                    async def _remote_server_undo() -> Any:
                        return await _call_runnable_or_fn(
                            runnable,
                            {"_rewind_action": "rollback", "undo_token": u_token},
                        )

                    return ActionReceipt(result=clean_result, undo=_remote_server_undo)
            return raw_res

        _forward.__name__ = self.name
        _forward.__doc__ = self.description

        comp_fn = None
        if compensate is not None:
            async def _comp(args: dict[str, Any], result: Any, pre: Any = None) -> Any:
                merged = {**args, **(result if isinstance(result, dict) else {"result": result})}
                if pre is not None:
                    merged["pre_state"] = pre
                if hasattr(compensate, "ainvoke") or hasattr(compensate, "invoke"):
                    return await _call_runnable_or_fn(compensate, merged)
                from agent_rewind.decorators import invoke_compensate_fn
                return await invoke_compensate_fn(compensate, args, result, pre)

            comp_fn = _comp

        pre_fn = None
        if pre_state is not None:
            async def _pre(args: dict[str, Any]) -> Any:
                if hasattr(pre_state, "ainvoke") or hasattr(pre_state, "invoke"):
                    return await _call_runnable_or_fn(pre_state, args)
                return await invoke_maybe_async(pre_state, args)

            pre_fn = _pre

        self._wrapped = tx_tool(
            _forward,
            name=self.name,
            deferred=deferred,
            read_only=read_only,
            compensate=comp_fn,
            pre_state=pre_fn,
            deny_tainted=deny_tainted,
            requires_prior=requires_prior,
            max_value=max_value,
            require_approval=require_approval,
            timeout=timeout,
        )
        self.__tx_spec__: TransactionalToolSpec = self._wrapped.__tx_spec__

    async def ainvoke(self, input_data: dict[str, Any] | Any = None, config: Any = None, **kwargs: Any) -> Any:
        """Async LangChain Runnable interface (`await tool.ainvoke(...)`)."""
        params = dict(input_data) if isinstance(input_data, dict) else (kwargs or {"input": input_data})
        return await self._wrapped(**params)

    def invoke(self, input_data: dict[str, Any] | Any = None, config: Any = None, **kwargs: Any) -> Any:
        """Sync LangChain Runnable interface (`tool.invoke(...)` inside `agent_executor.invoke`)."""
        return _run_coro_sync(self.ainvoke(input_data, config=config, **kwargs))

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if args and isinstance(args[0], dict) and not kwargs:
            return await self.ainvoke(args[0])
        return await self._wrapped(*args, **kwargs)

    def as_langchain_tool(self, args_schema: Any = None) -> Any:
        """Convert this `TransactionalRunnable` into a native LangChain `StructuredTool` (`BaseTool`)."""
        from langchain_core.tools import StructuredTool

        return StructuredTool.from_function(
            func=lambda **kw: self.invoke(kw),
            coroutine=lambda **kw: self.ainvoke(kw),
            name=self.name,
            description=self.description or f"Transactional tool {self.name}",
            args_schema=args_schema,
        )


def rewind_tools(
    runnables_or_tools: list[Any],
) -> list[TransactionalRunnable]:
    """Wrap a list of LangChain `RemoteRunnable`s or tools into `TransactionalRunnable`s in one call."""
    wrapped_list: list[TransactionalRunnable] = []
    for item in runnables_or_tools:
        if isinstance(item, TransactionalRunnable):
            wrapped_list.append(item)
        else:
            wrapped_list.append(rewind_runnable(item))
    return wrapped_list


def rewind_runnable(
    runnable: Any,
    *,
    name: str | None = None,
    description: str = "",
    compensate: Any = None,
    pre_state: Any = None,
    deferred: bool = False,
    read_only: bool = False,
    deny_tainted: list[str] | None = None,
    requires_prior: list[str] | None = None,
    max_value: dict[str, float] | None = None,
    require_approval: bool = False,
    timeout: float | None = None,
) -> TransactionalRunnable:
    """Wrap a LangChain `RemoteRunnable`, `BaseTool`, or external runnable with `AgentRewind` guarantees."""
    return TransactionalRunnable(
        runnable,
        name=name,
        description=description,
        compensate=compensate,
        pre_state=pre_state,
        deferred=deferred,
        read_only=read_only,
        deny_tainted=deny_tainted,
        requires_prior=requires_prior,
        max_value=max_value,
        require_approval=require_approval,
        timeout=timeout,
    )


def to_mcp_tool_schema(
    tool_fn: Callable[..., Any], *, include_tx_metadata: bool = True
) -> dict[str, Any]:
    """Export a @tx_tool function into an MCP-compatible JSON Schema definition.

    Note: The LLM only receives name, description, and inputSchema.
    The `x-transactional` block is strictly for the MCP Host / Orchestrator.
    """
    spec: TransactionalToolSpec | None = getattr(tool_fn, "__tx_spec__", None)
    raw_fn = getattr(tool_fn, "__raw_fn__", tool_fn)
    name = spec.name if spec else raw_fn.__name__
    doc = (inspect.getdoc(raw_fn) or "").strip()
    sig = inspect.signature(raw_fn)

    properties: dict[str, Any] = {}
    required: list[str] = []

    for param_name, param in sig.parameters.items():
        if param_name == "self":
            continue
        ann = param.annotation if param.annotation is not inspect.Parameter.empty else Any
        try:
            prop_schema = TypeAdapter(ann).json_schema()
        except Exception:
            prop_schema = {"type": "string"}
        properties[param_name] = prop_schema
        if param.default is inspect.Parameter.empty:
            required.append(param_name)

    schema: dict[str, Any] = {
        "name": name,
        "description": doc,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required,
        },
    }

    if include_tx_metadata and spec is not None:
        schema["x-transactional"] = {
            "effectType": spec.effect.value,
            "hasCompensation": spec.compensate_fn is not None,
            "taintLevel": spec.taint.value,
            "denyTaintedArgs": spec.deny_tainted,
        }

    return schema


def render_transaction_report(
    report: TransactionReport,
    *,
    console: Any = None,
    title: str | None = None,
) -> None:
    """Render a visual audit table of an AgentTransaction (uses Rich if installed, else pure ASCII)."""
    try:
        from rich.console import Console
        from rich.panel import Panel
        from rich.table import Table
    except ImportError:
        # Pure-Python ASCII fallback when `rich` is not installed
        print(f"=== {title or f'Agent Transaction Audit ({report.tx_id})'} ===")
        print(f"State: {report.state.value} | Aborted Reason: {report.aborted_reason}")
        for s in report.steps:
            print(
                f"  #{s.step_index} {s.tool_name} [{s.effect_type.value}] -> {s.status.value} "
                f"(pre={s.pre_state}, comp={s.compensation_result})"
            )
        return

    con = console or Console()
    state_color = {
        TransactionState.COMMITTED: "bold green",
        TransactionState.ROLLED_BACK: "bold yellow",
        TransactionState.FAILED_ROLLBACK: "bold red",
        TransactionState.ACTIVE: "bold cyan",
    }.get(report.state, "white")

    header = (
        f"[bold]Transaction ID:[/bold] {report.tx_id}   "
        f"[bold]Final State:[/bold] [{state_color}]{report.state.value}[/{state_color}]"
    )
    if report.aborted_reason:
        header += f"\n[bold red]Trigger / Abort Reason:[/bold red] {report.aborted_reason}"

    table = Table(show_header=True, header_style="bold magenta", expand=True)
    table.add_column("#", justify="right", width=3)
    table.add_column("Tool Name", style="cyan")
    table.add_column("Effect Type")
    table.add_column("Taint")
    table.add_column("Pre-State")
    table.add_column("Status")
    table.add_column("Compensation / Notes")

    status_styles = {
        StepStatus.EXECUTED: "[green]EXECUTED[/green]",
        StepStatus.COMPENSATED: "[yellow]ROLLED BACK (COMPENSATED)[/yellow]",
        StepStatus.FLUSHED: "[green]OUTBOX FLUSHED[/green]",
        StepStatus.DISCARDED: "[dim yellow]OUTBOX DISCARDED (NEVER SENT)[/dim yellow]",
        StepStatus.FAILED: "[bold red]FAILED / BLOCKED[/bold red]",
        StepStatus.FAILED_COMPENSATION: "[bold red]COMPENSATION FAILED[/bold red]",
        StepStatus.BUFFERED: "[blue]BUFFERED[/blue]",
    }

    for step in report.steps:
        taint_str = (
            "[red]UNTRUSTED[/red]"
            if step.taint_level.value == "UNTRUSTED"
            else "[green]TRUSTED[/green]"
        )
        notes = ""
        if step.error:
            notes = f"[red]{step.error}[/red]"
        elif step.compensation_result is not None:
            notes = f"[yellow]Undo -> {step.compensation_result}[/yellow]"
        elif step.status == StepStatus.DISCARDED:
            notes = "[dim]Prevented side-effect[/dim]"
        elif step.result is not None:
            notes = str(step.result)[:60]

        table.add_row(
            str(step.step_index),
            step.tool_name,
            step.effect_type.value,
            taint_str,
            str(step.pre_state) if step.pre_state is not None else "-",
            status_styles.get(step.status, step.status.value),
            notes,
        )

    con.print(
        Panel(
            table,
            title=title or f"Agent Transaction Audit ({report.tx_id})",
            subtitle=header,
        )
    )

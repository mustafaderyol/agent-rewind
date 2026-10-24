"""Protocol Extensions built on the Official `mcp` and `a2a-sdk` Libraries.

Built directly on top of:
- **Official Model Context Protocol SDK (`mcp`)**: `https://github.com/modelcontextprotocol/python-sdk`
  (`mcp.server.mcpserver.MCPServer`, `mcp.types.CallToolResult`, `mcp.types.Tool`, `mcp.types.ToolAnnotations`)
- **Official Agent2Agent SDK (`a2a-sdk`)**: `https://github.com/a2aproject/a2a`
  (`a2a.server.agent_execution.AgentExecutor`, `a2a.server.agent_execution.RequestContext`,
   `a2a.server.events.EventQueue`, `a2a.server.tasks.TaskUpdater`, `a2a.types.AgentExtension`)

Key Classes:
1. **`RewindA2AExecutor(a2a.server.agent_execution.AgentExecutor)`**:
   Subclasses the official `a2a-sdk` `AgentExecutor`. Executes any A2A agent inside an `AgentRewind`
   transaction during `execute(context, event_queue)`, exports a composite `undo_token` bound to
   `context.task_id`, and automatically rolls back all internal tool mutations when `cancel(context, event_queue)`
   is invoked via the A2A protocol!
2. **`RewindSubAgentHost`**:
   Wraps a LangGraph sub-agent, LangServe runnable, or A2A handler so all internal `@rewind_tool`
   mutations are bundled into a single composite `undo_token` via `tx.export_undo_lease()`.
3. **`rewind_agent` & `rewind_graph`**:
   Drop-in wrappers for `RemoteRunnable`, A2A agents, and compiled LangGraph `StateGraph` workflows.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from a2a.server.agent_execution import AgentExecutor as OfficialA2AAgentExecutor
from a2a.server.agent_execution import RequestContext as A2ARequestContext
from a2a.server.events import EventQueue as A2AEventQueue
from a2a.server.tasks import TaskUpdater as A2ATaskUpdater
from a2a.types import AgentExtension as A2AAgentExtension
from langchain_core.runnables import Runnable
from langgraph.graph import StateGraph
from langgraph.prebuilt import ToolNode

from agent_rewind.decorators import invoke_maybe_async, tx_tool
from agent_rewind.kernel import AgentRewind, AgentTransaction, _run_coro_sync
from agent_rewind.mcp import ServerLeaseManager
from agent_rewind.models import ActionReceipt

REWIND_A2A_EXTENSION = A2AAgentExtension(
    uri="urn:agent-rewind:a2a:v1",
    description="Transactional LIFO Rewind & Server-Side Compensation Extension for A2A Agents",
    required=False,
)


def extract_rewind_metadata(payload: Any) -> dict[str, Any]:
    """Extract `rewind` metadata from an MCP `CallToolResult`, A2A `Task`, LangChain `AIMessage`, or dict."""
    if payload is None:
        return {}
    # 1. Official `mcp.types.CallToolResult` or `mcp.types.Tool` (`.meta`)
    mcp_meta_attr = getattr(payload, "meta", None)
    if isinstance(mcp_meta_attr, dict) and "rewind" in mcp_meta_attr:
        return dict(mcp_meta_attr["rewind"])

    # 2. LangChain AIMessage / BaseMessage (`response_metadata` or `additional_kwargs`)
    resp_meta = getattr(payload, "response_metadata", None)
    if isinstance(resp_meta, dict) and "rewind" in resp_meta:
        return dict(resp_meta["rewind"])
    add_kw = getattr(payload, "additional_kwargs", None)
    if isinstance(add_kw, dict) and "rewind" in add_kw:
        return dict(add_kw["rewind"])

    # 3. Dict envelopes (MCP `_meta.rewind`, A2A `metadata.rewind`, or `_rewind`)
    if isinstance(payload, dict):
        if "_meta" in payload and isinstance(payload["_meta"], dict) and "rewind" in payload["_meta"]:
            return dict(payload["_meta"]["rewind"])
        if "metadata" in payload and isinstance(payload["metadata"], dict) and "rewind" in payload["metadata"]:
            return dict(payload["metadata"]["rewind"])
        if "_rewind" in payload and isinstance(payload["_rewind"], dict):
            return dict(payload["_rewind"])
        if "output" in payload:
            return extract_rewind_metadata(payload["output"])
        if "result" in payload and isinstance(payload["result"], dict):
            return extract_rewind_metadata(payload["result"])
    return {}


def inject_rewind_metadata(payload: Any, rewind_meta: dict[str, Any]) -> Any:
    """Attach `rewind` metadata into an `AIMessage`, A2A `Task`, MCP `CallToolResult`, or dict non-intrusively."""
    if hasattr(payload, "response_metadata") and isinstance(payload.response_metadata, dict):
        payload.response_metadata["rewind"] = rewind_meta
        return payload
    if isinstance(payload, dict):
        out = dict(payload)
        if "status" in out and "id" in out:
            meta = dict(out.get("metadata") or {})
            meta["rewind"] = rewind_meta
            out["metadata"] = meta
        mcp_meta = dict(out.get("_meta") or {})
        mcp_meta["rewind"] = rewind_meta
        out["_meta"] = mcp_meta
        return out
    return {"content": str(payload), "_meta": {"rewind": rewind_meta}}


# ============================================================================
# 1. Official `a2a-sdk` AgentExecutor Subclass (`RewindA2AExecutor`)
# ============================================================================


class RewindA2AExecutor(OfficialA2AAgentExecutor):
    """Official `a2a.server.agent_execution.AgentExecutor` subclass with transactional rewind.

    Wraps an existing `a2a.server.agent_execution.AgentExecutor` (or any agent callable):
    - During `execute(context, event_queue)`: runs inside `async with AgentRewind() as sub_tx:`,
      exports a composite `undo_token` via `sub_tx.export_undo_lease()`, and maps `context.task_id -> undo_token`.
    - During `cancel(context, event_queue)`: automatically rolls back the lease associated with
      `context.task_id`, undoing all internal `@rewind_tool` mutations made by this A2A agent!
    """

    def __init__(
        self,
        inner_executor_or_fn: OfficialA2AAgentExecutor | Any = None,
        *,
        name: str = "a2a-rewind-agent",
        default_lease_ttl: float = 120.0,
    ) -> None:
        super().__init__()
        self.inner = inner_executor_or_fn
        self.name = name
        self.default_lease_ttl = default_lease_ttl
        self.lease_manager = ServerLeaseManager()
        self.task_undo_tokens: dict[str, str] = {}

    async def execute(
        self,
        context: A2ARequestContext,
        event_queue: A2AEventQueue,
    ) -> None:
        """Official `a2a-sdk` `AgentExecutor.execute` hook wrapped in an `AgentRewind` transaction."""
        task_id = context.task_id or f"task_{uuid4().hex[:8]}"
        context_id = context.context_id or f"ctx_{uuid4().hex[:8]}"
        updater = A2ATaskUpdater(event_queue, task_id, context_id)

        async with AgentRewind(tx_id=f"a2a_{self.name}_{task_id}") as sub_tx:
            if isinstance(self.inner, OfficialA2AAgentExecutor):
                await self.inner.execute(context, event_queue)
            elif self.inner is not None:
                user_input = context.get_user_input() if hasattr(context, "get_user_input") else ""
                await invoke_maybe_async(self.inner, user_input)
                await updater.complete()

            undo_token = sub_tx.export_undo_lease(
                self.lease_manager, ttl_seconds=self.default_lease_ttl
            )
            if undo_token is not None:
                self.task_undo_tokens[task_id] = undo_token

    async def cancel(
        self,
        context: A2ARequestContext,
        event_queue: A2AEventQueue,
    ) -> None:
        """Official `a2a-sdk` `AgentExecutor.cancel` hook — triggers automatic LIFO rollback!"""
        task_id = context.task_id or ""
        meta = context.metadata if hasattr(context, "metadata") and isinstance(context.metadata, dict) else {}
        rw_meta = extract_rewind_metadata({"metadata": meta})
        u_token = rw_meta.get("undo_token") or self.task_undo_tokens.get(task_id)

        if u_token is not None:
            await self.lease_manager.rollback_lease(u_token)
            self.task_undo_tokens.pop(task_id, None)

        if isinstance(self.inner, OfficialA2AAgentExecutor):
            await self.inner.cancel(context, event_queue)
        else:
            context_id = context.context_id or f"ctx_{uuid4().hex[:8]}"
            updater = A2ATaskUpdater(event_queue, task_id, context_id)
            await updater.cancel()


# ============================================================================
# 2. Remote Sub-Agent Host (`RewindSubAgentHost`)
# ============================================================================


class RewindSubAgentHost:
    """Wraps a remote sub-agent (LangGraph graph, LangServe runnable, or A2A agent).

    Also exposes `.as_a2a_executor()` returning an official `RewindA2AExecutor` (`a2a.server.agent_execution.AgentExecutor`)!
    """

    def __init__(
        self,
        agent_fn_or_runnable: Any,
        *,
        name: str = "remote-sub-agent",
        default_lease_ttl: float = 120.0,
    ) -> None:
        self.agent = agent_fn_or_runnable
        self.name = name
        self.default_lease_ttl = default_lease_ttl
        self.lease_manager = ServerLeaseManager()

    def as_a2a_executor(self) -> RewindA2AExecutor:
        """Return an official `a2a.server.agent_execution.AgentExecutor` instance backed by this sub-agent."""
        exec_instance = RewindA2AExecutor(
            self.agent,
            name=self.name,
            default_lease_ttl=self.default_lease_ttl,
        )
        exec_instance.lease_manager = self.lease_manager
        return exec_instance

    async def ainvoke(self, input_data: Any, config: Any = None, **kwargs: Any) -> Any:
        if isinstance(input_data, dict):
            if input_data.get("_rewind_action") == "rollback":
                u_token = input_data["undo_token"]
                comp_res = await self.lease_manager.rollback_lease(u_token)
                return {"rolled_back": True, "agent": self.name, "undo_token": u_token, "detail": comp_res}

            if input_data.get("_rewind_action") == "commit":
                u_token = input_data["undo_token"]
                ok = await self.lease_manager.commit_lease(u_token)
                return {"committed": ok, "agent": self.name, "undo_token": u_token}

            if input_data.get("jsonrpc") == "2.0":
                return await self._handle_a2a_jsonrpc(input_data)

        async with AgentRewind(tx_id=f"sub_{self.name}_{uuid4().hex[:6]}") as sub_tx:
            if hasattr(self.agent, "ainvoke"):
                raw_out = await self.agent.ainvoke(input_data, config=config, **kwargs)
            elif hasattr(self.agent, "invoke"):
                raw_out = await invoke_maybe_async(self.agent.invoke, input_data)
            else:
                raw_out = await invoke_maybe_async(self.agent, input_data)

            undo_token = sub_tx.export_undo_lease(
                self.lease_manager, ttl_seconds=self.default_lease_ttl
            )

        if undo_token is not None:
            return inject_rewind_metadata(
                raw_out,
                {
                    "undo_token": undo_token,
                    "agent": self.name,
                    "subagent_tx_id": sub_tx.tx_id,
                },
            )
        return raw_out

    def invoke(self, input_data: Any, config: Any = None, **kwargs: Any) -> Any:
        return _run_coro_sync(self.ainvoke(input_data, config=config, **kwargs))

    async def _handle_a2a_jsonrpc(self, rpc_req: dict[str, Any]) -> dict[str, Any]:
        req_id = rpc_req.get("id")
        method = rpc_req.get("method", "")
        params = rpc_req.get("params") or {}

        if method == "tasks/cancel":
            rw_meta = extract_rewind_metadata(params)
            u_token = rw_meta.get("undo_token") or params.get("undo_token")
            comp_res = await self.lease_manager.rollback_lease(u_token) if u_token else None
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "id": params.get("id", "task_canceled"),
                    "status": {"state": "canceled"},
                    "metadata": {
                        "rewind": {
                            "rolled_back": True,
                            "undo_token": u_token,
                            "detail": comp_res,
                        }
                    },
                },
            }

        if method in ("tasks/send", "message/send"):
            task_id = params.get("id") or f"task_{uuid4().hex[:8]}"
            try:
                out = await self.ainvoke(params.get("message") or params)
                rw_meta = extract_rewind_metadata(out)
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "id": task_id,
                        "status": {"state": "completed"},
                        "artifacts": [{"parts": [{"type": "text", "text": str(out)}]}],
                        "metadata": {"rewind": rw_meta} if rw_meta else {},
                    },
                }
            except Exception as exc:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "id": task_id,
                        "status": {
                            "state": "failed",
                            "message": f"{type(exc).__name__}: {exc}",
                        },
                        "metadata": {},
                    },
                }

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Unsupported A2A method: {method}"},
        }


# ============================================================================
# 3. Client-Side Wrappers (`rewind_agent` & `rewind_graph`)
# ============================================================================


class TransactionalRemoteAgent(Runnable[Any, Any]):
    """Official `langchain_core.runnables.Runnable` subclass wrapping `RemoteRunnable` or A2A sub-agents."""

    def __init__(
        self,
        remote_agent: Any,
        *,
        name: str | None = None,
        compensate: Any = None,
        protocol: str = "auto",
    ) -> None:
        self.remote_agent = remote_agent
        self.name = name or getattr(remote_agent, "name", None) or "remote_agent"
        self.protocol = protocol

        async def _call_agent(payload: Any) -> Any:
            if hasattr(remote_agent, "ainvoke"):
                return await remote_agent.ainvoke(payload)
            if hasattr(remote_agent, "invoke"):
                return await invoke_maybe_async(remote_agent.invoke, payload)
            return await invoke_maybe_async(remote_agent, payload)

        async def _forward(__agent_input__: Any = None, **kwargs: Any) -> Any:
            actual_input = __agent_input__ if __agent_input__ is not None else kwargs
            raw_res = await _call_agent(actual_input)

            if isinstance(raw_res, dict):
                if "error" in raw_res and raw_res.get("jsonrpc") == "2.0":
                    raise RuntimeError(f"A2A Error on '{self.name}': {raw_res['error']}")
                res_task = raw_res.get("result") if raw_res.get("jsonrpc") == "2.0" else raw_res
                if isinstance(res_task, dict):
                    status = res_task.get("status")
                    if isinstance(status, dict) and status.get("state") in ("failed", "TASK_STATE_FAILED"):
                        err_msg = status.get("message", f"Remote agent '{self.name}' returned state=failed")
                        raise RuntimeError(err_msg)

            rw_meta = extract_rewind_metadata(raw_res)
            u_token = rw_meta.get("undo_token") or rw_meta.get("undoToken")

            if u_token:
                async def _undo_subagent() -> Any:
                    if self.protocol == "a2a" or (
                        isinstance(raw_res, dict) and raw_res.get("jsonrpc") == "2.0"
                    ):
                        task_obj = raw_res.get("result", {}) if isinstance(raw_res, dict) else {}
                        task_id = task_obj.get("id", "task_rollback")
                        return await _call_agent({
                            "jsonrpc": "2.0",
                            "id": f"cancel_{uuid4().hex[:6]}",
                            "method": "tasks/cancel",
                            "params": {
                                "id": task_id,
                                "metadata": {"rewind": {"action": "rollback", "undo_token": u_token}},
                            },
                        })
                    return await _call_agent({"_rewind_action": "rollback", "undo_token": u_token})

                return ActionReceipt(result=raw_res, undo=_undo_subagent)

            return raw_res

        _forward.__name__ = self.name

        comp_fn = None
        if compensate is not None:
            async def _custom_comp(args: dict[str, Any], result: Any, pre: Any = None) -> Any:
                if hasattr(compensate, "ainvoke"):
                    return await compensate.ainvoke({"input": args, "result": result})
                if hasattr(compensate, "invoke"):
                    return await invoke_maybe_async(compensate.invoke, {"input": args, "result": result})
                return await invoke_maybe_async(compensate, args, result)

            comp_fn = _custom_comp

        self._wrapped = tx_tool(_forward, name=self.name, compensate=comp_fn)

    async def ainvoke(self, input_data: Any, config: Any = None, **kwargs: Any) -> Any:
        return await self._wrapped(__agent_input__=input_data)

    def invoke(self, input_data: Any, config: Any = None, **kwargs: Any) -> Any:
        return _run_coro_sync(self.ainvoke(input_data, config=config, **kwargs))


def rewind_agent(
    remote_agent: Any,
    *,
    name: str | None = None,
    compensate: Any = None,
    protocol: str = "auto",
) -> TransactionalRemoteAgent:
    """Wrap a `RemoteRunnable` or A2A sub-agent so multi-agent graphs roll back automatically."""
    return TransactionalRemoteAgent(
        remote_agent,
        name=name,
        compensate=compensate,
        protocol=protocol,
    )


class TransactionalStateGraph(Runnable[Any, Any]):
    """Official `langchain_core.runnables.Runnable` subclass wrapping a compiled LangGraph `StateGraph` (`workflow.compile()`) with `AgentRewind`."""

    def __init__(self, compiled_graph: Any, **tx_kwargs: Any) -> None:
        self.graph = compiled_graph
        self.tx_kwargs = tx_kwargs
        self.last_transaction: AgentTransaction | None = None

    async def ainvoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        from agent_rewind.kernel import get_current_transaction

        existing_tx = get_current_transaction()
        if existing_tx is not None:
            self.last_transaction = existing_tx
            if hasattr(self.graph, "ainvoke"):
                return await self.graph.ainvoke(input, config=config, **kwargs)
            return await invoke_maybe_async(self.graph.invoke, input)

        async with AgentRewind(**self.tx_kwargs) as tx:
            self.last_transaction = tx
            if hasattr(self.graph, "ainvoke"):
                return await self.graph.ainvoke(input, config=config, **kwargs)
            return await invoke_maybe_async(self.graph.invoke, input)

    def invoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        return _run_coro_sync(self.ainvoke(input, config=config, **kwargs))


class RewindStateGraph(StateGraph):  # type: ignore[type-arg]
    """Official `langgraph.graph.StateGraph` subclass whose `.compile()` returns a `TransactionalStateGraph`."""

    def __init__(
        self,
        state_schema: Any,
        context_schema: Any = None,
        *,
        rewind_kwargs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(state_schema, context_schema, **kwargs)
        self.rewind_kwargs = rewind_kwargs or {}

    def compile(self, *args: Any, **kwargs: Any) -> TransactionalStateGraph:  # type: ignore[override]
        compiled = super().compile(*args, **kwargs)
        return TransactionalStateGraph(compiled, **self.rewind_kwargs)


class RewindToolNode(ToolNode):
    """Official `langgraph.prebuilt.ToolNode` subclass that wraps tools with `AgentRewind` transactional guarantees."""

    def __init__(self, tools: list[Any], **kwargs: Any) -> None:
        from agent_rewind.adapters import rewind_tools

        wrapped = [
            t.as_langchain_tool() if hasattr(t, "as_langchain_tool") else t
            for t in rewind_tools(list(tools))
        ]
        super().__init__(wrapped, **kwargs)


def rewind_graph(compiled_graph: Any, **tx_kwargs: Any) -> TransactionalStateGraph:
    """Wrap a compiled LangGraph `StateGraph` so `.invoke()` runs atomically inside `AgentRewind`."""
    return TransactionalStateGraph(compiled_graph, **tx_kwargs)

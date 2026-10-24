"""Server-Side Transactional MCP Host (`RewindServer`) built on the Official `mcp` Python SDK.

Directly subclasses `mcp.server.mcpserver.MCPServer` from `https://github.com/modelcontextprotocol/python-sdk`
and extends it with transactional pre-state snapshots, server-side compensation closures, and TTL leases:

1. **Inherits Official `MCPServer` Capabilities**:
   - `streamable_http_app()`, `sse_app()`, `run()`, `run_stdio_async()`, `run_streamable_http_async()`
   - Official `mcp.types.Tool`, `mcp.types.ToolAnnotations`, and `mcp.types.CallToolResult` models
   - `add_resource()`, `add_prompt()`, `middleware`, and official MCP session management
2. **Extends `@server.tool(...)` with Server-Side Compensation**:
   - Adds `compensate`, `pre_state`, `deferred`, `read_only`, `deny_tainted`, `requires_prior`, `max_value`.
   - Automatically populates official `mcp.types.ToolAnnotations` and `Tool._meta["rewind"]`.
3. **Extends `call_tool(...)` with `CallToolResult._meta["rewind"]["undo_token"]`**:
   - Captures `pre_state` locally on the server before tool execution.
   - Delegates execution to the official `MCPServer` tool manager (`super().call_tool(...)`).
   - Mints a TTL-leased `undo_token` in `ServerLeaseManager` and attaches it to `CallToolResult.meta["rewind"]`.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool, ToolAnnotations

from agent_rewind.adapters import to_mcp_tool_schema
from agent_rewind.decorators import (
    TransactionalToolSpec,
    invoke_compensate_fn,
    invoke_maybe_async,
    tx_tool,
)
from agent_rewind.mcp import ServerLeaseManager
from agent_rewind.models import ActionReceipt, EffectType

MCP_PROTOCOL_VERSION = "2024-11-05"


class RewindServer(MCPServer):
    """Official `mcp.server.mcpserver.MCPServer` subclass with built-in transactional rewind & leases."""

    def __init__(
        self,
        service_name: str = "rewind-mcp-server",
        *,
        name: str | None = None,
        version: str = "0.2.0",
        default_lease_ttl: float = 60.0,
        **mcp_kwargs: Any,
    ) -> None:
        resolved_name = name or service_name
        super().__init__(name=resolved_name, version=version, **mcp_kwargs)
        self.service_name = resolved_name
        self.default_lease_ttl = default_lease_ttl
        self.lease_manager = ServerLeaseManager()
        self._tools: dict[str, Callable[..., Any]] = {}
        self._specs: dict[str, TransactionalToolSpec] = {}
        self._compensator_names: set[str] = {"_rewind_rollback", "_rewind_commit"}
        self._initialized: bool = False

        # Register built-in rollback & commit tools on the underlying official MCPServer
        super().tool(
            name="_rewind_rollback",
            description="Execute server-side transactional compensation for an undo_token.",
            annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True),
        )(self.handle_rollback)

        super().tool(
            name="_rewind_commit",
            description="Commit a server-side transactional lease for an undo_token.",
            annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True),
        )(self.handle_commit)

    def tool(
        self,
        fn: Callable[..., Any] | None = None,
        *,
        name: str | None = None,
        title: str | None = None,
        description: str | None = None,
        annotations: ToolAnnotations | None = None,
        meta: dict[str, Any] | None = None,
        structured_output: bool | None = None,
        deferred: bool = False,
        read_only: bool = False,
        compensate: Callable[..., Any] | None = None,
        pre_state: Callable[[dict[str, Any]], Any] | None = None,
        deny_tainted: list[str] | None = None,
        requires_prior: list[str] | None = None,
        max_value: dict[str, float] | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Register a tool on the official `MCPServer` along with its server-side compensation."""

        def _register(target_fn: Callable[..., Any]) -> Callable[..., Any]:
            if description and not target_fn.__doc__:
                target_fn.__doc__ = description
            wrapped = tx_tool(
                target_fn,
                name=name,
                deferred=deferred,
                read_only=read_only,
                compensate=compensate,
                pre_state=pre_state,
                deny_tainted=deny_tainted,
                requires_prior=requires_prior,
                max_value=max_value,
                timeout=timeout,
            )
            spec: TransactionalToolSpec = wrapped.__tx_spec__
            self._tools[spec.name] = wrapped
            self._specs[spec.name] = spec

            has_comp = spec.compensate_fn is not None or spec.effect == EffectType.COMPENSATABLE
            rewind_meta = {
                "service": self.service_name,
                "effectType": spec.effect.value,
                "hasServerSideCompensation": has_comp,
                "taintLevel": spec.taint.value,
                "denyTaintedArgs": spec.deny_tainted,
                "requiresPrior": spec.requires_prior,
                "maxValue": spec.max_value,
                "rollbackMethod": "rewind/rollback",
                "commitMethod": "rewind/commit",
                "rollbackTool": "_rewind_rollback",
                "commitTool": "_rewind_commit",
                "rollbackEndpoint": "/_rewind/rollback",
                "commitEndpoint": "/_rewind/commit",
            }
            merged_meta = {**(meta or {}), "rewind": rewind_meta}
            resolved_annotations = annotations or ToolAnnotations(
                title=title or spec.name,
                readOnlyHint=spec.effect == EffectType.READ_ONLY,
                destructiveHint=spec.effect == EffectType.DEFERRED,
                idempotentHint=True,
                openWorldHint=True,
            )

            # Register on the official `mcp.server.mcpserver.MCPServer` tool manager!
            super(RewindServer, self).tool(
                name=spec.name,
                title=title or spec.name,
                description=description or (target_fn.__doc__ or "").strip(),
                annotations=resolved_annotations,
                meta=merged_meta,
                structured_output=structured_output,
            )(target_fn)

            return wrapped

        if fn is not None:
            return _register(fn)
        return _register

    async def list_mcp_tools(self) -> list[Tool]:
        """Return official `mcp.types.Tool` instances from `super().list_tools()`, filtering internal tools."""
        all_tools = await super().list_tools()
        return [t for t in all_tools if t.name not in self._compensator_names]

    def list_tools_dict(self) -> dict[str, list[dict[str, Any]]]:
        """Return serialized MCP `ListToolsResult` dict with `annotations` and `_meta.rewind`."""
        schemas: list[dict[str, Any]] = []
        for t_name, wrapped_fn in self._tools.items():
            if t_name in self._compensator_names:
                continue
            schema = to_mcp_tool_schema(wrapped_fn, include_tx_metadata=True)
            spec = self._specs[t_name]
            has_comp = spec.compensate_fn is not None or spec.effect == EffectType.COMPENSATABLE

            rewind_meta = {
                "service": self.service_name,
                "effectType": spec.effect.value,
                "hasServerSideCompensation": has_comp,
                "taintLevel": spec.taint.value,
                "denyTaintedArgs": spec.deny_tainted,
                "requiresPrior": spec.requires_prior,
                "maxValue": spec.max_value,
                "rollbackMethod": "rewind/rollback",
                "commitMethod": "rewind/commit",
                "rollbackTool": "_rewind_rollback",
                "commitTool": "_rewind_commit",
                "rollbackEndpoint": "/_rewind/rollback",
                "commitEndpoint": "/_rewind/commit",
            }
            schema["annotations"] = ToolAnnotations(
                title=spec.name,
                readOnlyHint=spec.effect == EffectType.READ_ONLY,
                destructiveHint=spec.effect == EffectType.DEFERRED,
                idempotentHint=True,
                openWorldHint=True,
            ).model_dump(by_alias=True, exclude_none=True)
            schema["_meta"] = {"rewind": rewind_meta}
            schema["x-transactional"] = rewind_meta
            schemas.append(schema)
        return {"tools": schemas}

    def list_tools(self) -> dict[str, list[dict[str, Any]]]:  # type: ignore[override]
        """Return MCP `ListToolsResult` dictionary."""
        return self.list_tools_dict()

    async def _handle_list_tools(
        self, ctx: Any, params: Any = None
    ) -> ListToolsResult:
        """Override official `MCPServer._handle_list_tools` for Streamable HTTP / SSE / stdio sessions."""
        return ListToolsResult(tools=await self.list_mcp_tools())

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        context: Any = None,
        *,
        lease_ttl_seconds: float | None = None,
    ) -> CallToolResult:
        """Override official `MCPServer.call_tool` to return `mcp.types.CallToolResult` with `meta.rewind`."""
        raw_dict = await self.handle_call(
            name, arguments, lease_ttl_seconds=lease_ttl_seconds
        )
        return CallToolResult(
            content=[
                TextContent(type="text", text=c["text"])
                for c in raw_dict.get("content", [])
            ],
            structured_content=(
                raw_dict.get("structuredContent")
                if isinstance(raw_dict.get("structuredContent"), dict)
                else {"result": raw_dict.get("structuredContent")}
            ),
            is_error=bool(raw_dict.get("isError", False)),
            _meta=raw_dict.get("_meta"),
        )

    async def handle_call(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        lease_ttl_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Execute a registered MCP tool, capture pre-state locally, and mint an `undo_token`."""
        args = dict(arguments or {})

        if tool_name in ("_rewind_rollback", "rewind/rollback"):
            return await self.handle_rollback(args["undo_token"])
        if tool_name in ("_rewind_commit", "rewind/commit"):
            return await self.handle_commit(args["undo_token"])

        if tool_name not in self._specs:
            raise KeyError(f"MCP Tool '{tool_name}' is not registered on {self.service_name}.")

        spec = self._specs[tool_name]
        ttl = lease_ttl_seconds if lease_ttl_seconds is not None else self.default_lease_ttl

        # 1. Capture pre-state locally on the server
        pre_val: Any = None
        if spec.pre_state_fn is not None:
            pre_val = await invoke_maybe_async(spec.pre_state_fn, args)

        # 2. Execute the forward function
        raw_out = await invoke_maybe_async(spec.fn, **args)

        receipt_undo: Callable[[], Any] | None = None
        undo_args: dict[str, Any] | None = None
        if isinstance(raw_out, ActionReceipt):
            actual_result = raw_out.result
            receipt_undo = raw_out.undo
            undo_args = raw_out.undo_args
            if raw_out.pre_state is not None:
                pre_val = raw_out.pre_state
        else:
            actual_result = raw_out

        # 3. Mint a server-side lease token if compensatable
        undo_token: str | None = None
        if spec.effect == EffectType.COMPENSATABLE and (
            receipt_undo is not None or spec.compensate_fn is not None
        ):
            call_args = {**args, "__undo_args__": undo_args} if undo_args else args

            async def _server_undo_closure() -> Any:
                if receipt_undo is not None:
                    return await invoke_maybe_async(receipt_undo)
                if spec.compensate_fn is not None:
                    return await invoke_compensate_fn(
                        spec.compensate_fn, call_args, actual_result, pre_val
                    )
                return None

            undo_token = self.lease_manager.register_lease(
                _server_undo_closure, ttl_seconds=ttl
            )

        text_payload = (
            json.dumps(actual_result, default=str)
            if not isinstance(actual_result, str)
            else actual_result
        )
        rewind_block: dict[str, Any] | None = None
        if undo_token is not None:
            rewind_block = {
                "undo_token": undo_token,
                "undoToken": undo_token,
                "service": self.service_name,
                "tool": tool_name,
                "rollback_method": "rewind/rollback",
                "commit_method": "rewind/commit",
                "rollback_endpoint": "/_rewind/rollback",
                "commit_endpoint": "/_rewind/commit",
            }

        # Build official `mcp.types.CallToolResult` and serialize it with aliases (`_meta`, `structuredContent`)
        mcp_call_result = CallToolResult(
            content=[TextContent(type="text", text=text_payload)],
            structured_content=(
                actual_result if isinstance(actual_result, dict) else {"value": actual_result}
            ),
            is_error=False,
            _meta={"rewind": rewind_block} if rewind_block else None,
        )
        response = mcp_call_result.model_dump(by_alias=True, exclude_none=True)
        response["structuredContent"] = actual_result
        response["result"] = actual_result
        if rewind_block is not None:
            response["_rewind"] = rewind_block
        return response

    async def handle_rollback(self, undo_token: str) -> dict[str, Any]:
        """Execute the server-side compensation bound to `undo_token`."""
        comp_res = await self.lease_manager.rollback_lease(undo_token)
        payload = {
            "rolled_back": True,
            "undo_token": undo_token,
            "compensation_result": comp_res,
        }
        mcp_res = CallToolResult(
            content=[TextContent(type="text", text=json.dumps(payload, default=str))],
            structured_content=payload,
            is_error=False,
        )
        return {**mcp_res.model_dump(by_alias=True, exclude_none=True), **payload}

    async def handle_commit(self, undo_token: str) -> dict[str, Any]:
        """Finalize the lease for `undo_token` so TTL expiration will not roll it back."""
        ok = await self.lease_manager.commit_lease(undo_token)
        payload = {"committed": ok, "undo_token": undo_token}
        mcp_res = CallToolResult(
            content=[TextContent(type="text", text=json.dumps(payload, default=str))],
            structured_content=payload,
            is_error=False,
        )
        return {**mcp_res.model_dump(by_alias=True, exclude_none=True), **payload}

    async def handle_jsonrpc(
        self, message: dict[str, Any] | list[dict[str, Any]]
    ) -> dict[str, Any] | list[dict[str, Any]] | None:
        """Process a standard MCP JSON-RPC 2.0 request, notification, or batch."""
        if isinstance(message, list):
            responses = []
            for item in message:
                res = await self._handle_single_jsonrpc(item)
                if res is not None:
                    responses.append(res)
            return responses
        return await self._handle_single_jsonrpc(message)

    async def _handle_single_jsonrpc(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        req_id = msg.get("id")
        method = msg.get("method", "")
        params = msg.get("params") or {}

        if req_id is None and method.startswith("notifications/"):
            if method == "notifications/initialized":
                self._initialized = True
            return None

        try:
            if method == "initialize":
                self._initialized = True
                client_version = params.get("protocolVersion", MCP_PROTOCOL_VERSION)
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": client_version,
                        "capabilities": {
                            "tools": {"listChanged": False},
                            "experimental": {
                                "rewind": {
                                    "version": self.version or "0.2.0",
                                    "supportsUndoTokens": True,
                                    "supportsServerLeases": True,
                                    "rollbackMethod": "rewind/rollback",
                                    "commitMethod": "rewind/commit",
                                }
                            },
                        },
                        "serverInfo": {
                            "name": self.service_name,
                            "version": self.version or "0.2.0",
                        },
                    },
                }

            if method == "ping":
                return {"jsonrpc": "2.0", "id": req_id, "result": {}}

            if method == "tools/list":
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": self.list_tools_dict(),
                }

            if method == "tools/call":
                tool_name = params.get("name") or params.get("tool")
                arguments = params.get("arguments") or {}
                meta = params.get("_meta") or {}
                ttl = meta.get("leaseTtlSeconds") or params.get("lease_ttl_seconds")
                call_out = await self.handle_call(
                    tool_name, arguments, lease_ttl_seconds=ttl
                )
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": call_out,
                }

            if method in ("rewind/rollback", "tools/rollback"):
                token = params.get("undo_token") or params.get("undoToken")
                rb_out = await self.handle_rollback(token)
                return {"jsonrpc": "2.0", "id": req_id, "result": rb_out}

            if method in ("rewind/commit", "tools/commit"):
                token = params.get("undo_token") or params.get("undoToken")
                cm_out = await self.handle_commit(token)
                return {"jsonrpc": "2.0", "id": req_id, "result": cm_out}

            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": -32601,
                    "message": f"MCP Method not found: {method}",
                },
            }
        except KeyError as k_err:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": -32602,
                    "message": str(k_err),
                },
            }
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": -32000,
                    "message": f"{type(exc).__name__}: {exc}",
                },
            }

    async def run_stdio_async(  # type: ignore[override]
        self,
        stdin: Any = None,
        stdout: Any = None,
    ) -> None:
        """Run the MCP Server over `stdio`. Uses official `MCPServer.run_stdio_async()` when using real stdio."""
        if stdin is None and stdout is None:
            await super().run_stdio_async()
            return

        in_stream = stdin or sys.stdin
        out_stream = stdout or sys.stdout
        loop = asyncio.get_running_loop()

        while True:
            line = await loop.run_in_executor(None, in_stream.readline)
            if not line:
                break
            raw = line.strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                err_resp = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": "Parse error"},
                }
                out_stream.write(json.dumps(err_resp) + "\n")
                out_stream.flush()
                continue

            resp = await self.handle_jsonrpc(msg)
            if resp is not None:
                out_stream.write(json.dumps(resp, default=str) + "\n")
                out_stream.flush()

    def run_stdio(self) -> None:
        """Synchronous entry point to run the MCP server over `stdio`."""
        asyncio.run(self.run_stdio_async())

    def mount_to_fastmcp(self, mcp_server_app: Any) -> Any:
        """Mount all registered `@server.tool` functions onto another official `MCPServer` / `FastMCP` instance."""
        for t_name, spec in self._specs.items():
            if t_name in self._compensator_names:
                continue

            def _make_handler(bound_name: str) -> Callable[..., Any]:
                async def _mcp_tool_wrapper(**kwargs: Any) -> dict[str, Any]:
                    return await self.handle_call(bound_name, kwargs)

                _mcp_tool_wrapper.__name__ = bound_name
                _mcp_tool_wrapper.__doc__ = spec.fn.__doc__
                return _mcp_tool_wrapper

            mcp_server_app.tool(name=t_name, description=spec.fn.__doc__ or "")(
                _make_handler(t_name)
            )

        mcp_server_app.tool(
            name="_rewind_rollback",
            description="Execute server-side transactional compensation for an undo_token.",
        )(self.handle_rollback)

        mcp_server_app.tool(
            name="_rewind_commit",
            description="Commit a server-side transactional lease for an undo_token.",
        )(self.handle_commit)

        return mcp_server_app

    async def invoke_runnable(
        self,
        tool_name: str,
        input_data: dict[str, Any] | Any = None,
    ) -> dict[str, Any]:
        """Execute tool via MCP JSON-RPC `tools/call` or `rewind/rollback` for `RemoteRunnable` compatibility."""
        payload = dict(input_data) if isinstance(input_data, dict) else {"input": input_data}
        action = payload.get("_rewind_action")
        if action == "rollback":
            rpc_resp = await self.handle_jsonrpc(
                {
                    "jsonrpc": "2.0",
                    "id": "rb_1",
                    "method": "rewind/rollback",
                    "params": {"undo_token": payload["undo_token"]},
                }
            )
        elif action == "commit":
            rpc_resp = await self.handle_jsonrpc(
                {
                    "jsonrpc": "2.0",
                    "id": "cm_1",
                    "method": "rewind/commit",
                    "params": {"undo_token": payload["undo_token"]},
                }
            )
        else:
            rpc_resp = await self.handle_jsonrpc(
                {
                    "jsonrpc": "2.0",
                    "id": "call_1",
                    "method": "tools/call",
                    "params": {"name": tool_name, "arguments": payload},
                }
            )
        if isinstance(rpc_resp, dict) and "error" in rpc_resp:
            err_msg = rpc_resp["error"].get("message", "MCP tool call failed")
            raise RuntimeError(err_msg)
        return rpc_resp["result"] if isinstance(rpc_resp, dict) else {}

    def as_remote_runnable(self, tool_name: str) -> Any:
        """Return a `RemoteRunnable`-compatible MCP client object speaking MCP JSON-RPC 2.0 to this server."""
        from agent_rewind.kernel import _run_coro_sync

        spec = self._specs[tool_name]
        server_ref = self

        class _MCPBoundRunnable:
            name = spec.name
            description = (spec.fn.__doc__ or "").strip()

            async def ainvoke(
                self, input_data: dict[str, Any] | Any = None, config: Any = None, **kwargs: Any
            ) -> Any:
                data = input_data if isinstance(input_data, dict) else kwargs
                return await server_ref.invoke_runnable(tool_name, data)

            def invoke(
                self, input_data: dict[str, Any] | Any = None, config: Any = None, **kwargs: Any
            ) -> Any:
                return _run_coro_sync(self.ainvoke(input_data, config=config, **kwargs))

        return _MCPBoundRunnable()

    async def asgi_transport(
        self,
        method: str,
        url: str,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """In-process transport supporting both standard MCP JSON-RPC 2.0 (`POST /mcp`) and REST."""
        body = json_body or {}

        if (isinstance(body, dict) and body.get("jsonrpc") == "2.0") or url.endswith("/mcp"):
            if method.upper() == "GET":
                return self.list_tools_dict()
            return await self.handle_jsonrpc(body)

        if url.endswith("/mcp/tools"):
            return self.list_tools_dict()
        if url.endswith("/_rewind/rollback"):
            return await self.handle_rollback(body["undo_token"])
        if url.endswith("/_rewind/commit"):
            return await self.handle_commit(body["undo_token"])
        if url.endswith("/mcp/call"):
            return await self.handle_call(
                body["tool"],
                body.get("arguments", {}),
                lease_ttl_seconds=body.get("lease_ttl_seconds"),
            )
        raise ValueError(f"Unsupported RewindServer endpoint: {url}")

    def create_fastapi_router(self) -> Any:
        """Create a `fastapi.APIRouter` exposing MCP Streamable HTTP (`POST /mcp`) + LangServe routes."""
        from fastapi import APIRouter, Request
        from pydantic import BaseModel

        router = APIRouter()

        class CallRequest(BaseModel):
            tool: str
            arguments: dict[str, Any] = {}
            lease_ttl_seconds: float | None = None

        class TokenRequest(BaseModel):
            undo_token: str

        class LangServeInvokeRequest(BaseModel):
            input: dict[str, Any] = {}
            config: dict[str, Any] = {}

        @router.post("/mcp")
        async def _mcp_jsonrpc_endpoint(request: Request) -> Any:
            payload = await request.json()
            return await self.handle_jsonrpc(payload)

        @router.get("/mcp/tools")
        async def _list_tools() -> dict[str, Any]:
            return self.list_tools_dict()

        @router.post("/mcp/call")
        async def _call_tool(req: CallRequest) -> dict[str, Any]:
            return await self.handle_call(
                req.tool, req.arguments, lease_ttl_seconds=req.lease_ttl_seconds
            )

        @router.post("/_rewind/rollback")
        async def _rollback(req: TokenRequest) -> dict[str, Any]:
            return await self.handle_rollback(req.undo_token)

        @router.post("/_rewind/commit")
        async def _commit(req: TokenRequest) -> dict[str, Any]:
            return await self.handle_commit(req.undo_token)

        @router.post("/{tool_name}/invoke")
        async def _langserve_invoke(tool_name: str, req: LangServeInvokeRequest) -> dict[str, Any]:
            out = await self.invoke_runnable(tool_name, req.input)
            return {"output": out}

        return router

    def create_fastapi_app(self, title: str | None = None, **fastapi_kwargs: Any) -> Any:
        """Create a standalone `FastAPI` application exposing MCP JSON-RPC (`POST /mcp`) and LangServe routes."""
        from fastapi import FastAPI

        app = FastAPI(
            title=title or f"{self.service_name} (Transactional MCP Server)",
            version=self.version or "0.2.0",
            **fastapi_kwargs,
        )
        app.include_router(self.create_fastapi_router())
        return app


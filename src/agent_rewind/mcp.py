"""Zero-Config Distributed MCP Gateway & Semantic Auto-Discovery Engine.

Allows an agent to connect to remote MCP servers using ONLY their URLs (`RemoteMCPGateway.discover`),
automatically inferring forward tools, pre-state getters, compensating tools, Outbox deferral,
and sensitive parameter taint rules from standard MCP tool schemas.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
import inspect
import json
from pathlib import Path
import time
from typing import Any, Protocol
import urllib.request
from uuid import uuid4

from agent_rewind.decorators import TransactionalToolSpec, tx_tool
from agent_rewind.models import ActionReceipt, EffectType, TaintLevel


class MCPTransport(Protocol):
    """Pluggable network transport protocol for RemoteMCPGateway (HTTP, SSE, gRPC, Queue, etc.)."""

    async def request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any: ...

    async def close(self) -> None: ...


class StdlibHTTPTransport:
    """Zero-dependency HTTP transport using Python's built-in `urllib.request` (no `httpx` required)."""

    def __init__(self, timeout: float = 10.0) -> None:
        self.timeout = timeout

    def _sync_request(
        self,
        method: str,
        url: str,
        json_body: dict[str, Any] | None,
        headers: dict[str, str] | None,
    ) -> Any:
        req_headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if headers:
            req_headers.update(headers)
        data = json.dumps(json_body).encode("utf-8") if json_body is not None else None
        req = urllib.request.Request(url, data=data, headers=req_headers, method=method.upper())
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}

    async def request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        return await asyncio.to_thread(self._sync_request, method, url, json_body, headers)

    async def close(self) -> None:
        pass


class HttpxTransport:
    """Adapter wrapping an `httpx.AsyncClient` instance."""

    def __init__(self, client: Any, *, owns_client: bool = False) -> None:
        self._client = client
        self._owns_client = owns_client

    async def request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        if method.upper() == "GET":
            resp = await self._client.get(url, headers=headers)
        else:
            resp = await self._client.post(url, json=json_body, headers=headers)
        resp.raise_for_status()
        return resp.json()

    async def close(self) -> None:
        if self._owns_client and hasattr(self._client, "aclose"):
            await self._client.aclose()


class CallableMCPTransport:
    """Adapter allowing any custom function `(method, url, json_body, headers) -> dict` as transport."""

    def __init__(self, fn: Callable[..., Any]) -> None:
        self._fn = fn

    async def request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        res = self._fn(method, url, json_body, headers)
        if inspect.isawaitable(res):
            return await res
        return res

    async def close(self) -> None:
        pass

_READ_VERBS = {"get", "fetch", "list", "read", "query", "inspect", "find", "check"}
_DEFERRED_VERBS = {"send", "notify", "publish", "emit", "broadcast", "mail", "webhook", "post_message"}
_COMPENSATOR_VERBS = {
    "rollback",
    "cancel",
    "reverse",
    "undo",
    "delete",
    "remove",
    "release",
    "restore",
    "deprovision",
    "set",
}
_FORWARD_MUTATION_VERBS = {
    "create",
    "add",
    "insert",
    "adjust",
    "increment",
    "decrement",
    "process",
    "charge",
    "reserve",
    "provision",
    "update",
    "issue",
}
_SENSITIVE_PARAM_KEYWORDS = {
    "iban",
    "account",
    "wallet",
    "recipient_iban",
    "destination_account",
    "role",
    "permission",
    "password",
    "secret",
    "api_key",
}


@dataclass
class DiscoveredToolBinding:
    """Represents the auto-discovered transactional contract for a remote MCP tool."""

    server_url: str
    tool_name: str
    description: str
    input_schema: dict[str, Any]
    effect: EffectType
    is_compensator_only: bool = False
    pre_state_tool: str | None = None
    compensate_tool: str | None = None
    arg_mapping: dict[str, str] = field(default_factory=dict)
    deny_tainted: list[str] = field(default_factory=list)
    requires_prior: list[str] = field(default_factory=list)
    max_value: dict[str, float] = field(default_factory=dict)
    has_server_side_undo: bool = False
    rollback_endpoint: str = "/_rewind/rollback"
    commit_endpoint: str = "/_rewind/commit"


def _decompose_tool_name(name: str) -> tuple[str | None, str, str]:
    """Decompose a tool name like 'crm_create_return' or 'create_return' into (prefix, verb, entity)."""
    parts = name.lower().split("_")
    for idx, token in enumerate(parts):
        if token in (_READ_VERBS | _DEFERRED_VERBS | _COMPENSATOR_VERBS | _FORWARD_MUTATION_VERBS):
            prefix = "_".join(parts[:idx]) if idx > 0 else None
            verb = token
            entity = "_".join(parts[idx + 1 :]) or "resource"
            return prefix, verb, entity
    return None, parts[0], "_".join(parts[1:]) or "resource"


def resolve_remote_args(
    target_schema: dict[str, Any],
    explicit_mapping: dict[str, str],
    forward_args: dict[str, Any],
    forward_result: Any,
    pre_state: Any,
) -> dict[str, Any]:
    """Smartly resolve arguments required by a remote pre_state or compensate MCP tool."""
    props: dict[str, Any] = target_schema.get("properties", {})
    required: list[str] = target_schema.get("required", list(props.keys()))

    res_dict = forward_result if isinstance(forward_result, dict) else {"value": forward_result}
    pre_dict = (
        pre_state
        if isinstance(pre_state, dict)
        else ({"qty": pre_state, "value": pre_state} if pre_state is not None else {})
    )

    resolved: dict[str, Any] = {}

    # 1. Apply explicit JSONPath-like mappings ($args.x, $result.y, $pre_state.z)
    for target_param, expr in explicit_mapping.items():
        if expr.startswith("$args."):
            key = expr[len("$args.") :]
            if key in forward_args:
                resolved[target_param] = forward_args[key]
        elif expr.startswith("$result."):
            key = expr[len("$result.") :]
            if key in res_dict:
                resolved[target_param] = res_dict[key]
        elif expr.startswith("$pre_state."):
            key = expr[len("$pre_state.") :]
            if key in pre_dict:
                resolved[target_param] = pre_dict[key]
        else:
            resolved[target_param] = expr

    # 2. Auto-resolve any remaining required parameters via exact & suffix matching
    for param in required:
        if param in resolved:
            continue

        # Priority A: Check pre_state (e.g. 'qty' from get_stock -> set_stock)
        if param in pre_dict:
            resolved[param] = pre_dict[param]
            continue

        # Priority B: Exact match in forward_result (e.g. 'return_id', 'payment_tx_id')
        if param in res_dict:
            resolved[param] = res_dict[param]
            continue

        # Priority C: Exact match in forward_args (e.g. 'sku', 'order_id')
        if param in forward_args:
            resolved[param] = forward_args[param]
            continue

        # Priority D: Semantic suffix/token match in forward_result
        # e.g. 'previous_status' matches 'previous_order_status'
        param_tokens = set(param.split("_"))
        best_key = None
        best_overlap = 0
        for rk, rv in res_dict.items():
            rk_tokens = set(rk.split("_"))
            overlap = len(param_tokens & rk_tokens)
            if overlap >= 2 or (overlap == 1 and param.split("_")[-1] == rk.split("_")[-1]):
                if overlap > best_overlap:
                    best_overlap = overlap
                    best_key = rk
        if best_key is not None:
            resolved[param] = res_dict[best_key]

    return resolved


class AutoDiscoveryEngine:
    """Analyzes raw MCP `tools/list` schemas and synthesizes transactional bindings."""

    @classmethod
    def synthesize_bindings(
        cls,
        server_url: str,
        raw_tools: list[dict[str, Any]],
    ) -> dict[str, DiscoveredToolBinding]:
        bindings: dict[str, DiscoveredToolBinding] = {}
        by_entity: dict[str, dict[str, list[str]]] = {}

        # Pass 1: Classify verbs, entities, and explicit metadata
        for t in raw_tools:
            name = t["name"]
            desc = t.get("description", "")
            schema = t.get("inputSchema", {"type": "object", "properties": {}})
            meta_block = t.get("_meta") or {}
            tx_meta = (
                meta_block.get("rewind")
                or t.get("x-transactional")
                or t.get("x-agent-tx")
                or {}
            )

            _, verb, entity = _decompose_tool_name(name)
            by_entity.setdefault(entity, {"read": [], "forward": [], "comp": [], "deferred": []})

            # Detect sensitive arguments automatically
            props = schema.get("properties", {})
            auto_deny = [
                p for p in props if any(kw in p.lower() for kw in _SENSITIVE_PARAM_KEYWORDS)
            ]
            deny_tainted = tx_meta.get("denyTaintedArgs") or tx_meta.get("deny_tainted") or auto_deny
            requires_prior = tx_meta.get("requiresPrior") or tx_meta.get("requires_prior") or []
            max_value = tx_meta.get("maxValue") or tx_meta.get("max_value") or {}
            has_server_undo = bool(
                tx_meta.get("hasServerSideCompensation") or tx_meta.get("has_server_side_undo")
            )
            eff_str = tx_meta.get("effectType") or tx_meta.get("effect")

            if eff_str:
                effect = EffectType(eff_str)
                is_comp_only = bool(tx_meta.get("is_compensator_only", False))
            elif verb in _DEFERRED_VERBS:
                effect = EffectType.DEFERRED
                is_comp_only = False
                by_entity[entity]["deferred"].append(name)
            elif verb in _READ_VERBS:
                effect = EffectType.READ_ONLY
                is_comp_only = False
                by_entity[entity]["read"].append(name)
            elif verb in _COMPENSATOR_VERBS:
                effect = EffectType.READ_ONLY
                is_comp_only = True
                by_entity[entity]["comp"].append(name)
            else:
                effect = EffectType.COMPENSATABLE
                is_comp_only = False
                by_entity[entity]["forward"].append(name)

            bindings[name] = DiscoveredToolBinding(
                server_url=server_url,
                tool_name=name,
                description=desc,
                input_schema=schema,
                effect=effect,
                is_compensator_only=is_comp_only,
                pre_state_tool=tx_meta.get("pre_state_tool"),
                compensate_tool=tx_meta.get("compensate_tool"),
                arg_mapping=tx_meta.get("arg_mapping", {}),
                deny_tainted=deny_tainted,
                requires_prior=requires_prior,
                max_value=max_value,
                has_server_side_undo=has_server_undo,
                rollback_endpoint=tx_meta.get("rollbackEndpoint", "/_rewind/rollback"),
                commit_endpoint=tx_meta.get("commitEndpoint", "/_rewind/commit"),
            )

        # Pass 2: Auto-pair forward tools with pre_state readers and compensators of the same entity
        for entity, groups in by_entity.items():
            readers = groups["read"]
            compensators = groups["comp"]
            for fwd_name in groups["forward"]:
                b = bindings[fwd_name]
                if b.pre_state_tool is None and readers:
                    b.pre_state_tool = readers[0]
                if b.compensate_tool is None and compensators:
                    b.compensate_tool = compensators[0]

        return bindings


class RemoteMCPGateway:
    """Client-side Distributed MCP Gateway with Pluggable Transport & Zero-Config Auto-Discovery."""

    def __init__(
        self,
        client: Any = None,
        *,
        transport: MCPTransport | Callable[..., Any] | None = None,
    ) -> None:
        if transport is not None:
            self._transport: MCPTransport = (
                CallableMCPTransport(transport)
                if callable(transport) and not hasattr(transport, "request")
                else transport
            )
        elif client is not None:
            self._transport = HttpxTransport(client, owns_client=False)
        else:
            self._transport = StdlibHTTPTransport()

        self.bindings: dict[str, DiscoveredToolBinding] = {}
        self.tools: dict[str, Any] = {}
        self._jsonrpc_servers: set[str] = set()

    async def close(self) -> None:
        await self._transport.close()

    @classmethod
    async def discover(
        cls,
        server_urls: list[str],
        *,
        client: Any = None,
        transport: MCPTransport | Callable[..., Any] | None = None,
        save_manifest: str | Path | None = None,
        verify_unmodified_tools: list[str] | None = None,
    ) -> RemoteMCPGateway:
        """Connect to one or more remote MCP server URLs over MCP JSON-RPC 2.0 (`POST /mcp`) or REST."""
        gw = cls(client=client, transport=transport)
        verify_set = set(verify_unmodified_tools or [])

        for base_url in server_urls:
            clean_url = base_url.rstrip("/")
            raw_tools: list[dict[str, Any]] | None = None

            # 1. Try Official MCP JSON-RPC 2.0 (`initialize` + `tools/list` on POST /mcp)
            try:
                rpc_list = await gw._transport.request(
                    "POST",
                    f"{clean_url}/mcp",
                    json_body={
                        "jsonrpc": "2.0",
                        "id": "disc_1",
                        "method": "tools/list",
                        "params": {},
                    },
                )
                if (
                    isinstance(rpc_list, dict)
                    and rpc_list.get("jsonrpc") == "2.0"
                    and "result" in rpc_list
                ):
                    gw._jsonrpc_servers.add(clean_url)
                    res_obj = rpc_list["result"]
                    raw_tools = res_obj.get("tools", res_obj if isinstance(res_obj, list) else [])
            except Exception:
                raw_tools = None

            # 2. Fallback to REST `GET /mcp/tools` if custom transport does not implement JSON-RPC
            if raw_tools is None:
                payload = await gw._transport.request("GET", f"{clean_url}/mcp/tools")
                raw_tools = payload.get("tools", payload if isinstance(payload, list) else [])

            server_bindings = AutoDiscoveryEngine.synthesize_bindings(clean_url, raw_tools)
            gw.bindings.update(server_bindings)

        gw._compile_transactional_tools(verify_set)

        if save_manifest is not None:
            Path(save_manifest).write_text(json.dumps(gw.export_manifest(), indent=2))

        return gw

    async def invoke_raw_mcp_tool(
        self,
        server_url: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        lease_ttl_seconds: float | None = None,
    ) -> Any:
        """Invoke a tool on a remote MCP server via MCP JSON-RPC 2.0 (`tools/call`) or REST."""
        from agent_rewind.kernel import get_current_idempotency_key, get_current_transaction

        headers: dict[str, str] = {}
        active_tx = get_current_transaction()
        active_idem = get_current_idempotency_key()
        if active_tx is not None:
            headers["X-Transaction-ID"] = active_tx.tx_id
        if active_idem is not None:
            headers["Idempotency-Key"] = active_idem

        use_jsonrpc = server_url in self._jsonrpc_servers

        if use_jsonrpc:
            rpc_resp = await self._transport.request(
                "POST",
                f"{server_url}/mcp",
                json_body={
                    "jsonrpc": "2.0",
                    "id": f"call_{uuid4().hex[:8]}",
                    "method": "tools/call",
                    "params": {
                        "name": tool_name,
                        "arguments": arguments,
                        "_meta": {
                            "transactionId": active_tx.tx_id if active_tx else None,
                            "idempotencyKey": active_idem,
                            "leaseTtlSeconds": lease_ttl_seconds,
                        },
                    },
                },
                headers=headers,
            )
            if isinstance(rpc_resp, dict) and "error" in rpc_resp:
                err_msg = rpc_resp["error"].get("message", "MCP JSON-RPC error")
                raise RuntimeError(err_msg)
            data = rpc_resp.get("result", rpc_resp) if isinstance(rpc_resp, dict) else rpc_resp
        else:
            req_payload: dict[str, Any] = {
                "tool": tool_name,
                "arguments": arguments,
            }
            if lease_ttl_seconds is not None:
                req_payload["lease_ttl_seconds"] = lease_ttl_seconds

            data = await self._transport.request(
                "POST",
                f"{server_url}/mcp/call",
                json_body=req_payload,
                headers=headers,
            )

        if isinstance(data, dict):
            rw_meta = data.get("_meta", {}).get("rewind") or data.get("_rewind") or {}
            u_token = rw_meta.get("undo_token") or rw_meta.get("undoToken")
            if u_token:
                rb_path = rw_meta.get("rollback_endpoint", "/_rewind/rollback")
                inner_res = (
                    data["structuredContent"]
                    if "structuredContent" in data
                    else data.get("result")
                )

                async def _remote_token_undo() -> Any:
                    if use_jsonrpc:
                        rpc_rb = await self._transport.request(
                            "POST",
                            f"{server_url}/mcp",
                            json_body={
                                "jsonrpc": "2.0",
                                "id": f"rb_{uuid4().hex[:8]}",
                                "method": "rewind/rollback",
                                "params": {"undo_token": u_token},
                            },
                            headers=headers,
                        )
                        return rpc_rb.get("result", rpc_rb) if isinstance(rpc_rb, dict) else rpc_rb
                    return await self._transport.request(
                        "POST",
                        f"{server_url}{rb_path}",
                        json_body={"undo_token": u_token},
                        headers=headers,
                    )

                return ActionReceipt(result=inner_res, undo=_remote_token_undo)

            if "structuredContent" in data:
                return data["structuredContent"]
            return data.get("result", data)

        return data

    def _compile_transactional_tools(self, verify_set: set[str]) -> None:
        """Create dynamic @tx_tool functions for every discovered remote tool."""
        for name, binding in self.bindings.items():
            self.tools[name] = self._build_wrapped_tool(
                binding, verify_unmodified=(name in verify_set)
            )

    def _build_wrapped_tool(
        self,
        binding: DiscoveredToolBinding,
        *,
        verify_unmodified: bool = False,
    ) -> Any:
        pre_binding = (
            self.bindings.get(binding.pre_state_tool) if binding.pre_state_tool else None
        )
        comp_binding = (
            self.bindings.get(binding.compensate_tool) if binding.compensate_tool else None
        )

        async def _pre_state_hook(fwd_args: dict[str, Any], _res: Any = None) -> Any:
            if pre_binding is None:
                return None
            pre_args = resolve_remote_args(
                pre_binding.input_schema, binding.arg_mapping, fwd_args, {}, None
            )
            return await self.invoke_raw_mcp_tool(
                pre_binding.server_url, pre_binding.tool_name, pre_args
            )

        async def _compensate_hook(
            fwd_args: dict[str, Any], fwd_result: Any, pre_state: Any
        ) -> Any:
            if comp_binding is None:
                return None
            comp_args = resolve_remote_args(
                comp_binding.input_schema,
                binding.arg_mapping,
                fwd_args,
                fwd_result,
                pre_state,
            )
            return await self.invoke_raw_mcp_tool(
                comp_binding.server_url, comp_binding.tool_name, comp_args
            )

        async def _forward_call(**kwargs: Any) -> Any:
            return await self.invoke_raw_mcp_tool(
                binding.server_url, binding.tool_name, kwargs
            )

        _forward_call.__name__ = binding.tool_name
        _forward_call.__doc__ = binding.description

        wrapped = tx_tool(
            name=binding.tool_name,
            effect=binding.effect,
            pre_state=_pre_state_hook if pre_binding is not None else None,
            current_state=_pre_state_hook if (pre_binding is not None and verify_unmodified) else None,
            verify_unmodified=verify_unmodified,
            compensate=_compensate_hook if comp_binding is not None else None,
            deny_tainted=binding.deny_tainted,
            requires_prior=binding.requires_prior,
            max_value=binding.max_value,
        )(_forward_call)

        return wrapped

    async def call(self, tool_name: str, **kwargs: Any) -> Any:
        """Execute a discovered remote MCP tool inside the active AgentTransaction."""
        if tool_name not in self.tools:
            raise KeyError(f"Remote MCP tool '{tool_name}' not found in discovered catalog.")
        return await self.tools[tool_name](**kwargs)

    def get_llm_visible_tools(self) -> list[dict[str, Any]]:
        """Return only forward and read-only tools for the LLM, hiding compensator-only tools."""
        visible: list[dict[str, Any]] = []
        for b in self.bindings.values():
            if not b.is_compensator_only:
                visible.append(
                    {
                        "name": b.tool_name,
                        "description": b.description,
                        "inputSchema": b.input_schema,
                    }
                )
        return visible

    def as_langchain_tools(self) -> list[Any]:
        """Return discovered LLM-visible MCP tools ready for LangChain `AgentExecutor` / `create_tool_calling_agent`."""
        return [
            self.tools[b.tool_name]
            for b in self.bindings.values()
            if not b.is_compensator_only
        ]

    def export_manifest(self) -> dict[str, Any]:
        """Export the auto-discovered transactional bindings as a JSON-serializable manifest."""
        return {
            name: {
                "server_url": b.server_url,
                "effect": b.effect.value,
                "is_compensator_only": b.is_compensator_only,
                "pre_state_tool": b.pre_state_tool,
                "compensate_tool": b.compensate_tool,
                "deny_tainted": b.deny_tainted,
            }
            for name, b in self.bindings.items()
        }


@dataclass
class _LeaseEntry:
    undo_token: str
    expires_at: float
    undo_fn: Any
    committed: bool = False
    rolled_back: bool = False


class ServerLeaseManager:
    """Server-side TTL Lease & Undo-Token Manager ('Dead-Man's Switch' for Remote MCP Servers).

    If an agent crashes or loses network connectivity after mutating a remote MCP server
    and fails to send a `commit` before `ttl_seconds` expires, the server automatically
    executes the registered `undo_fn` to self-heal!
    """

    def __init__(self) -> None:
        self._leases: dict[str, _LeaseEntry] = {}

    def register_lease(self, undo_fn: Any, *, ttl_seconds: float = 30.0) -> str:
        token = f"utok_{uuid4().hex[:10]}"
        self._leases[token] = _LeaseEntry(
            undo_token=token,
            expires_at=time.monotonic() + ttl_seconds,
            undo_fn=undo_fn,
        )
        return token

    async def commit_lease(self, undo_token: str) -> bool:
        entry = self._leases.get(undo_token)
        if entry is None or entry.rolled_back:
            return False
        entry.committed = True
        return True

    async def rollback_lease(self, undo_token: str) -> Any:
        entry = self._leases.get(undo_token)
        if entry is None or entry.committed or entry.rolled_back:
            return None
        entry.rolled_back = True
        res = entry.undo_fn()
        if hasattr(res, "__await__"):
            res = await res
        return res

    async def sweep_expired_leases(self) -> list[str]:
        """Roll back all uncommitted leases whose TTL has expired."""
        now = time.monotonic()
        expired_tokens: list[str] = []
        for token, entry in list(self._leases.items()):
            if not entry.committed and not entry.rolled_back and now >= entry.expires_at:
                await self.rollback_lease(token)
                expired_tokens.append(token)
        return expired_tokens

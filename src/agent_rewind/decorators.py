"""Ergonomic decorators (@tx_tool) for registering transactional tools and compensations."""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from typing import Any, overload

from agent_rewind.models import EffectType, TaintLevel


class TransactionalToolSpec:
    """Metadata specification attached to a function decorated with @tx_tool."""

    def __init__(
        self,
        fn: Callable[..., Any],
        *,
        name: str | None = None,
        effect: EffectType = EffectType.COMPENSATABLE,
        compensate_fn: Callable[..., Any] | None = None,
        pre_state_fn: Callable[..., Any] | None = None,
        current_state_fn: Callable[..., Any] | None = None,
        verify_unmodified: bool = False,
        taint: TaintLevel = TaintLevel.TRUSTED,
        deny_tainted: list[str] | None = None,
        requires_prior: list[str] | None = None,
        max_value: dict[str, float] | None = None,
        require_approval: bool = False,
        compensation_retries: int | None = None,
        compensation_backoff_ms: float | None = None,
        simulated_result: Any = None,
        timeout: float | None = None,
    ) -> None:
        self.fn = fn
        self.name = name or fn.__name__
        self.effect = effect
        self.compensate_fn = compensate_fn
        self.pre_state_fn = pre_state_fn
        self.current_state_fn = current_state_fn
        self.verify_unmodified = verify_unmodified
        self.taint = taint
        self.deny_tainted = deny_tainted or []
        self.requires_prior = requires_prior or []
        self.max_value = max_value or {}
        self.require_approval = require_approval
        self.compensation_retries = compensation_retries
        self.compensation_backoff_ms = compensation_backoff_ms
        self.simulated_result = simulated_result
        self.timeout = timeout
        self.signature = inspect.signature(fn)


async def invoke_maybe_async(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Invoke a callable whether it is synchronous or asynchronous."""
    res = fn(*args, **kwargs)
    if inspect.isawaitable(res):
        return await res
    return res


async def invoke_compensate_fn(
    comp_fn: Callable[..., Any],
    forward_args: dict[str, Any],
    forward_result: Any,
    pre_state: Any,
) -> Any:
    """Smartly invoke a compensation function based on its parameter arity or undo_args."""
    if isinstance(forward_args, dict) and "__undo_args__" in forward_args:
        return await invoke_maybe_async(comp_fn, **forward_args["__undo_args__"])
    sig = inspect.signature(comp_fn)
    params = [
        p
        for p in sig.parameters.values()
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    has_varargs = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in sig.parameters.values())

    if has_varargs or len(params) >= 3:
        return await invoke_maybe_async(comp_fn, forward_args, forward_result, pre_state)
    if len(params) == 2:
        return await invoke_maybe_async(comp_fn, forward_args, forward_result)
    if len(params) == 1:
        return await invoke_maybe_async(comp_fn, forward_args)
    return await invoke_maybe_async(comp_fn)


@overload
def tx_tool(fn: Callable[..., Any]) -> Callable[..., Any]: ...


@overload
def tx_tool(
    *,
    name: str | None = None,
    effect: EffectType | None = None,
    deferred: bool = False,
    read_only: bool = False,
    compensate: Callable[..., Any] | None = None,
    pre_state: Callable[[dict[str, Any]], Any] | None = None,
    current_state: Callable[[dict[str, Any], Any], Any] | None = None,
    verify_unmodified: bool = False,
    taint: TaintLevel | str = TaintLevel.TRUSTED,
    deny_tainted: list[str] | None = None,
    requires_prior: list[str] | None = None,
    max_value: dict[str, float] | None = None,
    require_approval: bool = False,
    compensation_retries: int | None = None,
    compensation_backoff_ms: float | None = None,
    simulated_result: Any = None,
    timeout: float | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]: ...


def tx_tool(
    fn: Callable[..., Any] | None = None,
    *,
    name: str | None = None,
    effect: EffectType | None = None,
    deferred: bool = False,
    read_only: bool = False,
    compensate: Callable[..., Any] | None = None,
    pre_state: Callable[[dict[str, Any]], Any] | None = None,
    current_state: Callable[[dict[str, Any], Any], Any] | None = None,
    verify_unmodified: bool = False,
    taint: TaintLevel | str = TaintLevel.TRUSTED,
    deny_tainted: list[str] | None = None,
    requires_prior: list[str] | None = None,
    max_value: dict[str, float] | None = None,
    require_approval: bool = False,
    compensation_retries: int | None = None,
    compensation_backoff_ms: float | None = None,
    simulated_result: Any = None,
    timeout: float | None = None,
) -> Any:
    """Decorate a sync or async function as a Zero-Trust Transactional Tool.

    Supports three ways to register compensation:
    1. Inline parameter: `@tx_tool(compensate=lambda args, res: ...)`
    2. Companion decorator: `@my_tool.compensate`
    3. Returning an `ActionReceipt(result=..., undo=lambda: ...)` from the tool body.

    Supports Outbox buffering for irreversible tools via `@tx_tool(deferred=True)`.
    Supports Data-Flow Isolation via `taint="UNTRUSTED"` and `deny_tainted=["iban"]`.
    """
    resolved_effect = effect
    if resolved_effect is None:
        if deferred:
            resolved_effect = EffectType.DEFERRED
        elif read_only:
            resolved_effect = EffectType.READ_ONLY
        else:
            resolved_effect = EffectType.COMPENSATABLE

    resolved_taint = TaintLevel(taint) if isinstance(taint, str) else taint

    def _decorator(target_fn: Callable[..., Any]) -> Callable[..., Any]:
        spec = TransactionalToolSpec(
            target_fn,
            name=name,
            effect=resolved_effect,
            compensate_fn=compensate,
            pre_state_fn=pre_state,
            current_state_fn=current_state,
            verify_unmodified=verify_unmodified,
            taint=resolved_taint,
            deny_tainted=deny_tainted,
            requires_prior=requires_prior,
            max_value=max_value,
            require_approval=require_approval,
            compensation_retries=compensation_retries,
            compensation_backoff_ms=compensation_backoff_ms,
            simulated_result=simulated_result,
            timeout=timeout,
        )

        is_async_fn = inspect.iscoroutinefunction(target_fn)

        async def _async_exec(*args: Any, **kwargs: Any) -> Any:
            from agent_rewind.kernel import get_current_transaction

            tx = get_current_transaction()
            if tx is None:
                return await invoke_maybe_async(target_fn, *args, **kwargs)
            return await tx.execute_spec(spec, *args, **kwargs)

        @functools.wraps(target_fn)
        def _wrapper(*args: Any, **kwargs: Any) -> Any:
            from agent_rewind.kernel import _run_coro_sync

            if is_async_fn:
                return _async_exec(*args, **kwargs)
            return _run_coro_sync(_async_exec(*args, **kwargs))

        async def _ainvoke(input_data: Any = None, config: Any = None, **kwargs: Any) -> Any:
            params = dict(input_data) if isinstance(input_data, dict) else kwargs
            return await _async_exec(**params)

        def _invoke(input_data: Any = None, config: Any = None, **kwargs: Any) -> Any:
            from agent_rewind.kernel import _run_coro_sync

            return _run_coro_sync(_ainvoke(input_data, config=config, **kwargs))

        def _register_compensate(comp_fn: Callable[..., Any]) -> Callable[..., Any]:
            spec.compensate_fn = comp_fn
            return comp_fn

        def _register_pre_state(pre_fn: Callable[[dict[str, Any]], Any]) -> Callable[..., Any]:
            spec.pre_state_fn = pre_fn
            return pre_fn

        _wrapper.__tx_spec__ = spec  # type: ignore[attr-defined]
        _wrapper.name = spec.name  # type: ignore[attr-defined]
        _wrapper.description = (target_fn.__doc__ or "").strip()  # type: ignore[attr-defined]
        _wrapper.compensate = _register_compensate  # type: ignore[attr-defined]
        _wrapper.pre_state = _register_pre_state  # type: ignore[attr-defined]
        _wrapper.ainvoke = _ainvoke  # type: ignore[attr-defined]
        _wrapper.invoke = _invoke  # type: ignore[attr-defined]
        _wrapper.__raw_fn__ = target_fn  # type: ignore[attr-defined]
        return _wrapper

    if fn is not None:
        return _decorator(fn)
    return _decorator

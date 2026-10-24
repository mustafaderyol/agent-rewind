"""Transactional Agent Kernel: Context Manager, Savepoints, LIFO Compensator, Outbox & DLQ."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import concurrent.futures
import contextvars
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, TypeVar
from uuid import uuid4

from pydantic import BaseModel

from agent_rewind.decorators import (
    TransactionalToolSpec,
    invoke_compensate_fn,
    invoke_maybe_async,
)
from agent_rewind.exceptions import (
    CompensationFailedError,
    SecurityManipulationError,
    StateConflictError,
    StepExecutionError,
)
from agent_rewind.journal import JournalBackend, create_journal_backend
from agent_rewind.models import (
    ActionReceipt,
    DLQRecord,
    EffectType,
    Ref,
    StepRecord,
    StepStatus,
    TaintLevel,
    TransactionReport,
    TransactionState,
)
from agent_rewind.policy import PolicyGuard
from agent_rewind.quarantine import QuarantinedExtractor
from agent_rewind.vault import DataVault

_current_tx: contextvars.ContextVar[AgentTransaction | None] = contextvars.ContextVar(
    "_current_tx", default=None
)
_current_idem_key: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_current_idem_key", default=None
)

S = TypeVar("S", bound=BaseModel)


def get_current_transaction() -> AgentTransaction | None:
    """Return the currently active AgentTransaction in this async context, if any."""
    return _current_tx.get()


def get_current_idempotency_key() -> str | None:
    """Return the active step's deterministic idempotency key for HTTP header injection."""
    return _current_idem_key.get()


def compute_idempotency_key(tx_id: str, phase: str, tool_name: str, args: dict[str, Any]) -> str:
    """Compute a deterministic SHA-256 idempotency key from transaction ID, tool name, and arguments."""
    canonical = json.dumps(args, sort_keys=True, default=str)
    digest = hashlib.sha256(f"{tx_id}:{phase}:{tool_name}:{canonical}".encode()).hexdigest()[:16]
    return f"idem_{phase}_{digest}"


@dataclass
class _CompensationEntry:
    step_record: StepRecord
    spec: TransactionalToolSpec
    resolved_args: dict[str, Any]
    raw_result: Any
    pre_state: Any
    post_state: Any
    receipt_undo: Callable[[], Any] | None


@dataclass
class _OutboxEntry:
    step_record: StepRecord
    spec: TransactionalToolSpec
    resolved_args: dict[str, Any]


class _SavepointContext:
    """Nested Savepoint context manager allowing partial rollback without aborting earlier steps."""

    def __init__(
        self,
        tx: AgentTransaction,
        name: str,
        *,
        suppress_errors: bool = False,
    ) -> None:
        self.tx = tx
        self.name = name
        self.suppress_errors = suppress_errors
        self._comp_stack_idx = 0
        self._outbox_idx = 0

    async def __aenter__(self) -> _SavepointContext:
        self._comp_stack_idx = len(self.tx._compensation_stack)
        self._outbox_idx = len(self.tx._outbox_queue)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> bool:
        if exc_val is not None:
            self.tx._unrecovered_error = None
            await self.tx._rollback_to_indices(
                comp_idx=self._comp_stack_idx,
                outbox_idx=self._outbox_idx,
            )
            return self.suppress_errors
        return False

    def __enter__(self) -> _SavepointContext:
        self._comp_stack_idx = len(self.tx._compensation_stack)
        self._outbox_idx = len(self.tx._outbox_queue)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> bool:
        if exc_val is not None:
            self.tx._unrecovered_error = None
            _run_coro_sync(
                self.tx._rollback_to_indices(
                    comp_idx=self._comp_stack_idx,
                    outbox_idx=self._outbox_idx,
                )
            )
            return self.suppress_errors
        return False


def _run_coro_sync(coro: Any) -> Any:
    """Run an async coroutine synchronously, even if an event loop is already running in this thread."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        ctx = contextvars.copy_context()
        return pool.submit(ctx.run, asyncio.run, coro).result()


class AgentTransaction:
    """Zero-Trust Transactional Execution Context for Multi-System Agent Workflows.

    Features:
    - Automatic LIFO compensation stack for reversible tools (`@tx_tool(compensate=...)`)
    - Partial Rollback via Savepoints (`async with tx.savepoint("branch"):`)
    - Outbox commit buffer for irreversible tools (`@tx_tool(deferred=True)`)
    - DataVault & PolicyGuard for CaMeL-style taint tracking, endorsement & HITL approval
    - Durable SQLite Write-Ahead Log (WAL), Idempotency Cache, and Dead-Letter Queue (DLQ)
    - Tamper-evident SHA-256 hash-chained audit trail (`tx.report.verify_audit_integrity()`)
    """

    def __init__(
        self,
        *,
        tx_id: str | None = None,
        storage: JournalBackend | str | Path | None = None,
        journal: JournalBackend | str | Path | None = None,
        wal_path: str | Path = ":memory:",
        policy: PolicyGuard | None = None,
        approval_handler: Callable[[str, dict[str, Any]], Any] | None = None,
        quarantine_extractor: Callable[[str, type[BaseModel]], Any] | None = None,
        suppress_errors: bool = False,
        auto_wrap_refs: bool = False,
        compensation_retries: int = 3,
        retry_backoff_ms: float = 20.0,
        default_step_timeout: float | None = None,
        close_journal_on_exit: bool = True,
    ) -> None:
        self.tx_id = tx_id or f"tx_{uuid4().hex[:10]}"
        self.vault = DataVault()
        self.policy = policy or PolicyGuard()
        self.approval_handler = approval_handler
        effective_storage = journal if journal is not None else storage
        self.journal: JournalBackend = create_journal_backend(
            effective_storage if effective_storage is not None else wal_path
        )
        self.quarantine = QuarantinedExtractor(quarantine_extractor)
        self.suppress_errors = suppress_errors
        self.auto_wrap_refs = auto_wrap_refs
        self.compensation_retries = max(1, compensation_retries)
        self.retry_backoff_ms = retry_backoff_ms
        self.default_step_timeout = default_step_timeout
        self.close_journal_on_exit = close_journal_on_exit

        self.state = TransactionState.ACTIVE
        self.steps: list[StepRecord] = []
        self.dlq_entries: list[DLQRecord] = []
        self._compensation_stack: list[_CompensationEntry] = []
        self._outbox_queue: list[_OutboxEntry] = []
        self._token: contextvars.Token[AgentTransaction | None] | None = None
        self._started_at = datetime.now(timezone.utc)
        self._completed_at: datetime | None = None
        self._aborted_reason: str | None = None
        self._unrecovered_error: str | None = None
        self._last_ref: Ref[Any] | None = None
        self._last_hash: str = "GENESIS"
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> AgentTransaction:
        self._token = _current_tx.set(self)
        self.journal.record_tx_start(self.tx_id, self._started_at.isoformat())
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> bool:
        try:
            if exc_val is not None:
                self._aborted_reason = f"{exc_type.__name__ if exc_type else 'Error'}: {exc_val}"
                await self.rollback(reason=self._aborted_reason)
                return self.suppress_errors
            elif self._unrecovered_error is not None:
                self._aborted_reason = self._unrecovered_error
                await self.rollback(reason=self._aborted_reason)
                return False
            else:
                await self.commit()
                return False
        finally:
            if self._token is not None:
                _current_tx.reset(self._token)
            if self.close_journal_on_exit:
                self.journal.close()

    def __enter__(self) -> AgentTransaction:
        """Synchronous context manager support (e.g. for `agent_executor.invoke(...)`)."""
        self._token = _current_tx.set(self)
        self.journal.record_tx_start(self.tx_id, self._started_at.isoformat())
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> bool:
        try:
            if exc_val is not None:
                self._aborted_reason = f"{exc_type.__name__ if exc_type else 'Error'}: {exc_val}"
                _run_coro_sync(self.rollback(reason=self._aborted_reason))
                return self.suppress_errors
            elif self._unrecovered_error is not None:
                self._aborted_reason = self._unrecovered_error
                _run_coro_sync(self.rollback(reason=self._aborted_reason))
                return False
            else:
                _run_coro_sync(self.commit())
                return False
        finally:
            if self._token is not None:
                _current_tx.reset(self._token)
            if self.close_journal_on_exit:
                self.journal.close()

    def savepoint(self, name: str = "sp", *, suppress_errors: bool = False) -> _SavepointContext:
        """Create a nested checkpoint (Savepoint) for partial rollback of a sub-workflow."""
        return _SavepointContext(self, name, suppress_errors=suppress_errors)

    async def run(self, tool_fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Convenience method to invoke a @rewind_tool / @tx_tool or plain callable inside this transaction."""
        spec = getattr(tool_fn, "__tx_spec__", None)
        if spec is not None:
            return await self.execute_spec(spec, *args, **kwargs)
        return await invoke_maybe_async(tool_fn, *args, **kwargs)

    async def dispatch_tool_calls(
        self,
        tool_calls: list[Any],
        tools: list[Any] | dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Dispatch a batch of LLM tool calls (OpenAI, Anthropic, Gemini, or LangChain format) inside this transaction."""
        registry: dict[str, Any] = {}
        if isinstance(tools, dict):
            registry.update(tools)
        else:
            for t in tools:
                spec = getattr(t, "__tx_spec__", None)
                t_name = spec.name if spec else getattr(t, "name", getattr(t, "__name__", str(t)))
                registry[t_name] = t

        results: list[dict[str, Any]] = []
        for idx, call in enumerate(tool_calls):
            call_id = f"call_{idx + 1}"
            if isinstance(call, dict):
                call_id = call.get("id", call.get("tool_call_id", call_id))
                fn_name = call.get("name") or call.get("tool") or call.get("function", {}).get("name")
                raw_args = call.get("args") or call.get("arguments") or call.get("input") or call.get("function", {}).get("arguments", {})
            elif hasattr(call, "function"):  # OpenAI ChatCompletionMessageToolCall
                call_id = getattr(call, "id", call_id)
                fn_name = call.function.name
                raw_args = call.function.arguments
            else:  # Anthropic ToolUseBlock / Gemini FunctionCall
                call_id = getattr(call, "id", call_id)
                fn_name = getattr(call, "name", "")
                raw_args = getattr(call, "input", getattr(call, "args", {}))

            parsed_args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args or {})
            if fn_name not in registry:
                raise KeyError(f"LLM requested unknown tool '{fn_name}'.")
            target = registry[fn_name]
            out = await self.run(target, **parsed_args)
            results.append({"tool_call_id": call_id, "name": fn_name, "output": out})
        return results

    def dispatch_tool_calls_sync(
        self,
        tool_calls: list[Any],
        tools: list[Any] | dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Synchronous version of `dispatch_tool_calls` for use inside `with AgentRewind() as tx:`."""
        return _run_coro_sync(self.dispatch_tool_calls(tool_calls, tools))

    def _seal_and_persist_step(self, step_record: StepRecord) -> None:
        prev = (
            self.steps[step_record.step_index - 2].step_hash
            if step_record.step_index >= 2
            else "GENESIS"
        )
        self._last_hash = step_record.seal_hash(prev)
        self.journal.upsert_step(self.tx_id, step_record)

    def _recompute_hash_chain(self) -> None:
        prev = "GENESIS"
        for step in self.steps:
            prev = step.seal_hash(prev)
            self.journal.upsert_step(self.tx_id, step)
        self._last_hash = prev

    @property
    def last_ref(self) -> Ref[Any] | None:
        """Return the symbolic Ref of the most recently executed tool step."""
        return self._last_ref

    async def quarantine_extract(
        self,
        untrusted_input: str | Ref[Any],
        schema: type[S],
        *,
        extractor: Callable[[str, type[S]], Any] | None = None,
        alias: str | None = None,
    ) -> Ref[S]:
        """Extract a strictly typed Pydantic schema from untrusted text inside a zero-tool sandbox."""
        extracted_model = await self.quarantine.extract(
            untrusted_input,
            schema,
            custom_extractor=extractor,
        )
        ref = self.vault.put(
            extracted_model,
            taint=TaintLevel.UNTRUSTED,
            source_tool="quarantine_extractor",
            alias=alias,
        )
        self._last_ref = ref
        return ref

    async def execute_spec(
        self,
        spec: TransactionalToolSpec,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Intercept and execute a @tx_tool call under transactional & zero-trust control."""
        async with self._lock:
            if self.state != TransactionState.ACTIVE:
                raise RuntimeError(
                    f"Cannot execute tool '{spec.name}' when transaction state is {self.state.value}"
                )

            bound = spec.signature.bind(*args, **kwargs)
            bound.apply_defaults()
            raw_args: dict[str, Any] = {}
            for p_name, p_val in bound.arguments.items():
                param_obj = spec.signature.parameters.get(p_name)
                if (
                    param_obj is not None
                    and param_obj.kind == param_obj.VAR_KEYWORD
                    and isinstance(p_val, dict)
                ):
                    raw_args.update(p_val)
                else:
                    raw_args[p_name] = p_val

            # 1. Resolve symbolic Refs (recursively across dicts/lists) and track taint provenance
            resolved_args, provenance = self.vault.resolve_arguments(raw_args)

            step_index = len(self.steps) + 1
            idem_key = compute_idempotency_key(self.tx_id, "fwd", spec.name, resolved_args)
            step_record = StepRecord(
                step_index=step_index,
                idempotency_key=idem_key,
                tool_name=spec.name,
                effect_type=spec.effect,
                taint_level=spec.taint,
                args=resolved_args,
                status=StepStatus.PENDING,
            )
            self.steps.append(step_record)
            self._seal_and_persist_step(step_record)

            # 2. Enforce transaction-level PolicyGuard (sync + async HITL) & tool-level rules
            try:
                await self.policy.evaluate_async(spec.name, resolved_args, provenance, self.vault)
                if spec.requires_prior:
                    executed_tools = {
                        s.tool_name
                        for s in self.steps[:-1]
                        if s.status in (StepStatus.EXECUTED, StepStatus.BUFFERED)
                    }
                    missing_prior = [req for req in spec.requires_prior if req not in executed_tools]
                    if missing_prior:
                        raise SecurityManipulationError(
                            f"Execution order violation on '{spec.name}': prerequisite tool(s) {missing_prior} must be executed first.",
                            tool_name=spec.name,
                            violated_rule="requires_prior",
                        )
                if spec.deny_tainted:
                    tainted_hits = [
                        arg
                        for arg in spec.deny_tainted
                        if arg in provenance
                        and provenance[arg].taint == TaintLevel.UNTRUSTED
                        and not self.policy.is_endorsed(spec.name, arg)
                    ]
                    if tainted_hits:
                        raise SecurityManipulationError(
                            f"Tool '{spec.name}' rejected unverified untrusted/tainted arguments: {tainted_hits}",
                            tool_name=spec.name,
                            violated_rule="tool_deny_tainted",
                            tainted_fields=tainted_hits,
                        )
                if spec.max_value:
                    for field_name, limit_val in spec.max_value.items():
                        if field_name in resolved_args:
                            actual_val = float(resolved_args[field_name])
                            if actual_val > limit_val:
                                raise SecurityManipulationError(
                                    f"Tool '{spec.name}' argument '{field_name}'={actual_val} exceeds max_value ceiling ({limit_val})",
                                    tool_name=spec.name,
                                    violated_rule=f"max_value:{field_name}<={limit_val}",
                                    tainted_fields=[field_name],
                                )
                if spec.require_approval and self.approval_handler is not None:
                    approved = await invoke_maybe_async(
                        self.approval_handler, spec.name, resolved_args
                    )
                    if not approved:
                        raise SecurityManipulationError(
                            f"Human-in-the-Loop approval rejected for tool '{spec.name}'",
                            tool_name=spec.name,
                            violated_rule="require_approval",
                        )
            except SecurityManipulationError as sec_err:
                step_record.status = StepStatus.FAILED
                step_record.error = str(sec_err)
                self._unrecovered_error = step_record.error
                self._seal_and_persist_step(step_record)
                raise

            # 3. Handle DEFERRED (Outbox / Irreversible) tools
            if spec.effect == EffectType.DEFERRED:
                step_record.status = StepStatus.BUFFERED
                simulated = (
                    spec.simulated_result
                    if spec.simulated_result is not None
                    else {
                        "status": "buffered_in_outbox",
                        "tool": spec.name,
                        "step_id": step_record.step_id,
                    }
                )
                step_record.result = simulated
                self._seal_and_persist_step(step_record)
                self._outbox_queue.append(
                    _OutboxEntry(step_record=step_record, spec=spec, resolved_args=resolved_args)
                )
                ref = self.vault.put(
                    simulated,
                    taint=spec.taint,
                    source_tool=spec.name,
                    alias=f"step_{step_index}",
                )
                self._last_ref = ref
                return ref if self.auto_wrap_refs else simulated

            # 4. Idempotency Guard
            already_done, cached_res = self.journal.get_idempotent_result(idem_key)
            if already_done:
                step_record.result = cached_res
                step_record.status = StepStatus.EXECUTED
                self._seal_and_persist_step(step_record)
                ref = self.vault.put(
                    cached_res,
                    taint=spec.taint,
                    source_tool=spec.name,
                    alias=f"step_{step_index}",
                )
                self._last_ref = ref
                return ref if self.auto_wrap_refs else cached_res

            # 5. Capture pre-state snapshot before executing COMPENSATABLE tool
            pre_state: Any = None
            if spec.pre_state_fn is not None:
                pre_state = await invoke_maybe_async(spec.pre_state_fn, resolved_args)
                step_record.pre_state = pre_state
                self._seal_and_persist_step(step_record)

            # 6. Execute the forward tool (with timeout & active idempotency context var)
            effective_timeout = spec.timeout or self.default_step_timeout
            idem_token = _current_idem_key.set(idem_key)
            try:
                coro = invoke_maybe_async(spec.fn, **resolved_args)
                if effective_timeout is not None:
                    raw_output = await asyncio.wait_for(coro, timeout=effective_timeout)
                else:
                    raw_output = await coro
            except SecurityManipulationError as sec_err:
                step_record.status = StepStatus.FAILED
                step_record.error = str(sec_err)
                self._unrecovered_error = step_record.error
                self._seal_and_persist_step(step_record)
                raise
            except Exception as exc:
                step_record.status = StepStatus.FAILED
                step_record.error = f"{type(exc).__name__}: {exc}"
                self._unrecovered_error = step_record.error
                self._seal_and_persist_step(step_record)
                raise StepExecutionError(
                    f"Step {step_index} ('{spec.name}') failed: {exc}",
                    tool_name=spec.name,
                    original_error=exc,
                ) from exc
            finally:
                _current_idem_key.reset(idem_token)

            # 7. Unwrap ActionReceipt if returned
            receipt_undo: Callable[[], Any] | None = None
            post_state: Any = None
            effective_taint = spec.taint

            if isinstance(raw_output, ActionReceipt):
                actual_result = raw_output.result
                receipt_undo = raw_output.undo
                if raw_output.undo_args is not None:
                    resolved_args = {**resolved_args, "__undo_args__": dict(raw_output.undo_args)}
                if raw_output.pre_state is not None:
                    pre_state = raw_output.pre_state
                if raw_output.post_state is not None:
                    post_state = raw_output.post_state
                if raw_output.taint is not None:
                    effective_taint = raw_output.taint
            else:
                actual_result = raw_output

            if post_state is None and spec.current_state_fn is not None:
                post_state = await invoke_maybe_async(
                    spec.current_state_fn, resolved_args, actual_result
                )

            step_record.pre_state = pre_state
            step_record.post_state = post_state
            step_record.result = actual_result
            step_record.taint_level = effective_taint
            step_record.status = StepStatus.EXECUTED
            self._seal_and_persist_step(step_record)

            if spec.effect != EffectType.READ_ONLY:
                self.journal.save_idempotent_result(
                    idem_key,
                    spec.name,
                    "fwd",
                    actual_result,
                    step_record.timestamp.isoformat(),
                )

            # 8. Push to LIFO Compensation Stack if COMPENSATABLE
            if spec.effect == EffectType.COMPENSATABLE and (
                receipt_undo is not None or spec.compensate_fn is not None
            ):
                self._compensation_stack.append(
                    _CompensationEntry(
                        step_record=step_record,
                        spec=spec,
                        resolved_args=resolved_args,
                        raw_result=actual_result,
                        pre_state=pre_state,
                        post_state=post_state,
                        receipt_undo=receipt_undo,
                    )
                )

            # 9. Store output in DataVault with provenance & taint label
            ref = self.vault.put(
                actual_result,
                taint=effective_taint,
                source_tool=spec.name,
                alias=f"step_{step_index}",
            )
            self._last_ref = ref
            return ref if self.auto_wrap_refs else actual_result

    async def commit(self) -> None:
        """Flush all buffered Outbox steps and finalize the transaction."""
        if self.state != TransactionState.ACTIVE:
            return
        self.state = TransactionState.COMMITTING
        self.journal.update_tx_state(self.tx_id, self.state)

        for entry in self._outbox_queue:
            try:
                flushed_res = await invoke_maybe_async(entry.spec.fn, **entry.resolved_args)
                entry.step_record.result = flushed_res
                entry.step_record.status = StepStatus.FLUSHED
            except Exception as exc:
                entry.step_record.status = StepStatus.FAILED
                entry.step_record.error = f"Outbox flush failed: {exc}"
                self._recompute_hash_chain()
                await self.rollback(reason=f"Outbox flush failed on '{entry.spec.name}': {exc}")
                raise StepExecutionError(
                    f"Outbox flush failed on '{entry.spec.name}': {exc}",
                    tool_name=entry.spec.name,
                    original_error=exc,
                ) from exc

        self._recompute_hash_chain()
        self.state = TransactionState.COMMITTED
        self._completed_at = datetime.now(timezone.utc)
        self.journal.update_tx_state(
            self.tx_id,
            self.state,
            completed_at=self._completed_at.isoformat(),
        )

    async def _rollback_to_indices(self, *, comp_idx: int, outbox_idx: int) -> None:
        """Unwind compensation stack down to `comp_idx` and discard outbox down to `outbox_idx`."""
        while len(self._outbox_queue) > outbox_idx:
            outbox_entry = self._outbox_queue.pop()
            if outbox_entry.step_record.status == StepStatus.BUFFERED:
                outbox_entry.step_record.status = StepStatus.DISCARDED

        compensation_failures: list[dict[str, Any]] = []
        while len(self._compensation_stack) > comp_idx:
            entry = self._compensation_stack.pop()
            step_rec = entry.step_record
            spec = entry.spec

            try:
                if spec.verify_unmodified and spec.current_state_fn is not None:
                    live_state = await invoke_maybe_async(
                        spec.current_state_fn, entry.resolved_args, entry.raw_result
                    )
                    if live_state != entry.post_state:
                        raise StateConflictError(
                            f"Concurrent modification detected on '{spec.name}' before compensation: "
                            f"expected state {entry.post_state!r}, found {live_state!r}",
                            tool_name=spec.name,
                            expected_post_state=entry.post_state,
                            actual_current_state=live_state,
                        )

                last_comp_err: Exception | None = None
                comp_res: Any = None
                max_attempts = max(
                    1,
                    spec.compensation_retries
                    if spec.compensation_retries is not None
                    else self.compensation_retries,
                )
                backoff_ms = (
                    spec.compensation_backoff_ms
                    if spec.compensation_backoff_ms is not None
                    else self.retry_backoff_ms
                )
                for attempt in range(max_attempts):
                    try:
                        if entry.receipt_undo is not None:
                            comp_res = await invoke_maybe_async(entry.receipt_undo)
                        elif spec.compensate_fn is not None:
                            comp_res = await invoke_compensate_fn(
                                spec.compensate_fn,
                                entry.resolved_args,
                                entry.raw_result,
                                entry.pre_state,
                            )
                        else:
                            comp_res = None
                        last_comp_err = None
                        break
                    except Exception as attempt_exc:
                        last_comp_err = attempt_exc
                        if attempt + 1 < max_attempts:
                            await asyncio.sleep(
                                (backoff_ms / 1000.0) * (2**attempt)
                            )

                if last_comp_err is not None:
                    raise last_comp_err

                step_rec.compensation_result = comp_res
                step_rec.status = StepStatus.COMPENSATED
            except Exception as comp_exc:
                step_rec.status = StepStatus.FAILED_COMPENSATION
                step_rec.error = f"Compensation error: {comp_exc}"
                dlq_rec = DLQRecord(
                    tx_id=self.tx_id,
                    step_id=step_rec.step_id,
                    tool_name=spec.name,
                    args=entry.resolved_args,
                    result=entry.raw_result,
                    pre_state=entry.pre_state,
                    error=str(comp_exc),
                )
                self.dlq_entries.append(dlq_rec)
                self.journal.insert_dlq_record(dlq_rec)
                compensation_failures.append(
                    {
                        "step_id": step_rec.step_id,
                        "tool_name": spec.name,
                        "dlq_id": dlq_rec.dlq_id,
                        "error": str(comp_exc),
                        "exception": comp_exc,
                    }
                )

        self._recompute_hash_chain()

        if compensation_failures:
            first_exc = compensation_failures[0]["exception"]
            if isinstance(first_exc, StateConflictError):
                raise first_exc
            raise CompensationFailedError(
                f"Rollback encountered {len(compensation_failures)} failed compensation(s); saved to DLQ.",
                failed_steps=compensation_failures,
            )

    async def rollback(self, *, reason: str = "Explicit rollback") -> None:
        """Unwind all executed steps in strict LIFO order and discard buffered Outbox actions."""
        if self.state in (TransactionState.ROLLED_BACK, TransactionState.FAILED_ROLLBACK):
            return

        self.state = TransactionState.ROLLING_BACK
        self._aborted_reason = reason
        self.journal.update_tx_state(self.tx_id, self.state, aborted_reason=reason)

        try:
            await self._rollback_to_indices(comp_idx=0, outbox_idx=0)
        except (StateConflictError, CompensationFailedError):
            self._completed_at = datetime.now(timezone.utc)
            self.state = TransactionState.FAILED_ROLLBACK
            self.journal.update_tx_state(
                self.tx_id,
                self.state,
                completed_at=self._completed_at.isoformat(),
            )
            raise

        self._completed_at = datetime.now(timezone.utc)
        self.state = TransactionState.ROLLED_BACK
        self.journal.update_tx_state(
            self.tx_id,
            self.state,
            completed_at=self._completed_at.isoformat(),
        )

    def export_undo_lease(
        self,
        lease_manager: Any,
        *,
        ttl_seconds: float = 120.0,
    ) -> str | None:
        """Bundle this transaction's entire LIFO compensation stack into a single server lease `undo_token`.

        Used by Remote Sub-Agents (LangGraph nodes, A2A agents, RemoteRunnables) so that if a
        downstream agent in a multi-agent workflow fails, the parent orchestrator can roll back
        every internal tool mutation performed by this sub-agent with a single token.
        """
        if not self._compensation_stack:
            return None
        self.close_journal_on_exit = False

        async def _undo_entire_subagent_tx() -> dict[str, Any]:
            await self.rollback(reason="Parent multi-agent workflow triggered rollback")
            return {
                "subagent_tx_id": self.tx_id,
                "state": self.state.value,
                "compensated_steps": [
                    s.tool_name for s in self.steps if s.status == StepStatus.COMPENSATED
                ],
            }

        return lease_manager.register_lease(_undo_entire_subagent_tx, ttl_seconds=ttl_seconds)

    @property
    def report(self) -> TransactionReport:
        """Generate a structured TransactionReport for auditing and observability."""
        return TransactionReport(
            tx_id=self.tx_id,
            state=self.state,
            steps=list(self.steps),
            aborted_reason=self._aborted_reason,
            compensated_steps=[
                s.tool_name for s in reversed(self.steps) if s.status == StepStatus.COMPENSATED
            ],
            flushed_outbox_steps=[
                s.tool_name for s in self.steps if s.status == StepStatus.FLUSHED
            ],
            discarded_outbox_steps=[
                s.tool_name for s in self.steps if s.status == StepStatus.DISCARDED
            ],
            dlq_entries=list(self.dlq_entries),
            started_at=self._started_at,
            completed_at=self._completed_at,
        )

    @classmethod
    async def replay_dlq(
        cls,
        storage_or_path: JournalBackend | str | Path,
        tools: list[Callable[..., Any]],
    ) -> list[DLQRecord]:
        """Replay unresolved Dead-Letter Queue (DLQ) compensations once downstream services recover."""
        journal = create_journal_backend(storage_or_path)
        tool_map: dict[str, TransactionalToolSpec] = {}
        for t in tools:
            spec = getattr(t, "__tx_spec__", None)
            if spec is not None:
                tool_map[spec.name] = spec

        unresolved = journal.get_unresolved_dlq_records()
        resolved_records: list[DLQRecord] = []

        for dlq in unresolved:
            spec = tool_map.get(dlq.tool_name)
            if spec and spec.compensate_fn:
                await invoke_compensate_fn(
                    spec.compensate_fn,
                    dlq.args,
                    dlq.result,
                    dlq.pre_state,
                )
                dlq.resolved = True
                journal.mark_dlq_resolved(dlq.dlq_id)
                resolved_records.append(dlq)

        journal.close()
        return resolved_records

    @classmethod
    async def recover_from_wal(
        cls,
        storage_or_path: JournalBackend | str | Path,
        tools: list[Callable[..., Any]],
    ) -> list[TransactionReport]:
        """Recover and roll back any incomplete transactions from a persistent WAL backend (JSONL, Redis/KV, SQLite)."""
        journal = create_journal_backend(storage_or_path)
        tool_map: dict[str, TransactionalToolSpec] = {}
        for t in tools:
            spec = getattr(t, "__tx_spec__", None)
            if spec is not None:
                tool_map[spec.name] = spec

        incomplete_tx_ids = journal.get_incomplete_transactions()
        reports: list[TransactionReport] = []

        for tx_id in incomplete_tx_ids:
            steps = journal.load_steps(tx_id)
            journal.update_tx_state(
                tx_id,
                TransactionState.ROLLING_BACK,
                aborted_reason="Recovered from incomplete WAL after process crash",
            )
            compensated_names: list[str] = []
            discarded_names: list[str] = []

            for step in reversed(steps):
                if step.status == StepStatus.BUFFERED:
                    step.status = StepStatus.DISCARDED
                    journal.upsert_step(tx_id, step)
                    discarded_names.append(step.tool_name)
                elif (
                    step.status == StepStatus.EXECUTED
                    and step.effect_type == EffectType.COMPENSATABLE
                ):
                    spec = tool_map.get(step.tool_name)
                    if spec and spec.compensate_fn:
                        comp_res = await invoke_compensate_fn(
                            spec.compensate_fn,
                            step.args,
                            step.result,
                            step.pre_state,
                        )
                        step.compensation_result = comp_res
                        step.status = StepStatus.COMPENSATED
                        journal.upsert_step(tx_id, step)
                        compensated_names.append(step.tool_name)

            now_iso = datetime.now(timezone.utc).isoformat()
            journal.update_tx_state(tx_id, TransactionState.ROLLED_BACK, completed_at=now_iso)
            reports.append(
                TransactionReport(
                    tx_id=tx_id,
                    state=TransactionState.ROLLED_BACK,
                    steps=steps,
                    aborted_reason="Recovered from incomplete WAL after process crash",
                    compensated_steps=compensated_names,
                    discarded_outbox_steps=discarded_names,
                )
            )

        journal.close()
        return reports


AgentRewind = AgentTransaction

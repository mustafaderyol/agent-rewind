"""Zero-Trust Policy, Provenance Guard & Human-in-the-Loop (HITL) Approval Engine."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import inspect
from typing import Any

from agent_rewind.exceptions import SecurityManipulationError
from agent_rewind.models import Ref, TaintLevel
from agent_rewind.vault import DataVault


@dataclass
class PolicyRule:
    """Represents a deterministic security constraint evaluated before tool execution."""

    name: str
    target_tool: str | None  # None means applies to all tools
    check_fn: Callable[[str, dict[str, Any], dict[str, Ref[Any]], DataVault], Any]


class PolicyGuard:
    """Deterministic policy engine that enforces data provenance, financial ceilings, and HITL gates.

    Prevents a compromised/manipulated LLM from passing untrusted or hijacked
    parameters to critical tools at runtime.
    """

    def __init__(self) -> None:
        self._rules: list[PolicyRule] = []
        self._endorsed_targets: set[tuple[str, str]] = set()

    def is_endorsed(self, tool_name: str, arg_name: str) -> bool:
        """Return True if (tool_name, arg_name) was verified by a require_trusted_match rule."""
        return (tool_name, arg_name) in self._endorsed_targets

    def deny_tainted_args(self, tool_name: str, arg_names: list[str]) -> PolicyGuard:
        """Forbid UNTRUSTED (tainted) data from flowing into specific sensitive arguments."""

        def _check(
            t_name: str,
            resolved_args: dict[str, Any],
            provenance: dict[str, Ref[Any]],
            vault: DataVault,
        ) -> None:
            tainted = [
                arg
                for arg in arg_names
                if arg in provenance and provenance[arg].taint == TaintLevel.UNTRUSTED
            ]
            if tainted:
                sources = [f"{arg} (from {provenance[arg].source_tool})" for arg in tainted]
                raise SecurityManipulationError(
                    f"Policy violation on '{t_name}': Untrusted/tainted data cannot be passed to sensitive arguments: {', '.join(sources)}",
                    tool_name=t_name,
                    violated_rule="deny_tainted_args",
                    tainted_fields=tainted,
                )

        self._rules.append(
            PolicyRule(
                name=f"deny_tainted_args:{tool_name}",
                target_tool=tool_name,
                check_fn=_check,
            )
        )
        return self

    def require_trusted_match(
        self,
        tool_name: str,
        arg_name: str,
        trusted_value_or_ref: Any,
        *,
        reason: str = "Argument does not match trusted source of truth",
    ) -> PolicyGuard:
        """Require that a tool argument matches a trusted reference value (e.g. CRM IBAN vs Note IBAN)."""

        def _check(
            t_name: str,
            resolved_args: dict[str, Any],
            provenance: dict[str, Ref[Any]],
            vault: DataVault,
        ) -> None:
            if arg_name not in resolved_args:
                return
            actual_val = resolved_args[arg_name]
            expected_val = (
                trusted_value_or_ref.value
                if isinstance(trusted_value_or_ref, Ref)
                else trusted_value_or_ref()
                if callable(trusted_value_or_ref)
                else trusted_value_or_ref
            )
            if actual_val != expected_val:
                raise SecurityManipulationError(
                    f"Policy violation on '{t_name}.{arg_name}': {reason} "
                    f"(got '{actual_val}', expected trusted value '{expected_val}')",
                    tool_name=t_name,
                    violated_rule="require_trusted_match",
                    tainted_fields=[arg_name],
                )
            self._endorsed_targets.add((t_name, arg_name))

        self._rules.append(
            PolicyRule(
                name=f"require_trusted_match:{tool_name}.{arg_name}",
                target_tool=tool_name,
                check_fn=_check,
            )
        )
        return self

    def max_value(
        self,
        tool_name: str,
        arg_name: str,
        limit: float,
        *,
        reason: str | None = None,
    ) -> PolicyGuard:
        """Circuit breaker that trips if a numeric argument (e.g. financial amount) exceeds a limit."""

        def _check(
            t_name: str,
            resolved_args: dict[str, Any],
            provenance: dict[str, Ref[Any]],
            vault: DataVault,
        ) -> None:
            if arg_name not in resolved_args:
                return
            val = float(resolved_args[arg_name])
            if val > limit:
                msg = reason or (
                    f"Financial circuit breaker tripped on '{t_name}.{arg_name}': "
                    f"value {val} exceeds maximum allowed limit {limit}"
                )
                raise SecurityManipulationError(
                    msg,
                    tool_name=t_name,
                    violated_rule="max_value_circuit_breaker",
                    tainted_fields=[arg_name],
                )

        self._rules.append(
            PolicyRule(
                name=f"max_value:{tool_name}.{arg_name}",
                target_tool=tool_name,
                check_fn=_check,
            )
        )
        return self

    def require_approval(
        self,
        tool_name: str,
        approval_handler: Callable[[str, dict[str, Any]], Any],
        *,
        when: Callable[[dict[str, Any]], bool] | None = None,
        reason: str = "Human-in-the-Loop operator rejected the operation",
    ) -> PolicyGuard:
        """Human-in-the-Loop (HITL) approval gate.

        If `when` is None or returns True, calls `approval_handler(tool_name, resolved_args)`.
        If the handler returns False, aborts the transaction and rolls back preceding steps.
        """

        async def _check(
            t_name: str,
            resolved_args: dict[str, Any],
            provenance: dict[str, Ref[Any]],
            vault: DataVault,
        ) -> None:
            if when is not None and not when(resolved_args):
                return
            approved = approval_handler(t_name, resolved_args)
            if inspect.isawaitable(approved):
                approved = await approved
            if not approved:
                raise SecurityManipulationError(
                    f"HITL Approval Gate denied '{t_name}': {reason}",
                    tool_name=t_name,
                    violated_rule="human_in_the_loop_approval",
                )

        self._rules.append(
            PolicyRule(
                name=f"require_approval:{tool_name}",
                target_tool=tool_name,
                check_fn=_check,
            )
        )
        return self

    def add_rule(
        self,
        name: str,
        predicate: Callable[[dict[str, Any], dict[str, Ref[Any]], DataVault], bool],
        *,
        tool_name: str | None = None,
        error_message: str | None = None,
    ) -> PolicyGuard:
        """Register a custom deterministic predicate. Raises SecurityManipulationError if False."""

        def _check(
            t_name: str,
            resolved_args: dict[str, Any],
            provenance: dict[str, Ref[Any]],
            vault: DataVault,
        ) -> None:
            ok = predicate(resolved_args, provenance, vault)
            if not ok:
                msg = error_message or f"Custom security policy '{name}' failed on tool '{t_name}'."
                raise SecurityManipulationError(
                    msg,
                    tool_name=t_name,
                    violated_rule=name,
                    tainted_fields=[k for k, r in provenance.items() if r.is_tainted],
                )

        self._rules.append(PolicyRule(name=name, target_tool=tool_name, check_fn=_check))
        return self

    def require_prior(
        self,
        tool_name: str,
        required_prior_tools: list[str] | str,
        *,
        reason: str | None = None,
    ) -> PolicyGuard:
        """Enforce that specific prerequisite tools have already executed before `tool_name` is called."""
        req_list = (
            [required_prior_tools]
            if isinstance(required_prior_tools, str)
            else list(required_prior_tools)
        )

        def _check(
            t_name: str,
            resolved_args: dict[str, Any],
            provenance: dict[str, Ref[Any]],
            vault: DataVault,
        ) -> None:
            executed_tools = {r.source_tool for r in vault.refs.values()}
            missing = [req for req in req_list if req not in executed_tools]
            if missing:
                msg = reason or (
                    f"Execution order violation on '{t_name}': prerequisite tool(s) {missing} "
                    f"must be executed before calling '{t_name}'."
                )
                raise SecurityManipulationError(
                    msg,
                    tool_name=t_name,
                    violated_rule="require_prior_tools",
                )

        self._rules.append(
            PolicyRule(
                name=f"require_prior:{tool_name}",
                target_tool=tool_name,
                check_fn=_check,
            )
        )
        return self

    def require_sequence(
        self,
        ordered_tools: list[str],
        *,
        reason: str | None = None,
    ) -> PolicyGuard:
        """Enforce that the LLM calls the specified tools in strict sequential order."""
        for idx in range(1, len(ordered_tools)):
            current_tool = ordered_tools[idx]
            prior_tools = ordered_tools[:idx]
            self.require_prior(
                current_tool,
                prior_tools,
                reason=reason
                or (
                    f"Sequence violation: LLM attempted to call '{current_tool}' out of order "
                    f"(expected sequence: {' -> '.join(ordered_tools)})"
                ),
            )
        return self

    async def evaluate_async(
        self,
        tool_name: str,
        resolved_args: dict[str, Any],
        provenance: dict[str, Ref[Any]],
        vault: DataVault,
    ) -> None:
        """Run all matching sync or async rules for the given tool call."""
        for rule in self._rules:
            if rule.target_tool is None or rule.target_tool == tool_name:
                res = rule.check_fn(tool_name, resolved_args, provenance, vault)
                if inspect.isawaitable(res):
                    await res

    def evaluate(
        self,
        tool_name: str,
        resolved_args: dict[str, Any],
        provenance: dict[str, Ref[Any]],
        vault: DataVault,
    ) -> None:
        """Run all matching synchronous rules for the given tool call."""
        for rule in self._rules:
            if rule.target_tool is None or rule.target_tool == tool_name:
                rule.check_fn(tool_name, resolved_args, provenance, vault)

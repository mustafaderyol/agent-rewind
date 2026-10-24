"""Custom exceptions for the agent-tx transactional runtime."""

from __future__ import annotations

from typing import Any


class AgentTxError(Exception):
    """Base exception for all agent-tx errors."""


class SecurityManipulationError(AgentTxError):
    """Raised when a policy guard detects prompt injection, tainted data flow, or parameter tampering."""

    def __init__(
        self,
        message: str,
        *,
        tool_name: str | None = None,
        violated_rule: str | None = None,
        tainted_fields: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.tool_name = tool_name
        self.violated_rule = violated_rule
        self.tainted_fields = tainted_fields or []


class StepExecutionError(AgentTxError):
    """Raised when a forward tool call fails at runtime."""

    def __init__(self, message: str, *, tool_name: str, original_error: Exception | None = None) -> None:
        super().__init__(message)
        self.tool_name = tool_name
        self.original_error = original_error


class StateConflictError(AgentTxError):
    """Raised when compensating a step detects that the external resource was modified concurrently (dirty write protection)."""

    def __init__(
        self,
        message: str,
        *,
        tool_name: str,
        expected_post_state: Any,
        actual_current_state: Any,
    ) -> None:
        super().__init__(message)
        self.tool_name = tool_name
        self.expected_post_state = expected_post_state
        self.actual_current_state = actual_current_state


class CompensationFailedError(AgentTxError):
    """Raised when one or more compensating actions fail during rollback."""

    def __init__(self, message: str, *, failed_steps: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.failed_steps = failed_steps

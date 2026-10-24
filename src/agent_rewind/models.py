"""Core data structures and types for agent-tx."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from typing import Any, Generic, TypeVar
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class EffectType(str, Enum):
    """Defines how a tool interacts with external systems."""

    READ_ONLY = "READ_ONLY"
    COMPENSATABLE = "COMPENSATABLE"
    DEFERRED = "DEFERRED"  # Buffered in Outbox until transaction commits


class TaintLevel(str, Enum):
    """Information-flow security label for CaMeL-style data provenance tracking."""

    TRUSTED = "TRUSTED"
    UNTRUSTED = "UNTRUSTED"


class StepStatus(str, Enum):
    """Lifecycle status of a single transactional tool step."""

    PENDING = "PENDING"
    EXECUTED = "EXECUTED"
    BUFFERED = "BUFFERED"
    COMPENSATED = "COMPENSATED"
    FLUSHED = "FLUSHED"
    DISCARDED = "DISCARDED"
    FAILED = "FAILED"
    FAILED_COMPENSATION = "FAILED_COMPENSATION"


class TransactionState(str, Enum):
    """Overall state of an AgentTransaction."""

    ACTIVE = "ACTIVE"
    COMMITTING = "COMMITTING"
    COMMITTED = "COMMITTED"
    ROLLING_BACK = "ROLLING_BACK"
    ROLLED_BACK = "ROLLED_BACK"
    FAILED_ROLLBACK = "FAILED_ROLLBACK"


class ActionReceipt(Generic[T]):
    """Self-contained receipt that a tool can return to dynamically bind its own undo closure.

    Example:
        ```python
        @tx_tool
        async def create_user(name: str) -> ActionReceipt[dict]:
            user = await db.insert_user(name)
            return ActionReceipt(
                result=user,
                undo=lambda: db.delete_user(user["id"]),
            )
        ```
    """

    def __init__(
        self,
        result: T,
        *,
        undo: Callable[[], Any] | None = None,
        undo_args: dict[str, Any] | None = None,
        pre_state: Any = None,
        post_state: Any = None,
        taint: TaintLevel | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.result = result
        self.undo = undo
        self.undo_args = undo_args
        self.pre_state = pre_state
        self.post_state = post_state
        self.taint = taint
        self.metadata = metadata or {}


class Ref(BaseModel, Generic[T]):
    """Symbolic reference (pointer) to a value stored in the DataVault.

    Tracks data provenance and taint level across tool steps without exposing
    raw untrusted strings to the privileged planner context.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    ref_id: str = Field(default_factory=lambda: f"ref_{uuid4().hex[:8]}")
    value: T
    taint: TaintLevel = TaintLevel.TRUSTED
    source_tool: str = "user_input"
    field_path: str | None = None

    def attr(self, key: str) -> Ref[Any]:
        """Derive a child Ref from a dict key or object attribute, preserving taint."""
        if isinstance(self.value, dict):
            child_val = self.value[key]
        else:
            child_val = getattr(self.value, key)
        path = f"{self.field_path}.{key}" if self.field_path else key
        return Ref(
            ref_id=f"{self.ref_id}.{key}",
            value=child_val,
            taint=self.taint,
            source_tool=self.source_tool,
            field_path=path,
        )

    @property
    def is_tainted(self) -> bool:
        return self.taint == TaintLevel.UNTRUSTED


class StepRecord(BaseModel):
    """Immutable audit log entry for a single tool invocation within a transaction."""

    step_id: str = Field(default_factory=lambda: f"step_{uuid4().hex[:8]}")
    step_index: int
    idempotency_key: str = Field(default_factory=lambda: f"idem_{uuid4().hex[:12]}")
    tool_name: str
    effect_type: EffectType
    taint_level: TaintLevel = TaintLevel.TRUSTED
    args: dict[str, Any] = Field(default_factory=dict)
    pre_state: Any = None
    post_state: Any = None
    result: Any = None
    status: StepStatus = StepStatus.PENDING
    compensation_result: Any = None
    error: str | None = None
    prev_hash: str = "GENESIS"
    step_hash: str = ""
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def seal_hash(self, prev_hash: str) -> str:
        """Compute a tamper-evident SHA-256 hash chaining this step to the previous step."""
        self.prev_hash = prev_hash
        payload = json.dumps(
            {
                "prev_hash": self.prev_hash,
                "step_id": self.step_id,
                "step_index": self.step_index,
                "idempotency_key": self.idempotency_key,
                "tool_name": self.tool_name,
                "effect_type": self.effect_type.value,
                "taint_level": self.taint_level.value,
                "args": self.args,
                "pre_state": self.pre_state,
                "result": self.result,
                "status": self.status.value,
            },
            sort_keys=True,
            default=str,
        )
        self.step_hash = hashlib.sha256(payload.encode()).hexdigest()
        return self.step_hash


class DLQRecord(BaseModel):
    """Dead-Letter Queue entry for compensations that exhausted all retries."""

    dlq_id: str = Field(default_factory=lambda: f"dlq_{uuid4().hex[:10]}")
    tx_id: str
    step_id: str
    tool_name: str
    args: dict[str, Any]
    result: Any = None
    pre_state: Any = None
    error: str
    resolved: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class TransactionReport(BaseModel):
    """Summary and audit trail of a completed or rolled-back AgentTransaction."""

    tx_id: str
    state: TransactionState
    steps: list[StepRecord] = Field(default_factory=list)
    aborted_reason: str | None = None
    compensated_steps: list[str] = Field(default_factory=list)
    flushed_outbox_steps: list[str] = Field(default_factory=list)
    discarded_outbox_steps: list[str] = Field(default_factory=list)
    dlq_entries: list[DLQRecord] = Field(default_factory=list)
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None

    def verify_audit_integrity(self) -> bool:
        """Cryptographically verify the hash chain across all steps in this transaction."""
        expected_prev = "GENESIS"
        for step in self.steps:
            if step.prev_hash != expected_prev:
                return False
            payload = json.dumps(
                {
                    "prev_hash": step.prev_hash,
                    "step_id": step.step_id,
                    "step_index": step.step_index,
                    "idempotency_key": step.idempotency_key,
                    "tool_name": step.tool_name,
                    "effect_type": step.effect_type.value,
                    "taint_level": step.taint_level.value,
                    "args": step.args,
                    "pre_state": step.pre_state,
                    "result": step.result,
                    "status": step.status.value,
                },
                sort_keys=True,
                default=str,
            )
            recomputed = hashlib.sha256(payload.encode()).hexdigest()
            if step.step_hash != recomputed:
                return False
            expected_prev = step.step_hash
        return True

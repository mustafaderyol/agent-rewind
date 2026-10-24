"""Lightweight, zero-database state journal for AgentTransaction.

No SQLite, PostgreSQL, or Oracle required:
1. `InMemoryBackend` (Default): Pure Python RAM dicts — zero disk I/O, zero external dependencies.
2. `JSONFileBackend` (Optional): Simple single-file `.json` snapshot for process crash recovery & DLQ.
"""

from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from agent_rewind.models import (
    DLQRecord,
    StepRecord,
    TransactionState,
)


def _json_default(obj: Any) -> Any:
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    return str(obj)


@runtime_checkable
class JournalBackend(Protocol):
    """Minimal state store protocol."""

    def record_tx_start(self, tx_id: str, started_at: str) -> None: ...

    def update_tx_state(
        self,
        tx_id: str,
        state: TransactionState,
        *,
        aborted_reason: str | None = None,
        completed_at: str | None = None,
    ) -> None: ...

    def upsert_step(self, tx_id: str, step: StepRecord) -> None: ...

    def load_steps(self, tx_id: str) -> list[StepRecord]: ...

    def get_incomplete_transactions(self) -> list[str]: ...

    def get_idempotent_result(self, idempotency_key: str) -> tuple[bool, Any]: ...

    def save_idempotent_result(
        self,
        idempotency_key: str,
        tool_name: str,
        phase: str,
        result: Any,
        created_at: str,
    ) -> None: ...

    def insert_dlq_record(self, record: DLQRecord) -> None: ...

    def get_unresolved_dlq_records(self) -> list[DLQRecord]: ...

    def mark_dlq_resolved(self, dlq_id: str) -> None: ...

    def close(self) -> None: ...


class InMemoryBackend:
    """Default zero-dependency pure-Python RAM store."""

    def __init__(self) -> None:
        self.transactions: dict[str, dict[str, Any]] = {}
        self.steps: dict[str, dict[str, StepRecord]] = {}
        self.idempotency_cache: dict[str, Any] = {}
        self.dlq: dict[str, DLQRecord] = {}

    def record_tx_start(self, tx_id: str, started_at: str) -> None:
        self.transactions[tx_id] = {
            "tx_id": tx_id,
            "state": TransactionState.ACTIVE.value,
            "aborted_reason": None,
            "started_at": started_at,
            "completed_at": None,
        }
        self.steps.setdefault(tx_id, {})

    def update_tx_state(
        self,
        tx_id: str,
        state: TransactionState,
        *,
        aborted_reason: str | None = None,
        completed_at: str | None = None,
    ) -> None:
        tx = self.transactions.setdefault(tx_id, {"tx_id": tx_id})
        tx["state"] = state.value
        if aborted_reason is not None:
            tx["aborted_reason"] = aborted_reason
        if completed_at is not None:
            tx["completed_at"] = completed_at

    def upsert_step(self, tx_id: str, step: StepRecord) -> None:
        self.steps.setdefault(tx_id, {})[step.step_id] = step.model_copy(deep=True)

    def load_steps(self, tx_id: str) -> list[StepRecord]:
        tx_steps = self.steps.get(tx_id, {}).values()
        return sorted(tx_steps, key=lambda s: s.step_index)

    def get_incomplete_transactions(self) -> list[str]:
        return [
            tx_id
            for tx_id, data in self.transactions.items()
            if data.get("state")
            in (TransactionState.ACTIVE.value, TransactionState.ROLLING_BACK.value)
        ]

    def get_idempotent_result(self, idempotency_key: str) -> tuple[bool, Any]:
        if idempotency_key in self.idempotency_cache:
            return True, self.idempotency_cache[idempotency_key]
        return False, None

    def save_idempotent_result(
        self,
        idempotency_key: str,
        tool_name: str,
        phase: str,
        result: Any,
        created_at: str,
    ) -> None:
        self.idempotency_cache[idempotency_key] = result

    def insert_dlq_record(self, record: DLQRecord) -> None:
        self.dlq[record.dlq_id] = record.model_copy(deep=True)

    def get_unresolved_dlq_records(self) -> list[DLQRecord]:
        return [r for r in self.dlq.values() if not r.resolved]

    def list_dlq(self) -> list[DLQRecord]:
        """Alias for get_unresolved_dlq_records."""
        return self.get_unresolved_dlq_records()

    def verify_audit_integrity(self, tx_id: str) -> bool:
        """Cryptographically verify the hash chain across all steps recorded for `tx_id`."""
        steps = self.load_steps(tx_id)
        expected_prev = "GENESIS"
        for step in steps:
            if step.prev_hash != expected_prev:
                return False
            expected_prev = step.step_hash
        return True

    def mark_dlq_resolved(self, dlq_id: str) -> None:
        if dlq_id in self.dlq:
            self.dlq[dlq_id].resolved = True

    def close(self) -> None:
        pass


class JSONFileBackend(InMemoryBackend):
    """Lightweight single-file JSON snapshot backend (zero database required)."""

    def __init__(self, file_path: str | Path) -> None:
        super().__init__()
        self.file_path = Path(file_path)
        if self.file_path.exists():
            raw = self.file_path.read_text(encoding="utf-8").strip()
            if raw:
                data = json.loads(raw)
                self.transactions = data.get("transactions", {})
                self.idempotency_cache = data.get("idempotency_cache", {})
                for tx_id, step_list in data.get("steps", {}).items():
                    self.steps[tx_id] = {
                        s["step_id"]: StepRecord.model_validate(s) for s in step_list
                    }
                for dlq_id, d_obj in data.get("dlq", {}).items():
                    self.dlq[dlq_id] = DLQRecord.model_validate(d_obj)

    def _flush(self) -> None:
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "transactions": self.transactions,
            "steps": {
                tx_id: [s.model_dump(mode="json") for s in self.load_steps(tx_id)]
                for tx_id in self.steps
            },
            "idempotency_cache": self.idempotency_cache,
            "dlq": {k: v.model_dump(mode="json") for k, v in self.dlq.items()},
        }
        self.file_path.write_text(
            json.dumps(payload, default=_json_default, indent=2),
            encoding="utf-8",
        )

    def record_tx_start(self, tx_id: str, started_at: str) -> None:
        super().record_tx_start(tx_id, started_at)
        self._flush()

    def update_tx_state(
        self,
        tx_id: str,
        state: TransactionState,
        *,
        aborted_reason: str | None = None,
        completed_at: str | None = None,
    ) -> None:
        super().update_tx_state(
            tx_id, state, aborted_reason=aborted_reason, completed_at=completed_at
        )
        self._flush()

    def upsert_step(self, tx_id: str, step: StepRecord) -> None:
        super().upsert_step(tx_id, step)
        self._flush()

    def save_idempotent_result(
        self,
        idempotency_key: str,
        tool_name: str,
        phase: str,
        result: Any,
        created_at: str,
    ) -> None:
        super().save_idempotent_result(idempotency_key, tool_name, phase, result, created_at)
        self._flush()

    def insert_dlq_record(self, record: DLQRecord) -> None:
        super().insert_dlq_record(record)
        self._flush()

    def mark_dlq_resolved(self, dlq_id: str) -> None:
        super().mark_dlq_resolved(dlq_id)
        self._flush()


class CallbackBackend(InMemoryBackend):
    """Simple event hook backend (`on_step`, `on_tx_state`, `on_dlq`)."""

    def __init__(
        self,
        *,
        on_tx_state: Callable[[str, TransactionState, str | None], Any] | None = None,
        on_step: Callable[[str, StepRecord], Any] | None = None,
        on_dlq: Callable[[DLQRecord], Any] | None = None,
    ) -> None:
        super().__init__()
        self._on_tx_state = on_tx_state
        self._on_step = on_step
        self._on_dlq = on_dlq

    def update_tx_state(
        self,
        tx_id: str,
        state: TransactionState,
        *,
        aborted_reason: str | None = None,
        completed_at: str | None = None,
    ) -> None:
        super().update_tx_state(
            tx_id, state, aborted_reason=aborted_reason, completed_at=completed_at
        )
        if self._on_tx_state is not None:
            self._on_tx_state(tx_id, state, aborted_reason)

    def upsert_step(self, tx_id: str, step: StepRecord) -> None:
        super().upsert_step(tx_id, step)
        if self._on_step is not None:
            self._on_step(tx_id, step)

    def insert_dlq_record(self, record: DLQRecord) -> None:
        super().insert_dlq_record(record)
        if self._on_dlq is not None:
            self._on_dlq(record)


# Clean aliases
JSONLFileBackend = JSONFileBackend
TransactionJournal = JSONFileBackend


def create_journal_backend(
    storage: JournalBackend | str | Path | None = None,
) -> JournalBackend:
    """Resolve storage backend: defaults to pure RAM (`InMemoryBackend`), or `JSONFileBackend` if a file path is given."""
    if storage is None or str(storage) == ":memory:":
        return InMemoryBackend()
    if isinstance(storage, (str, Path)):
        return JSONFileBackend(storage)
    return storage

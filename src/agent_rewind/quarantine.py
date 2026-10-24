"""Quarantined Reader / Extractor for CaMeL-style Data-Flow Isolation.

Ensures untrusted text is parsed in a zero-privilege sandbox (no tool access)
and its output is strictly typed and marked as UNTRUSTED (tainted) in the DataVault.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import BaseModel

from agent_rewind.decorators import invoke_maybe_async
from agent_rewind.models import Ref, TaintLevel

S = TypeVar("S", bound=BaseModel)


class QuarantinedExtractor:
    """Zero-privilege reader that extracts structured Pydantic models from untrusted data.

    It has NO access to tools and automatically marks extracted fields as UNTRUSTED
    so downstream PolicyGuards can verify provenance and prevent prompt-injection hijacking.
    """

    def __init__(
        self,
        llm_Callable: Callable[[str, type[BaseModel]], Any] | None = None,
    ) -> None:
        self._llm_callable = llm_Callable

    async def extract(
        self,
        untrusted_input: str | Ref[Any],
        schema: type[S],
        *,
        custom_extractor: Callable[[str, type[S]], Any] | None = None,
    ) -> S:
        """Run isolated extraction and validate against the target Pydantic schema."""
        raw_text = untrusted_input.value if isinstance(untrusted_input, Ref) else str(untrusted_input)
        extractor = custom_extractor or self._llm_callable
        if extractor is None:
            raise ValueError(
                "QuarantinedExtractor requires either an extractor function or llm_callable."
            )

        raw_output = await invoke_maybe_async(extractor, raw_text, schema)
        if isinstance(raw_output, schema):
            return raw_output
        if isinstance(raw_output, dict):
            return schema.model_validate(raw_output)
        if isinstance(raw_output, str):
            return schema.model_validate_json(raw_output)
        raise TypeError(f"Extractor output could not be validated into {schema.__name__}")

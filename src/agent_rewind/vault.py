"""Symbolic Data Vault for tracking data provenance and taint propagation (CaMeL pattern)."""

from __future__ import annotations

from typing import Any

from agent_rewind.models import Ref, TaintLevel


class DataVault:
    """Isolated runtime memory store that tracks values and their TaintLevel."""

    def __init__(self) -> None:
        self._store: dict[str, Ref[Any]] = {}

    def put(
        self,
        value: Any,
        *,
        taint: TaintLevel = TaintLevel.TRUSTED,
        source_tool: str = "system",
        alias: str | None = None,
    ) -> Ref[Any]:
        """Store a value in the vault and return a symbolic Ref pointer."""
        if isinstance(value, Ref):
            ref = value
        else:
            ref = Ref(
                ref_id=alias or f"ref_{len(self._store) + 1}",
                value=value,
                taint=taint,
                source_tool=source_tool,
            )
        self._store[ref.ref_id] = ref
        return ref

    def get(self, ref_id: str) -> Ref[Any]:
        """Retrieve a Ref by its ID."""
        if ref_id not in self._store:
            raise KeyError(f"Reference '{ref_id}' not found in DataVault.")
        return self._store[ref_id]

    def _unwrap_value(self, item: Any) -> tuple[Any, list[Ref[Any]]]:
        """Recursively unwrap Ref objects inside dicts, lists, or tuples to prevent nested taint bypass."""
        if isinstance(item, Ref):
            return item.value, [item]
        if isinstance(item, str) and item.startswith("$") and item[1:] in self._store:
            ref = self._store[item[1:]]
            return ref.value, [ref]
        if isinstance(item, dict):
            out_dict: dict[str, Any] = {}
            found_refs: list[Ref[Any]] = []
            for k, v in item.items():
                unwrapped_v, sub_refs = self._unwrap_value(v)
                out_dict[k] = unwrapped_v
                found_refs.extend(sub_refs)
            return out_dict, found_refs
        if isinstance(item, list):
            out_list: list[Any] = []
            found_refs = []
            for elem in item:
                unwrapped_e, sub_refs = self._unwrap_value(elem)
                out_list.append(unwrapped_e)
                found_refs.extend(sub_refs)
            return out_list, found_refs
        if isinstance(item, tuple):
            out_items: list[Any] = []
            found_refs = []
            for elem in item:
                unwrapped_e, sub_refs = self._unwrap_value(elem)
                out_items.append(unwrapped_e)
                found_refs.extend(sub_refs)
            return tuple(out_items), found_refs
        return item, []

    def resolve_arguments(
        self, raw_args: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Ref[Any]]]:
        """Resolve any Ref objects (including nested inside dicts/lists) into raw values.

        Conservative Taint Propagation: if any nested element inside an argument is UNTRUSTED,
        the top-level argument provenance is marked as UNTRUSTED.
        """
        resolved: dict[str, Any] = {}
        provenance: dict[str, Ref[Any]] = {}

        for key, val in raw_args.items():
            unwrapped, refs = self._unwrap_value(val)
            resolved[key] = unwrapped
            if refs:
                # Prioritize any UNTRUSTED ref found inside the structure
                tainted_refs = [r for r in refs if r.taint == TaintLevel.UNTRUSTED]
                chosen_ref = tainted_refs[0] if tainted_refs else refs[0]
                provenance[key] = chosen_ref

        return resolved, provenance

    @property
    def refs(self) -> dict[str, Ref[Any]]:
        return dict(self._store)

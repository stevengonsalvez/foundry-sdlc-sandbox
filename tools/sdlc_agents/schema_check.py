"""
Validate parsed JSON against the same schema dict that is registered with the agent.

Supports exactly the JSON Schema subset the agent schemas use. An unknown keyword raises
TypeError (a programming error), so a schema change can never be silently ignored here.
Stdlib only.
"""

from __future__ import annotations

from typing import Any

_TYPES: dict[str, type] = {"object": dict, "array": list, "string": str}
_KEYWORDS = frozenset({
    "type", "properties", "required", "additionalProperties",
    "items", "minItems", "maxItems", "minLength", "maxLength", "enum",
})


def _short(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= 60 else text[:57] + "..."


def check(schema: dict[str, Any], value: Any, where: str) -> None:
    """Raise ValueError naming `where` (e.g. 'sdlc-triage: labels[0]') on the first violation."""
    unknown = set(schema) - _KEYWORDS
    if unknown:
        raise TypeError(f"schema at {where} uses unsupported keywords: {sorted(unknown)}")

    expected = schema.get("type")
    if expected is not None and expected not in _TYPES:
        raise TypeError(f"schema at {where} uses unsupported type: {expected!r}")
    if expected is not None and not isinstance(value, _TYPES[expected]):
        raise ValueError(f"{where}: expected {expected}, got {type(value).__name__}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{where}: {_short(value)} not in allowed set")

    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            raise ValueError(f"{where}: shorter than {schema['minLength']} chars")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise ValueError(f"{where}: exceeds {schema['maxLength']} chars")

    elif isinstance(value, list):
        lo, hi = schema.get("minItems", 0), schema.get("maxItems")
        if len(value) < lo or (hi is not None and len(value) > hi):
            bounds = f"{lo}-{hi}" if hi is not None else f"at least {lo}"
            raise ValueError(f"{where}: must have {bounds} items, got {len(value)}")
        if "items" in schema:
            for i, item in enumerate(value):
                check(schema["items"], item, f"{where}[{i}]")

    elif isinstance(value, dict):
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(props)
            if extra:
                raise ValueError(f"{where}: unexpected keys: {sorted(extra)}")
        missing = set(schema.get("required", ())) - set(value)
        if missing:
            raise ValueError(f"{where}: missing keys: {sorted(missing)}")
        for key, sub in props.items():
            if key in value:
                check(sub, value[key], f"{where}.{key}")

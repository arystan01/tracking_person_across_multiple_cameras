"""Conservative JSON Schema compatibility checks for alpha evolution."""

from __future__ import annotations

from typing import Any


def _types(schema: dict[str, Any]) -> set[str]:
    value = schema.get("type")
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        return {item for item in value if isinstance(item, str)}
    return set()


def compatibility_issues(old: dict[str, Any], new: dict[str, Any], path: str = "$") -> list[str]:
    """Report removals, new requirements and narrowing constraints.

    Optional additive properties and enum widening are allowed during alpha.
    """

    issues: list[str] = []
    old_types, new_types = _types(old), _types(new)
    if old_types and new_types and not old_types.issubset(new_types):
        issues.append(f"{path}: type narrowed from {sorted(old_types)} to {sorted(new_types)}")
    old_enum, new_enum = old.get("enum"), new.get("enum")
    if isinstance(old_enum, list) and isinstance(new_enum, list):
        removed = sorted(set(old_enum) - set(new_enum), key=repr)
        if removed:
            issues.append(f"{path}: enum values removed: {removed!r}")
    old_required = set(old.get("required", []))
    new_required = set(new.get("required", []))
    added_required = sorted(new_required - old_required)
    if added_required:
        issues.append(f"{path}: newly required properties: {added_required!r}")
    old_properties = old.get("properties", {})
    new_properties = new.get("properties", {})
    if isinstance(old_properties, dict) and isinstance(new_properties, dict):
        for name, old_property in old_properties.items():
            if name not in new_properties:
                issues.append(f"{path}: property removed: {name!r}")
            elif isinstance(old_property, dict) and isinstance(new_properties[name], dict):
                issues.extend(
                    compatibility_issues(old_property, new_properties[name], f"{path}.{name}")
                )
    for key, direction in (
        ("minimum", "increased"),
        ("exclusiveMinimum", "increased"),
        ("minLength", "increased"),
        ("minItems", "increased"),
    ):
        if key in old and key in new and new[key] > old[key]:
            issues.append(f"{path}: {key} {direction}")
    for key, direction in (
        ("maximum", "decreased"),
        ("exclusiveMaximum", "decreased"),
        ("maxLength", "decreased"),
        ("maxItems", "decreased"),
    ):
        if key in old and key in new and new[key] < old[key]:
            issues.append(f"{path}: {key} {direction}")
    for combinator in ("anyOf", "oneOf", "allOf"):
        old_items, new_items = old.get(combinator), new.get(combinator)
        if isinstance(old_items, list) and isinstance(new_items, list) and len(new_items) < len(old_items):
            issues.append(f"{path}: {combinator} alternatives removed")
    return issues

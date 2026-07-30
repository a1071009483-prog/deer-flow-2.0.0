"""Canonical audit and cache fingerprints for delegated tool catalogs."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_function

if TYPE_CHECKING:
    from deerflow.skills.types import Skill
    from deerflow.subagents.delegation import DelegationPolicy, DelegationRequest
    from deerflow.tools.tools import ToolCatalogSnapshot

_POLICY_VERSION = 1
_CATALOG_VERSION = 1
_DEFERRED_CATALOG_REVISION = 1
_SCHEMA_VALUE_KEYS_TO_DIGEST = frozenset({"const", "default", "description", "enum", "example", "examples", "title"})


def _normalized_names(values: Iterable[str] | None) -> list[str] | None:
    if values is None:
        return None
    return sorted({value.strip() for value in values if value.strip()})


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _fingerprint(value: Mapping[str, Any]) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json(value)).hexdigest()}"


def _json_safe_schema_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe_schema_value(child) for key, child in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_json_safe_schema_value(child) for child in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return {"non_json_type": f"{type(value).__module__}.{type(value).__qualname__}"}


def _sanitize_schema(value: Any) -> Any:
    """Keep callable shape while replacing content-bearing values with digests."""
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for key, child in sorted(value.items(), key=lambda item: str(item[0])):
            normalized_key = str(key)
            if normalized_key in _SCHEMA_VALUE_KEYS_TO_DIGEST:
                sanitized[normalized_key] = {"value_digest": _fingerprint({"value": _json_safe_schema_value(child)})}
            else:
                sanitized[normalized_key] = _sanitize_schema(child)
        return sanitized
    if isinstance(value, (list, tuple)):
        return [_sanitize_schema(child) for child in value]
    return _json_safe_schema_value(value)


def _tool_schema(tool: BaseTool) -> Mapping[str, Any]:
    return _sanitize_schema(convert_to_openai_function(tool))


def _skill_content_digest(skill: Skill) -> str:
    digest = hashlib.sha256()
    with skill.skill_file.open("rb") as skill_file:
        for chunk in iter(lambda: skill_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def fingerprint_parent_policy(policy: DelegationPolicy) -> str:
    """Hash only the normalized parent authorization policy."""
    return _fingerprint(
        {
            "policy_version": _POLICY_VERSION,
            "tool_groups": _normalized_names(policy.tool_groups),
            "skills": _normalized_names(policy.available_skills),
        }
    )


def _qualified_effective_tools(
    catalog: ToolCatalogSnapshot,
    effective_tools: Sequence[BaseTool],
) -> list[dict[str, str]]:
    qualified: list[dict[str, str]] = []
    for tool in effective_tools:
        entry = next((candidate for candidate in catalog.entries if candidate.tool is tool), None)
        if entry is None:
            entry = next((candidate for candidate in catalog.entries if candidate.tool.name == tool.name), None)
        source = entry.source if entry is not None else "unknown"
        qualified.append({"name": tool.name, "source": source})
    return sorted(qualified, key=lambda item: (item["source"], item["name"]))


def fingerprint_delegation_decision(
    *,
    parent_policy_fingerprint: str,
    request: DelegationRequest,
    effective_skills: tuple[str, ...] | None,
    effective_tools: Sequence[BaseTool],
    catalog: ToolCatalogSnapshot,
) -> str:
    """Hash the normalized authorization decision, excluding catalog shape."""
    return _fingerprint(
        {
            "policy_version": _POLICY_VERSION,
            "parent_policy_fingerprint": parent_policy_fingerprint,
            "subagent_type": request.subagent_type,
            "requested_tools": _normalized_names(request.requested_tools),
            "disallowed_tools": _normalized_names(request.disallowed_tools),
            "requested_skills": _normalized_names(request.requested_skills),
            "effective_skills": _normalized_names(effective_skills),
            "effective_tools": _qualified_effective_tools(catalog, effective_tools),
        }
    )


def canonical_tool_catalog_payload(
    *,
    catalog: ToolCatalogSnapshot,
    skills: Sequence[Skill],
    app_config_generation: int,
    mcp_catalog_generation: int,
    deferred_enabled: bool,
) -> dict[str, Any]:
    """Build the secret-safe canonical catalog payload used by the hash."""
    entries = [
        {
            "source": entry.source,
            "name": entry.tool.name,
            "configured_group": entry.configured_group,
            "schema": _tool_schema(entry.tool),
        }
        for entry in catalog.entries
    ]
    entries.sort(
        key=lambda item: (
            item["source"],
            item["name"],
            item["configured_group"] or "",
            _canonical_json(item["schema"]).decode("utf-8"),
        )
    )
    skill_entries = [
        {
            "name": skill.name,
            "content_digest": _skill_content_digest(skill),
            "allowed_tools": _normalized_names(skill.allowed_tools),
        }
        for skill in skills
    ]
    skill_entries.sort(key=lambda item: item["name"])
    return {
        "catalog_version": _CATALOG_VERSION,
        "app_config_generation": app_config_generation,
        "mcp_catalog_generation": mcp_catalog_generation,
        "deferred": {
            "enabled": deferred_enabled,
            "revision": _DEFERRED_CATALOG_REVISION,
        },
        "tools": entries,
        "skills": skill_entries,
    }


def fingerprint_tool_catalog(
    *,
    catalog: ToolCatalogSnapshot,
    skills: Sequence[Skill],
    app_config_generation: int,
    mcp_catalog_generation: int,
    deferred_enabled: bool,
) -> str:
    """Hash actual tool schemas, skill definitions, and hot-reload revisions."""
    return _fingerprint(
        canonical_tool_catalog_payload(
            catalog=catalog,
            skills=skills,
            app_config_generation=app_config_generation,
            mcp_catalog_generation=mcp_catalog_generation,
            deferred_enabled=deferred_enabled,
        )
    )

"""Trusted authorization values for subagent delegation.

The parent policy is captured when an agent's ``task`` tool is built. Runtime
metadata is deliberately not an input to this module.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from langchain_core.tools import BaseTool

from deerflow.skills.tool_policy import filter_tools_by_skill_allowed_tools
from deerflow.subagents.registry import get_available_subagent_names, get_subagent_config

if TYPE_CHECKING:
    from deerflow.config.app_config import AppConfig
    from deerflow.skills.types import Skill


class DelegationPolicyError(RuntimeError):
    """Raised before execution when delegation authorization cannot resolve."""


def _normalize_names(values: Iterable[str] | None) -> tuple[str, ...] | None:
    if values is None:
        return None
    normalized = {value.strip() for value in values if value.strip()}
    return tuple(sorted(normalized))


def intersect_allowlists(
    parent: Iterable[str] | None,
    child: Iterable[str] | None,
) -> tuple[str, ...] | None:
    """Intersect allowlists while preserving ``None`` and empty semantics."""
    normalized_parent = _normalize_names(parent)
    normalized_child = _normalize_names(child)
    if normalized_parent is None:
        return normalized_child
    if normalized_child is None:
        return normalized_parent
    return tuple(sorted(set(normalized_parent) & set(normalized_child)))


@dataclass(frozen=True, slots=True)
class DelegationPolicy:
    tool_groups: tuple[str, ...] | None
    available_skills: frozenset[str] | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool_groups", _normalize_names(self.tool_groups))
        normalized_skills = _normalize_names(self.available_skills)
        object.__setattr__(
            self,
            "available_skills",
            frozenset(normalized_skills) if normalized_skills is not None else None,
        )


@dataclass(frozen=True, slots=True)
class DelegationRequest:
    subagent_type: str
    requested_tools: tuple[str, ...] | None = None
    disallowed_tools: tuple[str, ...] = ()
    requested_skills: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "subagent_type", self.subagent_type.strip())
        object.__setattr__(self, "requested_tools", _normalize_names(self.requested_tools))
        object.__setattr__(self, "disallowed_tools", _normalize_names(self.disallowed_tools) or ())
        object.__setattr__(self, "requested_skills", _normalize_names(self.requested_skills))


@dataclass(frozen=True, slots=True)
class ResolvedDelegation:
    parent_policy: DelegationPolicy
    request: DelegationRequest
    effective_skills: tuple[str, ...] | None
    tools: tuple[BaseTool, ...]


def _load_enabled_skills(app_config: AppConfig) -> list[Skill]:
    from deerflow.agents.lead_agent.prompt import get_enabled_skills_for_config

    return list(get_enabled_skills_for_config(app_config))


def _load_tool_catalog(**kwargs):
    from deerflow.tools.tools import ToolCatalogLoadError, load_available_tool_catalog

    try:
        return load_available_tool_catalog(**kwargs)
    except ToolCatalogLoadError as exc:
        raise DelegationPolicyError("Tool catalog resolution failed") from exc


def _raise_unknown(kind: str, values: set[str]) -> None:
    rendered = ", ".join(sorted(values))
    raise DelegationPolicyError(f"Unknown {kind}: {rendered}")


def resolve_delegation(
    *,
    parent_policy: DelegationPolicy,
    request: DelegationRequest,
    app_config: AppConfig,
    parent_model: str | None,
) -> ResolvedDelegation:
    """Resolve one immutable child authorization decision, failing closed."""
    known_subagents = set(get_available_subagent_names(app_config=app_config))
    if request.subagent_type not in known_subagents:
        _raise_unknown("subagent type", {request.subagent_type})

    subagent_config = get_subagent_config(request.subagent_type, app_config=app_config)
    if subagent_config is None:
        _raise_unknown("subagent type", {request.subagent_type})

    from deerflow.subagents.config import resolve_subagent_model_name

    effective_model = resolve_subagent_model_name(
        subagent_config,
        parent_model,
        app_config=app_config,
    )
    try:
        catalog = _load_tool_catalog(
            include_mcp=True,
            model_name=effective_model,
            app_config=app_config,
            strict=True,
        )
        all_skills = _load_enabled_skills(app_config)
    except DelegationPolicyError:
        raise
    except Exception as exc:
        raise DelegationPolicyError("Skill catalog resolution failed") from exc

    unknown_groups = set(parent_policy.tool_groups or ()) - set(catalog.known_groups)
    if unknown_groups:
        _raise_unknown("tool group", unknown_groups)

    known_skill_names = {skill.name for skill in all_skills}
    unknown_parent_skills = set(parent_policy.available_skills or ()) - known_skill_names
    if unknown_parent_skills:
        _raise_unknown("parent skill", unknown_parent_skills)
    unknown_requested_skills = set(request.requested_skills or ()) - known_skill_names
    if unknown_requested_skills:
        _raise_unknown("requested skill", unknown_requested_skills)

    requested_tool_names = set(request.requested_tools or ())
    unknown_requested_tools = requested_tool_names - set(catalog.known_tool_names)
    if unknown_requested_tools:
        _raise_unknown("requested tool", unknown_requested_tools)
    unknown_disallowed_tools = set(request.disallowed_tools) - set(catalog.known_tool_names)
    if unknown_disallowed_tools:
        _raise_unknown("disallowed tool", unknown_disallowed_tools)

    projected_tools = catalog.project(parent_policy.tool_groups)
    projected_names = {tool.name for tool in projected_tools}
    denied_requested_tools = requested_tool_names - projected_names
    if denied_requested_tools:
        rendered = ", ".join(sorted(denied_requested_tools))
        raise DelegationPolicyError(f"Requested tool is not permitted by the parent policy: {rendered}")

    if request.requested_tools is not None:
        projected_tools = [tool for tool in projected_tools if tool.name in requested_tool_names]
    denied_names = set(request.disallowed_tools)
    projected_tools = [tool for tool in projected_tools if tool.name not in denied_names]

    effective_skills = intersect_allowlists(
        parent_policy.available_skills,
        request.requested_skills,
    )
    if effective_skills is None:
        selected_skills = all_skills
    else:
        selected_names = set(effective_skills)
        selected_skills = [skill for skill in all_skills if skill.name in selected_names]
    effective_tools = filter_tools_by_skill_allowed_tools(projected_tools, selected_skills)

    return ResolvedDelegation(
        parent_policy=parent_policy,
        request=request,
        effective_skills=effective_skills,
        tools=tuple(effective_tools),
    )

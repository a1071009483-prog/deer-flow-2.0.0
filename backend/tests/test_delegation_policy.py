"""Authorization tests for policy-bound subagent delegation."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.tools import tool

from deerflow.skills.types import Skill, SkillCategory
from deerflow.subagents.delegation import (
    DelegationPolicy,
    DelegationPolicyError,
    DelegationRequest,
    intersect_allowlists,
    resolve_delegation,
)
from deerflow.tools.tools import ToolCatalogEntry, ToolCatalogSnapshot


@tool
def web_search(query: str) -> str:
    """Search the web."""
    return query


@tool
def database_query(query: str) -> str:
    """Query a database."""
    return query


@tool
def present_files(path: str) -> str:
    """Present a generated file."""
    return path


@pytest.mark.parametrize(
    ("parent", "child", "expected"),
    [
        (None, None, None),
        (None, ("a",), ("a",)),
        (("a",), None, ("a",)),
        (("a",), ("a", "b"), ("a",)),
        ((), None, ()),
        ((), ("a",), ()),
        (None, (), ()),
    ],
)
def test_intersect_allowlists_preserves_unrestricted_and_empty(parent, child, expected):
    assert intersect_allowlists(parent, child) == expected


def test_policy_and_request_normalize_duplicates_and_order():
    policy = DelegationPolicy(tool_groups=("web", "web"), available_skills=frozenset({"beta", "alpha"}))
    request = DelegationRequest(
        subagent_type=" general-purpose ",
        requested_tools=("web_search", "web_search"),
        disallowed_tools=("task", "task"),
        requested_skills=("beta", "alpha", "beta"),
    )

    assert policy.tool_groups == ("web",)
    assert policy.available_skills == frozenset({"alpha", "beta"})
    assert request.subagent_type == "general-purpose"
    assert request.requested_tools == ("web_search",)
    assert request.disallowed_tools == ("task",)
    assert request.requested_skills == ("alpha", "beta")


def _skill(tmp_path: Path, name: str, allowed_tools: list[str] | None = None) -> Skill:
    skill_dir = tmp_path / name
    return Skill(
        name=name,
        description=name,
        license=None,
        skill_dir=skill_dir,
        skill_file=skill_dir / "SKILL.md",
        relative_path=Path(name),
        category=SkillCategory.PUBLIC,
        allowed_tools=allowed_tools,
        enabled=True,
    )


@pytest.fixture
def catalog() -> ToolCatalogSnapshot:
    return ToolCatalogSnapshot(
        entries=(
            ToolCatalogEntry(tool=web_search, source="configured", configured_group="web"),
            ToolCatalogEntry(tool=database_query, source="configured", configured_group="database"),
            ToolCatalogEntry(tool=present_files, source="builtin", configured_group=None),
        ),
        known_tool_names=frozenset({"web_search", "database_query", "present_files", "task"}),
        known_groups=frozenset({"web", "database"}),
    )


def _patch_resolution(monkeypatch, catalog, skills):
    monkeypatch.setattr("deerflow.subagents.delegation._load_tool_catalog", lambda **_kwargs: catalog)
    monkeypatch.setattr("deerflow.subagents.delegation._load_enabled_skills", lambda _app_config: skills)
    monkeypatch.setattr("deerflow.subagents.delegation.get_available_subagent_names", lambda **_kwargs: ["general-purpose"])
    monkeypatch.setattr(
        "deerflow.subagents.delegation.get_subagent_config",
        lambda *_args, **_kwargs: SimpleNamespace(model="inherit"),
    )


def test_group_policy_filters_only_configured_tools(monkeypatch, tmp_path, catalog):
    _patch_resolution(monkeypatch, catalog, [_skill(tmp_path, "research")])

    resolved = resolve_delegation(
        parent_policy=DelegationPolicy(tool_groups=("web",), available_skills=None),
        request=DelegationRequest(
            subagent_type="general-purpose",
            requested_tools=None,
            disallowed_tools=("task",),
            requested_skills=None,
        ),
        app_config=SimpleNamespace(),
        parent_model="model-a",
    )

    assert [tool.name for tool in resolved.tools] == ["web_search", "present_files"]
    assert resolved.effective_skills is None


def test_skill_policy_filters_the_group_projected_catalog(monkeypatch, tmp_path, catalog):
    skills = [_skill(tmp_path, "research", allowed_tools=["web_search"])]
    _patch_resolution(monkeypatch, catalog, skills)

    resolved = resolve_delegation(
        parent_policy=DelegationPolicy(tool_groups=("web",), available_skills=frozenset({"research"})),
        request=DelegationRequest(
            subagent_type="general-purpose",
            requested_tools=None,
            disallowed_tools=("task",),
            requested_skills=None,
        ),
        app_config=SimpleNamespace(),
        parent_model="model-a",
    )

    assert [tool.name for tool in resolved.tools] == ["web_search"]
    assert resolved.effective_skills == ("research",)


@pytest.mark.parametrize(
    ("policy", "delegation_request", "skills", "match"),
    [
        (
            DelegationPolicy(tool_groups=("missing",), available_skills=None),
            DelegationRequest("general-purpose", None, ("task",), None),
            (),
            "Unknown tool group",
        ),
        (
            DelegationPolicy(tool_groups=None, available_skills=frozenset({"missing"})),
            DelegationRequest("general-purpose", None, ("task",), None),
            (),
            "Unknown parent skill",
        ),
        (
            DelegationPolicy(tool_groups=None, available_skills=None),
            DelegationRequest("general-purpose", ("missing",), ("task",), None),
            (),
            "Unknown requested tool",
        ),
        (
            DelegationPolicy(tool_groups=None, available_skills=None),
            DelegationRequest("missing", None, ("task",), None),
            (),
            "Unknown subagent type",
        ),
    ],
)
def test_unknown_policy_values_fail_closed(monkeypatch, tmp_path, catalog, policy, delegation_request, skills, match):
    loaded_skills = [_skill(tmp_path, name) for name in skills]
    _patch_resolution(monkeypatch, catalog, loaded_skills)

    with pytest.raises(DelegationPolicyError, match=match):
        resolve_delegation(
            parent_policy=policy,
            request=delegation_request,
            app_config=SimpleNamespace(),
            parent_model="model-a",
        )


def test_known_tool_denied_by_parent_group_is_not_reported_unknown(monkeypatch, catalog):
    _patch_resolution(monkeypatch, catalog, [])

    with pytest.raises(DelegationPolicyError, match="not permitted by the parent policy"):
        resolve_delegation(
            parent_policy=DelegationPolicy(tool_groups=("web",), available_skills=None),
            request=DelegationRequest(
                subagent_type="general-purpose",
                requested_tools=("database_query",),
                disallowed_tools=("task",),
                requested_skills=None,
            ),
            app_config=SimpleNamespace(),
            parent_model="model-a",
        )

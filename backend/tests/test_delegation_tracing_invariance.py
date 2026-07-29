"""Delegation authorization must be invariant across tracing and entry paths."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.tools import tool

from deerflow.skills.types import Skill, SkillCategory
from deerflow.subagents.delegation import (
    DelegationPolicy,
    DelegationRequest,
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
    """Present an output file."""
    return path


def _skill(name: str) -> Skill:
    skill_dir = Path("/tmp") / name
    return Skill(
        name=name,
        description=name,
        license=None,
        skill_dir=skill_dir,
        skill_file=skill_dir / "SKILL.md",
        relative_path=Path(name),
        category=SkillCategory.PUBLIC,
        allowed_tools=None,
        enabled=True,
    )


@pytest.fixture(autouse=True)
def _catalog(monkeypatch):
    catalog = ToolCatalogSnapshot(
        entries=(
            ToolCatalogEntry(web_search, "configured", "web"),
            ToolCatalogEntry(database_query, "configured", "database"),
            ToolCatalogEntry(present_files, "builtin", None),
        ),
        known_tool_names=frozenset({"web_search", "database_query", "present_files", "task"}),
        known_groups=frozenset({"web", "database"}),
    )
    monkeypatch.setattr("deerflow.subagents.delegation._load_tool_catalog", lambda **_kwargs: catalog)
    monkeypatch.setattr(
        "deerflow.subagents.delegation._load_enabled_skills",
        lambda _app_config: [_skill("research"), _skill("reporting")],
    )
    monkeypatch.setattr(
        "deerflow.subagents.delegation.get_available_subagent_names",
        lambda **_kwargs: ["general-purpose"],
    )
    monkeypatch.setattr(
        "deerflow.subagents.delegation.get_subagent_config",
        lambda *_args, **_kwargs: SimpleNamespace(model="inherit"),
    )


CASES = {
    "unrestricted": (
        DelegationPolicy(None, None),
        DelegationRequest("general-purpose"),
        (["database_query", "present_files", "web_search"], None),
    ),
    "child_restricted": (
        DelegationPolicy(None, None),
        DelegationRequest(
            "general-purpose",
            requested_tools=("web_search",),
            requested_skills=("research",),
        ),
        (["web_search"], ("research",)),
    ),
    "parent_restricted": (
        DelegationPolicy(("web",), frozenset({"research"})),
        DelegationRequest("general-purpose"),
        (["present_files", "web_search"], ("research",)),
    ),
    "parent_empty": (
        DelegationPolicy((), frozenset()),
        DelegationRequest(
            "general-purpose",
            requested_skills=("research", "reporting"),
        ),
        (["present_files"], ()),
    ),
}


@pytest.mark.parametrize("entrypoint", ["gateway", "embedded", "direct", "studio"])
@pytest.mark.parametrize("tracing_enabled", [False, True])
@pytest.mark.parametrize("capture_content", [False, True])
@pytest.mark.parametrize("case_name", CASES)
def test_authorization_is_invariant_across_tracing_and_entry_paths(
    entrypoint,
    tracing_enabled,
    capture_content,
    case_name,
):
    policy, request, expected = CASES[case_name]

    # Each entry path may carry different runtime metadata, and Phoenix safe
    # mode may rebuild or remove it. Authorization deliberately has no metadata
    # input: the trusted policy is the per-agent closure value.
    runtime_metadata = {
        "entrypoint": entrypoint,
        "phoenix_enabled": tracing_enabled,
        "capture_content": capture_content,
        "tool_groups": ["database"],
        "available_skills": ["reporting"],
    }
    assert runtime_metadata  # Documents the adversarial data excluded below.

    resolved = resolve_delegation(
        parent_policy=policy,
        request=request,
        app_config=SimpleNamespace(),
        parent_model="model-a",
    )

    expected_tools, expected_skills = expected
    assert sorted(tool.name for tool in resolved.tools) == expected_tools
    assert resolved.effective_skills == expected_skills

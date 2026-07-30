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
    skill_dir.mkdir(exist_ok=True)
    (skill_dir / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
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


# Tracing modes that must not change delegation authorization:
# - disabled: no Phoenix at all;
# - capture: Phoenix enabled with content capture on;
# - safe: Phoenix enabled with PHOENIX_CAPTURE_CONTENT=false (safe mode
#   rebuilds the metadata handed to the graph);
# - safe_no_auto_instrument: safe mode plus PHOENIX_AUTO_INSTRUMENT=false.
TRACING_MODES = {
    "disabled": {"enabled": False, "capture_content": False, "auto_instrument": False},
    "capture": {"enabled": True, "capture_content": True, "auto_instrument": True},
    "safe": {"enabled": True, "capture_content": False, "auto_instrument": True},
    "safe_no_auto_instrument": {"enabled": True, "capture_content": False, "auto_instrument": False},
}


def _metadata_for_mode(mode: str) -> dict:
    """Metadata a caller/tracing layer could plausibly present in each mode.

    Safe modes model the Phoenix metadata rebuild by stripping or rewriting
    fields — including a spoofed ``model_name`` — none of which may influence
    authorization.
    """
    phoenix = TRACING_MODES[mode]
    metadata = {
        "tracing_mode": mode,
        "phoenix_enabled": phoenix["enabled"],
        "capture_content": phoenix["capture_content"],
        "auto_instrument": phoenix["auto_instrument"],
        "tool_groups": ["database"],
        "available_skills": ["reporting"],
        "model_name": "spoofed-metadata-model",
    }
    if mode.startswith("safe"):
        # Safe mode rebuilds metadata: content-bearing keys disappear.
        metadata = {key: value for key, value in metadata.items() if key != "capture_content"}
    return metadata


@pytest.mark.parametrize("case_name", CASES)
def test_authorization_is_invariant_across_phoenix_tracing_modes(monkeypatch, case_name):
    """Effective model/skills/tools and the decision fingerprint must be
    identical across tracing disabled / capture / safe / safe+no-auto-instrument."""
    policy, request, expected = CASES[case_name]
    expected_tools, expected_skills = expected

    catalog = ToolCatalogSnapshot(
        entries=(
            ToolCatalogEntry(web_search, "configured", "web"),
            ToolCatalogEntry(database_query, "configured", "database"),
            ToolCatalogEntry(present_files, "builtin", None),
        ),
        known_tool_names=frozenset({"web_search", "database_query", "present_files", "task"}),
        known_groups=frozenset({"web", "database"}),
    )

    outcomes = {}
    for mode in TRACING_MODES:
        record: dict = {}

        def recording_catalog(*, model_name=None, _record=record, **_kwargs):
            _record["model_name"] = model_name
            return catalog

        # The autouse fixture already patches the loader; re-patch with a
        # recording wrapper around an identical catalog snapshot.
        monkeypatch.setattr("deerflow.subagents.delegation._load_tool_catalog", recording_catalog)

        # Simulate the metadata a caller/tracing layer could present in this
        # mode (including a spoofed model_name in safe modes). Authorization
        # has no metadata input, so this value documents the excluded data.
        runtime_metadata = _metadata_for_mode(mode)
        assert runtime_metadata

        resolved = resolve_delegation(
            parent_policy=policy,
            request=request,
            app_config=SimpleNamespace(),
            parent_model="model-a",
        )
        outcomes[mode] = {
            # The subagent uses model="inherit", so the effective model is
            # whatever the resolver passed to the model-aware catalog.
            "effective_model": record["model_name"],
            "tools": sorted(tool.name for tool in resolved.tools),
            "skills": resolved.effective_skills,
            "decision_fingerprint": resolved.delegation_decision_fingerprint,
            "parent_policy_fingerprint": resolved.parent_policy_fingerprint,
            "tool_catalog_fingerprint": resolved.tool_catalog_fingerprint,
        }

    baseline = outcomes["disabled"]
    assert baseline["effective_model"] == "model-a"
    assert baseline["tools"] == expected_tools
    assert baseline["skills"] == expected_skills
    for mode, outcome in outcomes.items():
        assert outcome == baseline, f"tracing mode {mode!r} changed the delegation decision"

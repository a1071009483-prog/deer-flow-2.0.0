"""Deterministic, secret-safe delegation fingerprint tests."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.tools import tool

from deerflow.skills.types import Skill, SkillCategory
from deerflow.subagents.delegation import DelegationPolicy, DelegationRequest
from deerflow.tools.catalog_fingerprint import (
    canonical_tool_catalog_payload,
    fingerprint_delegation_decision,
    fingerprint_parent_policy,
    fingerprint_tool_catalog,
)
from deerflow.tools.tools import (
    ToolCatalogEntry,
    ToolCatalogSnapshot,
    load_parent_tool_set,
)


@tool
def lookup(query: str, token: str = "fixture-secret") -> str:
    """Look up a record."""
    return f"{query}:{token}"


@tool
def lookup_v2(query: str, limit: int = 5) -> str:
    """Look up a record with a limit."""
    return f"{query}:{limit}"


lookup_v2.name = "lookup"


def _catalog(tool_obj=lookup, *, source="configured", group="web") -> ToolCatalogSnapshot:
    return ToolCatalogSnapshot(
        entries=(ToolCatalogEntry(tool_obj, source, group),),
        known_tool_names=frozenset({"lookup", "task"}),
        known_groups=frozenset({"web", "database"}),
    )


def _skill(tmp_path: Path, *, body="instructions", allowed_tools=None) -> Skill:
    skill_dir = tmp_path / "research"
    skill_dir.mkdir(exist_ok=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(body, encoding="utf-8")
    return Skill(
        name="research",
        description="Research",
        license=None,
        skill_dir=skill_dir,
        skill_file=skill_file,
        relative_path=Path("research"),
        category=SkillCategory.PUBLIC,
        allowed_tools=allowed_tools,
        enabled=True,
    )


def test_parent_policy_fingerprint_normalizes_order_and_duplicates():
    first = DelegationPolicy(("web", "database", "web"), frozenset({"b", "a"}))
    second = DelegationPolicy(("database", "web"), frozenset({"a", "b"}))

    assert fingerprint_parent_policy(first) == fingerprint_parent_policy(second)
    assert fingerprint_parent_policy(first).startswith("sha256:")
    assert len(fingerprint_parent_policy(first)) == 71


def test_decision_fingerprint_covers_subagent_and_requested_skills():
    policy_hash = fingerprint_parent_policy(DelegationPolicy(None, None))
    base = dict(
        parent_policy_fingerprint=policy_hash,
        effective_skills=("research",),
        effective_tools=(lookup,),
        catalog=_catalog(),
    )

    first = fingerprint_delegation_decision(
        request=DelegationRequest("general-purpose", requested_skills=("research",)),
        **base,
    )
    different_agent = fingerprint_delegation_decision(
        request=DelegationRequest("bash", requested_skills=("research",)),
        **base,
    )
    different_skills = fingerprint_delegation_decision(
        request=DelegationRequest("general-purpose", requested_skills=("reporting",)),
        **base,
    )

    assert len({first, different_agent, different_skills}) == 3


def test_catalog_changes_do_not_change_same_authorization_decision(tmp_path):
    policy_hash = fingerprint_parent_policy(DelegationPolicy(None, None))
    request = DelegationRequest("general-purpose", requested_tools=("lookup",))
    first_catalog = _catalog(lookup)
    second_catalog = _catalog(lookup_v2)
    decision_kwargs = dict(
        parent_policy_fingerprint=policy_hash,
        request=request,
        effective_skills=("research",),
        effective_tools=(lookup,),
    )

    first_decision = fingerprint_delegation_decision(catalog=first_catalog, **decision_kwargs)
    second_decision = fingerprint_delegation_decision(catalog=second_catalog, **decision_kwargs)
    first_catalog_hash = fingerprint_tool_catalog(
        catalog=first_catalog,
        skills=(_skill(tmp_path, body="one"),),
        app_config_generation=1,
        mcp_catalog_generation=1,
        deferred_enabled=False,
    )
    second_catalog_hash = fingerprint_tool_catalog(
        catalog=second_catalog,
        skills=(_skill(tmp_path, body="two", allowed_tools=["lookup"]),),
        app_config_generation=1,
        mcp_catalog_generation=1,
        deferred_enabled=True,
    )

    assert first_decision == second_decision
    assert first_catalog_hash != second_catalog_hash


def test_each_catalog_revision_input_changes_fingerprint(tmp_path):
    skill = _skill(tmp_path)
    base = dict(
        catalog=_catalog(),
        skills=(skill,),
        app_config_generation=10,
        mcp_catalog_generation=20,
        deferred_enabled=False,
    )
    baseline = fingerprint_tool_catalog(**base)

    variants = [
        {**base, "catalog": _catalog(lookup_v2)},
        {**base, "catalog": _catalog(group="database")},
        {**base, "catalog": _catalog(source="mcp", group=None)},
        {**base, "catalog": _catalog(source="acp", group=None)},
        {**base, "app_config_generation": 11},
        {**base, "mcp_catalog_generation": 21},
        {**base, "deferred_enabled": True},
    ]

    assert all(fingerprint_tool_catalog(**variant) != baseline for variant in variants)


def test_catalog_payload_contains_digests_not_secrets_paths_or_repr(tmp_path):
    skill = _skill(tmp_path, body="skill-secret-body", allowed_tools=["lookup", "lookup"])
    payload = canonical_tool_catalog_payload(
        catalog=_catalog(),
        skills=(skill,),
        app_config_generation=1,
        mcp_catalog_generation=2,
        deferred_enabled=True,
    )
    serialized = json.dumps(payload, sort_keys=True)

    assert "fixture-secret" not in serialized
    assert "skill-secret-body" not in serialized
    assert str(tmp_path) not in serialized
    assert "0x" not in serialized
    assert payload["skills"][0]["content_digest"].startswith("sha256:")
    assert payload["skills"][0]["allowed_tools"] == ["lookup"]


def test_app_and_mcp_generations_are_monotonic_on_replace_and_clear(monkeypatch):
    from deerflow.config.app_config import (
        get_app_config_generation,
        reset_app_config,
        set_app_config,
    )
    from deerflow.mcp import cache as mcp_cache

    app_before = get_app_config_generation()
    set_app_config(object())
    app_replaced = get_app_config_generation()
    reset_app_config()
    app_cleared = get_app_config_generation()

    monkeypatch.setattr(
        "deerflow.mcp.session_pool.get_session_pool",
        lambda: type("Pool", (), {"close_all_sync": lambda self: None})(),
    )
    monkeypatch.setattr("deerflow.mcp.session_pool.reset_session_pool", lambda: None)
    mcp_before = mcp_cache.get_mcp_catalog_generation()
    mcp_cache.reset_mcp_tools_cache()
    mcp_after = mcp_cache.get_mcp_catalog_generation()

    assert app_before < app_replaced < app_cleared
    assert mcp_before < mcp_after


def test_parent_tool_set_reuses_loaded_catalog_and_tracks_skill_content(monkeypatch, tmp_path):
    catalog = _catalog()
    skill = _skill(tmp_path, body="first")
    monkeypatch.setattr("deerflow.tools.tools.load_available_tool_catalog", lambda **_kwargs: catalog)
    monkeypatch.setattr(
        "deerflow.agents.lead_agent.prompt.get_enabled_skills_for_config",
        lambda _app_config: [skill],
    )
    monkeypatch.setattr("deerflow.config.app_config.get_app_config_generation", lambda: 4)
    monkeypatch.setattr("deerflow.mcp.cache.get_mcp_catalog_generation", lambda: 7)
    policy = DelegationPolicy(("web",), frozenset({"research"}))
    app_config = SimpleNamespace(tool_search=SimpleNamespace(enabled=False))

    first = load_parent_tool_set(
        model_name="model-a",
        subagent_enabled=False,
        app_config=app_config,
        delegation_policy=policy,
    )
    skill.skill_file.write_text("second", encoding="utf-8")
    second = load_parent_tool_set(
        model_name="model-a",
        subagent_enabled=False,
        app_config=app_config,
        delegation_policy=policy,
    )

    assert [tool.name for tool in first.tools] == ["lookup"]
    assert first.parent_policy_fingerprint == second.parent_policy_fingerprint
    assert first.tool_catalog_fingerprint != second.tool_catalog_fingerprint


def test_strict_mcp_cache_load_propagates_initialization_failure(monkeypatch):
    from deerflow.mcp import cache as mcp_cache

    async def fail_initialization():
        raise RuntimeError("mcp unavailable")

    def no_event_loop():
        raise RuntimeError("no loop")

    monkeypatch.setattr(mcp_cache, "_cache_initialized", False)
    monkeypatch.setattr(mcp_cache.asyncio, "get_event_loop", no_event_loop)
    monkeypatch.setattr(mcp_cache, "initialize_mcp_tools", fail_initialization)

    with pytest.raises(RuntimeError, match="mcp unavailable"):
        mcp_cache.get_cached_mcp_tools(strict=True)

"""Tests for deerflow.tracing.metadata trace metadata builders."""

from __future__ import annotations

import copy

import pytest

from deerflow.tracing import metadata as tracing_metadata


@pytest.fixture(autouse=True)
def _clear_tracing_env(monkeypatch):
    from deerflow.config.tracing_config import reset_tracing_config

    for name in (
        "LANGFUSE_TRACING",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "LANGFUSE_BASE_URL",
        "LANGSMITH_TRACING",
        "LANGCHAIN_TRACING_V2",
        "LANGCHAIN_TRACING",
        "LANGSMITH_API_KEY",
        "LANGCHAIN_API_KEY",
        "PHOENIX_TRACING",
        "PHOENIX_COLLECTOR_ENDPOINT",
        "PHOENIX_PROJECT_NAME",
        "PHOENIX_CAPTURE_CONTENT",
        "PHOENIX_METADATA_ALLOWLIST",
    ):
        monkeypatch.delenv(name, raising=False)
    reset_tracing_config()
    yield
    reset_tracing_config()


def _enable_langfuse(monkeypatch):
    monkeypatch.setenv("LANGFUSE_TRACING", "true")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")


def _enable_phoenix(monkeypatch):
    monkeypatch.setenv("PHOENIX_TRACING", "true")
    monkeypatch.setenv("PHOENIX_COLLECTOR_ENDPOINT", "http://localhost:6006")
    monkeypatch.setenv("PHOENIX_PROJECT_NAME", "deer-flow-test")


def test_returns_empty_when_langfuse_disabled(monkeypatch):
    # No env vars set → langfuse not in enabled providers.
    result = tracing_metadata.build_langfuse_trace_metadata(
        thread_id="t-1",
        user_id="u-1",
        assistant_id="lead-agent",
        model_name="gpt-4o",
    )
    assert result == {}


def test_session_id_maps_to_thread_id(monkeypatch):
    _enable_langfuse(monkeypatch)

    result = tracing_metadata.build_langfuse_trace_metadata(
        thread_id="thread-abc",
        user_id="user-42",
    )

    assert result["langfuse_session_id"] == "thread-abc"


def test_user_id_falls_back_to_default(monkeypatch):
    _enable_langfuse(monkeypatch)

    result = tracing_metadata.build_langfuse_trace_metadata(
        thread_id="thread-abc",
        user_id=None,
    )

    assert result["langfuse_user_id"] == "default"


def test_user_id_explicit_value_wins(monkeypatch):
    _enable_langfuse(monkeypatch)

    result = tracing_metadata.build_langfuse_trace_metadata(
        thread_id="thread-abc",
        user_id="alice@example.com",
    )

    assert result["langfuse_user_id"] == "alice@example.com"


def test_trace_name_uses_assistant_id_when_provided(monkeypatch):
    _enable_langfuse(monkeypatch)

    result = tracing_metadata.build_langfuse_trace_metadata(
        thread_id="t",
        assistant_id="custom-agent",
    )

    assert result["langfuse_trace_name"] == "custom-agent"


def test_trace_name_defaults_to_lead_agent(monkeypatch):
    _enable_langfuse(monkeypatch)

    result = tracing_metadata.build_langfuse_trace_metadata(
        thread_id="t",
        assistant_id=None,
    )

    assert result["langfuse_trace_name"] == "lead-agent"


def test_tags_include_env_and_model(monkeypatch):
    _enable_langfuse(monkeypatch)

    result = tracing_metadata.build_langfuse_trace_metadata(
        thread_id="t",
        environment="production",
        model_name="gpt-4o",
    )

    assert result["langfuse_tags"] == ["env:production", "model:gpt-4o"]


def test_tags_omitted_when_no_tag_inputs(monkeypatch):
    _enable_langfuse(monkeypatch)

    result = tracing_metadata.build_langfuse_trace_metadata(
        thread_id="t",
        user_id="u",
    )

    assert "langfuse_tags" not in result


def test_thread_id_none_still_produces_metadata(monkeypatch):
    # Stateless run paths may not have a thread_id — we still want
    # user_id / trace_name to flow through so Users page works.
    _enable_langfuse(monkeypatch)

    result = tracing_metadata.build_langfuse_trace_metadata(
        thread_id=None,
        user_id="u-1",
    )

    assert result["langfuse_session_id"] is None
    assert result["langfuse_user_id"] == "u-1"


def test_phoenix_export_builder_does_not_mutate_business_metadata(monkeypatch):
    _enable_phoenix(monkeypatch)
    monkeypatch.setenv("PHOENIX_CAPTURE_CONTENT", "false")
    monkeypatch.setenv("PHOENIX_METADATA_ALLOWLIST", "request_id")
    from deerflow.config.tracing_config import reset_tracing_config

    reset_tracing_config()

    source = {
        "request_id": "request-1",
        "private": {"values": [1, 2]},
        "tool_groups": ["web"],
        "available_skills": ["research"],
    }
    before = copy.deepcopy(source)

    exported = tracing_metadata.build_phoenix_correlation_metadata(
        thread_id="thread-1",
        user_id="user-1",
        assistant_id="lead-agent",
        caller_metadata=source,
    )

    assert source == before
    assert exported["request_id"] == "request-1"
    assert "private" not in exported
    assert "tool_groups" not in exported
    assert "available_skills" not in exported


def test_phoenix_export_builder_includes_full_caller_metadata_when_capture_enabled(monkeypatch):
    _enable_phoenix(monkeypatch)
    monkeypatch.setenv("PHOENIX_CAPTURE_CONTENT", "true")
    monkeypatch.setenv("PHOENIX_METADATA_ALLOWLIST", "request_id")
    from deerflow.config.tracing_config import reset_tracing_config

    reset_tracing_config()

    source = {
        "request_id": "request-1",
        "private": {"values": [1, 2]},
        "tool_groups": ["web"],
        "available_skills": ["research"],
    }

    exported = tracing_metadata.build_phoenix_correlation_metadata(
        thread_id="thread-1",
        user_id="user-1",
        assistant_id="lead-agent",
        caller_metadata=source,
    )

    assert exported["request_id"] == "request-1"
    assert exported["private"] == {"values": [1, 2]}
    assert exported["tool_groups"] == ["web"]
    assert exported["available_skills"] == ["research"]


def test_phoenix_export_builder_excludes_other_provider_reserved_keys(monkeypatch):
    _enable_phoenix(monkeypatch)
    monkeypatch.setenv("PHOENIX_CAPTURE_CONTENT", "true")
    monkeypatch.setenv("PHOENIX_METADATA_ALLOWLIST", "langfuse_session_id,request_id")
    from deerflow.config.tracing_config import reset_tracing_config

    reset_tracing_config()

    source = {
        "request_id": "request-1",
        "langfuse_session_id": "session-1",
        "langfuse_user_id": "user-1",
    }

    exported = tracing_metadata.build_phoenix_correlation_metadata(
        thread_id="thread-1",
        caller_metadata=source,
    )

    assert exported["request_id"] == "request-1"
    assert "langfuse_session_id" not in exported
    assert "langfuse_user_id" not in exported


def test_phoenix_export_builder_copies_nested_values(monkeypatch):
    _enable_phoenix(monkeypatch)
    monkeypatch.setenv("PHOENIX_CAPTURE_CONTENT", "true")
    monkeypatch.setenv("PHOENIX_METADATA_ALLOWLIST", "private")
    from deerflow.config.tracing_config import reset_tracing_config

    reset_tracing_config()

    source = {"private": {"values": [1, 2]}}

    exported = tracing_metadata.build_phoenix_correlation_metadata(
        thread_id="thread-1",
        caller_metadata=source,
    )

    assert exported["private"] == {"values": [1, 2]}
    assert exported["private"] is not source["private"]
    exported["private"]["values"].append(3)
    assert source["private"] == {"values": [1, 2]}

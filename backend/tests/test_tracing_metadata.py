"""Tests for deerflow.tracing.metadata trace metadata builders."""

from __future__ import annotations

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


def test_build_trace_metadata_keeps_existing_langfuse_keys(monkeypatch):
    _enable_langfuse(monkeypatch)

    result = tracing_metadata.build_trace_metadata(
        thread_id="thread-abc",
        user_id="user-42",
        assistant_id="lead-agent",
        model_name="gpt-4o",
        environment="production",
    )

    assert result["langfuse_session_id"] == "thread-abc"
    assert result["langfuse_user_id"] == "user-42"
    assert result["langfuse_trace_name"] == "lead-agent"
    assert result["langfuse_tags"] == ["env:production", "model:gpt-4o"]


def test_build_trace_metadata_adds_phoenix_session_thread_user(monkeypatch):
    _enable_phoenix(monkeypatch)

    result = tracing_metadata.build_trace_metadata(
        thread_id="thread-abc",
        user_id="user-42",
        assistant_id="lead-agent",
        model_name="gpt-4o",
        environment="production",
        caller_tags=["gateway", "interactive"],
        root_run_name="deerflow:lead-agent",
    )

    assert result["session_id"] == "thread-abc"
    assert result["thread_id"] == "thread-abc"
    assert result["user_id"] == "user-42"
    assert result["assistant_id"] == "lead-agent"
    assert result["model_name"] == "gpt-4o"
    assert result["environment"] == "production"
    assert result["root_run_name"] == "deerflow:lead-agent"
    assert result["caller_tags"] == ["gateway", "interactive"]


def test_inject_trace_metadata_preserves_caller_overrides(monkeypatch):
    _enable_langfuse(monkeypatch)
    _enable_phoenix(monkeypatch)
    monkeypatch.setenv("PHOENIX_CAPTURE_CONTENT", "true")
    monkeypatch.setenv("PHOENIX_METADATA_ALLOWLIST", "request_id,tenant_id")
    config = {
        "metadata": {
            "langfuse_session_id": "caller-langfuse-session",
            "session_id": "caller-phoenix-session",
            "caller_tags": ["caller"],
            "custom": "kept",
        }
    }

    tracing_metadata.inject_trace_metadata(
        config,
        thread_id="thread-abc",
        user_id="user-42",
        assistant_id="lead-agent",
        model_name="gpt-4o",
        environment="production",
        caller_tags=["gateway"],
        root_run_name="deerflow:lead-agent",
    )

    metadata = config["metadata"]
    assert metadata["langfuse_session_id"] == "caller-langfuse-session"
    assert metadata["session_id"] == "caller-phoenix-session"
    assert metadata["caller_tags"] == ["caller"]
    assert metadata["custom"] == "kept"
    assert metadata["langfuse_user_id"] == "user-42"
    assert metadata["thread_id"] == "thread-abc"
    assert metadata["root_run_name"] == "deerflow:lead-agent"


def test_inject_trace_metadata_replaces_caller_values_when_phoenix_content_is_hidden(monkeypatch):
    import json

    from langchain_core.tracers.schemas import Run
    from openinference.instrumentation.langchain._tracer import _update_span

    class _RecordingSpan:
        def __init__(self) -> None:
            self.attributes: dict[str, object] = {}

        def set_status(self, _status: object) -> None:
            pass

        def set_attribute(self, key: str, value: object) -> None:
            self.attributes[key] = value

        def set_attributes(self, attributes: dict[str, object]) -> None:
            self.attributes.update(attributes)

    _enable_phoenix(monkeypatch)
    monkeypatch.setenv("PHOENIX_CAPTURE_CONTENT", "false")
    monkeypatch.setenv("PHOENIX_METADATA_ALLOWLIST", "request_id,tenant_id")
    config = {
        "metadata": {
            "prompt": "TOP SECRET",
            "request_id": "request-123",
            "tenant_id": "tenant-456",
            "unlisted": "must-not-export",
            "session_id": "caller-session",
            "user_id": "caller-user",
            "caller_tags": ["secret"],
        }
    }

    tracing_metadata.inject_trace_metadata(
        config,
        thread_id="thread-abc",
        user_id="user-42",
        assistant_id="lead-agent",
        model_name="gpt-4o",
        environment="production",
        caller_tags=["caller"],
        root_run_name="deerflow:lead-agent",
        run_id="run-123",
    )

    expected_metadata = {
        "request_id": "request-123",
        "tenant_id": "tenant-456",
        "session_id": "thread-abc",
        "thread_id": "thread-abc",
        "user_id": "user-42",
        "assistant_id": "lead-agent",
        "model_name": "gpt-4o",
        "environment": "production",
        "root_run_name": "deerflow:lead-agent",
        "caller_tags": None,
        "run_id": "run-123",
    }
    assert config["metadata"] == expected_metadata

    # Exercise the locked OpenInference update path with caller tags present.
    # It serializes ``run.extra.metadata``, but must not turn ``Run.tags`` into
    # a span attribute when Phoenix safe mode has excluded those caller tags.
    run = Run(
        name="lead-agent",
        run_type="chain",
        inputs={},
        outputs={},
        extra={"metadata": config["metadata"]},
        tags=["caller:secret"],
    )
    span = _RecordingSpan()
    _update_span(span, run)

    assert json.loads(str(span.attributes["metadata"])) == expected_metadata
    assert "tag.tags" not in span.attributes
    assert all("caller:secret" not in str(value) for value in span.attributes.values())

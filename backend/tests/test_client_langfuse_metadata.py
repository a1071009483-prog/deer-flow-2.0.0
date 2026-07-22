"""Tests for DeerFlowClient's graph-root tracing wiring.

Regression coverage for the Copilot review on PR #2944: when the title
and summarization middlewares request ``attach_tracing=False`` we must
make sure ``DeerFlowClient`` injects the tracing callbacks at the graph
invocation root instead, otherwise those middlewares produce untraced
LLM calls.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from deerflow.client import DeerFlowClient
from deerflow.tracing.phoenix import _resolve_parent_context


class _FakeScope:
    """Mirror the production PhoenixRootScope interface for client tests."""

    def __init__(self, root_context, *, on_start=None, state=None):
        self.root_context = root_context
        self._on_start = on_start
        self._state = state if state is not None else {"active": False}
        self.closed_with: list[Any] = []

    def start(self) -> None:
        if self._on_start is not None:
            self._on_start(self.root_context)

    @contextmanager
    def activate(self):
        self._state["active"] = True
        try:
            yield
        finally:
            self._state["active"] = False

    def close(self, exc: BaseException | None = None) -> None:
        self.closed_with.append(exc)


class _FakeAgent:
    """Capture the ``config`` handed to ``agent.stream``."""

    def __init__(self) -> None:
        self.captured_config: dict | None = None
        self.checkpointer = None
        self.store = None

    def stream(self, state, *, config, context, stream_mode):
        self.captured_config = config
        return iter(())  # empty stream


@pytest.fixture(autouse=True)
def _clear_langfuse_env(monkeypatch):
    from deerflow.config.tracing_config import reset_tracing_config
    from deerflow.tracing import reset_phoenix_tracing_for_tests

    for name in (
        "LANGFUSE_TRACING",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "LANGFUSE_BASE_URL",
        "PHOENIX_TRACING",
        "PHOENIX_COLLECTOR_ENDPOINT",
        "PHOENIX_PROJECT_NAME",
        "PHOENIX_CAPTURE_CONTENT",
        "PHOENIX_METADATA_ALLOWLIST",
        "PHOENIX_TRACE_PARENT_MODE",
        "PHOENIX_TRACE_PARENT_REQUIRED",
        "PHOENIX_PROPAGATE_BAGGAGE",
    ):
        monkeypatch.delenv(name, raising=False)
    reset_tracing_config()
    reset_phoenix_tracing_for_tests()
    yield
    reset_tracing_config()
    reset_phoenix_tracing_for_tests()


def _stub_agent_creation(monkeypatch, fake_agent: _FakeAgent) -> dict[str, Any]:
    """Short-circuit the heavy parts of ``_ensure_agent`` so we can drive
    ``stream()`` against a fake graph without touching real models, tools
    or middleware factories.
    """
    captured: dict[str, Any] = {}

    def _stub_ensure_agent(self, config):
        captured["config"] = config
        self._agent = fake_agent
        self._agent_config_key = ("stub",)

    monkeypatch.setattr(DeerFlowClient, "_ensure_agent", _stub_ensure_agent)
    return captured


def _make_client(_monkeypatch) -> DeerFlowClient:
    """Build a client without going through ``__init__`` so we never load
    config.yaml or perform any other side-effectful startup work."""
    fake_app_config = SimpleNamespace(models=[SimpleNamespace(name="stub-model")])
    client = DeerFlowClient.__new__(DeerFlowClient)
    client._app_config = fake_app_config
    client._extensions_config = None
    client._model_name = "stub-model"
    client._thinking_enabled = False
    client._plan_mode = False
    client._subagent_enabled = False
    client._agent_name = None
    client._available_skills = None
    client._middlewares = None
    client._checkpointer = None
    client._agent = None
    client._agent_config_key = None
    client._environment = None
    return client


def test_stream_injects_langfuse_metadata_when_enabled(monkeypatch):
    monkeypatch.setenv("LANGFUSE_TRACING", "true")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    from deerflow.config.tracing_config import reset_tracing_config

    reset_tracing_config()

    class _SentinelHandler:
        pass

    sentinel = _SentinelHandler()
    monkeypatch.setattr("deerflow.client.build_tracing_callbacks", lambda: [sentinel])

    fake_agent = _FakeAgent()
    captured = _stub_agent_creation(monkeypatch, fake_agent)
    client = _make_client(monkeypatch)

    list(client.stream("hi", thread_id="thread-client-1"))

    config = captured["config"]
    metadata = config.get("metadata") or {}
    assert metadata.get("langfuse_session_id") == "thread-client-1"
    assert metadata.get("langfuse_trace_name") == "lead-agent"
    # Default no-auth context falls back to ``"default"`` user.
    assert metadata.get("langfuse_user_id") in {"default", "test-user-autouse"}
    callbacks = config.get("callbacks") or []
    assert sentinel in callbacks


def test_stream_is_inert_when_langfuse_disabled(monkeypatch):
    monkeypatch.setattr("deerflow.client.build_tracing_callbacks", lambda: [])

    fake_agent = _FakeAgent()
    captured = _stub_agent_creation(monkeypatch, fake_agent)
    client = _make_client(monkeypatch)

    list(client.stream("hi", thread_id="thread-client-2"))

    config = captured["config"]
    assert "callbacks" not in config or not config["callbacks"]
    metadata = config.get("metadata") or {}
    assert "langfuse_session_id" not in metadata
    assert "langfuse_user_id" not in metadata


def test_stream_preserves_caller_metadata_overrides(monkeypatch):
    monkeypatch.setenv("LANGFUSE_TRACING", "true")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    from deerflow.config.tracing_config import reset_tracing_config

    reset_tracing_config()
    monkeypatch.setattr("deerflow.client.build_tracing_callbacks", lambda: [])

    fake_agent = _FakeAgent()
    captured = _stub_agent_creation(monkeypatch, fake_agent)
    client = _make_client(monkeypatch)

    # Drive stream with a pre-populated metadata so the worker-equivalent
    # ``setdefault`` semantics are exercised.
    original_get_config = DeerFlowClient._get_runnable_config

    def patched_get_runnable_config(self, thread_id, **overrides):
        cfg = original_get_config(self, thread_id, **overrides)
        cfg["metadata"] = {
            "langfuse_session_id": "explicit-session-override",
            "langfuse_user_id": "explicit-user",
        }
        return cfg

    monkeypatch.setattr(DeerFlowClient, "_get_runnable_config", patched_get_runnable_config)
    list(client.stream("hi", thread_id="thread-client-3"))

    metadata = captured["config"].get("metadata") or {}
    assert metadata["langfuse_session_id"] == "explicit-session-override"
    assert metadata["langfuse_user_id"] == "explicit-user"
    # ``trace_name`` was not supplied by caller so the worker still fills it.
    assert metadata["langfuse_trace_name"] == "lead-agent"


def test_stream_accepts_trace_context_and_enters_phoenix_root(monkeypatch):
    monkeypatch.setenv("PHOENIX_TRACING", "true")
    monkeypatch.setenv("PHOENIX_TRACE_PARENT_MODE", "auto")
    from deerflow.config.tracing_config import reset_tracing_config

    reset_tracing_config()
    monkeypatch.setattr("deerflow.client.build_tracing_callbacks", lambda: [])

    entered: list[Any] = []
    scopes: list[Any] = []
    phoenix_state = {"active": False}
    observed_during_iteration: list[bool] = []

    def factory(root_context):
        entered.append(root_context)
        scope = _FakeScope(root_context, state=phoenix_state)
        scopes.append(scope)
        return scope

    monkeypatch.setattr("deerflow.client.open_phoenix_root_scope", factory)

    class _PhoenixAwareAgent(_FakeAgent):
        def stream(self, state, *, config, context, stream_mode):
            self.captured_config = config

            def iterator():
                observed_during_iteration.append(phoenix_state["active"])
                yield {"messages": []}

            return iterator()

    fake_agent = _PhoenixAwareAgent()
    client = _make_client(monkeypatch)
    _stub_agent_creation(monkeypatch, fake_agent)

    list(
        client.stream(
            "hi",
            thread_id="thread-client-phoenix-1",
            trace_context={
                "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
                "tracestate": "vendor=value",
                "baggage": "user.id=abc123",
            },
        )
    )

    assert len(entered) == 1
    root_context = entered[0]
    assert root_context.run_name == "lead-agent"
    assert root_context.session_id == "thread-client-phoenix-1"
    assert root_context.upstream_context is not None
    assert root_context.upstream_context.traceparent == "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    assert root_context.upstream_context.tracestate == "vendor=value"
    assert root_context.upstream_context.baggage == "user.id=abc123"
    assert observed_during_iteration == [True]
    assert scopes[0].closed_with == [None]


def test_stream_root_mode_ignores_supplied_trace_context(monkeypatch):
    from opentelemetry import trace

    monkeypatch.setenv("PHOENIX_TRACING", "true")
    monkeypatch.setenv("PHOENIX_TRACE_PARENT_MODE", "root")
    from deerflow.config.tracing_config import get_tracing_config, reset_tracing_config

    reset_tracing_config()
    monkeypatch.setattr("deerflow.client.build_tracing_callbacks", lambda: [])

    resolution: list[Any] = []

    def on_start(root_context):
        resolved_context, fallback_reason = _resolve_parent_context(
            get_tracing_config().phoenix,
            root_context.upstream_context,
        )
        resolution.append(
            (
                trace.get_current_span(resolved_context).get_span_context().is_valid,
                fallback_reason,
            )
        )

    def factory(root_context):
        return _FakeScope(root_context, on_start=on_start)

    monkeypatch.setattr("deerflow.client.open_phoenix_root_scope", factory)

    fake_agent = _FakeAgent()
    client = _make_client(monkeypatch)
    _stub_agent_creation(monkeypatch, fake_agent)

    list(
        client.stream(
            "hi",
            thread_id="thread-client-phoenix-2",
            trace_context={"traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"},
        )
    )

    assert resolution == [(False, None)]


def test_stream_child_required_missing_parent_raises_before_agent_stream(monkeypatch):
    monkeypatch.setenv("PHOENIX_TRACING", "true")
    monkeypatch.setenv("PHOENIX_TRACE_PARENT_MODE", "child")
    monkeypatch.setenv("PHOENIX_TRACE_PARENT_REQUIRED", "true")
    from deerflow.config.tracing_config import get_tracing_config, reset_tracing_config

    reset_tracing_config()
    monkeypatch.setattr("deerflow.client.build_tracing_callbacks", lambda: [])

    def on_start(root_context):
        _resolve_parent_context(get_tracing_config().phoenix, root_context.upstream_context)

    def factory(root_context):
        return _FakeScope(root_context, on_start=on_start)

    monkeypatch.setattr("deerflow.client.open_phoenix_root_scope", factory)

    fake_agent = _FakeAgent()
    client = _make_client(monkeypatch)
    _stub_agent_creation(monkeypatch, fake_agent)

    with pytest.raises(RuntimeError, match="missing upstream trace context"):
        list(client.stream("hi", thread_id="thread-client-phoenix-3"))

    assert fake_agent.captured_config is None


def test_stream_keeps_graph_root_callbacks_and_metadata(monkeypatch):
    monkeypatch.setenv("LANGFUSE_TRACING", "true")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    monkeypatch.setenv("PHOENIX_TRACING", "true")
    monkeypatch.setenv("PHOENIX_METADATA_ALLOWLIST", "request_id,tenant_id,langfuse_session_id")
    from deerflow.config.tracing_config import reset_tracing_config

    reset_tracing_config()

    class _SentinelHandler:
        pass

    existing_callback = _SentinelHandler()
    sentinel = _SentinelHandler()
    monkeypatch.setattr("deerflow.client.build_tracing_callbacks", lambda: [sentinel])

    entered: list[Any] = []

    def factory(root_context):
        entered.append(root_context)
        return _FakeScope(root_context)

    monkeypatch.setattr("deerflow.client.open_phoenix_root_scope", factory)

    fake_agent = _FakeAgent()
    captured = _stub_agent_creation(monkeypatch, fake_agent)
    client = _make_client(monkeypatch)
    original_get_config = DeerFlowClient._get_runnable_config

    def patched_get_runnable_config(self, thread_id, **overrides):
        cfg = original_get_config(self, thread_id, **overrides)
        cfg["callbacks"] = [existing_callback]
        cfg["metadata"] = {
            "request_id": "request-123",
            "tenant_id": "tenant-456",
            "langfuse_session_id": "caller-controlled-session",
            "unlisted": "must-not-export",
            "session_id": "caller-session",
        }
        cfg["tags"] = ["caller-tag"]
        return cfg

    monkeypatch.setattr(DeerFlowClient, "_get_runnable_config", patched_get_runnable_config)

    list(
        client.stream(
            "hi",
            thread_id="thread-client-phoenix-4",
            trace_context={"traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"},
        )
    )

    config = captured["config"]
    metadata = config.get("metadata") or {}
    callbacks = config.get("callbacks") or []
    assert callbacks == [existing_callback, sentinel]
    expected_phoenix_safe_metadata = {
        "request_id": "request-123",
        "tenant_id": "tenant-456",
        "session_id": "thread-client-phoenix-4",
        "thread_id": "thread-client-phoenix-4",
        "user_id": "test-user-autouse",
        "assistant_id": "lead-agent",
        "model_name": "stub-model",
        "environment": None,
        "root_run_name": "lead-agent",
        "caller_tags": None,
    }
    assert metadata == {
        "langfuse_session_id": "thread-client-phoenix-4",
        "langfuse_user_id": "test-user-autouse",
        "langfuse_trace_name": "lead-agent",
        "langfuse_tags": ["model:stub-model"],
        **expected_phoenix_safe_metadata,
    }
    assert len(entered) == 1
    assert entered[0].metadata == metadata
    assert entered[0].tags == list(config.get("tags") or [])
    assert entered[0].correlation_metadata == expected_phoenix_safe_metadata
    assert {key: metadata[key] for key in expected_phoenix_safe_metadata} == entered[0].correlation_metadata
    assert not any(key.startswith("langfuse_") for key in entered[0].correlation_metadata)
    assert entered[0].correlation_tags == []

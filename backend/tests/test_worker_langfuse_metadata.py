"""Integration test: worker.run_agent injects Langfuse trace metadata.

Verifies that the agent factory's resulting graph receives a
``RunnableConfig`` whose ``metadata`` carries the Langfuse reserved keys
(``langfuse_session_id`` / ``langfuse_user_id`` / ``langfuse_trace_name``).
"""

from __future__ import annotations

import asyncio
import sys
import types
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from deerflow.runtime.events.store.memory import MemoryRunEventStore
from deerflow.runtime.journal import RunJournal
from deerflow.runtime.runs.manager import RunRecord
from deerflow.runtime.runs.schemas import DisconnectMode, RunStatus
from deerflow.runtime.runs.worker import RunContext, run_agent


def _make_llm_response(content: str = "Hello", *, usage_metadata: dict[str, int] | None = None):
    msg = MagicMock()
    msg.type = "ai"
    msg.content = content
    msg.id = f"msg-{id(msg)}"
    msg.tool_calls = []
    msg.invalid_tool_calls = []
    msg.response_metadata = {"model_name": "test-model"}
    msg.usage_metadata = usage_metadata
    msg.additional_kwargs = {}
    msg.name = None
    msg.model_dump.return_value = {
        "content": content,
        "additional_kwargs": {},
        "response_metadata": {"model_name": "test-model"},
        "type": "ai",
        "name": None,
        "id": msg.id,
        "tool_calls": [],
        "invalid_tool_calls": [],
        "usage_metadata": usage_metadata,
    }

    gen = MagicMock()
    gen.message = msg

    response = MagicMock()
    response.generations = [[gen]]
    return response


class _FakeAgent:
    """Minimal LangGraph-like graph that captures the runnable config."""

    def __init__(self) -> None:
        self.captured_config: dict | None = None
        self.metadata: dict = {}
        # Worker may assign these attributes; need them to exist.
        self.checkpointer = None
        self.store = None
        self.interrupt_before_nodes: list[str] = []
        self.interrupt_after_nodes: list[str] = []

    async def astream(self, graph_input, *, config, stream_mode, **kwargs):
        self.captured_config = config
        # Empty async generator — no chunks produced.
        return
        yield  # pragma: no cover (makes this an async generator)


class _FakeRunManager:
    def __init__(self) -> None:
        self.status_updates: list[tuple[str, dict]] = []
        self.completion_updates: list[dict[str, Any]] = []

    async def set_status(self, *_args, **_kwargs) -> None:
        if len(_args) >= 2:
            self.status_updates.append((_args[1].value, dict(_kwargs)))
        return None

    async def update_model_name(self, *_args, **_kwargs) -> None:
        return None

    async def update_run_completion(self, *_args, **_kwargs) -> None:
        self.completion_updates.append(dict(_kwargs))
        return None

    async def update_run_progress(self, *_args, **_kwargs) -> None:
        return None


class _FakeBridge:
    def __init__(self) -> None:
        self.events: list[tuple[str, object]] = []

    async def publish(self, _run_id, event, payload) -> None:
        self.events.append((event, payload))

    async def publish_end(self, _run_id) -> None:
        self.events.append(("end", None))

    async def cleanup(self, _run_id, *, delay: int = 0) -> None:
        return None


class _FakeThreadStore:
    async def update_status(self, *_args, **_kwargs) -> None:
        return None

    async def update_display_name(self, *_args, **_kwargs) -> None:
        return None


class _FakeSpan:
    def __init__(self, name: str) -> None:
        self.name = name
        self.attributes: dict[str, Any] = {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def set_attribute(self, key: str, value: Any) -> None:
        if isinstance(value, dict):
            raise AssertionError(f"Python dict cannot be written directly as OTel attribute {key!r}")
        self.attributes[key] = value


class _FakeTracer:
    def __init__(self, spans: list[_FakeSpan]) -> None:
        self._spans = spans

    def start_as_current_span(self, name: str):
        span = _FakeSpan(name)
        self._spans.append(span)
        return span


def _install_fake_openinference_runtime(
    monkeypatch: pytest.MonkeyPatch,
    *,
    using_attributes_calls: list[dict[str, Any]],
    spans: list[_FakeSpan],
) -> None:
    from opentelemetry import trace

    from deerflow.tracing import phoenix

    phoenix_module = types.ModuleType("phoenix")
    phoenix_module.__path__ = []
    phoenix_otel_module = types.ModuleType("phoenix.otel")
    openinference_module = types.ModuleType("openinference")
    instrumentation_module = types.ModuleType("openinference.instrumentation")
    config_module = types.ModuleType("openinference.instrumentation.config")
    semconv_module = types.ModuleType("openinference.semconv")
    trace_module = types.ModuleType("openinference.semconv.trace")

    @contextmanager
    def using_attributes(**kwargs):
        using_attributes_calls.append(kwargs)
        yield

    class SpanAttributes:
        OPENINFERENCE_SPAN_KIND = "openinference.span.kind"
        SESSION_ID = "session.id"
        USER_ID = "user.id"
        METADATA = "metadata"
        TAG_TAGS = "tag.tags"

    class _AgentValue:
        value = "agent"

    class OpenInferenceSpanKindValues:
        AGENT = _AgentValue()

    for env_name in (
        "OPENINFERENCE_HIDE_INPUTS",
        "OPENINFERENCE_HIDE_OUTPUTS",
        "OPENINFERENCE_HIDE_INPUT_MESSAGES",
        "OPENINFERENCE_HIDE_OUTPUT_MESSAGES",
        "OPENINFERENCE_HIDE_PROMPTS",
        "OPENINFERENCE_HIDE_CHOICES",
        "OPENINFERENCE_HIDE_INPUT_TEXT",
        "OPENINFERENCE_HIDE_OUTPUT_TEXT",
        "OPENINFERENCE_HIDE_LLM_INVOCATION_PARAMETERS",
        "OPENINFERENCE_HIDE_LLM_TOOLS",
    ):
        setattr(config_module, env_name, env_name)

    phoenix_otel_module.register = lambda **_kwargs: object()
    phoenix_module.otel = phoenix_otel_module
    instrumentation_module.using_attributes = using_attributes
    instrumentation_module.config = config_module
    trace_module.SpanAttributes = SpanAttributes
    trace_module.OpenInferenceSpanKindValues = OpenInferenceSpanKindValues
    openinference_module.instrumentation = instrumentation_module
    openinference_module.semconv = semconv_module
    semconv_module.trace = trace_module
    monkeypatch.setitem(sys.modules, "phoenix", phoenix_module)
    monkeypatch.setitem(sys.modules, "phoenix.otel", phoenix_otel_module)
    monkeypatch.setitem(sys.modules, "openinference", openinference_module)
    monkeypatch.setitem(sys.modules, "openinference.instrumentation", instrumentation_module)
    monkeypatch.setitem(sys.modules, "openinference.instrumentation.config", config_module)
    monkeypatch.setitem(sys.modules, "openinference.semconv", semconv_module)
    monkeypatch.setitem(sys.modules, "openinference.semconv.trace", trace_module)

    class _FakeInstrumentor:
        def __init__(self) -> None:
            self._is_instrumented_by_opentelemetry = False

        @property
        def is_instrumented_by_opentelemetry(self) -> bool:
            return self._is_instrumented_by_opentelemetry

        def instrument(self, *, tracer_provider: Any, config: Any) -> None:
            self._is_instrumented_by_opentelemetry = True

        def uninstrument(self) -> None:
            self._is_instrumented_by_opentelemetry = False

    monkeypatch.setattr(phoenix, "_get_langchain_instrumentor", lambda: _FakeInstrumentor)
    monkeypatch.setattr(trace, "get_tracer", lambda *_args, **_kwargs: _FakeTracer(spans))
    monkeypatch.setattr(phoenix, "_validate_openinference_langchain_parent_contract", lambda: None)
    monkeypatch.setattr(phoenix, "_install_openinference_langchain_parent_compat", lambda *_args: None)
    monkeypatch.setattr(phoenix, "_get_phoenix_tracer", lambda *_args, **_kwargs: _FakeTracer(spans))


@pytest.fixture(autouse=True)
def _clear_tracing_env(monkeypatch):
    from deerflow.config.tracing_config import reset_tracing_config
    from deerflow.tracing.phoenix import reset_phoenix_tracing_for_tests

    for name in (
        "LANGFUSE_TRACING",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "LANGFUSE_BASE_URL",
        "PHOENIX_TRACING",
        "PHOENIX_CAPTURE_CONTENT",
        "DEER_FLOW_ENV",
        "ENVIRONMENT",
    ):
        monkeypatch.delenv(name, raising=False)
    reset_tracing_config()
    reset_phoenix_tracing_for_tests()
    yield
    reset_tracing_config()
    reset_phoenix_tracing_for_tests()


@pytest.mark.asyncio
async def test_run_agent_injects_langfuse_metadata(monkeypatch):
    monkeypatch.setenv("LANGFUSE_TRACING", "true")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    from deerflow.config.tracing_config import reset_tracing_config

    reset_tracing_config()

    fake_agent = _FakeAgent()

    def agent_factory(config):
        return fake_agent

    record = RunRecord(
        run_id="run-1",
        thread_id="thread-xyz",
        assistant_id="lead-agent",
        status=RunStatus.pending,
        on_disconnect=DisconnectMode.cancel,
        model_name="gpt-4o",
    )
    record.abort_event = asyncio.Event()
    ctx = RunContext(checkpointer=None)

    await run_agent(
        _FakeBridge(),
        _FakeRunManager(),
        record,
        ctx=ctx,
        agent_factory=agent_factory,
        graph_input={"messages": []},
        config={"configurable": {"thread_id": "thread-xyz"}},
    )

    assert fake_agent.captured_config is not None, "astream was not invoked"
    metadata = fake_agent.captured_config.get("metadata") or {}
    assert metadata.get("langfuse_session_id") == "thread-xyz"
    # conftest.py autouse fixture injects ``test-user-autouse`` into the
    # contextvar — the worker should read it via ``get_effective_user_id``.
    user_id = metadata.get("langfuse_user_id")
    assert user_id == "test-user-autouse", f"expected test-user-autouse, got {user_id}"
    assert metadata.get("langfuse_trace_name") == "lead-agent"
    tags = metadata.get("langfuse_tags") or []
    assert "model:gpt-4o" in tags


@pytest.mark.asyncio
async def test_run_agent_falls_back_to_default_user_when_unset(monkeypatch):
    """When no user is in the contextvar, langfuse_user_id falls back to 'default'.

    Uses ``monkeypatch.setattr`` to redirect ``get_effective_user_id`` to return
    ``"default"`` rather than directly mutating the contextvar — direct contextvar
    operations across pytest test boundaries have produced spooky cross-file
    pollution when combined with the langfuse OTel global tracer provider.
    """
    monkeypatch.setenv("LANGFUSE_TRACING", "true")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    from deerflow.config.tracing_config import reset_tracing_config
    from deerflow.runtime.runs import worker as worker_module
    from deerflow.runtime.user_context import DEFAULT_USER_ID

    reset_tracing_config()
    monkeypatch.setattr(worker_module, "get_effective_user_id", lambda: DEFAULT_USER_ID)

    fake_agent = _FakeAgent()

    def agent_factory(config):
        return fake_agent

    record = RunRecord(
        run_id="run-fallback",
        thread_id="thread-fb",
        assistant_id="lead-agent",
        status=RunStatus.pending,
        on_disconnect=DisconnectMode.cancel,
    )
    record.abort_event = asyncio.Event()
    ctx = RunContext(checkpointer=None)

    await run_agent(
        _FakeBridge(),
        _FakeRunManager(),
        record,
        ctx=ctx,
        agent_factory=agent_factory,
        graph_input={"messages": []},
        config={"configurable": {"thread_id": "thread-fb"}},
    )

    metadata = fake_agent.captured_config.get("metadata") or {}
    assert metadata.get("langfuse_user_id") == "default"


@pytest.mark.asyncio
async def test_run_agent_preserves_caller_metadata_overrides(monkeypatch):
    """Caller-provided langfuse_* keys must NOT be overridden by the default injection."""
    monkeypatch.setenv("LANGFUSE_TRACING", "true")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    from deerflow.config.tracing_config import reset_tracing_config

    reset_tracing_config()

    fake_agent = _FakeAgent()

    def agent_factory(config):
        return fake_agent

    record = RunRecord(
        run_id="run-2",
        thread_id="thread-default",
        assistant_id="lead-agent",
        status=RunStatus.pending,
        on_disconnect=DisconnectMode.cancel,
    )
    record.abort_event = asyncio.Event()
    ctx = RunContext(checkpointer=None)

    await run_agent(
        _FakeBridge(),
        _FakeRunManager(),
        record,
        ctx=ctx,
        agent_factory=agent_factory,
        graph_input={"messages": []},
        config={
            "configurable": {"thread_id": "thread-default"},
            "metadata": {
                "langfuse_session_id": "custom-session-id",
                "langfuse_user_id": "explicit-user",
            },
        },
    )

    metadata = fake_agent.captured_config.get("metadata") or {}
    # Caller-supplied keys win.
    assert metadata["langfuse_session_id"] == "custom-session-id"
    assert metadata["langfuse_user_id"] == "explicit-user"
    # Worker still fills in keys that the caller didn't set.
    assert metadata["langfuse_trace_name"] == "lead-agent"


@pytest.mark.asyncio
async def test_run_agent_skips_metadata_when_langfuse_disabled(monkeypatch):
    fake_agent = _FakeAgent()

    def agent_factory(config):
        return fake_agent

    record = RunRecord(
        run_id="run-3",
        thread_id="thread-noop",
        assistant_id="lead-agent",
        status=RunStatus.pending,
        on_disconnect=DisconnectMode.cancel,
    )
    record.abort_event = asyncio.Event()
    ctx = RunContext(checkpointer=None)

    await run_agent(
        _FakeBridge(),
        _FakeRunManager(),
        record,
        ctx=ctx,
        agent_factory=agent_factory,
        graph_input={"messages": []},
        config={"configurable": {"thread_id": "thread-noop"}},
    )

    metadata = fake_agent.captured_config.get("metadata") or {}
    assert "langfuse_session_id" not in metadata
    assert "langfuse_user_id" not in metadata
    assert "langfuse_trace_name" not in metadata


@pytest.mark.asyncio
async def test_run_agent_enters_phoenix_context_before_astream(monkeypatch):
    from deerflow.runtime.runs import worker as worker_module

    state = {"phoenix_active": False}
    observed: list[bool] = []
    root_contexts: list[object] = []

    class _PhoenixAwareAgent(_FakeAgent):
        async def astream(self, graph_input, *, config, stream_mode, **kwargs):
            self.captured_config = config
            observed.append(state["phoenix_active"])
            yield {"messages": []}

    @contextmanager
    def fake_activate(root):
        root_contexts.append(root)
        state["phoenix_active"] = True
        try:
            yield
        finally:
            state["phoenix_active"] = False

    monkeypatch.setattr(worker_module, "activate_phoenix_root_context", fake_activate, raising=False)

    fake_agent = _PhoenixAwareAgent()

    def agent_factory(config):
        return fake_agent

    record = RunRecord(
        run_id="run-phoenix",
        thread_id="thread-phoenix",
        assistant_id="lead-agent",
        status=RunStatus.pending,
        on_disconnect=DisconnectMode.cancel,
    )
    record.abort_event = asyncio.Event()

    await run_agent(
        _FakeBridge(),
        _FakeRunManager(),
        record,
        ctx=RunContext(checkpointer=None),
        agent_factory=agent_factory,
        graph_input={"messages": []},
        config={
            "configurable": {"thread_id": "thread-phoenix"},
            "context": {
                "__otel_trace_context": {
                    "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
                    "tracestate": "vendor=test",
                }
            },
        },
    )

    assert observed == [True]
    assert root_contexts
    assert root_contexts[0].upstream_context.traceparent == ("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01")


@pytest.mark.asyncio
async def test_run_agent_uses_canonical_identity_when_assistant_id_is_missing(monkeypatch):
    from deerflow.runtime.runs import worker as worker_module

    root_contexts: list[object] = []

    @contextmanager
    def fake_activate(root):
        root_contexts.append(root)
        yield

    monkeypatch.setattr(worker_module, "activate_phoenix_root_context", fake_activate)

    fake_agent = _FakeAgent()
    record = RunRecord(
        run_id="run-phoenix-default-assistant",
        thread_id="thread-phoenix-default-assistant",
        assistant_id=None,
        status=RunStatus.pending,
        on_disconnect=DisconnectMode.cancel,
    )
    record.abort_event = asyncio.Event()

    await run_agent(
        _FakeBridge(),
        _FakeRunManager(),
        record,
        ctx=RunContext(checkpointer=None),
        agent_factory=lambda config: fake_agent,
        graph_input={"messages": []},
        config={
            "configurable": {"thread_id": "thread-phoenix-default-assistant"},
            "metadata": {"assistant_id": "caller-controlled"},
        },
    )

    assert root_contexts
    assert root_contexts[0].agent_name == "lead_agent"


@pytest.mark.asyncio
async def test_run_agent_preserves_runjournal_callback_with_phoenix(monkeypatch):
    from deerflow.runtime.runs import worker as worker_module

    callbacks_seen: list[list[object]] = []

    class _CallbackCapturingAgent(_FakeAgent):
        async def astream(self, graph_input, *, config, stream_mode, **kwargs):
            self.captured_config = config
            callbacks_seen.append(list(config.get("callbacks") or []))
            return
            yield  # pragma: no cover

    @contextmanager
    def fake_activate(_root):
        yield

    monkeypatch.setattr(worker_module, "activate_phoenix_root_context", fake_activate, raising=False)

    fake_agent = _CallbackCapturingAgent()

    def agent_factory(config):
        return fake_agent

    record = RunRecord(
        run_id="run-journal",
        thread_id="thread-journal",
        assistant_id="lead-agent",
        status=RunStatus.pending,
        on_disconnect=DisconnectMode.cancel,
    )
    record.abort_event = asyncio.Event()

    await run_agent(
        _FakeBridge(),
        _FakeRunManager(),
        record,
        ctx=RunContext(
            checkpointer=None,
            event_store=MemoryRunEventStore(),
            run_events_config=SimpleNamespace(track_token_usage=True),
            thread_store=_FakeThreadStore(),
        ),
        agent_factory=agent_factory,
        graph_input={"messages": []},
        config={
            "configurable": {"thread_id": "thread-journal"},
            "context": {"__otel_trace_context": {"traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"}},
        },
    )

    assert callbacks_seen
    assert any(isinstance(callback, RunJournal) for callback in callbacks_seen[0])


@pytest.mark.asyncio
async def test_phoenix_enabled_does_not_replace_runjournal_events(monkeypatch):
    from deerflow.config.tracing_config import reset_tracing_config

    monkeypatch.setenv("PHOENIX_TRACING", "true")
    reset_tracing_config()

    event_store = MemoryRunEventStore()
    run_manager = _FakeRunManager()
    usage_metadata = {"input_tokens": 3, "output_tokens": 5, "total_tokens": 8}

    class _JournalEmittingAgent(_FakeAgent):
        async def astream(self, graph_input, *, config, stream_mode, **kwargs):
            self.captured_config = config
            for callback in config.get("callbacks") or []:
                if isinstance(callback, RunJournal):
                    callback.on_llm_end(
                        _make_llm_response("Phoenix leaves RunJournal intact", usage_metadata=usage_metadata),
                        run_id=uuid4(),
                        parent_run_id=None,
                        tags=["lead_agent"],
                    )
            yield {"messages": []}

    _install_fake_openinference_runtime(monkeypatch, using_attributes_calls=[], spans=[])

    fake_agent = _JournalEmittingAgent()

    def agent_factory(config):
        return fake_agent

    record = RunRecord(
        run_id="run-phoenix-journal",
        thread_id="thread-phoenix-journal",
        assistant_id="lead-agent",
        status=RunStatus.pending,
        on_disconnect=DisconnectMode.cancel,
    )
    record.abort_event = asyncio.Event()

    await run_agent(
        _FakeBridge(),
        run_manager,
        record,
        ctx=RunContext(
            checkpointer=None,
            event_store=event_store,
            run_events_config=SimpleNamespace(track_token_usage=True),
            thread_store=_FakeThreadStore(),
        ),
        agent_factory=agent_factory,
        graph_input={"messages": []},
        config={"configurable": {"thread_id": "thread-phoenix-journal"}},
    )

    messages = await event_store.list_messages("thread-phoenix-journal")
    assert len(messages) == 1
    assert messages[0]["event_type"] == "llm.ai.response"
    assert messages[0]["content"]["content"] == "Phoenix leaves RunJournal intact"
    assert len(run_manager.completion_updates) == 1
    completion = run_manager.completion_updates[0]
    assert completion["status"] == "pending"
    assert completion["total_input_tokens"] == 3
    assert completion["total_output_tokens"] == 5
    assert completion["total_tokens"] == 8
    assert completion["llm_call_count"] == 1
    assert completion["lead_agent_tokens"] == 8
    assert completion["subagent_tokens"] == 0
    assert completion["middleware_tokens"] == 0
    assert completion["token_usage_by_model"] == {
        "test-model": {
            "input_tokens": 3,
            "output_tokens": 5,
            "total_tokens": 8,
        }
    }
    assert completion["message_count"] == 1
    assert completion["last_ai_message"] == "Phoenix leaves RunJournal intact"
    assert completion["first_human_message"] is None


@pytest.mark.asyncio
async def test_runjournal_records_are_not_forwarded_to_phoenix(monkeypatch):
    from deerflow.config.tracing_config import reset_tracing_config

    monkeypatch.setenv("PHOENIX_TRACING", "true")
    reset_tracing_config()

    using_attributes_calls: list[dict[str, Any]] = []
    spans: list[_FakeSpan] = []
    unique_response = "journal-event-content-must-stay-local"
    _install_fake_openinference_runtime(monkeypatch, using_attributes_calls=using_attributes_calls, spans=spans)

    class _JournalEmittingAgent(_FakeAgent):
        async def astream(self, graph_input, *, config, stream_mode, **kwargs):
            self.captured_config = config
            for callback in config.get("callbacks") or []:
                if isinstance(callback, RunJournal):
                    callback.on_llm_end(
                        _make_llm_response(unique_response),
                        run_id=uuid4(),
                        parent_run_id=None,
                        tags=["lead_agent"],
                    )
            yield {"messages": []}

    fake_agent = _JournalEmittingAgent()

    def agent_factory(config):
        return fake_agent

    record = RunRecord(
        run_id="run-phoenix-isolation",
        thread_id="thread-phoenix-isolation",
        assistant_id="lead-agent",
        status=RunStatus.pending,
        on_disconnect=DisconnectMode.cancel,
    )
    record.abort_event = asyncio.Event()

    await run_agent(
        _FakeBridge(),
        _FakeRunManager(),
        record,
        ctx=RunContext(
            checkpointer=None,
            event_store=MemoryRunEventStore(),
            run_events_config=SimpleNamespace(track_token_usage=True),
            thread_store=_FakeThreadStore(),
        ),
        agent_factory=agent_factory,
        graph_input={"messages": []},
        config={"configurable": {"thread_id": "thread-phoenix-isolation"}},
    )

    assert using_attributes_calls == [
        {
            "session_id": "thread-phoenix-isolation",
            "user_id": "test-user-autouse",
            "metadata": {
                "session_id": "thread-phoenix-isolation",
                "thread_id": "thread-phoenix-isolation",
                "user_id": "test-user-autouse",
                "assistant_id": "lead-agent",
                "model_name": None,
                "environment": None,
                "root_run_name": "lead-agent",
                "caller_tags": None,
                "run_id": "run-phoenix-isolation",
            },
            "tags": [],
        }
    ]
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "deerflow.run"
    assert span.attributes["session.id"] == "thread-phoenix-isolation"
    assert span.attributes["user.id"] == "test-user-autouse"
    assert span.attributes["openinference.span.kind"] == "agent"
    assert span.attributes["deerflow.trace_parent_mode"] == "auto"
    span_metadata = using_attributes_calls[0]["metadata"]
    assert "metadata" not in span.attributes
    assert span_metadata["session_id"] == "thread-phoenix-isolation"
    assert span_metadata["thread_id"] == "thread-phoenix-isolation"
    assert span_metadata["user_id"] == "test-user-autouse"
    assert span_metadata["assistant_id"] == "lead-agent"
    assert span_metadata["root_run_name"] == "lead-agent"
    assert span_metadata["caller_tags"] is None
    assert span_metadata["run_id"] == "run-phoenix-isolation"
    assert "messages" not in span_metadata
    assert "event_type" not in span_metadata
    assert unique_response not in span_metadata.values()
    assert "llm.ai.response" not in span_metadata.values()


@pytest.mark.asyncio
async def test_capture_content_disabled_filters_caller_metadata_from_phoenix(monkeypatch):
    from deerflow.config.tracing_config import reset_tracing_config

    monkeypatch.setenv("PHOENIX_TRACING", "true")
    monkeypatch.setenv("PHOENIX_CAPTURE_CONTENT", "false")
    monkeypatch.setenv("PHOENIX_METADATA_ALLOWLIST", "request_id,tenant_id")
    monkeypatch.setenv("DEER_FLOW_ENV", "production")
    reset_tracing_config()

    using_attributes_calls: list[dict[str, Any]] = []
    spans: list[_FakeSpan] = []
    _install_fake_openinference_runtime(monkeypatch, using_attributes_calls=using_attributes_calls, spans=spans)

    fake_agent = _FakeAgent()
    record = RunRecord(
        run_id="run-private-metadata",
        thread_id="thread-authoritative",
        assistant_id="lead-agent",
        status=RunStatus.pending,
        on_disconnect=DisconnectMode.cancel,
        model_name="gpt-4o",
    )
    record.abort_event = asyncio.Event()

    await run_agent(
        _FakeBridge(),
        _FakeRunManager(),
        record,
        ctx=RunContext(checkpointer=None),
        agent_factory=lambda config: fake_agent,
        graph_input={"messages": []},
        config={
            "configurable": {"thread_id": "thread-authoritative"},
            "metadata": {
                "prompt": "TOP SECRET",
                "payload": {"token": "abc"},
                "request_id": "request-123",
                "tenant_id": "tenant-456",
                "unlisted": "must-not-export",
                "session_id": "caller-session",
                "thread_id": "caller-thread",
                "user_id": "caller-user",
                "assistant_id": "caller-agent",
                "model_name": "caller-model",
                "environment": "caller-environment",
                "root_run_name": "caller-root",
                "caller_tags": ["secret:metadata"],
                "run_id": "caller-run",
            },
            "tags": ["secret:tag"],
        },
    )

    expected_metadata = {
        "request_id": "request-123",
        "tenant_id": "tenant-456",
        "session_id": "thread-authoritative",
        "thread_id": "thread-authoritative",
        "user_id": "test-user-autouse",
        "assistant_id": "lead-agent",
        "model_name": "gpt-4o",
        "environment": "production",
        "root_run_name": "lead-agent",
        "caller_tags": None,
        "run_id": "run-private-metadata",
    }
    expected_canonical_metadata = {
        "prompt": "TOP SECRET",
        "payload": {"token": "abc"},
        "request_id": "request-123",
        "tenant_id": "tenant-456",
        "unlisted": "must-not-export",
        "session_id": "caller-session",
        "thread_id": "caller-thread",
        "user_id": "caller-user",
        "assistant_id": "caller-agent",
        "model_name": "caller-model",
        "environment": "caller-environment",
        "root_run_name": "caller-root",
        "caller_tags": ["secret:metadata"],
        "run_id": "caller-run",
    }
    assert using_attributes_calls == [
        {
            "session_id": "thread-authoritative",
            "user_id": "test-user-autouse",
            "metadata": expected_metadata,
            "tags": [],
        }
    ]
    assert len(spans) == 1
    assert "metadata" not in spans[0].attributes
    assert spans[0].attributes["tag.tags"] == []
    assert fake_agent.captured_config is not None
    # Canonical metadata is unchanged by Phoenix safe-mode filtering.
    assert fake_agent.captured_config.get("metadata") == expected_canonical_metadata


@pytest.mark.asyncio
async def test_safe_mode_preserves_astream_metadata_after_effective_model_resolution(monkeypatch):
    """Factory-appended business metadata must reach astream unchanged; Phoenix export stays filtered."""
    from deerflow.config.tracing_config import reset_tracing_config

    monkeypatch.setenv("PHOENIX_TRACING", "true")
    monkeypatch.setenv("PHOENIX_CAPTURE_CONTENT", "false")
    monkeypatch.setenv("PHOENIX_METADATA_ALLOWLIST", "request_id")
    reset_tracing_config()

    using_attributes_calls: list[dict[str, Any]] = []
    spans: list[_FakeSpan] = []
    _install_fake_openinference_runtime(monkeypatch, using_attributes_calls=using_attributes_calls, spans=spans)

    fake_agent = _FakeAgent()
    factory_fields = {
        "agent_name": "lead-agent",
        "model_name": "resolved-model",
        "tool_groups": ["web"],
        "available_skills": ["research"],
    }

    def agent_factory(config):
        # RunnableConfig performs a shallow copy, so these mutations model fields
        # appended by a real factory after model resolution.
        config["metadata"].update(factory_fields)
        fake_agent.metadata = {"model_name": "resolved-model"}
        return fake_agent

    record = RunRecord(
        run_id="run-effective-model",
        thread_id="thread-effective-model",
        assistant_id="lead-agent",
        status=RunStatus.pending,
        on_disconnect=DisconnectMode.cancel,
        model_name="requested-model",
    )
    record.abort_event = asyncio.Event()

    await run_agent(
        _FakeBridge(),
        _FakeRunManager(),
        record,
        ctx=RunContext(checkpointer=None),
        agent_factory=agent_factory,
        graph_input={"messages": []},
        config={
            "configurable": {"thread_id": "thread-effective-model"},
            "metadata": {"request_id": "request-effective-model"},
        },
    )

    expected_export_metadata = {
        "request_id": "request-effective-model",
        "session_id": "thread-effective-model",
        "thread_id": "thread-effective-model",
        "user_id": "test-user-autouse",
        "assistant_id": "lead-agent",
        "model_name": "resolved-model",
        "environment": None,
        "root_run_name": "lead-agent",
        "caller_tags": None,
        "run_id": "run-effective-model",
    }
    assert fake_agent.captured_config is not None
    # The config consumed by LangGraph/business code preserves the caller metadata
    # plus the factory fields; it is NOT replaced by the Phoenix export view.
    assert fake_agent.captured_config.get("metadata") == {
        "request_id": "request-effective-model",
        **factory_fields,
    }
    # The Phoenix export view remains filtered to the allowlist plus server-owned fields.
    assert using_attributes_calls[0]["metadata"] == expected_export_metadata
    assert "metadata" not in spans[0].attributes


@pytest.mark.asyncio
async def test_safe_mode_captures_default_model_from_factory_metadata(monkeypatch):
    """A factory-selected default model must reach both Phoenix export paths."""
    from deerflow.config.tracing_config import reset_tracing_config

    monkeypatch.setenv("PHOENIX_TRACING", "true")
    monkeypatch.setenv("PHOENIX_CAPTURE_CONTENT", "false")
    monkeypatch.setenv("PHOENIX_METADATA_ALLOWLIST", "request_id")
    reset_tracing_config()

    using_attributes_calls: list[dict[str, Any]] = []
    spans: list[_FakeSpan] = []
    _install_fake_openinference_runtime(monkeypatch, using_attributes_calls=using_attributes_calls, spans=spans)

    fake_agent = _FakeAgent()

    def agent_factory(config):
        config["metadata"]["model_name"] = "resolved-default-model"
        return fake_agent

    record = RunRecord(
        run_id="run-default-model",
        thread_id="thread-default-model",
        assistant_id="lead-agent",
        status=RunStatus.pending,
        on_disconnect=DisconnectMode.cancel,
        model_name=None,
    )
    record.abort_event = asyncio.Event()

    await run_agent(
        _FakeBridge(),
        _FakeRunManager(),
        record,
        ctx=RunContext(checkpointer=None),
        agent_factory=agent_factory,
        graph_input={"messages": []},
        config={
            "configurable": {"thread_id": "thread-default-model"},
            "metadata": {"request_id": "request-default-model"},
        },
    )

    expected_canonical_metadata = {
        "request_id": "request-default-model",
        "model_name": "resolved-default-model",
    }
    expected_export_metadata = {
        "request_id": "request-default-model",
        "session_id": "thread-default-model",
        "thread_id": "thread-default-model",
        "user_id": "test-user-autouse",
        "assistant_id": "lead-agent",
        "model_name": "resolved-default-model",
        "environment": None,
        "root_run_name": "lead-agent",
        "caller_tags": None,
        "run_id": "run-default-model",
    }
    assert fake_agent.captured_config is not None
    # Canonical metadata preserves the factory-resolved model but not Phoenix export fields.
    assert fake_agent.captured_config.get("metadata") == expected_canonical_metadata
    assert using_attributes_calls[0]["metadata"] == expected_export_metadata
    assert "metadata" not in spans[0].attributes
    assert "metadata" not in spans[0].attributes


@pytest.mark.asyncio
async def test_run_agent_child_required_missing_parent_fails_before_astream(monkeypatch):
    from deerflow.runtime.runs import worker as worker_module
    from deerflow.tracing import PhoenixTracingError

    astream_calls: list[bool] = []

    class _FailIfStartedAgent(_FakeAgent):
        async def astream(self, graph_input, *, config, stream_mode, **kwargs):
            astream_calls.append(True)
            yield {"messages": []}

    @contextmanager
    def fake_activate(root):
        if root.upstream_context is None:
            raise PhoenixTracingError("missing upstream trace context")
        yield

    monkeypatch.setattr(worker_module, "activate_phoenix_root_context", fake_activate, raising=False)

    fake_bridge = _FakeBridge()
    run_manager = _FakeRunManager()
    fake_agent = _FailIfStartedAgent()

    def agent_factory(config):
        return fake_agent

    record = RunRecord(
        run_id="run-child-required",
        thread_id="thread-child-required",
        assistant_id="lead-agent",
        status=RunStatus.pending,
        on_disconnect=DisconnectMode.cancel,
    )
    record.abort_event = asyncio.Event()

    await run_agent(
        fake_bridge,
        run_manager,
        record,
        ctx=RunContext(checkpointer=None),
        agent_factory=agent_factory,
        graph_input={"messages": []},
        config={"configurable": {"thread_id": "thread-child-required"}},
    )

    assert astream_calls == []
    assert any(event == "error" for event, _payload in fake_bridge.events)
    assert any(status == "error" for status, _kwargs in run_manager.status_updates)

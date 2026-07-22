"""Generator-scope acceptance tests for Task 7.9.

Uses the real OpenTelemetry SDK + in-memory exporter to verify that
``DeerFlowClient.stream()`` attaches Phoenix/OpenInference context only
around each underlying iterator advancement, never across caller-visible
``yield`` suspension points.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from deerflow.client import DeerFlowClient
from deerflow.config.tracing_config import PhoenixTracingConfig, reset_tracing_config
from deerflow.tracing import PhoenixTracingError, reset_phoenix_tracing_for_tests
from deerflow.tracing import phoenix as phoenix_module


def _config(
    *,
    enabled: bool = True,
    capture_content: bool = False,
    mode: str = "root",
    required: bool = False,
) -> PhoenixTracingConfig:
    return PhoenixTracingConfig(
        enabled=enabled,
        collector_endpoint="http://phoenix.test:6006",
        api_key=None,
        project_name="deer-flow-task-7.9",
        auto_instrument=False,
        capture_content=capture_content,
        trace_parent_mode=mode,
        trace_parent_required=required,
        propagate_baggage=False,
    )


@pytest.fixture(autouse=True)
def _clear_tracing_env(monkeypatch):
    for name in (
        "PHOENIX_TRACING",
        "PHOENIX_COLLECTOR_ENDPOINT",
        "PHOENIX_PROJECT_NAME",
        "PHOENIX_CAPTURE_CONTENT",
        "PHOENIX_TRACE_PARENT_MODE",
        "PHOENIX_TRACE_PARENT_REQUIRED",
        "PHOENIX_PROPAGATE_BAGGAGE",
        "PHOENIX_METADATA_ALLOWLIST",
    ):
        monkeypatch.delenv(name, raising=False)
    reset_tracing_config()
    reset_phoenix_tracing_for_tests()
    yield
    reset_tracing_config()
    reset_phoenix_tracing_for_tests()


@pytest.fixture
def otel_runtime(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    monkeypatch.setattr(phoenix_module, "ensure_phoenix_tracing_initialized", lambda _config=None: None)
    monkeypatch.setattr(phoenix_module, "_get_phoenix_tracer", provider.get_tracer)

    runtime = SimpleNamespace(
        exporter=exporter,
        provider=provider,
        set_config=lambda config: monkeypatch.setattr(
            phoenix_module,
            "get_tracing_config",
            lambda: SimpleNamespace(phoenix=config),
        ),
    )
    try:
        yield runtime
    finally:
        provider.shutdown()


def _make_client() -> DeerFlowClient:
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


def _stub_agent_creation(monkeypatch, fake_agent):
    agents = iter(fake_agent if isinstance(fake_agent, list) else [fake_agent])

    def _stub_ensure_agent(self, config):
        self._agent = next(agents)
        self._agent_config_key = ("stub",)

    monkeypatch.setattr(DeerFlowClient, "_ensure_agent", _stub_ensure_agent)
    return {}


class _InstrumentedAgent:
    """Fake agent whose stream generator records the observed OTel context."""

    def __init__(
        self,
        *,
        provider: TracerProvider,
        chunks: int = 1,
        with_child_span: bool = False,
        fail_at: int | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.provider = provider
        self.chunks = chunks
        self.with_child_span = with_child_span
        self.fail_at = fail_at
        self.error = error
        self.observed: list[tuple[str, dict[str, Any]]] = []
        self.close_called = False
        self.stream_called = False

    def stream(self, state, *, config, context, stream_mode):
        self.stream_called = True

        def _generator():
            try:
                for i in range(self.chunks):
                    from openinference.instrumentation import get_attributes_from_context

                    current = trace.get_current_span()
                    attrs = dict(get_attributes_from_context())
                    self.observed.append((getattr(current, "name", None), attrs))

                    if self.with_child_span:
                        tracer = self.provider.get_tracer("fake-instrumented")
                        with tracer.start_as_current_span("fake.child"):
                            pass

                    if self.fail_at is not None and i + 1 == self.fail_at:
                        raise self.error

                    yield ("values", {"messages": []})
            finally:
                self.close_called = True

        return _generator()


def test_context_detached_between_yields(otel_runtime, monkeypatch):
    from openinference.instrumentation import get_attributes_from_context

    otel_runtime.set_config(_config())
    agent = _InstrumentedAgent(provider=otel_runtime.provider, chunks=2)
    client = _make_client()
    _stub_agent_creation(monkeypatch, agent)

    stream = client.stream("hi", thread_id="thread-scope-1")
    first = next(stream)
    assert trace.get_current_span() is trace.INVALID_SPAN
    assert dict(get_attributes_from_context()).get("session.id") is None
    second = next(stream)
    assert trace.get_current_span() is trace.INVALID_SPAN
    assert dict(get_attributes_from_context()).get("session.id") is None

    assert first.type == "values"
    assert second.type == "values"
    end = next(stream)
    assert end.type == "end"
    with pytest.raises(StopIteration):
        next(stream)

    assert [name for name, _attrs in agent.observed] == ["deerflow.run", "deerflow.run"]
    assert [attrs.get("session.id") for _name, attrs in agent.observed] == [
        "thread-scope-1",
        "thread-scope-1",
    ]

    boundaries = [s for s in otel_runtime.exporter.get_finished_spans() if s.name == "deerflow.run"]
    assert len(boundaries) == 1
    assert boundaries[0].status.status_code == StatusCode.OK
    assert boundaries[0].attributes["session.id"] == "thread-scope-1"


def test_interleaved_streams_isolate_contexts(otel_runtime, monkeypatch):
    otel_runtime.set_config(_config())
    agent_a = _InstrumentedAgent(provider=otel_runtime.provider, chunks=2, with_child_span=True)
    agent_b = _InstrumentedAgent(provider=otel_runtime.provider, chunks=2, with_child_span=True)
    client_a = _make_client()
    client_b = _make_client()
    _stub_agent_creation(monkeypatch, [agent_a, agent_b])

    stream_a = client_a.stream("a", thread_id="thread-a")
    stream_b = client_b.stream("b", thread_id="thread-b")

    next(stream_a)
    assert trace.get_current_span() is trace.INVALID_SPAN
    probe = otel_runtime.provider.get_tracer("probe").start_span("probe-between-steps")
    assert probe.parent is None
    probe.end()

    next(stream_b)
    assert trace.get_current_span() is trace.INVALID_SPAN
    next(stream_a)
    assert trace.get_current_span() is trace.INVALID_SPAN
    next(stream_b)
    assert trace.get_current_span() is trace.INVALID_SPAN

    for stream in (stream_a, stream_b):
        end = next(stream)
        assert end.type == "end"
        with pytest.raises(StopIteration):
            next(stream)

    spans = otel_runtime.exporter.get_finished_spans()
    boundaries = [s for s in spans if s.name == "deerflow.run"]
    children = [s for s in spans if s.name == "fake.child"]
    assert len(boundaries) == 2
    assert boundaries[0].context.trace_id != boundaries[1].context.trace_id
    by_trace = {s.context.trace_id: s for s in boundaries}
    for child in children:
        parent = by_trace[child.context.trace_id]
        assert child.parent is not None and child.parent.span_id == parent.context.span_id
    assert [attrs.get("session.id") for _n, attrs in agent_a.observed] == ["thread-a", "thread-a"]
    assert [attrs.get("session.id") for _n, attrs in agent_b.observed] == ["thread-b", "thread-b"]


def test_early_close_ends_span_once_and_restores_context(otel_runtime, monkeypatch):
    otel_runtime.set_config(_config())
    agent = _InstrumentedAgent(provider=otel_runtime.provider, chunks=3)
    client = _make_client()
    _stub_agent_creation(monkeypatch, agent)

    tracer = otel_runtime.provider.get_tracer("ambient")
    with tracer.start_as_current_span("ambient") as ambient:
        stream = client.stream("hi", thread_id="thread-close")
        next(stream)
        stream.close()

        assert trace.get_current_span() is ambient
        with pytest.raises(StopIteration):
            next(stream)
        stream.close()

    boundaries = [s for s in otel_runtime.exporter.get_finished_spans() if s.name == "deerflow.run"]
    assert len(boundaries) == 1
    assert boundaries[0].status.status_code == StatusCode.UNSET
    assert not [e for e in boundaries[0].events if e.name == "exception"]
    assert agent.close_called is True


def test_iteration_exception_records_error_and_restores_context(otel_runtime, monkeypatch):
    otel_runtime.set_config(_config())
    agent = _InstrumentedAgent(
        provider=otel_runtime.provider,
        chunks=3,
        fail_at=2,
        error=RuntimeError("boom"),
    )
    client = _make_client()
    _stub_agent_creation(monkeypatch, agent)

    stream = client.stream("hi", thread_id="thread-exc")
    next(stream)
    with pytest.raises(RuntimeError, match="boom"):
        next(stream)

    stream.close()

    boundaries = [s for s in otel_runtime.exporter.get_finished_spans() if s.name == "deerflow.run"]
    assert len(boundaries) == 1
    assert boundaries[0].status.status_code == StatusCode.ERROR
    exceptions = [e for e in boundaries[0].events if e.name == "exception"]
    assert len(exceptions) == 1
    assert "boom" in str(exceptions[0].attributes)


def test_span_starts_lazily_on_first_advance(otel_runtime, monkeypatch):
    otel_runtime.set_config(_config())
    agent = _InstrumentedAgent(provider=otel_runtime.provider, chunks=1)
    client = _make_client()
    _stub_agent_creation(monkeypatch, agent)

    stream = client.stream("hi", thread_id="thread-lazy")
    assert otel_runtime.exporter.get_finished_spans() == ()
    stream.close()
    assert otel_runtime.exporter.get_finished_spans() == ()


def test_phoenix_disabled_scope_is_noop(otel_runtime, monkeypatch):
    otel_runtime.set_config(_config(enabled=False))
    agent = _InstrumentedAgent(provider=otel_runtime.provider, chunks=2)
    client = _make_client()
    _stub_agent_creation(monkeypatch, agent)

    events = list(client.stream("hi", thread_id="thread-disabled"))
    assert events[-1].type == "end"
    assert otel_runtime.exporter.get_finished_spans() == ()


def test_strict_child_fails_before_span_and_iterator(otel_runtime, monkeypatch):
    otel_runtime.set_config(_config(mode="child", required=True))
    agent = _InstrumentedAgent(provider=otel_runtime.provider, chunks=1)
    client = _make_client()
    _stub_agent_creation(monkeypatch, agent)

    stream = client.stream("hi", thread_id="thread-strict")
    with pytest.raises(PhoenixTracingError, match="missing"):
        next(stream)

    assert agent.stream_called is False
    assert agent.observed == []
    assert otel_runtime.exporter.get_finished_spans() == ()

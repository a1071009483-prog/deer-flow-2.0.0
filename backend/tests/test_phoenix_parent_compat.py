"""Integration coverage for Phoenix's locked OpenInference parent bridge."""

from __future__ import annotations

import asyncio
import sys
import threading
import tomllib
import types
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from contextvars import copy_context
from importlib.metadata import version
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from langchain.agents import create_agent
from langchain_core.callbacks import CallbackManager
from langchain_core.documents import Document
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult, Generation, LLMResult
from langchain_core.runnables.config import set_config_context
from langchain_core.tools import tool
from langchain_core.tracers.langchain import LangChainTracer
from langsmith import Client
from langsmith.run_helpers import set_tracing_parent
from langsmith.run_trees import RunTree
from openinference.instrumentation.langchain import LangChainInstrumentor
from openinference.instrumentation.langchain._tracer import OpenInferenceTracer
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from deerflow.config.tracing_config import PhoenixTracingConfig

LOCKED_OPENINFERENCE_LANGCHAIN_VERSION = "0.1.67"
LOCKED_PARENT_COMPAT_DEPENDENCIES = {
    "langchain==1.2.15",
    "langchain-core==1.3.3",
    "langsmith==0.8.18",
    "openinference-instrumentation-langchain==0.1.67",
}
_LOCAL_TOOL_CALLS: list[str] = []


@tool
def parent_compat_echo(value: str) -> str:
    """Return a deterministic local value for tracing integration tests."""
    _LOCAL_TOOL_CALLS.append(value)
    return value


class _DeterministicToolModel(BaseChatModel):
    call_count: int = 0

    @property
    def _llm_type(self) -> str:
        return "parent-compat-local-model"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.call_count += 1
        if self.call_count == 1:
            message = AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "parent-compat-call",
                        "name": "parent_compat_echo",
                        "args": {"value": "local-result"},
                    }
                ],
            )
        else:
            message = AIMessage(content="done")
        return ChatResult(generations=[ChatGeneration(message=message)])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return self._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def _phoenix_config() -> PhoenixTracingConfig:
    return PhoenixTracingConfig(
        enabled=True,
        collector_endpoint="http://phoenix.test:6006",
        api_key=None,
        project_name="deer-flow-parent-compat-test",
        auto_instrument=True,
        capture_content=True,
        trace_parent_mode="auto",
        trace_parent_required=False,
        propagate_baggage=False,
    )


@pytest.fixture
def parent_runtime(monkeypatch: pytest.MonkeyPatch):
    from deerflow.tracing import phoenix

    assert version("openinference-instrumentation-langchain") == LOCKED_OPENINFERENCE_LANGCHAIN_VERSION

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    runtime: dict[str, Any] = {"register_calls": []}
    instrumentor = LangChainInstrumentor()
    assert not instrumentor._is_instrumented_by_opentelemetry

    def register(**kwargs: Any) -> TracerProvider:
        runtime["register_calls"].append(kwargs)
        return provider

    phoenix_module = types.ModuleType("phoenix")
    phoenix_module.__path__ = []
    otel_module = types.ModuleType("phoenix.otel")
    otel_module.register = register
    phoenix_module.otel = otel_module
    monkeypatch.setitem(sys.modules, "phoenix", phoenix_module)
    monkeypatch.setitem(sys.modules, "phoenix.otel", otel_module)

    phoenix.reset_phoenix_tracing_for_tests()
    config = _phoenix_config()
    phoenix.ensure_phoenix_tracing_initialized(config)
    runtime["openinference_tracer"] = instrumentor._tracer
    from opentelemetry import trace

    monkeypatch.setattr(phoenix, "get_tracing_config", lambda: types.SimpleNamespace(phoenix=config))
    monkeypatch.setattr(trace, "get_tracer", provider.get_tracer)
    runtime.update(
        exporter=exporter,
        provider=provider,
        config=config,
        manual_tracer=provider.get_tracer("deerflow.tests.manual-root"),
    )

    try:
        yield runtime
    finally:
        phoenix.shutdown_phoenix_tracing()
        provider.shutdown()


def _start_business_parent(tracer: OpenInferenceTracer, run_id: UUID, name: str = "business-parent") -> None:
    tracer.on_chain_start(
        {"name": name},
        {"input": "business"},
        run_id=run_id,
        name=name,
    )


def _end_business_parent(tracer: OpenInferenceTracer, run_id: UUID) -> None:
    tracer.on_chain_end({"output": "done"}, run_id=run_id)


def _run_terminal(
    tracer: OpenInferenceTracer,
    run_type: str,
    *,
    run_id: UUID,
    parent_run_id: UUID,
    name: str,
) -> None:
    if run_type == "llm":
        tracer.on_llm_start(
            {"name": name},
            ["prompt"],
            run_id=run_id,
            parent_run_id=parent_run_id,
            name=name,
        )
        tracer.on_llm_end(
            LLMResult(generations=[[Generation(text="answer")]]),
            run_id=run_id,
        )
    elif run_type == "tool":
        tracer.on_tool_start(
            {"name": name},
            "tool input",
            run_id=run_id,
            parent_run_id=parent_run_id,
            name=name,
        )
        tracer.on_tool_end("tool output", run_id=run_id)
    elif run_type == "chain":
        tracer.on_chain_start(
            {"name": name},
            {"input": "chain"},
            run_id=run_id,
            parent_run_id=parent_run_id,
            name=name,
        )
        tracer.on_chain_end({"output": "chain"}, run_id=run_id)
    elif run_type == "retriever":
        tracer.on_retriever_start(
            {"name": name},
            "query",
            run_id=run_id,
            parent_run_id=parent_run_id,
            name=name,
        )
        tracer.on_retriever_end([Document(page_content="result")], run_id=run_id)
    else:  # pragma: no cover - test helper contract
        raise AssertionError(f"unsupported run type: {run_type}")


def _finished_span(runtime: dict[str, Any], name: str):
    return next(span for span in runtime["exporter"].get_finished_spans() if span.name == name)


def _assert_task_boundary_graph_topology(
    spans: list[Any],
    *,
    task_span: Any,
    boundary_span: Any,
    graph_span: Any,
) -> None:
    assert boundary_span.context.trace_id == task_span.context.trace_id
    assert boundary_span.parent is not None
    assert boundary_span.parent.span_id == task_span.context.span_id
    assert graph_span.context.trace_id == boundary_span.context.trace_id
    assert graph_span.parent is not None
    assert graph_span.parent.span_id == boundary_span.context.span_id

    children_by_parent: dict[int, list[Any]] = {}
    for span in spans:
        if span.parent is not None:
            children_by_parent.setdefault(span.parent.span_id, []).append(span)

    assert graph_span in children_by_parent[boundary_span.context.span_id]
    assert boundary_span in children_by_parent[task_span.context.span_id]


def test_phoenix_task_handoff_prefers_registered_callback_span_over_ambient(parent_runtime):
    from opentelemetry import trace

    from deerflow.tracing import capture_current_trace_context, phoenix

    tracer = parent_runtime["openinference_tracer"]
    task_run_id = uuid4()

    with parent_runtime["manual_tracer"].start_as_current_span("ambient-run-boundary") as ambient_span:
        tracer.on_chain_start(
            {"name": "task"},
            {"input": "delegate"},
            run_id=task_run_id,
            name="task",
        )
        try:
            task_span = tracer._spans_by_run[task_run_id]
            callback_manager = CallbackManager(
                handlers=[tracer],
                parent_run_id=task_run_id,
            )
            with set_config_context({"callbacks": callback_manager}) as runnable_context:
                capture = getattr(
                    phoenix,
                    "capture_current_phoenix_trace_context",
                    capture_current_trace_context,
                )
                carrier = runnable_context.run(lambda: capture(include_baggage=False))

            assert carrier is not None
            assert carrier.traceparent is not None
            traceparent_span_id = carrier.traceparent.split("-")[2]
            assert traceparent_span_id == f"{task_span.get_span_context().span_id:016x}"
            assert traceparent_span_id != f"{ambient_span.get_span_context().span_id:016x}"
            assert trace.get_current_span().get_span_context() == ambient_span.get_span_context()
        finally:
            tracer.on_chain_end({"output": "delegated"}, run_id=task_run_id)


def test_phoenix_task_handoff_registry_miss_falls_back_to_ambient(parent_runtime):
    from opentelemetry import trace

    from deerflow.tracing import capture_current_phoenix_trace_context

    callback_manager = CallbackManager(
        handlers=[parent_runtime["openinference_tracer"]],
        parent_run_id=uuid4(),
    )
    with parent_runtime["manual_tracer"].start_as_current_span("ambient-registry-miss") as ambient_span:
        with set_config_context({"callbacks": callback_manager}) as runnable_context:
            carrier = runnable_context.run(lambda: capture_current_phoenix_trace_context(include_baggage=False))

        assert carrier is not None
        assert carrier.traceparent is not None
        _, trace_id, span_id, _ = carrier.traceparent.split("-")
        assert trace_id == f"{ambient_span.get_span_context().trace_id:032x}"
        assert span_id == f"{ambient_span.get_span_context().span_id:016x}"
        assert trace.get_current_span().get_span_context() == ambient_span.get_span_context()


def _external_wrapper_for_business_parent(business_run_id: UUID) -> RunTree:
    business_run_tree = RunTree(
        name="business-parent",
        id=business_run_id,
        trace_id=business_run_id,
        inputs={},
    )
    return business_run_tree.create_child("external-wrapper")


def _start_registered_callback_ancestors(
    tracer: OpenInferenceTracer,
    entry_name: str,
    langsmith_client: Client | None = None,
):
    outer_tree = RunTree(name=f"{entry_name}-outer", inputs={}, client=langsmith_client)
    nearest_tree = outer_tree.create_child(f"{entry_name}-nearest")
    external_tree = nearest_tree.create_child(f"{entry_name}-external")
    callback_manager = CallbackManager.configure(inheritable_callbacks=[tracer])
    outer_manager = callback_manager.on_chain_start(
        {"name": outer_tree.name},
        {},
        run_id=outer_tree.id,
        name=outer_tree.name,
    )
    nearest_manager = outer_manager.get_child().on_chain_start(
        {"name": nearest_tree.name},
        {},
        run_id=nearest_tree.id,
        name=nearest_tree.name,
    )
    return outer_tree, nearest_tree, external_tree, outer_manager, nearest_manager


def _make_production_equivalent_agent():
    from deerflow.agents.middlewares.tool_error_handling_middleware import ToolErrorHandlingMiddleware

    return create_agent(
        model=_DeterministicToolModel(),
        tools=[parent_compat_echo],
        middleware=[ToolErrorHandlingMiddleware()],
    )


def _make_no_network_langsmith_tracer(monkeypatch: pytest.MonkeyPatch, entry_name: str) -> LangChainTracer:
    monkeypatch.setattr(Client, "create_run", lambda self, *args, **kwargs: None)
    monkeypatch.setattr(Client, "update_run", lambda self, *args, **kwargs: None)
    client = Client(
        api_url="http://langsmith.invalid",
        api_key="test-key",
        auto_batch_tracing=False,
    )
    return LangChainTracer(project_name=f"parent-compat-{entry_name}", client=client)


def _invoke_embedded_client(
    monkeypatch: pytest.MonkeyPatch,
    agent: Any,
    entry_name: str,
    langsmith_tracer: Any | None,
    between_yield_span_ids: list[int] | None = None,
) -> None:
    from opentelemetry import trace

    from deerflow import client as client_module

    client = object.__new__(client_module.DeerFlowClient)
    client._agent = agent
    client._model_name = "parent-compat-local"
    client._thinking_enabled = False
    client._subagent_enabled = False
    client._plan_mode = False
    client._agent_name = entry_name
    client._available_skills = None
    client._environment = "test"
    client._agent_config_key = ("parent-compat-local", False, False, False, entry_name, None)
    monkeypatch.setattr(
        client_module,
        "build_tracing_callbacks",
        lambda: [langsmith_tracer] if langsmith_tracer is not None else [],
    )

    for _event in client.stream("run local tool", thread_id=f"thread-{entry_name}"):
        if between_yield_span_ids is not None:
            between_yield_span_ids.append(trace.get_current_span().get_span_context().span_id)


@contextmanager
def _automatic_graph_span(tracer: OpenInferenceTracer, run_name: str):
    run_id = uuid4()
    tracer.on_chain_start(
        {"name": run_name},
        {"input": "graph"},
        run_id=run_id,
        name=run_name,
    )
    try:
        yield
    finally:
        tracer.on_chain_end({"output": "graph"}, run_id=run_id)


def _assert_create_agent_terminal_parents(
    parent_runtime: dict[str, Any],
    *,
    nearest_span: Any,
    require_graph_parent: bool = True,
) -> None:
    spans = parent_runtime["exporter"].get_finished_spans()
    spans_by_id = {span.context.span_id: span for span in spans}
    nearest_span_id = nearest_span.get_span_context().span_id
    graph_spans = [span for span in spans if span.attributes.get("openinference.span.kind") == "CHAIN" and span.parent is not None and span.parent.span_id == nearest_span_id]
    if require_graph_parent:
        assert len(graph_spans) == 1, [(span.name, span.attributes.get("openinference.span.kind")) for span in spans]
        assert graph_spans[0].parent is not None
        assert graph_spans[0].parent.span_id == nearest_span_id

    terminal_spans = [span for span in spans if span.attributes.get("openinference.span.kind") in {"LLM", "TOOL"}]
    assert {span.attributes["openinference.span.kind"] for span in terminal_spans} == {"LLM", "TOOL"}
    for terminal_span in terminal_spans:
        assert terminal_span.parent is not None
        parent_span = spans_by_id[terminal_span.parent.span_id]
        expected_parent_name = "model" if terminal_span.attributes["openinference.span.kind"] == "LLM" else "tools"
        assert parent_span.name == expected_parent_name
        assert parent_span.attributes.get("openinference.span.kind") == "CHAIN"


def _assert_run_boundary_is_distinct_from_graph_span(
    parent_runtime: dict[str, Any],
    *,
    graph_run_name: str,
    agent_name: str,
) -> None:
    spans = parent_runtime["exporter"].get_finished_spans()
    boundary_spans = [span for span in spans if span.attributes.get("deerflow.span.role") == "run_boundary"]
    assert len(boundary_spans) == 1
    boundary_span = boundary_spans[0]
    assert boundary_span.name == "deerflow.run"
    assert [span for span in spans if span.name == "deerflow.run"] == [boundary_span]
    assert boundary_span.attributes["deerflow.agent_name"] == agent_name
    assert boundary_span.attributes["deerflow.root_run_name"] == graph_run_name

    graph_spans = [span for span in spans if span.name == graph_run_name]
    assert len(graph_spans) == 1, [
        (
            span.name,
            span.attributes.get("openinference.span.kind"),
            span.context.trace_id,
            span.parent.span_id if span.parent is not None else None,
        )
        for span in spans
    ]
    assert graph_spans[0].context.trace_id == boundary_span.context.trace_id
    assert graph_spans[0].parent is not None
    assert graph_spans[0].parent.span_id == boundary_span.context.span_id
    assert graph_spans[0].name != boundary_span.name


def _graph_root_binding_api():
    from deerflow.tracing import phoenix

    bind = getattr(phoenix, "bind_phoenix_graph_root_parent", None)
    consume = getattr(phoenix, "_consume_graph_root_parent_override", None)
    registry = getattr(phoenix, "_graph_root_parent_overrides", None)
    assert bind is not None, "exact graph-root parent binding API is required"
    assert consume is not None, "exact graph-root parent consumption API is required"
    assert registry is not None, "exact graph-root parent registry is required"
    return bind, consume, registry


def test_graph_root_override_wins_only_for_exact_run_id_and_is_consumed(parent_runtime):
    from deerflow.tracing import (
        PhoenixRootContext,
        activate_phoenix_root_context,
        capture_trace_context_from_span_context,
    )

    bind, _, registry = _graph_root_binding_api()
    tracer = parent_runtime["openinference_tracer"]
    task_run_id = uuid4()
    graph_run_id = uuid4()
    ordinary_run_id = uuid4()
    _start_business_parent(tracer, task_run_id, name="task")
    task_recording_span = tracer.get_span(task_run_id)
    assert task_recording_span is not None
    task_carrier = capture_trace_context_from_span_context(
        task_recording_span.get_span_context(),
        include_baggage=False,
    )
    assert task_carrier is not None
    root = PhoenixRootContext(
        run_name="subagent:exact-root",
        session_id="thread-exact-root",
        user_id="local-user",
        metadata={},
        tags=[],
        upstream_context=task_carrier,
        agent_name="subagent:exact-root",
    )

    try:
        with activate_phoenix_root_context(root) as boundary:
            assert boundary is not None
            with bind(graph_run_id, boundary):
                tracer.on_chain_start(
                    {"name": "subagent:exact-root"},
                    {"input": "graph"},
                    run_id=graph_run_id,
                    parent_run_id=task_run_id,
                    name="subagent:exact-root",
                )
                assert graph_run_id not in registry
                tracer.on_chain_end({"output": "graph"}, run_id=graph_run_id)

                tracer.on_chain_start(
                    {"name": "ordinary-child"},
                    {"input": "ordinary"},
                    run_id=ordinary_run_id,
                    parent_run_id=task_run_id,
                    name="ordinary-child",
                )
                tracer.on_chain_end({"output": "ordinary"}, run_id=ordinary_run_id)
            boundary.mark_complete()
    finally:
        _end_business_parent(tracer, task_run_id)

    spans = list(parent_runtime["exporter"].get_finished_spans())
    task_span = next(span for span in spans if span.name == "task")
    boundary_span = next(span for span in spans if span.attributes.get("deerflow.span.role") == "run_boundary")
    graph_span = next(span for span in spans if span.name == "subagent:exact-root")
    ordinary_span = next(span for span in spans if span.name == "ordinary-child")
    _assert_task_boundary_graph_topology(
        spans,
        task_span=task_span,
        boundary_span=boundary_span,
        graph_span=graph_span,
    )
    assert ordinary_span.parent is not None
    assert ordinary_span.parent.span_id == task_span.context.span_id


def test_graph_root_override_rejects_duplicates_and_cleans_unconsumed_entries(
    parent_runtime,
):
    from deerflow.tracing import PhoenixRunBoundary, PhoenixTracingError

    bind, _, registry = _graph_root_binding_api()
    recording_span = parent_runtime["manual_tracer"].start_span("binding-source")
    boundary = PhoenixRunBoundary(recording_span)
    duplicate_run_id = uuid4()
    exceptional_run_id = uuid4()
    try:
        with bind(duplicate_run_id, boundary):
            assert duplicate_run_id in registry
            with pytest.raises(PhoenixTracingError, match="already registered"):
                with bind(duplicate_run_id, boundary):
                    pass
        assert duplicate_run_id not in registry

        with pytest.raises(RuntimeError, match="binding scope failed"):
            with bind(exceptional_run_id, boundary):
                assert exceptional_run_id in registry
                raise RuntimeError("binding scope failed")
        assert exceptional_run_id not in registry
    finally:
        recording_span.end()


def test_graph_root_override_isolated_across_parallel_threads(parent_runtime):
    from deerflow.tracing import PhoenixRunBoundary

    bind, consume, registry = _graph_root_binding_api()
    run_ids = (uuid4(), uuid4())
    recording_spans = (
        parent_runtime["manual_tracer"].start_span("parallel-boundary-a"),
        parent_runtime["manual_tracer"].start_span("parallel-boundary-b"),
    )
    boundaries = tuple(PhoenixRunBoundary(span) for span in recording_spans)
    barrier = threading.Barrier(2)

    def bind_and_consume(run_id: UUID, boundary: Any):
        with bind(run_id, boundary):
            barrier.wait(timeout=5)
            consumed = consume(run_id)
            barrier.wait(timeout=5)
            return consumed

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(bind_and_consume, run_id, boundary) for run_id, boundary in zip(run_ids, boundaries, strict=True)]
            consumed_contexts = [future.result(timeout=10) for future in futures]
    finally:
        for span in recording_spans:
            span.end()

    assert consumed_contexts == [boundary.get_span_context() for boundary in boundaries]
    assert all(run_id not in registry for run_id in run_ids)


def test_graph_root_override_is_noop_without_boundary_and_rejects_invalid_context():
    from opentelemetry import trace

    from deerflow.tracing import PhoenixRunBoundary, PhoenixTracingError

    bind, _, registry = _graph_root_binding_api()
    no_boundary_run_id = uuid4()
    with bind(no_boundary_run_id, None):
        pass
    assert no_boundary_run_id not in registry

    invalid_boundary = PhoenixRunBoundary(trace.NonRecordingSpan(trace.INVALID_SPAN_CONTEXT))
    with pytest.raises(PhoenixTracingError, match="valid SpanContext"):
        with bind(uuid4(), invalid_boundary):
            pass


@pytest.mark.parametrize("entry_mode", ["main", "embedded", "subagent"])
def test_real_create_agent_entries_keep_terminal_spans_under_business_nodes(
    parent_runtime,
    monkeypatch: pytest.MonkeyPatch,
    entry_mode: str,
):
    from deerflow.tracing.phoenix import PhoenixRootContext, activate_phoenix_root_context

    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    monkeypatch.setattr(RunTree, "post", lambda self, *args, **kwargs: None)
    monkeypatch.setattr(RunTree, "patch", lambda self, *args, **kwargs: None)
    _LOCAL_TOOL_CALLS.clear()
    tracer = parent_runtime["openinference_tracer"]
    entry_name = f"actual-{entry_mode}-graph"
    langsmith_tracer = _make_no_network_langsmith_tracer(monkeypatch, entry_name)
    _, nearest_tree, external_tree, outer_manager, nearest_manager = _start_registered_callback_ancestors(
        tracer,
        entry_name,
        langsmith_tracer.client,
    )
    nearest_span = tracer._spans_by_run[nearest_tree.id]
    agent = _make_production_equivalent_agent()
    root = PhoenixRootContext(
        run_name=f"{entry_name}-manual-root",
        session_id=f"thread-{entry_name}",
        user_id="local-user",
        metadata={},
        tags=[],
        agent_name=entry_name,
    )
    entry_thread_ids: list[int] = []

    try:
        with set_tracing_parent(external_tree):
            if entry_mode == "main":
                with activate_phoenix_root_context(root):
                    agent.invoke(
                        {"messages": [HumanMessage(content="run local tool")]},
                        config={"run_name": entry_name, "callbacks": [langsmith_tracer]},
                    )
            elif entry_mode == "embedded":
                _invoke_embedded_client(monkeypatch, agent, entry_name, langsmith_tracer)
            else:
                with activate_phoenix_root_context(root):
                    graph_context = copy_context()
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        pool.submit(
                            graph_context.run,
                            lambda: (
                                entry_thread_ids.append(threading.get_ident()),
                                agent.invoke(
                                    {"messages": [HumanMessage(content="run local tool")]},
                                    config={"run_name": entry_name, "callbacks": [langsmith_tracer]},
                                ),
                            ),
                        ).result(timeout=10)
    finally:
        nearest_manager.on_chain_end({"output": "done"})
        outer_manager.on_chain_end({"output": "done"})

    _assert_create_agent_terminal_parents(
        parent_runtime,
        nearest_span=nearest_span,
        require_graph_parent=entry_mode != "subagent",
    )
    assert _LOCAL_TOOL_CALLS == ["local-result"]
    if entry_mode == "subagent":
        assert entry_thread_ids and entry_thread_ids[0] != threading.get_ident()


@pytest.mark.parametrize("entry_mode", ["main", "embedded", "subagent"])
def test_real_create_agent_entries_keep_distinct_run_boundary_and_graph_spans(
    parent_runtime,
    entry_mode: str,
):
    from deerflow.tracing.phoenix import PhoenixRootContext, activate_phoenix_root_context

    entry_name = f"actual-{entry_mode}-graph"
    instrumentation_tracer = parent_runtime["openinference_tracer"]
    root = PhoenixRootContext(
        run_name=entry_name,
        session_id=f"thread-{entry_name}",
        user_id="local-user",
        metadata={},
        tags=[],
        agent_name=entry_name,
    )
    with activate_phoenix_root_context(root):
        with _automatic_graph_span(instrumentation_tracer, entry_name):
            pass

    _assert_run_boundary_is_distinct_from_graph_span(
        parent_runtime,
        graph_run_name=entry_name,
        agent_name=entry_name,
    )
    boundary = next(span for span in parent_runtime["exporter"].get_finished_spans() if span.attributes.get("deerflow.span.role") == "run_boundary")
    assert boundary.status.status_code == StatusCode.UNSET


class _WorkerBridge:
    async def publish(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def publish_end(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def cleanup(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _WorkerRunManager:
    async def set_status(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def update_model_name(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def update_run_completion(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def update_run_progress(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _WorkerGraphAgent:
    def __init__(self, tracer: OpenInferenceTracer) -> None:
        self._tracer = tracer
        self.metadata: dict[str, Any] = {}
        self.checkpointer = None
        self.store = None
        self.interrupt_before_nodes: list[str] = []
        self.interrupt_after_nodes: list[str] = []

    async def astream(self, _graph_input: dict[str, Any], *, config: dict[str, Any], **_kwargs: Any):
        with _automatic_graph_span(self._tracer, str(config["run_name"])):
            return
            yield  # pragma: no cover - make this an async generator


class _WorkerGraphAgentAborting(_WorkerGraphAgent):
    """Sets the run's abort event as soon as streaming starts, then yields nothing."""

    def __init__(self, tracer: OpenInferenceTracer, abort_event: Any) -> None:
        super().__init__(tracer)
        self._abort_event = abort_event

    async def astream(self, _graph_input: dict[str, Any], *, config: dict[str, Any], **_kwargs: Any):
        self._abort_event.set()
        with _automatic_graph_span(self._tracer, str(config["run_name"])):
            return
            yield  # pragma: no cover - make this an async generator


@pytest.mark.asyncio
async def test_worker_aborted_run_boundary_stays_unset(parent_runtime, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")

    from deerflow.runtime.runs import worker as worker_module
    from deerflow.runtime.runs.manager import RunRecord
    from deerflow.runtime.runs.schemas import DisconnectMode, RunStatus
    from deerflow.runtime.runs.worker import RunContext, run_agent

    monkeypatch.setattr(worker_module, "get_tracing_config", lambda: types.SimpleNamespace(phoenix=parent_runtime["config"]))
    tracer = parent_runtime["openinference_tracer"]
    record = RunRecord(
        run_id="run-main-aborted",
        thread_id="thread-main-aborted",
        assistant_id=None,
        status=RunStatus.pending,
        on_disconnect=DisconnectMode.cancel,
    )
    agent = _WorkerGraphAgentAborting(tracer, record.abort_event)
    await run_agent(
        _WorkerBridge(),
        _WorkerRunManager(),
        record,
        ctx=RunContext(checkpointer=None),
        agent_factory=lambda config: agent,
        graph_input={"messages": []},
        config={"configurable": {"thread_id": record.thread_id}},
    )

    boundary = next(span for span in parent_runtime["exporter"].get_finished_spans() if span.attributes.get("deerflow.span.role") == "run_boundary")
    assert boundary.status.status_code == StatusCode.UNSET


class _EmbeddedGraphAgent:
    def __init__(self, tracer: OpenInferenceTracer) -> None:
        self._tracer = tracer

    def stream(self, _state: dict[str, Any], *, config: dict[str, Any], **_kwargs: Any):
        # Lazy generator: the graph span is created on the first next(), which
        # the client drives inside the Task 7.9 per-step Phoenix activation
        # (scope.activate() wraps each next(inner)). Matches real LangGraph
        # stream() laziness and the _WorkerGraphAgent.astream pattern above.
        with _automatic_graph_span(self._tracer, str(config["run_name"])):
            return
            yield  # pragma: no cover - make this a generator


@pytest.mark.asyncio
@pytest.mark.parametrize("entry_mode", ["main", "embedded"])
async def test_real_exporter_accepts_production_main_and_embedded_entries(
    parent_runtime,
    monkeypatch: pytest.MonkeyPatch,
    entry_mode: str,
):
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")

    tracer = parent_runtime["openinference_tracer"]
    if entry_mode == "main":
        from deerflow.runtime.runs import worker as worker_module
        from deerflow.runtime.runs.manager import RunRecord
        from deerflow.runtime.runs.schemas import DisconnectMode, RunStatus
        from deerflow.runtime.runs.worker import RunContext, run_agent

        monkeypatch.setattr(worker_module, "get_tracing_config", lambda: types.SimpleNamespace(phoenix=parent_runtime["config"]))
        agent = _WorkerGraphAgent(tracer)
        record = RunRecord(
            run_id="run-main-default-assistant",
            thread_id="thread-main-default-assistant",
            assistant_id=None,
            status=RunStatus.pending,
            on_disconnect=DisconnectMode.cancel,
        )
        await run_agent(
            _WorkerBridge(),
            _WorkerRunManager(),
            record,
            ctx=RunContext(checkpointer=None),
            agent_factory=lambda config: agent,
            graph_input={"messages": []},
            config={"configurable": {"thread_id": record.thread_id}},
        )
        graph_run_name = "lead_agent"
        agent_name = "lead_agent"
    else:
        graph_run_name = "embedded-agent"
        _invoke_embedded_client(monkeypatch, _EmbeddedGraphAgent(tracer), graph_run_name, None)
        agent_name = graph_run_name

    _assert_run_boundary_is_distinct_from_graph_span(
        parent_runtime,
        graph_run_name=graph_run_name,
        agent_name=agent_name,
    )
    boundary = next(span for span in parent_runtime["exporter"].get_finished_spans() if span.attributes.get("deerflow.span.role") == "run_boundary")
    assert boundary.status.status_code == StatusCode.OK


class _RemoveAfterCaptureRegistry(dict[UUID, Any]):
    """Remove one span on another thread after its first successful lookup."""

    def __init__(
        self,
        values: dict[UUID, Any],
        *,
        target_id: UUID,
        captured: threading.Event,
        removed: threading.Event,
    ) -> None:
        super().__init__(values)
        self._target_id = target_id
        self._captured = captured
        self._removed = removed
        self._successful_target_lookups = 0

    def get(self, key: UUID, default: Any = None) -> Any:
        value = super().get(key, default)
        if key == self._target_id and value is not None:
            self._successful_target_lookups += 1
            if self._successful_target_lookups == 1:
                self._captured.set()
                assert self._removed.wait(timeout=5), "registry removal thread did not run"
        return value


@pytest.mark.parametrize("parent_mode", ["direct", "fallback"])
def test_parent_span_context_is_frozen_before_concurrent_registry_removal(parent_runtime, parent_mode: str):
    tracer = parent_runtime["openinference_tracer"]
    business_run_id = uuid4()
    _start_business_parent(tracer, business_run_id)
    business_span = tracer._spans_by_run[business_run_id]

    captured = threading.Event()
    removed = threading.Event()
    registry = _RemoveAfterCaptureRegistry(
        dict(tracer._spans_by_run),
        target_id=business_run_id,
        captured=captured,
        removed=removed,
    )
    tracer._spans_by_run = registry

    removed_spans: list[Any] = []

    def remove_parent() -> None:
        assert captured.wait(timeout=5), "parent span was not captured"
        removed_spans.append(registry.pop(business_run_id))
        removed.set()

    remover = threading.Thread(target=remove_parent, name=f"remove-{parent_mode}-parent")
    remover.start()

    external_wrapper = _external_wrapper_for_business_parent(business_run_id)
    parent_run_id = business_run_id if parent_mode == "direct" else external_wrapper.id
    terminal_name = f"concurrent-removal-{parent_mode}"
    parent_scope = set_tracing_parent(external_wrapper) if parent_mode == "fallback" else nullcontext()

    try:
        with parent_runtime["manual_tracer"].start_as_current_span("ambient-manual-root") as manual_root:
            with parent_scope:
                _run_terminal(
                    tracer,
                    "tool",
                    run_id=uuid4(),
                    parent_run_id=parent_run_id,
                    name=terminal_name,
                )
    finally:
        remover.join(timeout=5)
        if removed_spans:
            removed_spans[0].end()

    assert not remover.is_alive()
    assert removed_spans == [business_span]
    terminal_span = _finished_span(parent_runtime, terminal_name)
    assert terminal_span.parent is not None
    assert terminal_span.parent.span_id != manual_root.get_span_context().span_id
    assert terminal_span.parent.span_id == business_span.get_span_context().span_id


def test_parent_compat_dependencies_are_exactly_pinned():
    pyproject_path = Path(__file__).parents[1] / "packages" / "harness" / "pyproject.toml"
    dependencies = set(tomllib.loads(pyproject_path.read_text(encoding="utf-8"))["project"]["dependencies"])

    assert LOCKED_PARENT_COMPAT_DEPENDENCIES <= dependencies


def test_registered_direct_parent_keeps_openinference_behavior(parent_runtime):
    tracer = parent_runtime["openinference_tracer"]
    business_run_id = uuid4()
    terminal_run_id = uuid4()
    terminal_name = "direct-parent-tool"

    with parent_runtime["manual_tracer"].start_as_current_span("deerflow-manual-root"):
        _start_business_parent(tracer, business_run_id)
        unrelated_external_run = RunTree(name="unrelated-external", inputs={})
        with set_tracing_parent(unrelated_external_run):
            _run_terminal(
                tracer,
                "tool",
                run_id=terminal_run_id,
                parent_run_id=business_run_id,
                name=terminal_name,
            )
        _end_business_parent(tracer, business_run_id)

    business_span = _finished_span(parent_runtime, "business-parent")
    terminal_span = _finished_span(parent_runtime, terminal_name)
    assert terminal_span.parent is not None
    assert terminal_span.parent.span_id == business_span.context.span_id


def test_parent_compat_installation_is_idempotent(parent_runtime):
    from deerflow.tracing import phoenix

    tracer = parent_runtime["openinference_tracer"]
    installed_start_trace = tracer._start_trace.__func__

    phoenix._install_openinference_langchain_parent_compat(parent_runtime["provider"])

    assert tracer._start_trace.__func__ is installed_start_trace


@pytest.mark.parametrize(
    ("distribution", "expected_version"),
    [
        ("langchain", "1.2.15"),
        ("langchain-core", "1.3.3"),
        ("langsmith", "0.8.18"),
        ("openinference-instrumentation-langchain", "0.1.67"),
    ],
)
def test_parent_compat_rejects_unexpected_dependency_version(
    monkeypatch,
    distribution: str,
    expected_version: str,
):
    import importlib.metadata

    from deerflow.tracing import phoenix

    real_version = importlib.metadata.version
    target_distribution = distribution
    monkeypatch.setattr(
        importlib.metadata,
        "version",
        lambda distribution: "0.1.68" if distribution == target_distribution else real_version(distribution),
    )

    with pytest.raises(
        phoenix.PhoenixTracingError,
        match=rf"requires {distribution}=={expected_version}",
    ):
        phoenix._validate_openinference_langchain_parent_contract()


def test_parent_compat_rejects_changed_dotted_order_parser_contract(monkeypatch):
    from langsmith import run_trees

    from deerflow.tracing import phoenix

    monkeypatch.setattr(run_trees, "_parse_dotted_order", lambda _dotted_order: [])

    with pytest.raises(phoenix.PhoenixTracingError, match="unexpected ancestry order"):
        phoenix._validate_openinference_langchain_parent_contract()


@pytest.mark.parametrize("run_type", ["llm", "tool", "chain", "retriever"])
def test_external_parent_miss_uses_nearest_registered_business_ancestor(parent_runtime, run_type: str):
    tracer = parent_runtime["openinference_tracer"]
    business_run_id = uuid4()
    terminal_name = f"external-parent-{run_type}"
    external_wrapper = _external_wrapper_for_business_parent(business_run_id)

    with parent_runtime["manual_tracer"].start_as_current_span("deerflow-manual-root") as manual_root:
        _start_business_parent(tracer, business_run_id)
        with set_tracing_parent(external_wrapper):
            _run_terminal(
                tracer,
                run_type,
                run_id=uuid4(),
                parent_run_id=external_wrapper.id,
                name=terminal_name,
            )
        _end_business_parent(tracer, business_run_id)

    business_span = _finished_span(parent_runtime, "business-parent")
    terminal_span = _finished_span(parent_runtime, terminal_name)
    assert terminal_span.parent is not None
    assert terminal_span.parent.span_id != manual_root.get_span_context().span_id
    assert terminal_span.parent.span_id == business_span.context.span_id


def test_external_parent_without_registered_ancestor_does_not_use_ambient_manual_root(parent_runtime):
    tracer = parent_runtime["openinference_tracer"]
    external_root = RunTree(name="external-root", inputs={})
    terminal_name = "no-registered-ancestor-chain"

    with parent_runtime["manual_tracer"].start_as_current_span("deerflow-manual-root") as manual_root:
        with set_tracing_parent(external_root):
            _run_terminal(
                tracer,
                "chain",
                run_id=uuid4(),
                parent_run_id=external_root.id,
                name=terminal_name,
            )

    terminal_span = _finished_span(parent_runtime, terminal_name)
    assert terminal_span.parent is None
    assert terminal_span.context.trace_id != manual_root.get_span_context().trace_id


def test_external_parent_bridge_survives_isolated_loop_thread_context(parent_runtime):
    tracer = parent_runtime["openinference_tracer"]
    business_run_id = uuid4()
    external_wrapper = _external_wrapper_for_business_parent(business_run_id)
    terminal_name = "isolated-loop-thread-tool"

    async def run_terminal_in_isolated_loop() -> None:
        _run_terminal(
            tracer,
            "tool",
            run_id=uuid4(),
            parent_run_id=external_wrapper.id,
            name=terminal_name,
        )

    with parent_runtime["manual_tracer"].start_as_current_span("deerflow-manual-root") as manual_root:
        _start_business_parent(tracer, business_run_id)
        with set_tracing_parent(external_wrapper):
            parent_context = copy_context()
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(parent_context.run, lambda: asyncio.run(run_terminal_in_isolated_loop())).result(timeout=10)
        _end_business_parent(tracer, business_run_id)

    business_span = _finished_span(parent_runtime, "business-parent")
    terminal_span = _finished_span(parent_runtime, terminal_name)
    assert terminal_span.parent is not None
    assert terminal_span.parent.span_id != manual_root.get_span_context().span_id
    assert terminal_span.parent.span_id == business_span.context.span_id

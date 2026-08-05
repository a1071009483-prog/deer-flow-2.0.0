from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from typing import Any

import pytest
from openinference.semconv.trace import SpanAttributes
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from deerflow.tracing.phoenix_boundary_io import PhoenixBoundaryIOProcessor

_BOUNDARY_NAME = "deerflow.run"
_BOUNDARY_SCOPE = "deerflow.tracing.phoenix"
_GRAPH_SCOPE = "openinference.instrumentation.langchain"
_ROOT_RUN_NAME = "deerflow.root_run_name"
_IO_ATTRIBUTES = (
    SpanAttributes.INPUT_VALUE,
    SpanAttributes.INPUT_MIME_TYPE,
    SpanAttributes.OUTPUT_VALUE,
    SpanAttributes.OUTPUT_MIME_TYPE,
)


@pytest.fixture
def runtime():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    processor = PhoenixBoundaryIOProcessor(
        boundary_span_name=_BOUNDARY_NAME,
        boundary_instrumentation_scope=_BOUNDARY_SCOPE,
        root_run_name_attribute=_ROOT_RUN_NAME,
    )
    provider.add_span_processor(processor)
    yield provider, exporter, processor
    provider.shutdown()


def _start_boundary(provider: TracerProvider, run_name: str):
    boundary = provider.get_tracer(_BOUNDARY_SCOPE).start_span(_BOUNDARY_NAME)
    boundary.set_attribute("deerflow.span.role", "run_boundary")
    boundary.set_attribute(_ROOT_RUN_NAME, run_name)
    return boundary


def _start_child(provider: TracerProvider, parent: Any, name: str):
    parent_context = trace.set_span_in_context(parent)
    return provider.get_tracer(_GRAPH_SCOPE).start_span(name, context=parent_context)


def _set_io(span: Any, *, input_value: str, output_value: str | None) -> None:
    span.set_attribute(SpanAttributes.INPUT_VALUE, input_value)
    span.set_attribute(SpanAttributes.INPUT_MIME_TYPE, "application/json")
    if output_value is not None:
        span.set_attribute(SpanAttributes.OUTPUT_VALUE, output_value)
        span.set_attribute(SpanAttributes.OUTPUT_MIME_TYPE, "application/json")


def _finished_by_id(exporter: InMemorySpanExporter) -> dict[int, Any]:
    return {span.context.span_id: span for span in exporter.get_finished_spans()}


def test_copies_only_standard_io_from_matching_direct_graph_child(runtime):
    provider, exporter, processor = runtime
    boundary = _start_boundary(provider, "lead_agent")
    child = _start_child(provider, boundary, "lead_agent")
    _set_io(child, input_value='{"messages":["hello"]}', output_value='{"messages":["done"]}')
    child.set_attribute("metadata", '{"private":"do-not-copy"}')
    child.set_attribute("custom.business", "do-not-copy")

    child.end()
    boundary.end()

    spans = _finished_by_id(exporter)
    exported_boundary = spans[boundary.get_span_context().span_id]
    exported_child = spans[child.get_span_context().span_id]
    for attribute in _IO_ATTRIBUTES:
        assert exported_boundary.attributes[attribute] == exported_child.attributes[attribute]
    assert "metadata" not in exported_boundary.attributes
    assert "custom.business" not in exported_boundary.attributes
    assert processor._active_boundaries == {}


def test_ignores_wrong_name_and_non_direct_descendant(runtime):
    provider, exporter, processor = runtime
    boundary = _start_boundary(provider, "lead_agent")

    wrong_direct = _start_child(provider, boundary, "model")
    _set_io(wrong_direct, input_value='{"wrong":"direct"}', output_value='{"wrong":"direct"}')
    matching_grandchild = _start_child(provider, wrong_direct, "lead_agent")
    _set_io(matching_grandchild, input_value='{"wrong":"grandchild"}', output_value='{"wrong":"grandchild"}')
    matching_grandchild.end()
    wrong_direct.end()

    matching_direct = _start_child(provider, boundary, "lead_agent")
    _set_io(matching_direct, input_value='{"right":"input"}', output_value='{"right":"output"}')
    matching_direct.end()
    boundary.end()

    exported_boundary = _finished_by_id(exporter)[boundary.get_span_context().span_id]
    assert exported_boundary.attributes[SpanAttributes.INPUT_VALUE] == '{"right":"input"}'
    assert exported_boundary.attributes[SpanAttributes.OUTPUT_VALUE] == '{"right":"output"}'
    assert processor._active_boundaries == {}


def test_missing_output_is_not_synthesized(runtime):
    provider, exporter, processor = runtime
    boundary = _start_boundary(provider, "lead_agent")
    child = _start_child(provider, boundary, "lead_agent")
    _set_io(child, input_value='{"messages":["hello"]}', output_value=None)

    child.end()
    boundary.end()

    exported_boundary = _finished_by_id(exporter)[boundary.get_span_context().span_id]
    assert exported_boundary.attributes[SpanAttributes.INPUT_VALUE] == '{"messages":["hello"]}'
    assert exported_boundary.attributes[SpanAttributes.INPUT_MIME_TYPE] == "application/json"
    assert SpanAttributes.OUTPUT_VALUE not in exported_boundary.attributes
    assert SpanAttributes.OUTPUT_MIME_TYPE not in exported_boundary.attributes
    assert processor._active_boundaries == {}


def test_boundary_without_graph_child_stays_empty_and_is_cleaned(runtime):
    provider, exporter, processor = runtime
    boundary = _start_boundary(provider, "lead_agent")
    boundary.end()

    exported_boundary = _finished_by_id(exporter)[boundary.get_span_context().span_id]
    for attribute in _IO_ATTRIBUTES:
        assert attribute not in exported_boundary.attributes
    assert processor._active_boundaries == {}


def test_concurrent_boundaries_do_not_cross_contaminate(runtime):
    provider, exporter, processor = runtime
    boundary_a = _start_boundary(provider, "graph-a")
    boundary_b = _start_boundary(provider, "graph-b")
    barrier = Barrier(2)

    def finish_graph(boundary: Any, name: str, marker: str) -> None:
        child = _start_child(provider, boundary, name)
        _set_io(
            child,
            input_value=f'{{"input":"{marker}"}}',
            output_value=f'{{"output":"{marker}"}}',
        )
        barrier.wait(timeout=5)
        child.end()

    with ThreadPoolExecutor(max_workers=2) as pool:
        future_a = pool.submit(finish_graph, boundary_a, "graph-a", "a")
        future_b = pool.submit(finish_graph, boundary_b, "graph-b", "b")
        future_a.result(timeout=10)
        future_b.result(timeout=10)

    boundary_b.end()
    boundary_a.end()

    spans = _finished_by_id(exporter)
    exported_a = spans[boundary_a.get_span_context().span_id]
    exported_b = spans[boundary_b.get_span_context().span_id]
    assert exported_a.attributes[SpanAttributes.INPUT_VALUE] == '{"input":"a"}'
    assert exported_a.attributes[SpanAttributes.OUTPUT_VALUE] == '{"output":"a"}'
    assert exported_b.attributes[SpanAttributes.INPUT_VALUE] == '{"input":"b"}'
    assert exported_b.attributes[SpanAttributes.OUTPUT_VALUE] == '{"output":"b"}'
    assert processor._active_boundaries == {}


def test_mirror_failure_is_swallowed_without_logging_content(runtime, caplog):
    _, _, processor = runtime
    parent_context = trace.SpanContext(
        trace_id=1,
        span_id=2,
        is_remote=False,
        trace_flags=trace.TraceFlags(1),
        trace_state=trace.TraceState(),
    )
    child_context = trace.SpanContext(
        trace_id=1,
        span_id=3,
        is_remote=False,
        trace_flags=trace.TraceFlags(1),
        trace_state=trace.TraceState(),
    )

    class RejectingBoundary:
        attributes = {_ROOT_RUN_NAME: "lead_agent"}

        def set_attribute(self, _key: str, value: Any) -> None:
            raise RuntimeError(f"must not leak {value}")

    with processor._lock:
        processor._active_boundaries[(1, 2)] = RejectingBoundary()
    child = SimpleNamespace(
        name="lead_agent",
        parent=parent_context,
        context=child_context,
        attributes={SpanAttributes.INPUT_VALUE: "secret-input"},
        instrumentation_scope=SimpleNamespace(name=_GRAPH_SCOPE),
    )

    with caplog.at_level(logging.WARNING, logger="deerflow.tracing.phoenix_boundary_io"):
        processor.on_end(child)

    assert "could not mirror graph attributes" in caplog.text
    assert "secret-input" not in caplog.text
    assert processor._active_boundaries == {}


def test_shutdown_clears_live_boundary_registry(runtime):
    provider, exporter, processor = runtime
    boundary = _start_boundary(provider, "lead_agent")
    assert len(processor._active_boundaries) == 1

    processor.shutdown()
    assert processor._active_boundaries == {}

    child = _start_child(provider, boundary, "lead_agent")
    _set_io(child, input_value='{"after":"shutdown"}', output_value='{"ignored":true}')
    child.end()
    boundary.end()
    exported_boundary = _finished_by_id(exporter)[boundary.get_span_context().span_id]
    for attribute in _IO_ATTRIBUTES:
        assert attribute not in exported_boundary.attributes

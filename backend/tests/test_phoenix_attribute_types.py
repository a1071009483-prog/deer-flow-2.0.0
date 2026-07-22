"""Real OTel acceptance coverage for Task 7.8 root attribute types."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from deerflow.config.tracing_config import PhoenixTracingConfig
from deerflow.tracing.phoenix import PhoenixRootContext


def _config(*, capture_content: bool) -> PhoenixTracingConfig:
    return PhoenixTracingConfig(
        enabled=True,
        collector_endpoint="http://phoenix.test:6006",
        api_key=None,
        project_name="deer-flow-task-7.8",
        auto_instrument=False,
        capture_content=capture_content,
        trace_parent_mode="root",
        trace_parent_required=False,
        propagate_baggage=False,
    )


def _root() -> PhoenixRootContext:
    return PhoenixRootContext(
        run_name="lead_agent",
        session_id="thread-task-7.8",
        user_id="user-task-7.8",
        metadata={
            "prompt": "captured only when explicitly enabled",
            "custom": {"nested": True},
        },
        tags=["caller-tag"],
        correlation_metadata={
            "request_id": "request-task-7.8",
            "tenant_id": "tenant-task-7.8",
        },
        correlation_tags=["safe-tag"],
        agent_name="lead_agent",
    )


@pytest.fixture
def otel_runtime(monkeypatch: pytest.MonkeyPatch):
    from deerflow.tracing import phoenix

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    monkeypatch.setattr(phoenix, "ensure_phoenix_tracing_initialized", lambda _config=None: None)
    monkeypatch.setattr(phoenix, "_get_phoenix_tracer", provider.get_tracer)

    runtime = SimpleNamespace(
        exporter=exporter,
        provider=provider,
        set_config=lambda config: monkeypatch.setattr(
            phoenix,
            "get_tracing_config",
            lambda: SimpleNamespace(phoenix=config),
        ),
    )
    try:
        yield runtime
    finally:
        provider.shutdown()


@pytest.mark.parametrize("capture_content", [False, True], ids=["safe", "full"])
def test_deerflow_root_never_writes_dict_metadata_as_otel_attribute(
    otel_runtime,
    caplog: pytest.LogCaptureFixture,
    capture_content: bool,
) -> None:
    from deerflow.tracing.phoenix import activate_phoenix_root_context

    otel_runtime.set_config(_config(capture_content=capture_content))
    caplog.clear()

    with caplog.at_level(logging.WARNING):
        with activate_phoenix_root_context(_root()):
            pass

    assert otel_runtime.provider.force_flush() is not False
    boundaries = [span for span in otel_runtime.exporter.get_finished_spans() if span.name == "deerflow.run"]
    assert len(boundaries) == 1
    attributes = boundaries[0].attributes

    metadata_type_warnings = [record.getMessage() for record in caplog.records if "Invalid type dict" in record.getMessage() and "metadata" in record.getMessage()]
    assert metadata_type_warnings == []
    assert "metadata" not in attributes

    assert attributes["session.id"] == "thread-task-7.8"
    assert attributes["user.id"] == "user-task-7.8"
    assert isinstance(attributes["openinference.span.kind"], str)
    assert list(attributes["tag.tags"]) == (["caller-tag"] if capture_content else ["safe-tag"])
    assert attributes["deerflow.span.role"] == "run_boundary"
    assert attributes["deerflow.agent_name"] == "lead_agent"
    assert attributes["deerflow.root_run_name"] == "lead_agent"
    assert attributes["deerflow.trace_parent_mode"] == "root"

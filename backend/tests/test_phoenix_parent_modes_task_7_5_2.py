"""Real OTel acceptance coverage for Task 7.5.2 parent-mode semantics."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from opentelemetry import baggage, context, trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_ON

from deerflow.config.tracing_config import PhoenixTracingConfig
from deerflow.tracing.otel_context import (
    TraceContextCarrier,
    deserialize_trace_context,
    extract_trace_context_from_headers,
    serialize_trace_context,
)
from deerflow.tracing.phoenix import PhoenixRootContext

UPSTREAM_TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
UPSTREAM_PARENT_ID = "00f067aa0ba902b7"
VALID_UNSAMPLED_TRACEPARENT = f"00-{UPSTREAM_TRACE_ID}-{UPSTREAM_PARENT_ID}-00"
INVALID_TRACEPARENT = "not-valid"
INVALID_TRACEPARENTS = [
    pytest.param("   ", id="whitespace"),
    pytest.param(INVALID_TRACEPARENT, id="generic-malformed"),
    pytest.param(f"00-{'0' * 32}-{UPSTREAM_PARENT_ID}-01", id="zero-trace-id"),
    pytest.param(f"00-{UPSTREAM_TRACE_ID}-{'0' * 16}-01", id="zero-span-id"),
    pytest.param(f"ff-{UPSTREAM_TRACE_ID}-{UPSTREAM_PARENT_ID}-01", id="ff-version"),
    pytest.param(f"00-{UPSTREAM_TRACE_ID}-{UPSTREAM_PARENT_ID}-01-extra", id="version-00-suffix"),
]
PARENT_MODES = [
    pytest.param("auto", False, id="auto"),
    pytest.param("child", True, id="strict-child"),
    pytest.param("child", False, id="non-strict-child"),
]


def _config(
    mode: str,
    *,
    required: bool = False,
    propagate_baggage: bool = False,
) -> PhoenixTracingConfig:
    return PhoenixTracingConfig(
        enabled=True,
        collector_endpoint="http://phoenix.test:6006",
        api_key=None,
        project_name="deer-flow-parent-mode-test",
        auto_instrument=False,
        capture_content=True,
        trace_parent_mode=mode,
        trace_parent_required=required,
        propagate_baggage=propagate_baggage,
    )


def _root(upstream: TraceContextCarrier | None = None) -> PhoenixRootContext:
    return PhoenixRootContext(
        run_name="lead_agent",
        session_id="thread-parent-mode",
        user_id="user-parent-mode",
        metadata={},
        tags=[],
        upstream_context=upstream,
        agent_name="lead_agent",
    )


@pytest.fixture
def otel_runtime(monkeypatch: pytest.MonkeyPatch):
    from deerflow.tracing import phoenix

    exporter = InMemorySpanExporter()
    provider = TracerProvider(sampler=ALWAYS_ON)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("deerflow.tests.task-7.5.2")

    monkeypatch.setattr(phoenix, "ensure_phoenix_tracing_initialized", lambda _config=None: None)
    monkeypatch.setattr(phoenix, "_get_phoenix_tracer", provider.get_tracer)
    monkeypatch.setattr(trace, "get_tracer", provider.get_tracer)

    runtime = SimpleNamespace(
        exporter=exporter,
        provider=provider,
        tracer=tracer,
        set_config=lambda config_: monkeypatch.setattr(
            phoenix,
            "get_tracing_config",
            lambda: SimpleNamespace(phoenix=config_),
        ),
    )
    try:
        yield runtime
    finally:
        provider.shutdown()


def _boundary_span(runtime):
    boundaries = [span for span in runtime.exporter.get_finished_spans() if span.name == "deerflow.run"]
    assert len(boundaries) == 1
    return boundaries[0]


def _round_trip_ingress_traceparent(traceparent: str) -> TraceContextCarrier | None:
    extracted = extract_trace_context_from_headers({"traceparent": traceparent})
    return deserialize_trace_context(serialize_trace_context(extracted))


def test_root_mode_creates_root_isolated_from_ambient_and_restores_context(otel_runtime):
    from deerflow.tracing.phoenix import activate_phoenix_root_context

    otel_runtime.set_config(_config("root", propagate_baggage=True))
    upstream = TraceContextCarrier(
        traceparent=VALID_UNSAMPLED_TRACEPARENT,
        baggage="tenant=acme",
    )
    ambient_baggage = baggage.set_baggage("ambient-only", "keep")
    ambient_token = context.attach(ambient_baggage)
    try:
        with otel_runtime.tracer.start_as_current_span("ambient") as ambient:
            ambient_context = ambient.get_span_context()

            with activate_phoenix_root_context(_root(upstream)):
                active_context = trace.get_current_span().get_span_context()
                assert active_context.trace_id != ambient_context.trace_id
                assert active_context.trace_id != int(UPSTREAM_TRACE_ID, 16)
                assert baggage.get_baggage("tenant") == "acme"
                assert baggage.get_baggage("ambient-only") is None

            assert trace.get_current_span().get_span_context() == ambient_context
            assert baggage.get_baggage("ambient-only") == "keep"
    finally:
        context.detach(ambient_token)

    boundary = _boundary_span(otel_runtime)
    assert boundary.parent is None
    assert boundary.context.trace_id != ambient_context.trace_id
    assert boundary.context.trace_id != int(UPSTREAM_TRACE_ID, 16)
    assert "deerflow.trace_parent_fallback" not in boundary.attributes


def test_root_mode_ignores_invalid_carrier(otel_runtime):
    from deerflow.tracing.phoenix import activate_phoenix_root_context

    otel_runtime.set_config(_config("root"))

    with activate_phoenix_root_context(_root(TraceContextCarrier(traceparent=INVALID_TRACEPARENT))):
        pass

    assert _boundary_span(otel_runtime).parent is None


@pytest.mark.parametrize(
    ("upstream", "fallback"),
    [
        (None, "missing_parent"),
        (TraceContextCarrier(traceparent=INVALID_TRACEPARENT), "invalid_parent"),
    ],
    ids=["missing", "invalid"],
)
def test_auto_fallback_creates_root_isolated_from_ambient(otel_runtime, upstream, fallback):
    from deerflow.tracing.phoenix import activate_phoenix_root_context

    otel_runtime.set_config(_config("auto"))

    with otel_runtime.tracer.start_as_current_span("ambient") as ambient:
        ambient_context = ambient.get_span_context()
        with activate_phoenix_root_context(_root(upstream)):
            assert trace.get_current_span().get_span_context().trace_id != ambient_context.trace_id
        assert trace.get_current_span().get_span_context() == ambient_context

    boundary = _boundary_span(otel_runtime)
    assert boundary.parent is None
    assert boundary.context.trace_id != ambient_context.trace_id
    assert boundary.attributes["deerflow.trace_parent_fallback"] == fallback


@pytest.mark.parametrize(
    ("mode", "required"),
    [("auto", False), ("child", True), ("child", False)],
)
def test_valid_unsampled_parent_continues_trace_and_restores_ambient(
    otel_runtime,
    mode,
    required,
):
    from deerflow.tracing.phoenix import activate_phoenix_root_context

    otel_runtime.set_config(_config(mode, required=required, propagate_baggage=True))
    upstream = TraceContextCarrier(
        traceparent=VALID_UNSAMPLED_TRACEPARENT,
        tracestate="vendor=value",
        baggage="tenant=acme",
    )

    with otel_runtime.tracer.start_as_current_span("ambient") as ambient:
        ambient_context = ambient.get_span_context()
        with activate_phoenix_root_context(_root(upstream)):
            assert trace.get_current_span().get_span_context().trace_id == int(UPSTREAM_TRACE_ID, 16)
            assert baggage.get_baggage("tenant") == "acme"
        assert trace.get_current_span().get_span_context() == ambient_context

    boundary = _boundary_span(otel_runtime)
    assert boundary.context.trace_id == int(UPSTREAM_TRACE_ID, 16)
    assert boundary.parent is not None
    assert boundary.parent.span_id == int(UPSTREAM_PARENT_ID, 16)
    assert boundary.parent.is_remote
    assert "deerflow.trace_parent_fallback" not in boundary.attributes


@pytest.mark.parametrize(
    ("upstream", "reason"),
    [
        (None, "missing"),
        (TraceContextCarrier(traceparent=INVALID_TRACEPARENT), "invalid"),
    ],
    ids=["missing", "invalid"],
)
def test_strict_child_rejects_missing_or_invalid_parent_before_boundary(
    otel_runtime,
    upstream,
    reason,
):
    from deerflow.tracing.phoenix import PhoenixTracingError, activate_phoenix_root_context

    otel_runtime.set_config(_config("child", required=True))

    with otel_runtime.tracer.start_as_current_span("ambient") as ambient:
        ambient_context = ambient.get_span_context()
        with pytest.raises(PhoenixTracingError, match=reason):
            with activate_phoenix_root_context(_root(upstream)):
                pass
        assert trace.get_current_span().get_span_context() == ambient_context

    boundaries = [span for span in otel_runtime.exporter.get_finished_spans() if span.name == "deerflow.run"]
    assert boundaries == []


@pytest.mark.parametrize(
    ("upstream", "fallback"),
    [
        (None, "missing_parent"),
        (TraceContextCarrier(traceparent=INVALID_TRACEPARENT), "invalid_parent"),
    ],
    ids=["missing", "invalid"],
)
def test_non_strict_child_fallback_creates_isolated_root(otel_runtime, upstream, fallback):
    from deerflow.tracing.phoenix import activate_phoenix_root_context

    otel_runtime.set_config(_config("child"))

    with otel_runtime.tracer.start_as_current_span("ambient") as ambient:
        ambient_context = ambient.get_span_context()
        with activate_phoenix_root_context(_root(upstream)):
            pass
        assert trace.get_current_span().get_span_context() == ambient_context

    boundary = _boundary_span(otel_runtime)
    assert boundary.parent is None
    assert boundary.context.trace_id != ambient_context.trace_id
    assert boundary.attributes["deerflow.trace_parent_fallback"] == fallback


@pytest.mark.parametrize(("mode", "required"), PARENT_MODES)
def test_empty_ingress_traceparent_is_missing_and_isolates_ambient(
    otel_runtime,
    mode,
    required,
):
    from deerflow.tracing.phoenix import PhoenixTracingError, activate_phoenix_root_context

    upstream = _round_trip_ingress_traceparent("")
    assert upstream is None
    otel_runtime.set_config(_config(mode, required=required))

    with otel_runtime.tracer.start_as_current_span("ambient") as ambient:
        ambient_context = ambient.get_span_context()
        if required:
            with pytest.raises(PhoenixTracingError, match="missing"):
                with activate_phoenix_root_context(_root(upstream)):
                    pass
        else:
            with activate_phoenix_root_context(_root(upstream)):
                assert trace.get_current_span().get_span_context().trace_id != ambient_context.trace_id
        assert trace.get_current_span().get_span_context() == ambient_context

    boundaries = [span for span in otel_runtime.exporter.get_finished_spans() if span.name == "deerflow.run"]
    if required:
        assert boundaries == []
    else:
        boundary = _boundary_span(otel_runtime)
        assert boundary.parent is None
        assert boundary.context.trace_id != ambient_context.trace_id
        assert boundary.attributes["deerflow.trace_parent_fallback"] == "missing_parent"


@pytest.mark.parametrize(("mode", "required"), PARENT_MODES)
@pytest.mark.parametrize("traceparent", INVALID_TRACEPARENTS)
def test_supplied_invalid_ingress_traceparent_stays_invalid_and_isolates_ambient(
    otel_runtime,
    mode,
    required,
    traceparent,
):
    from deerflow.tracing.phoenix import PhoenixTracingError, activate_phoenix_root_context

    upstream = _round_trip_ingress_traceparent(traceparent)
    assert upstream is not None
    assert upstream.traceparent == traceparent
    otel_runtime.set_config(_config(mode, required=required))

    with otel_runtime.tracer.start_as_current_span("ambient") as ambient:
        ambient_context = ambient.get_span_context()
        if required:
            with pytest.raises(PhoenixTracingError, match="invalid"):
                with activate_phoenix_root_context(_root(upstream)):
                    pass
        else:
            with activate_phoenix_root_context(_root(upstream)):
                assert trace.get_current_span().get_span_context().trace_id != ambient_context.trace_id
        assert trace.get_current_span().get_span_context() == ambient_context

    boundaries = [span for span in otel_runtime.exporter.get_finished_spans() if span.name == "deerflow.run"]
    if required:
        assert boundaries == []
    else:
        boundary = _boundary_span(otel_runtime)
        assert boundary.parent is None
        assert boundary.context.trace_id != ambient_context.trace_id
        assert boundary.attributes["deerflow.trace_parent_fallback"] == "invalid_parent"


def test_exception_unwind_restores_ambient_and_preserves_original_error(otel_runtime):
    from deerflow.tracing.phoenix import activate_phoenix_root_context

    class SentinelError(Exception):
        pass

    otel_runtime.set_config(_config("root", propagate_baggage=True))
    upstream = TraceContextCarrier(
        traceparent=VALID_UNSAMPLED_TRACEPARENT,
        baggage="tenant=acme",
    )
    sentinel = SentinelError("sentinel body failure")
    ambient_baggage = baggage.set_baggage("ambient-only", "keep")
    ambient_token = context.attach(ambient_baggage)
    try:
        with otel_runtime.tracer.start_as_current_span("ambient") as ambient:
            ambient_context = ambient.get_span_context()
            with pytest.raises(SentinelError) as exc_info:
                with activate_phoenix_root_context(_root(upstream)):
                    assert baggage.get_baggage("tenant") == "acme"
                    assert baggage.get_baggage("ambient-only") is None
                    raise sentinel

            assert exc_info.value is sentinel
            assert trace.get_current_span().get_span_context() == ambient_context
            assert baggage.get_baggage("ambient-only") == "keep"
            assert baggage.get_baggage("tenant") is None
    finally:
        context.detach(ambient_token)

    boundary = _boundary_span(otel_runtime)
    assert boundary.parent is None
    assert boundary.end_time is not None


PARENT_BAGGAGE_CASES = [
    pytest.param("root", False, "valid", id="root-valid"),
    pytest.param("root", False, "invalid", id="root-invalid"),
    pytest.param("root", False, "missing", id="root-missing"),
    pytest.param("auto", False, "valid", id="auto-valid"),
    pytest.param("auto", False, "invalid", id="auto-invalid"),
    pytest.param("auto", False, "missing", id="auto-missing"),
    pytest.param("child", False, "valid", id="non-strict-child-valid"),
    pytest.param("child", False, "invalid", id="non-strict-child-invalid"),
    pytest.param("child", False, "missing", id="non-strict-child-missing"),
    pytest.param("child", True, "valid", id="strict-child-valid"),
]


@pytest.mark.parametrize("mode,required,parent_status", PARENT_BAGGAGE_CASES)
@pytest.mark.parametrize("propagate_baggage", [True, False], ids=["baggage-on", "baggage-off"])
def test_parent_and_baggage_matrix_never_inherits_ambient(
    otel_runtime,
    mode,
    required,
    parent_status,
    propagate_baggage,
):
    from deerflow.tracing.phoenix import activate_phoenix_root_context

    upstream_by_status = {
        "valid": TraceContextCarrier(
            traceparent=VALID_UNSAMPLED_TRACEPARENT,
            baggage="tenant=explicit",
        ),
        "invalid": TraceContextCarrier(
            traceparent=INVALID_TRACEPARENT,
            baggage="tenant=explicit",
        ),
        "missing": TraceContextCarrier(baggage="tenant=explicit"),
    }
    upstream = deserialize_trace_context(serialize_trace_context(upstream_by_status[parent_status]))
    assert upstream is not None
    otel_runtime.set_config(
        _config(
            mode,
            required=required,
            propagate_baggage=propagate_baggage,
        )
    )

    ambient_token = context.attach(baggage.set_baggage("ambient-only", "secret"))
    try:
        with otel_runtime.tracer.start_as_current_span("ambient") as ambient:
            ambient_context = ambient.get_span_context()
            with activate_phoenix_root_context(_root(upstream)):
                assert baggage.get_baggage("tenant") == ("explicit" if propagate_baggage else None)
                assert baggage.get_baggage("ambient-only") is None
                assert trace.get_current_span().get_span_context().trace_id != ambient_context.trace_id

            assert trace.get_current_span().get_span_context() == ambient_context
            assert baggage.get_baggage("ambient-only") == "secret"
            assert baggage.get_baggage("tenant") is None
    finally:
        context.detach(ambient_token)

    boundary = _boundary_span(otel_runtime)
    continues_parent = parent_status == "valid" and mode in {"auto", "child"}
    if continues_parent:
        assert boundary.context.trace_id == int(UPSTREAM_TRACE_ID, 16)
        assert boundary.parent is not None
        assert boundary.parent.span_id == int(UPSTREAM_PARENT_ID, 16)
        assert boundary.parent.is_remote
    else:
        assert boundary.parent is None
        assert boundary.context.trace_id != int(UPSTREAM_TRACE_ID, 16)
        assert boundary.context.trace_id != ambient_context.trace_id

    if mode == "root" or parent_status == "valid":
        assert "deerflow.trace_parent_fallback" not in boundary.attributes
    else:
        expected_fallback = "invalid_parent" if parent_status == "invalid" else "missing_parent"
        assert boundary.attributes["deerflow.trace_parent_fallback"] == expected_fallback


@pytest.mark.parametrize("parent_status", ["invalid", "missing"])
@pytest.mark.parametrize("propagate_baggage", [True, False])
def test_strict_child_rejects_before_activating_baggage_context(
    otel_runtime,
    parent_status,
    propagate_baggage,
):
    from deerflow.tracing.phoenix import PhoenixTracingError, activate_phoenix_root_context

    upstream_by_status = {
        "invalid": TraceContextCarrier(
            traceparent=INVALID_TRACEPARENT,
            baggage="tenant=explicit",
        ),
        "missing": TraceContextCarrier(baggage="tenant=explicit"),
    }
    upstream = deserialize_trace_context(serialize_trace_context(upstream_by_status[parent_status]))
    assert upstream is not None
    otel_runtime.set_config(
        _config(
            "child",
            required=True,
            propagate_baggage=propagate_baggage,
        )
    )

    ambient_token = context.attach(baggage.set_baggage("ambient-only", "secret"))
    try:
        with otel_runtime.tracer.start_as_current_span("ambient") as ambient:
            ambient_context = ambient.get_span_context()
            expected_reason = "invalid" if parent_status == "invalid" else "missing"
            with pytest.raises(PhoenixTracingError, match=expected_reason):
                with activate_phoenix_root_context(_root(upstream)):
                    pytest.fail("strict child must fail before entering the run body")

            assert trace.get_current_span().get_span_context() == ambient_context
            assert baggage.get_baggage("ambient-only") == "secret"
            assert baggage.get_baggage("tenant") is None
    finally:
        context.detach(ambient_token)

    boundaries = [span for span in otel_runtime.exporter.get_finished_spans() if span.name == "deerflow.run"]
    assert boundaries == []

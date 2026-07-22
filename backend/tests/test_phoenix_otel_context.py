"""Tests for W3C OpenTelemetry trace context carriers."""

from __future__ import annotations

from deerflow.tracing.otel_context import (
    TraceContextCarrier,
    capture_current_trace_context,
    deserialize_trace_context,
    extract_trace_context_from_headers,
    serialize_trace_context,
)

TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


def _capture_from_span_context(span_context, *, include_baggage):
    from deerflow.tracing import otel_context

    capture = getattr(otel_context, "capture_trace_context_from_span_context", None)
    assert capture is not None, "SpanContext carrier capture API is required"
    return capture(span_context, include_baggage=include_baggage)


def test_capture_trace_context_from_span_context_uses_exact_span_without_changing_current():
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider

    provider = TracerProvider()
    tracer = provider.get_tracer("deerflow.tests.otel-context")
    try:
        with tracer.start_as_current_span("ambient") as ambient_span:
            child_span = tracer.start_span("logical-child")
            try:
                carrier = _capture_from_span_context(
                    child_span.get_span_context(),
                    include_baggage=False,
                )

                assert carrier is not None
                assert carrier.traceparent is not None
                assert carrier.traceparent.split("-")[1] == f"{child_span.get_span_context().trace_id:032x}"
                assert carrier.traceparent.split("-")[2] == f"{child_span.get_span_context().span_id:016x}"
                assert carrier.baggage is None
                assert trace.get_current_span().get_span_context() == ambient_span.get_span_context()
            finally:
                child_span.end()
    finally:
        provider.shutdown()


def test_capture_trace_context_from_span_context_preserves_current_baggage_when_enabled():
    from opentelemetry import baggage, context
    from opentelemetry.sdk.trace import TracerProvider

    provider = TracerProvider()
    span = provider.get_tracer("deerflow.tests.otel-context").start_span("logical-child")
    token = context.attach(baggage.set_baggage("tenant", "acme"))
    try:
        carrier = _capture_from_span_context(
            span.get_span_context(),
            include_baggage=True,
        )

        assert carrier is not None
        assert carrier.baggage == "tenant=acme"
    finally:
        context.detach(token)
        span.end()
        provider.shutdown()


def test_capture_trace_context_from_span_context_rejects_invalid_context():
    from opentelemetry import trace

    assert (
        _capture_from_span_context(
            trace.INVALID_SPAN_CONTEXT,
            include_baggage=True,
        )
        is None
    )


def test_baggage_only_headers_survive_serialization_round_trip():
    carrier = extract_trace_context_from_headers({"baggage": "tenant=acme"})

    assert carrier == TraceContextCarrier(baggage="tenant=acme")
    assert serialize_trace_context(carrier) == {"baggage": "tenant=acme"}
    assert deserialize_trace_context({"baggage": "tenant=acme"}) == carrier


def test_tracestate_only_is_not_a_carrier():
    assert extract_trace_context_from_headers({"tracestate": "vendor=value"}) is None
    assert deserialize_trace_context({"tracestate": "vendor=value"}) is None


def test_capture_current_baggage_only_respects_include_flag(monkeypatch):
    from opentelemetry import baggage, context
    from opentelemetry.baggage.propagation import W3CBaggagePropagator

    from deerflow.tracing import otel_context

    monkeypatch.setattr(otel_context.propagate, "inject", W3CBaggagePropagator().inject)

    token = context.attach(baggage.set_baggage("tenant", "acme"))
    try:
        assert capture_current_trace_context(include_baggage=True) == TraceContextCarrier(baggage="tenant=acme")
        assert capture_current_trace_context(include_baggage=False) is None
    finally:
        context.detach(token)


def test_extract_trace_context_from_headers_serializes_w3c_fields():
    carrier = extract_trace_context_from_headers(
        {
            "TraceParent": TRACEPARENT,
            "TraceState": "rojo=00f067aa0ba902b7",
            "Baggage": "tenant=acme",
            "x-request-id": "ignored",
        }
    )

    assert carrier == TraceContextCarrier(
        traceparent=TRACEPARENT,
        tracestate="rojo=00f067aa0ba902b7",
        baggage="tenant=acme",
    )
    assert carrier.as_headers(include_baggage=False) == {
        "traceparent": TRACEPARENT,
        "tracestate": "rojo=00f067aa0ba902b7",
    }
    assert carrier.as_headers(include_baggage=True) == {
        "traceparent": TRACEPARENT,
        "tracestate": "rojo=00f067aa0ba902b7",
        "baggage": "tenant=acme",
    }
    assert serialize_trace_context(carrier) == {
        "traceparent": TRACEPARENT,
        "tracestate": "rojo=00f067aa0ba902b7",
        "baggage": "tenant=acme",
    }
    assert deserialize_trace_context(serialize_trace_context(carrier)) == carrier
    assert extract_trace_context_from_headers({"tracestate": "orphan"}) is None
    assert serialize_trace_context(None) == {}
    assert deserialize_trace_context({}) is None
    assert deserialize_trace_context(None) is None


def test_capture_current_trace_context_uses_propagator(monkeypatch):
    from deerflow.tracing import otel_context

    def inject(headers: dict[str, str]) -> None:
        headers["traceparent"] = TRACEPARENT
        headers["tracestate"] = "rojo=00f067aa0ba902b7"
        headers["baggage"] = "tenant=acme"

    monkeypatch.setattr(otel_context.propagate, "inject", inject)

    assert capture_current_trace_context(include_baggage=False) == TraceContextCarrier(
        traceparent=TRACEPARENT,
        tracestate="rojo=00f067aa0ba902b7",
        baggage=None,
    )
    assert capture_current_trace_context(include_baggage=True) == TraceContextCarrier(
        traceparent=TRACEPARENT,
        tracestate="rojo=00f067aa0ba902b7",
        baggage="tenant=acme",
    )

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from opentelemetry import propagate, trace

DEERFLOW_OTEL_TRACE_CONTEXT = "__otel_trace_context"


@dataclass(frozen=True)
class TraceContextCarrier:
    traceparent: str | None = None
    tracestate: str | None = None
    baggage: str | None = None

    def as_headers(self, *, include_baggage: bool) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.traceparent:
            headers["traceparent"] = self.traceparent
        if self.tracestate:
            headers["tracestate"] = self.tracestate
        if include_baggage and self.baggage:
            headers["baggage"] = self.baggage
        return headers


def extract_trace_context_from_headers(headers: Mapping[str, str]) -> TraceContextCarrier | None:
    lowered = {str(key).lower(): value for key, value in headers.items() if value}
    traceparent = lowered.get("traceparent")
    baggage_value = lowered.get("baggage")
    if not traceparent and not baggage_value:
        return None
    return TraceContextCarrier(
        traceparent=traceparent,
        tracestate=lowered.get("tracestate"),
        baggage=baggage_value,
    )


def serialize_trace_context(carrier: TraceContextCarrier | None) -> dict[str, str]:
    if carrier is None:
        return {}
    return carrier.as_headers(include_baggage=True)


def attach_trace_context_to_config(
    config: dict[str, Any],
    carrier: TraceContextCarrier | None,
    *,
    include_baggage: bool,
) -> None:
    serialized = carrier.as_headers(include_baggage=include_baggage) if carrier is not None else {}
    if not serialized:
        return
    runtime_context = config.setdefault("context", {})
    if isinstance(runtime_context, dict):
        runtime_context[DEERFLOW_OTEL_TRACE_CONTEXT] = serialized


def deserialize_trace_context(value: Mapping[str, Any] | None) -> TraceContextCarrier | None:
    if not value:
        return None
    carrier = TraceContextCarrier(
        traceparent=_supplied_string_or_none(value.get("traceparent")),
        tracestate=_string_or_none(value.get("tracestate")),
        baggage=_string_or_none(value.get("baggage")),
    )
    return carrier if carrier.traceparent is not None or carrier.baggage is not None else None


def capture_current_trace_context(*, include_baggage: bool) -> TraceContextCarrier | None:
    headers: dict[str, str] = {}
    propagate.inject(headers)
    carrier = extract_trace_context_from_headers(headers)
    return _apply_baggage_policy(carrier, include_baggage=include_baggage)


def capture_trace_context_from_span_context(
    span_context: Any,
    *,
    include_baggage: bool,
) -> TraceContextCarrier | None:
    if not getattr(span_context, "is_valid", False):
        return None

    context = trace.set_span_in_context(trace.NonRecordingSpan(span_context))
    headers: dict[str, str] = {}
    propagate.inject(headers, context=context)
    carrier = extract_trace_context_from_headers(headers)
    return _apply_baggage_policy(carrier, include_baggage=include_baggage)


def _apply_baggage_policy(
    carrier: TraceContextCarrier | None,
    *,
    include_baggage: bool,
) -> TraceContextCarrier | None:
    if carrier is None:
        return None
    if not include_baggage:
        filtered = TraceContextCarrier(
            traceparent=carrier.traceparent,
            tracestate=carrier.tracestate,
            baggage=None,
        )
        return filtered if filtered.traceparent is not None else None
    return carrier


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _supplied_string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text != "" else None

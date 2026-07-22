from __future__ import annotations

import argparse
import os
import uuid
from contextlib import ExitStack
from urllib.parse import urlsplit, urlunsplit

from opentelemetry import trace
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from phoenix.otel import OpenInferenceSpanKindValues, SpanAttributes, register, using_attributes

_PHOENIX_OTLP_TRACES_PATH = "/v1/traces"


def _extract_parent(traceparent: str | None):
    if not traceparent:
        return None
    return TraceContextTextMapPropagator().extract({"traceparent": traceparent})


def _normalize_collector_endpoint(endpoint: str) -> str:
    parts = urlsplit(endpoint)
    if parts.scheme in {"http", "https"} and parts.netloc and parts.path in {"", "/"}:
        return urlunsplit((parts.scheme, parts.netloc, _PHOENIX_OTLP_TRACES_PATH, "", ""))
    return endpoint


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default=os.environ.get("PHOENIX_PROJECT_NAME", "deer-flow-spike"))
    parser.add_argument("--endpoint", default=os.environ.get("PHOENIX_COLLECTOR_ENDPOINT", "http://localhost:6006"))
    parser.add_argument("--auto-instrument", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--traceparent", default=None)
    args = parser.parse_args()

    register(project_name=args.project, endpoint=_normalize_collector_endpoint(args.endpoint), auto_instrument=args.auto_instrument)
    tracer = trace.get_tracer("deerflow.phoenix.spike")
    session_id = f"spike-{uuid.uuid4()}"
    parent_context = _extract_parent(args.traceparent)

    with ExitStack() as stack:
        if parent_context is not None:
            from opentelemetry import context as otel_context

            token = otel_context.attach(parent_context)
            stack.callback(otel_context.detach, token)

        stack.enter_context(
            using_attributes(
                session_id=session_id,
                user_id="spike-user",
                metadata={"component": "deerflow", "spike": True},
                tags=["deer-flow", "phoenix-spike"],
            )
        )
        with tracer.start_as_current_span(
            "deerflow-spike-root",
            attributes={SpanAttributes.OPENINFERENCE_SPAN_KIND: OpenInferenceSpanKindValues.AGENT.value},
        ) as span:
            span.set_attribute(SpanAttributes.SESSION_ID, session_id)
            span.set_attribute(SpanAttributes.USER_ID, "spike-user")
            span.set_attribute("deerflow.trace_parent_mode", "spike")
            with tracer.start_as_current_span("deerflow-spike-child"):
                pass


if __name__ == "__main__":
    main()

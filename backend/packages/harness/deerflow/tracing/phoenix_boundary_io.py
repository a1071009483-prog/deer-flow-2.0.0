from __future__ import annotations

import logging
import threading
from typing import Any

from openinference.semconv.trace import SpanAttributes
from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor

type _SpanKey = tuple[int, int]

_COPIED_ATTRIBUTES = (
    SpanAttributes.INPUT_VALUE,
    SpanAttributes.INPUT_MIME_TYPE,
    SpanAttributes.OUTPUT_VALUE,
    SpanAttributes.OUTPUT_MIME_TYPE,
)

logger = logging.getLogger(__name__)


class PhoenixBoundaryIOProcessor(SpanProcessor):
    """Mirror a direct OpenInference graph span's I/O to its live run boundary."""

    def __init__(
        self,
        *,
        boundary_span_name: str,
        boundary_instrumentation_scope: str,
        root_run_name_attribute: str = "deerflow.root_run_name",
    ) -> None:
        self._boundary_span_name = boundary_span_name
        self._boundary_instrumentation_scope = boundary_instrumentation_scope
        self._root_run_name_attribute = root_run_name_attribute
        self._lock = threading.Lock()
        self._active_boundaries: dict[_SpanKey, Span] = {}

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        del parent_context
        try:
            if not self._is_boundary(span):
                return
            span_context = span.get_span_context()
            if not span_context.is_valid:
                return
            key = (span_context.trace_id, span_context.span_id)
            with self._lock:
                self._active_boundaries[key] = span
        except Exception:
            logger.warning("Phoenix boundary I/O processor could not register a run boundary.")

    def on_end(self, span: ReadableSpan) -> None:
        try:
            if self._is_boundary(span):
                self._discard_boundary((span.context.trace_id, span.context.span_id))
                return

            parent = span.parent
            if parent is None:
                return
            boundary_key = (parent.trace_id, parent.span_id)
            with self._lock:
                boundary = self._active_boundaries.get(boundary_key)
                if boundary is None:
                    return
                expected_graph_name = boundary.attributes.get(self._root_run_name_attribute)
                if span.name != expected_graph_name:
                    return
                boundary = self._active_boundaries.pop(boundary_key)

            for attribute in _COPIED_ATTRIBUTES:
                value = span.attributes.get(attribute)
                if value is not None:
                    boundary.set_attribute(attribute, value)
        except Exception:
            logger.warning("Phoenix boundary I/O processor could not mirror graph attributes.")

    def shutdown(self) -> None:
        with self._lock:
            self._active_boundaries.clear()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        del timeout_millis
        return True

    def _is_boundary(self, span: Any) -> bool:
        instrumentation_scope = span.instrumentation_scope
        return span.name == self._boundary_span_name and instrumentation_scope is not None and instrumentation_scope.name == self._boundary_instrumentation_scope

    def _discard_boundary(self, key: _SpanKey) -> None:
        with self._lock:
            self._active_boundaries.pop(key, None)

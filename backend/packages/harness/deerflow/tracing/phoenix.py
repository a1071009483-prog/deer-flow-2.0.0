from __future__ import annotations

import atexit
import importlib.metadata
import json
import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any
from uuid import UUID

from openinference.instrumentation import config as oi_config
from openinference.semconv.trace import SpanAttributes
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.context import Context
from opentelemetry.trace import get_current_span
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from deerflow.config.tracing_config import PhoenixTracingConfig, get_tracing_config
from deerflow.tracing.otel_context import (
    TraceContextCarrier,
    capture_current_trace_context,
    capture_trace_context_from_span_context,
)

type _PhoenixConfigKey = tuple[str, str, bool, bool, bool, str | None]

class DeerFlowTraceConfig(oi_config.TraceConfig):
    """Instance-local safe-capture policy for DeerFlow's Phoenix export.

    Delegates content hiding to the upstream ``TraceConfig`` and applies an
    exact allowlist to ``SpanAttributes.METADATA`` only when safe capture is
    active.  No process-level ``OPENINFERENCE_*`` environment variables are
    read or written.
    """

    def __init__(
        self,
        *,
        capture_content: bool,
        metadata_allowlist: tuple[str, ...],
    ) -> None:
        hidden = not capture_content
        super().__init__(
            hide_inputs=hidden,
            hide_outputs=hidden,
            hide_input_messages=hidden,
            hide_output_messages=hidden,
            hide_prompts=hidden,
            hide_choices=hidden,
            hide_llm_invocation_parameters=hidden,
            hide_llm_tools=hidden,
        )
        object.__setattr__(self, "_deerflow_capture_content", capture_content)
        object.__setattr__(self, "_deerflow_metadata_allowlist", frozenset(metadata_allowlist))

    def mask(self, key: str, value: Any) -> Any:
        masked = super().mask(key, value)
        if masked is None or self._deerflow_capture_content:
            return masked
        if key != SpanAttributes.METADATA:
            return masked
        try:
            decoded = json.loads(masked)
        except (TypeError, ValueError):
            return None
        if not isinstance(decoded, dict):
            return None
        filtered = {
            name: item
            for name, item in decoded.items()
            if name in self._deerflow_metadata_allowlist
            and not name.startswith("langfuse_")
        }
        return json.dumps(filtered, sort_keys=True, separators=(",", ":")) if filtered else None


_PARENT_COMPAT_DEPENDENCY_VERSIONS = {
    "langchain": "1.2.15",
    "langchain-core": "1.3.3",
    "langsmith": "0.8.18",
    "openinference-instrumentation-langchain": "0.1.67",
}
_init_lock = threading.Lock()
_graph_root_parent_override_lock = threading.Lock()
_graph_root_parent_overrides: dict[UUID, Any] = {}
_active_config_key: _PhoenixConfigKey | None = None
_phoenix_tracer_provider: Any | None = None
_phoenix_trace_config: DeerFlowTraceConfig | None = None
_parent_compat_tracer: Any | None = None
_parent_compat_base_class: type[Any] | None = None
_parent_compat_class: type[Any] | None = None
_phoenix_owned_instrumentors: list[_InstrumentorSnapshot] = []
_RUN_BOUNDARY_SPAN_NAME = "deerflow.run"
_DEFAULT_SHUTDOWN_TIMEOUT_MILLIS = 30_000

logger = logging.getLogger(__name__)


class PhoenixTracingError(RuntimeError):
    pass


def capture_current_phoenix_trace_context(
    *,
    include_baggage: bool,
) -> TraceContextCarrier | None:
    """Capture the logical Phoenix callback parent, with ambient fallback."""
    try:
        from langchain_core.runnables.config import ensure_config

        callbacks = ensure_config().get("callbacks")
        parent_run_id = getattr(callbacks, "parent_run_id", None)
        tracer = _parent_compat_tracer
        if parent_run_id is not None and tracer is not None:
            parent_span = tracer.get_span(parent_run_id)
            if parent_span is not None:
                carrier = capture_trace_context_from_span_context(
                    parent_span.get_span_context(),
                    include_baggage=include_baggage,
                )
                if carrier is not None:
                    return carrier
        logger.debug("Phoenix logical callback parent is unavailable; using ambient OTel context.")
    except Exception:
        logger.debug(
            "Phoenix logical callback parent capture failed; using ambient OTel context.",
            exc_info=True,
        )

    return capture_current_trace_context(include_baggage=include_baggage)


@dataclass(frozen=True)
class _InstrumentorSnapshot:
    instrumentor: Any
    instance_state: dict[str, Any]
    was_instrumented: bool


@dataclass(frozen=True)
class PhoenixRootContext:
    run_name: str
    session_id: str | None
    user_id: str | None
    metadata: dict[str, Any]
    tags: list[str]
    correlation_metadata: dict[str, Any] = field(default_factory=dict)
    correlation_tags: list[str] = field(default_factory=list)
    upstream_context: TraceContextCarrier | None = None
    agent_name: str = "unknown"


def ensure_phoenix_tracing_initialized(config: PhoenixTracingConfig | None = None) -> None:
    """Lazy, process-wide, idempotent Phoenix/OpenInference setup."""
    global _active_config_key, _phoenix_owned_instrumentors
    global _phoenix_trace_config, _phoenix_tracer_provider

    phoenix_config = config or get_tracing_config().phoenix
    if not phoenix_config.enabled:
        return

    config_key = _config_key(phoenix_config)
    with _init_lock:
        if _active_config_key is not None:
            if _active_config_key == config_key:
                return
            raise PhoenixTracingError("Phoenix tracing is already initialized with a different active configuration.")

        trace_config = DeerFlowTraceConfig(
            capture_content=phoenix_config.capture_content,
            metadata_allowlist=tuple(phoenix_config.metadata_allowlist),
        )
        tracer_provider: Any | None = None
        instrumentor_snapshots: list[_InstrumentorSnapshot] = []
        attempted_instrumentors: list[_InstrumentorSnapshot] = []
        owned_instrumentors: list[_InstrumentorSnapshot] = []
        try:
            if phoenix_config.auto_instrument:
                instrumentor_snapshots = _snapshot_openinference_instrumentors()

            from phoenix.otel import register

            register_kwargs = {
                "project_name": phoenix_config.project_name,
                "endpoint": phoenix_config.collector_endpoint,
                "auto_instrument": False,
                "set_global_tracer_provider": False,
                "batch": True,
            }
            if phoenix_config.api_key:
                register_kwargs["api_key"] = phoenix_config.api_key
            tracer_provider = register(**register_kwargs)
            if phoenix_config.auto_instrument:
                _reject_foreign_instrumentors(instrumentor_snapshots)
                _validate_openinference_langchain_parent_contract()
                for snapshot in instrumentor_snapshots:
                    attempted_instrumentors.append(snapshot)
                    snapshot.instrumentor.instrument(
                        tracer_provider=tracer_provider,
                        config=trace_config,
                    )
                    if not getattr(snapshot.instrumentor, "_is_instrumented_by_opentelemetry", False):
                        raise PhoenixTracingError("Phoenix tracing could not instrument an OpenInference entry point.")
                    owned_instrumentors.append(snapshot)
                _install_openinference_langchain_parent_compat(tracer_provider)
        except Exception as exc:
            _rollback_instrumentors(attempted_instrumentors)
            _clear_phoenix_state()
            if tracer_provider is not None:
                _shutdown_provider(tracer_provider, force_flush=False)
            raise PhoenixTracingError(f"Phoenix tracing initialization failed: {exc}") from exc

        _phoenix_tracer_provider = tracer_provider
        _phoenix_trace_config = trace_config
        _active_config_key = config_key
        _phoenix_owned_instrumentors = owned_instrumentors


def shutdown_phoenix_tracing(*, timeout_millis: int = _DEFAULT_SHUTDOWN_TIMEOUT_MILLIS) -> None:
    """Flush and close DeerFlow's process-owned Phoenix provider once."""
    global _phoenix_tracer_provider

    with _init_lock:
        tracer_provider = _phoenix_tracer_provider
        _clear_phoenix_state()
        if tracer_provider is not None:
            _shutdown_provider(tracer_provider, timeout_millis=timeout_millis)


def reset_phoenix_tracing_for_tests() -> None:
    """Reset only DeerFlow's initializer bookkeeping for tests."""
    with _init_lock:
        _clear_phoenix_state()


class PhoenixRunBoundary:
    """Explicit completion handle for a ``deerflow.run`` boundary span.

    ``activate_phoenix_root_context()`` yields this handle so the caller can
    affirm that the wrapped iteration ran to completion. Only an explicit
    :meth:`mark_complete` sets ``StatusCode.OK``; a clean ``with`` exit alone
    (abort ``break`` / cancel ``return``) leaves the span ``UNSET``, mirroring
    the ``PhoenixRootScope.close()`` three-state semantics.
    """

    def __init__(self, span: Any) -> None:
        self._span = span
        self._completed = False

    def mark_complete(self) -> None:
        if self._completed:
            return
        self._completed = True
        from opentelemetry.trace import Status, StatusCode

        self._span.set_status(Status(StatusCode.OK))

    def get_span_context(self) -> Any:
        return self._span.get_span_context()


@contextmanager
def bind_phoenix_graph_root_parent(
    run_id: UUID,
    boundary: PhoenixRunBoundary | None,
) -> Iterator[None]:
    """Bind one exact automatic graph root to its manual run boundary."""
    if boundary is None:
        yield
        return

    span_context = boundary.get_span_context()
    if not getattr(span_context, "is_valid", False):
        raise PhoenixTracingError("Phoenix graph-root parent binding requires a valid SpanContext.")

    with _graph_root_parent_override_lock:
        if run_id in _graph_root_parent_overrides:
            raise PhoenixTracingError(f"Phoenix graph-root parent is already registered for run {run_id}.")
        _graph_root_parent_overrides[run_id] = span_context

    try:
        yield
    finally:
        with _graph_root_parent_override_lock:
            _graph_root_parent_overrides.pop(run_id, None)


def _consume_graph_root_parent_override(run_id: UUID) -> Any | None:
    with _graph_root_parent_override_lock:
        return _graph_root_parent_overrides.pop(run_id, None)


@contextmanager
def activate_phoenix_root_context(root: PhoenixRootContext) -> Iterator[PhoenixRunBoundary | None]:
    """Activate Phoenix/OpenInference attributes around a root graph run."""
    phoenix_config = get_tracing_config().phoenix
    if not phoenix_config.enabled:
        yield
        return

    resolved_context, fallback_reason = _resolve_parent_context(phoenix_config, root.upstream_context)

    ensure_phoenix_tracing_initialized(phoenix_config)

    try:
        from openinference.instrumentation import using_attributes
        from openinference.semconv.trace import OpenInferenceSpanKindValues, SpanAttributes
        from opentelemetry import context as otel_context
    except Exception as exc:
        raise PhoenixTracingError(f"Phoenix root context activation failed: {exc}") from exc

    token = otel_context.attach(resolved_context)

    export_metadata = root.metadata if phoenix_config.capture_content else root.correlation_metadata
    export_tags = root.tags if phoenix_config.capture_content else root.correlation_tags

    try:
        with using_attributes(
            session_id=root.session_id,
            user_id=root.user_id,
            metadata=export_metadata,
            tags=list(export_tags),
        ):
            tracer = _get_phoenix_tracer(__name__)
            with tracer.start_as_current_span(_RUN_BOUNDARY_SPAN_NAME) as span:
                _set_root_span_attributes(
                    span=span,
                    span_attributes=SpanAttributes,
                    openinference_span_kind_values=OpenInferenceSpanKindValues,
                    root=root,
                    tags=export_tags,
                    parent_mode=phoenix_config.trace_parent_mode,
                    fallback_reason=fallback_reason,
                )
                yield PhoenixRunBoundary(span)
    finally:
        otel_context.detach(token)


def _get_phoenix_tracer(instrumentation_name: str) -> Any:
    if _phoenix_tracer_provider is None:
        raise PhoenixTracingError("Phoenix tracing provider is not initialized.")
    return _phoenix_tracer_provider.get_tracer(instrumentation_name)


class PhoenixRootScope:
    """Root span lifecycle decoupled from per-step context attachment.

    Owns the ``deerflow.run`` span across a sync generator's suspension
    points: the OTel context is attached only around each underlying
    iterator advancement, never across caller-visible ``yield`` points.
    """

    def __init__(self, root: PhoenixRootContext) -> None:
        self._root = root
        self._span: Any | None = None
        self._step_context: Context | None = None
        self._started = False
        self._closed = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True

        phoenix_config = get_tracing_config().phoenix
        if not phoenix_config.enabled:
            return

        resolved_context, fallback_reason = _resolve_parent_context(phoenix_config, self._root.upstream_context)
        ensure_phoenix_tracing_initialized(phoenix_config)

        try:
            from openinference.instrumentation import using_attributes
            from openinference.semconv.trace import OpenInferenceSpanKindValues, SpanAttributes
            from opentelemetry import context as otel_context
            from opentelemetry import trace as otel_trace
        except Exception as exc:
            raise PhoenixTracingError(f"Phoenix root scope start failed: {exc}") from exc

        export_metadata = self._root.metadata if phoenix_config.capture_content else self._root.correlation_metadata
        export_tags = self._root.tags if phoenix_config.capture_content else self._root.correlation_tags

        tracer = _get_phoenix_tracer(__name__)
        span = tracer.start_span(_RUN_BOUNDARY_SPAN_NAME, context=resolved_context)
        self._span = span
        _set_root_span_attributes(
            span=span,
            span_attributes=SpanAttributes,
            openinference_span_kind_values=OpenInferenceSpanKindValues,
            root=self._root,
            tags=export_tags,
            parent_mode=phoenix_config.trace_parent_mode,
            fallback_reason=fallback_reason,
        )

        base_context = otel_trace.set_span_in_context(span, resolved_context)
        token = otel_context.attach(base_context)
        try:
            with using_attributes(
                session_id=self._root.session_id,
                user_id=self._root.user_id,
                metadata=export_metadata,
                tags=list(export_tags),
            ):
                self._step_context = otel_context.get_current()
        finally:
            otel_context.detach(token)

    @contextmanager
    def activate(self) -> Iterator[None]:
        if self._step_context is None:
            yield
            return
        from opentelemetry import context as otel_context

        token = otel_context.attach(self._step_context)
        try:
            yield
        finally:
            otel_context.detach(token)

    def close(self, exc: BaseException | None = None) -> None:
        if self._closed:
            return
        self._closed = True
        span = self._span
        if span is None:
            return
        from opentelemetry.trace import Status, StatusCode

        if isinstance(exc, Exception):
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, f"{type(exc).__name__}: {exc}"))
        elif exc is None:
            span.set_status(Status(StatusCode.OK))
        span.end()


def open_phoenix_root_scope(root: PhoenixRootContext) -> PhoenixRootScope:
    return PhoenixRootScope(root)


def _clear_phoenix_state() -> None:
    global _active_config_key, _parent_compat_base_class, _parent_compat_class, _parent_compat_tracer
    global _phoenix_owned_instrumentors, _phoenix_trace_config, _phoenix_tracer_provider

    with _graph_root_parent_override_lock:
        _graph_root_parent_overrides.clear()
    if _parent_compat_tracer is not None and _parent_compat_base_class is not None and _parent_compat_tracer.__class__ is _parent_compat_class:
        _parent_compat_tracer.__class__ = _parent_compat_base_class
    _uninstrument_owned_instrumentors(_phoenix_owned_instrumentors)
    _parent_compat_tracer = None
    _parent_compat_base_class = None
    _parent_compat_class = None
    _phoenix_owned_instrumentors = []
    _phoenix_trace_config = None
    _phoenix_tracer_provider = None
    _active_config_key = None


def _shutdown_provider(
    tracer_provider: Any,
    *,
    timeout_millis: int = _DEFAULT_SHUTDOWN_TIMEOUT_MILLIS,
    force_flush: bool = True,
) -> None:
    _relinquish_provider_atexit_hook(tracer_provider)
    if force_flush:
        try:
            flushed = tracer_provider.force_flush(timeout_millis=timeout_millis)
            if flushed is False:
                logger.warning("Phoenix tracing provider flush did not complete before shutdown.")
        except Exception:
            logger.exception("Phoenix tracing provider flush failed during shutdown.")
    try:
        tracer_provider.shutdown()
    except Exception:
        logger.exception("Phoenix tracing provider shutdown failed.")


def _relinquish_provider_atexit_hook(tracer_provider: Any) -> None:
    """Detach the SDK's private exit hook before a blocking exporter operation.

    OpenTelemetry unregisters this hook only after its own shutdown completes.
    DeerFlow's bounded Gateway shutdown uses a daemon thread, so retaining the
    hook could otherwise make interpreter exit block on the same provider.
    """
    handler = getattr(tracer_provider, "_atexit_handler", None)
    if handler is None:
        return
    try:
        atexit.unregister(handler)
    except Exception:
        logger.exception("Phoenix tracing provider atexit hook could not be unregistered.")
    finally:
        tracer_provider._atexit_handler = None


def _snapshot_openinference_instrumentors() -> list[_InstrumentorSnapshot]:
    snapshots: list[_InstrumentorSnapshot] = []
    for entry_point in importlib.metadata.entry_points(group="openinference_instrumentor"):
        instrumentor = entry_point.load()()
        snapshots.append(
            _InstrumentorSnapshot(
                instrumentor=instrumentor,
                instance_state=dict(getattr(instrumentor, "__dict__", {})),
                was_instrumented=bool(getattr(instrumentor, "_is_instrumented_by_opentelemetry", False)),
            )
        )
    return snapshots


def _reject_foreign_instrumentors(snapshots: list[_InstrumentorSnapshot]) -> None:
    if any(snapshot.was_instrumented for snapshot in snapshots):
        raise PhoenixTracingError("Phoenix tracing found an OpenInference instrumentor owned by a foreign provider.")


def _rollback_instrumentors(snapshots: list[_InstrumentorSnapshot]) -> None:
    for snapshot in reversed(snapshots):
        try:
            snapshot.instrumentor._uninstrument()
        except Exception:
            logger.exception("Phoenix tracing could not roll back a partially instrumented entry point.")
        _restore_instrumentor_snapshot(snapshot)


def _uninstrument_owned_instrumentors(snapshots: list[_InstrumentorSnapshot]) -> None:
    for snapshot in reversed(snapshots):
        try:
            snapshot.instrumentor.uninstrument()
        except Exception:
            logger.exception("Phoenix tracing could not uninstrument an owned OpenInference entry point.")
        _restore_instrumentor_snapshot(snapshot)


def _restore_instrumentor_snapshot(snapshot: _InstrumentorSnapshot) -> None:
    instance_state = getattr(snapshot.instrumentor, "__dict__", None)
    if instance_state is not None:
        instance_state.clear()
        instance_state.update(snapshot.instance_state)


def _config_key(config: PhoenixTracingConfig) -> _PhoenixConfigKey:
    api_key = sha256(config.api_key.encode("utf-8")).hexdigest() if config.api_key else None
    return (
        config.collector_endpoint,
        config.project_name,
        config.auto_instrument,
        config.capture_content,
        api_key is not None,
        api_key,
    )


def _resolve_parent_context(
    config: PhoenixTracingConfig,
    upstream_context: TraceContextCarrier | None,
) -> tuple[Context, str | None]:
    carrier = upstream_context.as_headers(include_baggage=config.propagate_baggage) if upstream_context is not None else {}
    root_context = Context()
    if config.propagate_baggage:
        root_context = W3CBaggagePropagator().extract(carrier, context=root_context)
    parent_context = TraceContextTextMapPropagator().extract(carrier, context=root_context)
    has_traceparent = bool(upstream_context and upstream_context.traceparent)
    has_valid_parent = has_traceparent and get_current_span(parent_context).get_span_context().is_valid

    if config.trace_parent_mode == "root":
        return root_context, None

    if has_valid_parent:
        return parent_context, None

    fallback_reason = "missing_parent" if not has_traceparent else "invalid_parent"

    if config.trace_parent_mode == "child" and config.trace_parent_required:
        reason = "missing" if fallback_reason == "missing_parent" else "invalid"
        raise PhoenixTracingError(f"Phoenix trace parent mode 'child' has {reason} upstream trace context.")

    return root_context, fallback_reason


def _set_root_span_attributes(
    *,
    span: Any,
    span_attributes: Any,
    openinference_span_kind_values: Any,
    root: PhoenixRootContext,
    tags: list[str],
    parent_mode: str,
    fallback_reason: str | None,
) -> None:
    _set_span_attribute(
        span,
        getattr(span_attributes, "OPENINFERENCE_SPAN_KIND", "openinference.span.kind"),
        openinference_span_kind_values.AGENT.value,
    )
    _set_span_attribute(span, getattr(span_attributes, "SESSION_ID", "session.id"), root.session_id)
    _set_span_attribute(span, getattr(span_attributes, "USER_ID", "user.id"), root.user_id)
    _set_span_attribute(span, getattr(span_attributes, "TAG_TAGS", "tag.tags"), list(tags))
    _set_span_attribute(span, "deerflow.span.role", "run_boundary")
    _set_span_attribute(span, "deerflow.agent_name", root.agent_name)
    _set_span_attribute(span, "deerflow.root_run_name", root.run_name)
    _set_span_attribute(span, "deerflow.trace_parent_mode", parent_mode)
    if fallback_reason:
        _set_span_attribute(span, "deerflow.trace_parent_fallback", fallback_reason)


def _set_span_attribute(span: Any, key: str, value: Any) -> None:
    if value is None:
        return
    span.set_attribute(key, value)


def _validate_openinference_langchain_parent_contract() -> None:
    """Fail fast when the private parent-bridge contract differs from the lock."""
    import importlib.metadata

    for distribution, expected_version in _PARENT_COMPAT_DEPENDENCY_VERSIONS.items():
        installed_version = importlib.metadata.version(distribution)
        if installed_version != expected_version:
            raise PhoenixTracingError(f"Phoenix parent compatibility requires {distribution}=={expected_version}; found {installed_version}.")

    try:
        from langsmith.run_trees import RunTree, _parse_dotted_order
        from openinference.instrumentation.langchain._tracer import _SUPPRESS_INSTRUMENTATION_KEY, OpenInferenceTracer, _as_utc_nano, audit_timing
    except Exception as exc:
        raise PhoenixTracingError(f"Phoenix parent compatibility contract import failed: {exc}") from exc

    required_slots = {"_tracer", "_spans_by_run", "_separate_trace_from_runtime_context"}
    if not required_slots.issubset(set(OpenInferenceTracer.__slots__)):
        missing = ", ".join(sorted(required_slots - set(OpenInferenceTracer.__slots__)))
        raise PhoenixTracingError(f"Phoenix parent compatibility cannot find OpenInference tracer state: {missing}")
    if not callable(_as_utc_nano) or not callable(audit_timing) or _SUPPRESS_INSTRUMENTATION_KEY is None:
        raise PhoenixTracingError("Phoenix parent compatibility cannot find OpenInference start-trace helpers.")

    try:
        contract_root = RunTree(name="deerflow-parent-contract-root", inputs={})
        contract_child = contract_root.create_child("deerflow-parent-contract-child")
        parsed_ids = [run_id for _, run_id in _parse_dotted_order(contract_child.dotted_order)]
    except Exception as exc:
        raise PhoenixTracingError(f"Phoenix parent compatibility parser contract failed: {exc}") from exc
    if parsed_ids != [contract_root.id, contract_child.id]:
        raise PhoenixTracingError("Phoenix parent compatibility parser contract returned unexpected ancestry order.")


def _install_openinference_langchain_parent_compat(tracer_provider: Any) -> None:
    """Bridge LangSmith-only parents on Phoenix's OpenInference tracer instance."""
    global _parent_compat_base_class, _parent_compat_class, _parent_compat_tracer

    _validate_openinference_langchain_parent_contract()

    try:
        from langsmith.run_helpers import get_current_run_tree
        from langsmith.run_trees import _parse_dotted_order
        from openinference.instrumentation.langchain import LangChainInstrumentor
        from openinference.instrumentation.langchain._tracer import _SUPPRESS_INSTRUMENTATION_KEY, OpenInferenceTracer, _as_utc_nano, audit_timing
        from opentelemetry import context as context_api
        from opentelemetry import trace as trace_api
    except Exception as exc:
        raise PhoenixTracingError(f"Phoenix parent compatibility installation failed: {exc}") from exc

    instrumentor = LangChainInstrumentor()
    tracer = getattr(instrumentor, "_tracer", None)
    if not isinstance(tracer, OpenInferenceTracer):
        raise PhoenixTracingError("Phoenix parent compatibility cannot find the auto-instrumented LangChain tracer.")

    provider_span_processor = getattr(tracer_provider, "_active_span_processor", None)
    tracer_span_processor = getattr(tracer._tracer, "span_processor", None)
    if provider_span_processor is None or tracer_span_processor is not provider_span_processor:
        raise PhoenixTracingError("Phoenix parent compatibility found a LangChain tracer owned by another provider.")

    if tracer is _parent_compat_tracer and tracer.__class__ is _parent_compat_class:
        return
    if _parent_compat_tracer is not None or tracer.__class__ is not OpenInferenceTracer:
        raise PhoenixTracingError("Phoenix parent compatibility found an unexpected tracer instance state.")

    class PhoenixParentCompatOpenInferenceTracer(OpenInferenceTracer):
        __slots__ = ()

        @audit_timing
        def _start_trace(self, run: Any) -> None:
            self.run_map[str(run.id)] = run
            if context_api.get_value(_SUPPRESS_INSTRUMENTATION_KEY):
                return

            parent_context = None
            override_span_context = _consume_graph_root_parent_override(run.id)
            if override_span_context is not None:
                parent_context = trace_api.set_span_in_context(trace_api.NonRecordingSpan(override_span_context))
            else:
                direct_parent_id = run.parent_run_id
                if direct_parent_id is not None:
                    parent_span = self._spans_by_run.get(direct_parent_id)
                    if parent_span is None:
                        try:
                            current_run_tree = get_current_run_tree()
                            ancestry = [run_id for _, run_id in _parse_dotted_order(current_run_tree.dotted_order)] if current_run_tree is not None else []
                        except Exception as exc:
                            raise PhoenixTracingError(f"Phoenix parent ancestry resolution failed: {exc}") from exc

                        if direct_parent_id in ancestry:
                            direct_parent_index = ancestry.index(direct_parent_id)
                            for ancestor_id in reversed(ancestry[:direct_parent_index]):
                                parent_span = self._spans_by_run.get(ancestor_id)
                                if parent_span is not None:
                                    break

                    if parent_span is not None:
                        parent_span_context = parent_span.get_span_context()
                        parent_context = trace_api.set_span_in_context(trace_api.NonRecordingSpan(parent_span_context))
                    else:
                        parent_context = context_api.Context()
                elif self._separate_trace_from_runtime_context:
                    parent_context = context_api.Context()

            start_time_utc_nano = _as_utc_nano(run.start_time)
            span = self._tracer.start_span(
                name=run.name,
                context=parent_context,
                start_time=start_time_utc_nano,
            )
            with self._lock:
                self._spans_by_run[run.id] = span

    _parent_compat_tracer = tracer
    _parent_compat_base_class = OpenInferenceTracer
    _parent_compat_class = PhoenixParentCompatOpenInferenceTracer
    tracer.__class__ = PhoenixParentCompatOpenInferenceTracer



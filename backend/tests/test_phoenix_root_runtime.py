"""Tests for Phoenix tracing runtime initialization."""

from __future__ import annotations

import builtins
import contextlib
import inspect
import os
import sys
import types
from collections.abc import Callable
from typing import Any

import pytest

from deerflow.config.tracing_config import PhoenixTracingConfig


def _phoenix_config(
    *,
    enabled: bool = True,
    endpoint: str = "http://localhost:6006",
    api_key: str | None = None,
    project_name: str = "deer-flow-test",
    auto_instrument: bool = True,
    capture_content: bool = False,
    trace_parent_mode: str = "auto",
    trace_parent_required: bool = False,
    propagate_baggage: bool = False,
) -> PhoenixTracingConfig:
    return PhoenixTracingConfig(
        enabled=enabled,
        collector_endpoint=endpoint,
        api_key=api_key,
        project_name=project_name,
        auto_instrument=auto_instrument,
        capture_content=capture_content,
        trace_parent_mode=trace_parent_mode,
        trace_parent_required=trace_parent_required,
        propagate_baggage=propagate_baggage,
    )


_INITIALIZER_MODULES = (
    "phoenix",
    "phoenix.otel",
    "openinference",
    "openinference.instrumentation",
    "openinference.instrumentation.config",
    "openinference.instrumentation.langchain",
    "openinference.instrumentation.langchain._tracer",
)
_INITIALIZER_ENV = (
    "OPENINFERENCE_HIDE_INPUTS",
    "OPENINFERENCE_HIDE_OUTPUTS",
    "OPENINFERENCE_HIDE_INPUT_MESSAGES",
    "OPENINFERENCE_HIDE_OUTPUT_MESSAGES",
    "OPENINFERENCE_HIDE_PROMPTS",
    "OPENINFERENCE_HIDE_CHOICES",
    "OPENINFERENCE_HIDE_INPUT_TEXT",
    "OPENINFERENCE_HIDE_OUTPUT_TEXT",
    "OPENINFERENCE_HIDE_LLM_INVOCATION_PARAMETERS",
    "OPENINFERENCE_HIDE_LLM_TOOLS",
)
_MISSING = object()


class _InitializerStateSnapshot:
    def __init__(self) -> None:
        self.modules = {name: sys.modules.get(name, _MISSING) for name in _INITIALIZER_MODULES}
        self.environment = {name: os.environ.get(name, _MISSING) for name in _INITIALIZER_ENV}
        instrumentor = self._new_instrumentor()
        self.instrumentor_tracer = getattr(instrumentor, "_tracer", _MISSING)
        self.instrumented = instrumentor._is_instrumented_by_opentelemetry

    @staticmethod
    def _new_instrumentor():
        from openinference.instrumentation.langchain import LangChainInstrumentor

        return LangChainInstrumentor()


def _restore_initializer_state(snapshot: _InitializerStateSnapshot):
    from deerflow.tracing import phoenix

    phoenix.reset_phoenix_tracing_for_tests()
    instrumentor = _InitializerStateSnapshot._new_instrumentor()
    if instrumentor._is_instrumented_by_opentelemetry and not snapshot.instrumented:
        instrumentor.uninstrument()
    if snapshot.instrumentor_tracer is _MISSING:
        instrumentor.__dict__.pop("_tracer", None)
    else:
        instrumentor._tracer = snapshot.instrumentor_tracer
    instrumentor._is_instrumented_by_opentelemetry = snapshot.instrumented
    for name, value in snapshot.modules.items():
        if value is _MISSING:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = value
    for name, value in snapshot.environment.items():
        if value is _MISSING:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    return instrumentor


def assert_initializer_test_state_restored(snapshot: _InitializerStateSnapshot, instrumentor) -> None:
    current_tracer = getattr(instrumentor, "_tracer", _MISSING)
    if snapshot.instrumentor_tracer is _MISSING:
        assert current_tracer is _MISSING, "LangChainInstrumentor._tracer leaked across initializer test"
    else:
        assert current_tracer is snapshot.instrumentor_tracer, "LangChainInstrumentor._tracer identity changed"
    assert instrumentor._is_instrumented_by_opentelemetry == snapshot.instrumented, "_is_instrumented_by_opentelemetry not restored"
    for name, value in snapshot.modules.items():
        if value is _MISSING:
            assert name not in sys.modules, f"sys.modules[{name!r}] leaked"
        else:
            assert sys.modules.get(name) is value, f"sys.modules[{name!r}] identity changed"
    for name, value in snapshot.environment.items():
        if value is _MISSING:
            assert name not in os.environ, f"env {name} leaked"
        else:
            assert os.environ.get(name) == value, f"env {name} not restored"


@contextlib.contextmanager
def _initializer_isolation():
    snapshot = _InitializerStateSnapshot()
    try:
        yield snapshot
    finally:
        instrumentor = _restore_initializer_state(snapshot)
        assert_initializer_test_state_restored(snapshot, instrumentor)


def _reset_phoenix_if_available() -> None:
    module = sys.modules.get("deerflow.tracing.phoenix")
    reset = getattr(module, "reset_phoenix_tracing_for_tests", None)
    if reset is not None:
        reset()


@pytest.fixture(autouse=True)
def reset_phoenix_runtime(monkeypatch):
    for name in (
        "OPENINFERENCE_HIDE_INPUTS",
        "OPENINFERENCE_HIDE_OUTPUTS",
        "OPENINFERENCE_HIDE_INPUT_MESSAGES",
        "OPENINFERENCE_HIDE_OUTPUT_MESSAGES",
        "OPENINFERENCE_HIDE_PROMPTS",
        "OPENINFERENCE_HIDE_CHOICES",
        "OPENINFERENCE_HIDE_INPUT_TEXT",
        "OPENINFERENCE_HIDE_OUTPUT_TEXT",
        "OPENINFERENCE_HIDE_LLM_INVOCATION_PARAMETERS",
        "OPENINFERENCE_HIDE_LLM_TOOLS",
    ):
        monkeypatch.delenv(name, raising=False)
    _reset_phoenix_if_available()
    yield
    _reset_phoenix_if_available()


def _install_fake_phoenix(
    monkeypatch: pytest.MonkeyPatch,
    register: Callable[..., object],
    *,
    stub_parent_compat: bool = True,
) -> None:
    from deerflow.tracing import phoenix as phoenix_runtime

    phoenix_module = types.ModuleType("phoenix")
    phoenix_module.__path__ = []
    otel_module = types.ModuleType("phoenix.otel")
    otel_module.register = register
    phoenix_module.otel = otel_module
    monkeypatch.setitem(sys.modules, "phoenix", phoenix_module)
    monkeypatch.setitem(sys.modules, "phoenix.otel", otel_module)
    if stub_parent_compat:
        monkeypatch.setattr(phoenix_runtime, "_install_openinference_langchain_parent_compat", lambda *_args: None)
        monkeypatch.setattr(phoenix_runtime, "_validate_openinference_langchain_parent_contract", lambda: None)


def test_phoenix_initializer_is_idempotent_for_same_config(monkeypatch):
    from deerflow.tracing.phoenix import ensure_phoenix_tracing_initialized

    with _initializer_isolation():
        calls: list[dict[str, Any]] = []
        _install_fake_phoenix(monkeypatch, lambda **kwargs: calls.append(kwargs))
        cfg = _phoenix_config(api_key="phoenix-key", capture_content=False)

        ensure_phoenix_tracing_initialized(cfg)
        ensure_phoenix_tracing_initialized(cfg)

        assert calls == [
            {
                "project_name": "deer-flow-test",
                "endpoint": "http://localhost:6006/v1/traces",
                "auto_instrument": False,
                "set_global_tracer_provider": False,
                "batch": True,
                "shutdown_on_exit": False,
                "api_key": "phoenix-key",
            }
        ]
        assert sys.modules["phoenix.otel"].register is not None
        assert "OPENINFERENCE_HIDE_INPUTS" not in os.environ
        assert "OPENINFERENCE_HIDE_OUTPUTS" not in os.environ
        assert calls[0]["api_key"] == "phoenix-key"


def test_phoenix_initializer_rejects_changed_active_config(monkeypatch):
    from deerflow.tracing.phoenix import PhoenixTracingError, ensure_phoenix_tracing_initialized

    with _initializer_isolation():
        calls: list[dict[str, Any]] = []
        _install_fake_phoenix(monkeypatch, lambda **kwargs: calls.append(kwargs))

        ensure_phoenix_tracing_initialized(_phoenix_config(project_name="first"))

        with pytest.raises(PhoenixTracingError, match="already initialized"):
            ensure_phoenix_tracing_initialized(_phoenix_config(project_name="second"))

        assert len(calls) == 1


def test_phoenix_disabled_does_not_import_phoenix_packages(monkeypatch):
    from deerflow.tracing.phoenix import ensure_phoenix_tracing_initialized

    for module_name in (
        "phoenix",
        "phoenix.otel",
        "openinference",
        "openinference.instrumentation",
        "openinference.instrumentation.config",
    ):
        monkeypatch.delitem(sys.modules, module_name, raising=False)

    real_import = builtins.__import__

    def guarded_import(name: str, *args, **kwargs):
        if name == "phoenix" or name.startswith("phoenix."):
            raise AssertionError(f"unexpected Phoenix import: {name}")
        if name == "openinference" or name.startswith("openinference."):
            raise AssertionError(f"unexpected OpenInference import: {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    ensure_phoenix_tracing_initialized(_phoenix_config(enabled=False))

    assert "phoenix" not in sys.modules
    assert "phoenix.otel" not in sys.modules
    assert "openinference" not in sys.modules
    assert "openinference.instrumentation.config" not in sys.modules


def test_phoenix_initialization_error_is_provider_specific(monkeypatch):
    from deerflow.tracing.phoenix import PhoenixTracingError, ensure_phoenix_tracing_initialized

    def register(**_kwargs):
        raise ValueError("collector down")

    with _initializer_isolation():
        _install_fake_phoenix(monkeypatch, register)

        with pytest.raises(PhoenixTracingError, match="Phoenix tracing initialization failed: collector down"):
            ensure_phoenix_tracing_initialized(_phoenix_config())


def test_failed_registration_leaves_no_parent_compat_residual_and_can_retry(monkeypatch):
    from openinference.instrumentation.langchain import LangChainInstrumentor
    from openinference.instrumentation.langchain._tracer import OpenInferenceTracer
    from opentelemetry.sdk.trace import TracerProvider

    from deerflow.tracing import phoenix

    original_start_trace = inspect.unwrap(OpenInferenceTracer._start_trace)
    instrumentor = LangChainInstrumentor()
    provider = TracerProvider()
    phoenix_tracer = OpenInferenceTracer(
        provider.get_tracer("deerflow.tests.phoenix-owned"),
        separate_trace_from_runtime_context=False,
    )
    attempts = 0

    def register(**_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ValueError("collector down")
        instrumentor._tracer = phoenix_tracer
        return provider

    _install_fake_phoenix(monkeypatch, register, stub_parent_compat=False)

    with _initializer_isolation():
        with pytest.raises(phoenix.PhoenixTracingError, match="collector down"):
            phoenix.ensure_phoenix_tracing_initialized(_phoenix_config(capture_content=True))

        assert inspect.unwrap(OpenInferenceTracer._start_trace) is original_start_trace

        phoenix.ensure_phoenix_tracing_initialized(_phoenix_config(capture_content=True))
        assert attempts == 2
        provider.shutdown()


def test_parent_compat_only_wraps_phoenix_owned_tracer_instance(monkeypatch):
    from openinference.instrumentation.langchain import LangChainInstrumentor
    from openinference.instrumentation.langchain._tracer import OpenInferenceTracer
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider

    from deerflow.tracing import phoenix

    original_start_trace = inspect.unwrap(OpenInferenceTracer._start_trace)
    host_provider = TracerProvider()
    phoenix_provider = TracerProvider()
    monkeypatch.setattr(trace, "_TRACER_PROVIDER", host_provider)

    def register(**_kwargs):
        return phoenix_provider

    _install_fake_phoenix(monkeypatch, register, stub_parent_compat=False)
    monkeypatch.setattr(phoenix, "_validate_openinference_langchain_parent_contract", lambda: None)

    unrelated_before = OpenInferenceTracer(
        host_provider.get_tracer("deerflow.tests.unrelated-before"),
        separate_trace_from_runtime_context=False,
    )
    original_instance_method = unrelated_before._start_trace.__func__

    instrumentor = LangChainInstrumentor()
    instrumentor.uninstrument()
    try:
        with _initializer_isolation():
            phoenix.ensure_phoenix_tracing_initialized(_phoenix_config(capture_content=True))
            owned_tracer = instrumentor._tracer
            installed_method = owned_tracer._start_trace.__func__
            phoenix.ensure_phoenix_tracing_initialized(_phoenix_config(capture_content=True))
            unrelated_after = OpenInferenceTracer(
                host_provider.get_tracer("deerflow.tests.unrelated-after"),
                separate_trace_from_runtime_context=False,
            )

            assert installed_method is not original_instance_method
            assert owned_tracer._start_trace.__func__ is installed_method
            assert unrelated_before._start_trace.__func__ is original_instance_method
            assert unrelated_after._start_trace.__func__ is original_instance_method
            assert inspect.unwrap(OpenInferenceTracer._start_trace) is original_start_trace
    finally:
        instrumentor.uninstrument()
        host_provider.shutdown()
        phoenix_provider.shutdown()


def test_content_capture_disabled_does_not_mutate_environment(monkeypatch):
    from deerflow.tracing.phoenix import ensure_phoenix_tracing_initialized

    with _initializer_isolation():
        _install_fake_phoenix(monkeypatch, lambda **_kwargs: object())

        before = {name: os.environ.get(name) for name in OPENINFERENCE_HIDE_NAMES}
        ensure_phoenix_tracing_initialized(_phoenix_config(auto_instrument=False, capture_content=False))

        assert {name: os.environ.get(name) for name in OPENINFERENCE_HIDE_NAMES} == before


OPENINFERENCE_HIDE_NAMES = (
    "OPENINFERENCE_HIDE_INPUTS",
    "OPENINFERENCE_HIDE_OUTPUTS",
    "OPENINFERENCE_HIDE_INPUT_MESSAGES",
    "OPENINFERENCE_HIDE_OUTPUT_MESSAGES",
    "OPENINFERENCE_HIDE_PROMPTS",
    "OPENINFERENCE_HIDE_CHOICES",
    "OPENINFERENCE_HIDE_INPUT_TEXT",
    "OPENINFERENCE_HIDE_OUTPUT_TEXT",
    "OPENINFERENCE_HIDE_LLM_INVOCATION_PARAMETERS",
    "OPENINFERENCE_HIDE_LLM_TOOLS",
)


TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


class _FakeSpan:
    def __init__(self, name: str) -> None:
        self.name = name
        self.attributes: dict[str, Any] = {}
        self.ended = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.ended = True
        return False

    def set_attribute(self, key: str, value: Any) -> None:
        if isinstance(value, dict):
            raise AssertionError(f"Python dict cannot be written directly as OTel attribute {key!r}")
        self.attributes[key] = value


class _FakeTracer:
    def __init__(self, spans: list[_FakeSpan]) -> None:
        self._spans = spans

    def start_as_current_span(self, name: str):
        span = _FakeSpan(name)
        self._spans.append(span)
        return span


def _install_fake_openinference_runtime(monkeypatch: pytest.MonkeyPatch, attributes: list[dict[str, Any]]) -> None:
    from contextlib import contextmanager

    openinference_module = types.ModuleType("openinference")
    instrumentation_module = types.ModuleType("openinference.instrumentation")
    semconv_module = types.ModuleType("openinference.semconv")
    trace_module = types.ModuleType("openinference.semconv.trace")

    @contextmanager
    def using_attributes(**kwargs):
        attributes.append(kwargs)
        yield

    class SpanAttributes:
        OPENINFERENCE_SPAN_KIND = "openinference.span.kind"

    class _AgentValue:
        value = "agent"

    class OpenInferenceSpanKindValues:
        AGENT = _AgentValue()

    instrumentation_module.using_attributes = using_attributes
    trace_module.SpanAttributes = SpanAttributes
    trace_module.OpenInferenceSpanKindValues = OpenInferenceSpanKindValues
    openinference_module.instrumentation = instrumentation_module
    openinference_module.semconv = semconv_module
    semconv_module.trace = trace_module
    monkeypatch.setitem(sys.modules, "openinference", openinference_module)
    monkeypatch.setitem(sys.modules, "openinference.instrumentation", instrumentation_module)
    monkeypatch.setitem(sys.modules, "openinference.semconv", semconv_module)
    monkeypatch.setitem(sys.modules, "openinference.semconv.trace", trace_module)


def _patch_root_context_runtime(
    monkeypatch: pytest.MonkeyPatch,
    *,
    trace_parent_mode: str,
    trace_parent_required: bool = False,
    propagate_baggage: bool = False,
    capture_content: bool = False,
):
    from opentelemetry import context as otel_context
    from opentelemetry import propagate

    from deerflow.tracing import phoenix

    spans: list[_FakeSpan] = []
    using_attributes_calls: list[dict[str, Any]] = []
    extract_calls: list[dict[str, str]] = []
    attach_calls: list[Any] = []
    detach_calls: list[Any] = []
    initialized: list[PhoenixTracingConfig] = []
    cfg = _phoenix_config(
        trace_parent_mode=trace_parent_mode,
        trace_parent_required=trace_parent_required,
        propagate_baggage=propagate_baggage,
        capture_content=capture_content,
    )

    _install_fake_openinference_runtime(monkeypatch, using_attributes_calls)
    monkeypatch.setattr(phoenix, "get_tracing_config", lambda: types.SimpleNamespace(phoenix=cfg))
    monkeypatch.setattr(phoenix, "ensure_phoenix_tracing_initialized", lambda config=None: initialized.append(config or cfg))
    monkeypatch.setattr(phoenix, "_get_phoenix_tracer", lambda *_args, **_kwargs: _FakeTracer(spans))

    def extract(carrier, context=None, getter=None):
        extract_calls.append(dict(carrier))
        return f"context:{carrier.get('traceparent')}"

    def attach(context):
        attach_calls.append(context)
        return f"token:{context}"

    def detach(token):
        detach_calls.append(token)

    monkeypatch.setattr(propagate, "extract", extract)
    monkeypatch.setattr(otel_context, "attach", attach)
    monkeypatch.setattr(otel_context, "detach", detach)

    return {
        "spans": spans,
        "using_attributes_calls": using_attributes_calls,
        "extract_calls": extract_calls,
        "attach_calls": attach_calls,
        "detach_calls": detach_calls,
        "initialized": initialized,
    }


def _root_context(upstream_context=None):
    from deerflow.tracing.phoenix import PhoenixRootContext

    return PhoenixRootContext(
        run_name="deerflow:lead-agent",
        session_id="thread-abc",
        user_id="user-42",
        metadata={"thread_id": "thread-abc", "root_run_name": "deerflow:lead-agent"},
        tags=["gateway"],
        agent_name="lead-agent",
        correlation_metadata={"thread_id": "thread-abc", "root_run_name": "deerflow:lead-agent"},
        correlation_tags=["gateway"],
        upstream_context=upstream_context,
    )


def test_phoenix_root_context_preserves_existing_positional_optional_fields():
    from deerflow.tracing.otel_context import TraceContextCarrier
    from deerflow.tracing.phoenix import PhoenixRootContext

    correlation_metadata = {"root_run_name": "lead_agent"}
    correlation_tags = ["gateway"]
    upstream_context = TraceContextCarrier(traceparent=TRACEPARENT)

    root = PhoenixRootContext(
        "lead_agent",
        "thread-abc",
        "user-42",
        {"root_run_name": "lead_agent"},
        ["caller-tag"],
        correlation_metadata,
        correlation_tags,
        upstream_context,
    )

    assert root.correlation_metadata == correlation_metadata
    assert root.correlation_tags == correlation_tags
    assert root.upstream_context == upstream_context
    assert root.agent_name == "unknown"


def test_manual_run_boundary_uses_stable_name_and_queryable_attributes(monkeypatch):
    from opentelemetry import trace

    from deerflow.tracing.otel_context import TraceContextCarrier
    from deerflow.tracing.phoenix import activate_phoenix_root_context

    runtime = _patch_root_context_runtime(monkeypatch, trace_parent_mode="root")
    upstream = TraceContextCarrier(traceparent=TRACEPARENT, tracestate="rojo=00f067aa0ba902b7", baggage="tenant=acme")

    root = _root_context(upstream)
    with activate_phoenix_root_context(root):
        pass

    span = runtime["spans"][0]
    assert span.name == "deerflow.run"
    assert span.attributes["deerflow.span.role"] == "run_boundary"
    assert span.attributes["deerflow.agent_name"] == "lead-agent"
    assert span.attributes["deerflow.root_run_name"] == "deerflow:lead-agent"
    assert span.attributes["deerflow.trace_parent_mode"] == "root"
    assert span.attributes["openinference.span.kind"] == "agent"
    assert runtime["extract_calls"] == []
    assert len(runtime["attach_calls"]) == 1
    assert not trace.get_current_span(runtime["attach_calls"][0]).get_span_context().is_valid
    assert len(runtime["detach_calls"]) == 1
    assert runtime["using_attributes_calls"][0]["session_id"] == "thread-abc"
    assert root.metadata["root_run_name"] == "deerflow:lead-agent"
    assert runtime["using_attributes_calls"][0]["metadata"]["root_run_name"] == "deerflow:lead-agent"


def test_content_capture_enabled_keeps_full_root_metadata_and_tags(monkeypatch):
    from dataclasses import replace

    from deerflow.tracing.phoenix import activate_phoenix_root_context

    runtime = _patch_root_context_runtime(monkeypatch, trace_parent_mode="root", capture_content=True)
    root = replace(
        _root_context(),
        metadata={"prompt": "explicitly captured", "custom": {"value": 1}},
        tags=["caller-tag"],
        correlation_metadata={"thread_id": "thread-abc"},
        correlation_tags=["safe-tag"],
    )

    with activate_phoenix_root_context(root):
        pass

    assert runtime["using_attributes_calls"] == [
        {
            "session_id": "thread-abc",
            "user_id": "user-42",
            "metadata": {"prompt": "explicitly captured", "custom": {"value": 1}},
            "tags": ["caller-tag"],
        }
    ]
    assert "metadata" not in runtime["spans"][0].attributes
    assert runtime["spans"][0].attributes["tag.tags"] == ["caller-tag"]


def test_parent_mode_auto_uses_valid_parent(monkeypatch):
    from opentelemetry import trace

    from deerflow.tracing.otel_context import TraceContextCarrier
    from deerflow.tracing.phoenix import activate_phoenix_root_context

    runtime = _patch_root_context_runtime(monkeypatch, trace_parent_mode="auto", propagate_baggage=True)
    upstream = TraceContextCarrier(traceparent=TRACEPARENT, tracestate="rojo=00f067aa0ba902b7", baggage="tenant=acme")

    with activate_phoenix_root_context(_root_context(upstream)):
        pass

    span = runtime["spans"][0]
    assert runtime["extract_calls"] == []
    assert len(runtime["attach_calls"]) == 1
    attached_span_context = trace.get_current_span(runtime["attach_calls"][0]).get_span_context()
    assert attached_span_context.is_valid
    assert attached_span_context.trace_id == int("4bf92f3577b34da6a3ce929d0e0e4736", 16)
    assert attached_span_context.span_id == int("00f067aa0ba902b7", 16)
    assert len(runtime["detach_calls"]) == 1
    assert span.attributes["deerflow.trace_parent_mode"] == "auto"
    assert "deerflow.trace_parent_fallback" not in span.attributes


def test_parent_mode_auto_restores_baggage_when_enabled(monkeypatch):
    from opentelemetry import baggage
    from opentelemetry import context as otel_context

    from deerflow.tracing import phoenix
    from deerflow.tracing.otel_context import TraceContextCarrier
    from deerflow.tracing.phoenix import activate_phoenix_root_context

    spans: list[_FakeSpan] = []
    using_attributes_calls: list[dict[str, Any]] = []
    initialized: list[PhoenixTracingConfig] = []
    attached_contexts: list[Any] = []
    cfg = _phoenix_config(trace_parent_mode="auto", propagate_baggage=True)

    _install_fake_openinference_runtime(monkeypatch, using_attributes_calls)
    monkeypatch.setattr(phoenix, "get_tracing_config", lambda: types.SimpleNamespace(phoenix=cfg))
    monkeypatch.setattr(phoenix, "ensure_phoenix_tracing_initialized", lambda config=None: initialized.append(config or cfg))
    monkeypatch.setattr(phoenix, "_get_phoenix_tracer", lambda *_args, **_kwargs: _FakeTracer(spans))

    real_attach = otel_context.attach
    real_detach = otel_context.detach

    def attach(context):
        attached_contexts.append(context)
        return real_attach(context)

    def detach(token):
        real_detach(token)

    monkeypatch.setattr(otel_context, "attach", attach)
    monkeypatch.setattr(otel_context, "detach", detach)

    upstream = TraceContextCarrier(traceparent=TRACEPARENT, baggage="tenant=acme")

    with activate_phoenix_root_context(_root_context(upstream)):
        pass

    assert baggage.get_baggage("tenant", context=attached_contexts[0]) == "acme"


def test_parent_mode_auto_missing_parent_falls_back_root(monkeypatch):
    from opentelemetry import trace

    from deerflow.tracing.phoenix import activate_phoenix_root_context

    runtime = _patch_root_context_runtime(monkeypatch, trace_parent_mode="auto")

    with activate_phoenix_root_context(_root_context()):
        pass

    span = runtime["spans"][0]
    assert len(runtime["attach_calls"]) == 1
    assert not trace.get_current_span(runtime["attach_calls"][0]).get_span_context().is_valid
    assert len(runtime["detach_calls"]) == 1
    assert span.attributes["deerflow.trace_parent_mode"] == "auto"
    assert span.attributes["deerflow.trace_parent_fallback"] == "missing_parent"


def test_parent_mode_child_required_missing_parent_raises(monkeypatch):
    from deerflow.tracing.phoenix import PhoenixTracingError, activate_phoenix_root_context

    runtime = _patch_root_context_runtime(monkeypatch, trace_parent_mode="child", trace_parent_required=True)

    with pytest.raises(PhoenixTracingError, match="missing upstream trace context"):
        with activate_phoenix_root_context(_root_context()):
            pass

    assert runtime["spans"] == []
    assert runtime["attach_calls"] == []

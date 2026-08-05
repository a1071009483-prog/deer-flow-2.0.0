"""Real SDK coverage for Phoenix provider ownership and lifecycle."""

from __future__ import annotations

import importlib.metadata
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from deerflow.config.tracing_config import PhoenixTracingConfig
from deerflow.tracing.phoenix import DeerFlowTraceConfig, PhoenixRootContext
from deerflow.tracing.phoenix_boundary_io import PhoenixBoundaryIOProcessor


def _config(
    *,
    auto_instrument: bool = False,
    capture_content: bool = True,
    metadata_allowlist: tuple[str, ...] = (),
) -> PhoenixTracingConfig:
    return PhoenixTracingConfig(
        enabled=True,
        collector_endpoint="http://127.0.0.1:9",
        api_key=None,
        project_name="deer-flow-provider-lifecycle-test",
        auto_instrument=auto_instrument,
        capture_content=capture_content,
        metadata_allowlist=metadata_allowlist,
        trace_parent_mode="root",
        trace_parent_required=False,
        propagate_baggage=False,
    )


def _boundary_io_processors(provider: TracerProvider) -> list[PhoenixBoundaryIOProcessor]:
    return [processor for processor in provider._active_span_processor._span_processors if isinstance(processor, PhoenixBoundaryIOProcessor)]


class _RecordingProvider(TracerProvider):
    def __init__(self, *, flush_result: bool = True, flush_error: Exception | None = None) -> None:
        super().__init__()
        self.events: list[tuple[str, int | None]] = []
        self.tracer_names: list[str] = []
        self.flush_result = flush_result
        self.flush_error = flush_error

    def get_tracer(self, instrumentation_name: str, *args, **kwargs):
        self.tracer_names.append(instrumentation_name)
        return super().get_tracer(instrumentation_name, *args, **kwargs)

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        self.events.append(("force_flush", timeout_millis))
        if self.flush_error is not None:
            raise self.flush_error
        return self.flush_result

    def shutdown(self) -> None:
        self.events.append(("shutdown", None))


class _FakeInstrumentor:
    """Stand-in for ``LangChainInstrumentor`` in lifecycle tests."""

    def __init__(self, *, instrumented: bool = False, fails_during_instrument: bool = False) -> None:
        self._is_instrumented_by_opentelemetry = instrumented
        self.is_instrumented_by_opentelemetry = instrumented
        self.fails_during_instrument = fails_during_instrument
        self.providers: list[Any] = []
        self.configs: list[Any] = []
        self.events: list[str] = []
        self.uninstrument_calls = 0

    def instrument(self, *, tracer_provider: Any, config: Any) -> None:
        self.providers.append(tracer_provider)
        self.configs.append(config)
        if self.fails_during_instrument:
            raise RuntimeError("instrumentor mutation failed")
        self._is_instrumented_by_opentelemetry = True
        self.is_instrumented_by_opentelemetry = True
        self.events.append("instrument")

    def uninstrument(self) -> None:
        self.events.append("uninstrument")
        self.uninstrument_calls += 1
        self._is_instrumented_by_opentelemetry = False
        self.is_instrumented_by_opentelemetry = False


@pytest.fixture(autouse=True)
def _reset_phoenix_runtime():
    from deerflow.tracing import phoenix

    phoenix.shutdown_phoenix_tracing()
    phoenix.reset_phoenix_tracing_for_tests()
    yield
    phoenix.shutdown_phoenix_tracing()
    phoenix.reset_phoenix_tracing_for_tests()


@pytest.fixture
def _reject_entry_point_enumeration(monkeypatch):
    monkeypatch.setattr(
        importlib.metadata,
        "entry_points",
        Mock(side_effect=AssertionError("must not enumerate instrumentors")),
    )


def _install_openinference_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    openinference_module = type(sys)("openinference")
    instrumentation_module = type(sys)("openinference.instrumentation")
    semconv_module = type(sys)("openinference.semconv")
    trace_module = type(sys)("openinference.semconv.trace")

    @staticmethod
    def using_attributes(**_kwargs):
        from contextlib import contextmanager

        @contextmanager
        def _cm():
            yield

        return _cm()

    class SpanAttributes:
        OPENINFERENCE_SPAN_KIND = "openinference.span.kind"
        METADATA = "metadata"

    class _Agent:
        value = "agent"

    class OpenInferenceSpanKindValues:
        AGENT = _Agent()

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


def _root_context():
    from deerflow.tracing.phoenix import PhoenixRootContext

    return PhoenixRootContext(
        run_name="deerflow:provider-lifecycle",
        session_id="thread-provider-lifecycle",
        user_id=None,
        metadata={},
        tags=[],
    )


def test_registers_an_isolated_batch_provider_with_shutdown_on_exit_disabled(
    monkeypatch,
    _reject_entry_point_enumeration,
):
    from phoenix import otel as phoenix_otel

    from deerflow.tracing import phoenix

    host_provider = TracerProvider()
    monkeypatch.setattr(trace, "_TRACER_PROVIDER", host_provider)
    monkeypatch.setenv("OTEL_BSP_MAX_QUEUE_SIZE", "64")
    monkeypatch.setenv("OTEL_BSP_SCHEDULE_DELAY", "31")
    monkeypatch.setenv("OTEL_BSP_EXPORT_TIMEOUT", "47")
    monkeypatch.setenv("OTEL_BSP_MAX_EXPORT_BATCH_SIZE", "16")

    calls: list[dict[str, Any]] = []
    real_register = phoenix_otel.register

    def observing_register(**kwargs: Any):
        calls.append(kwargs)
        return real_register(verbose=False, **kwargs)

    monkeypatch.setattr(phoenix_otel, "register", observing_register)

    phoenix.ensure_phoenix_tracing_initialized(_config())

    provider = phoenix._phoenix_tracer_provider
    assert provider is not None
    assert trace.get_tracer_provider() is host_provider
    assert calls == [
        {
            "project_name": "deer-flow-provider-lifecycle-test",
            "endpoint": "http://127.0.0.1:9/v1/traces",
            "auto_instrument": False,
            "set_global_tracer_provider": False,
            "batch": True,
            "shutdown_on_exit": False,
        }
    ]

    processor = provider._active_span_processor._span_processors[0]._batch_processor
    assert processor._max_queue_size == 64
    assert processor._schedule_delay_millis == 31
    assert processor._export_timeout_millis == 47
    assert processor._max_export_batch_size == 16


def test_config_key_changes_when_metadata_allowlist_changes():
    from deerflow.tracing import phoenix

    without_allowlist = phoenix._config_key(_config(metadata_allowlist=()))
    with_allowlist = phoenix._config_key(_config(metadata_allowlist=("request_id",)))

    assert without_allowlist != with_allowlist


def test_manual_root_uses_the_saved_phoenix_provider(monkeypatch, _reject_entry_point_enumeration):
    from deerflow.tracing import phoenix
    from deerflow.tracing.phoenix import activate_phoenix_root_context

    provider = _RecordingProvider()
    host_provider = TracerProvider()
    monkeypatch.setattr(trace, "_TRACER_PROVIDER", host_provider)
    monkeypatch.setattr("phoenix.otel.register", lambda **_kwargs: provider)
    monkeypatch.setattr(phoenix, "get_tracing_config", lambda: SimpleNamespace(phoenix=_config()))
    _install_openinference_runtime(monkeypatch)

    phoenix.ensure_phoenix_tracing_initialized(_config())
    with activate_phoenix_root_context(_root_context()):
        pass

    assert provider.tracer_names == ["deerflow.tracing.phoenix"]
    assert trace.get_tracer_provider() is host_provider


def test_manual_root_uses_cleaned_caller_metadata_in_full_capture(monkeypatch, _reject_entry_point_enumeration):
    """Full capture must pass a cleaned copy of caller metadata, not the raw dict."""
    from deerflow.tracing import phoenix
    from deerflow.tracing.phoenix import activate_phoenix_root_context

    class _Uncopyable:
        def __deepcopy__(self, memo):
            raise RuntimeError("copy boom")

    provider = _RecordingProvider()
    host_provider = TracerProvider()
    monkeypatch.setattr(trace, "_TRACER_PROVIDER", host_provider)
    monkeypatch.setattr("phoenix.otel.register", lambda **_kwargs: provider)
    monkeypatch.setattr(
        phoenix,
        "get_tracing_config",
        lambda: SimpleNamespace(phoenix=_config(capture_content=True)),
    )
    _install_openinference_runtime(monkeypatch)

    captured: dict[str, Any] = {}

    import openinference.instrumentation

    real_using_attributes = openinference.instrumentation.using_attributes

    def _recording_using_attributes(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return real_using_attributes(**kwargs)

    monkeypatch.setattr(
        openinference.instrumentation,
        "using_attributes",
        _recording_using_attributes,
    )

    phoenix.ensure_phoenix_tracing_initialized(_config(capture_content=True))
    root = PhoenixRootContext(
        run_name="full-capture-correlation",
        session_id="thread",
        user_id="user",
        metadata={"raw": "caller", "extra": "should-not-be-used", "bad": _Uncopyable()},
        tags=["caller"],
        correlation_metadata={"safe": "correlation"},
        correlation_tags=["safe"],
    )
    with activate_phoenix_root_context(root):
        pass

    assert captured.get("metadata") == {"raw": "caller", "extra": "should-not-be-used"}
    assert captured.get("tags") == ["caller"]


def test_auto_instrumented_tracer_and_manual_root_share_the_phoenix_provider(
    monkeypatch,
    _reject_entry_point_enumeration,
):
    from openinference.instrumentation.langchain import LangChainInstrumentor

    from deerflow.tracing import phoenix
    from deerflow.tracing.phoenix import activate_phoenix_root_context

    instrumentor = LangChainInstrumentor()
    instrumentor.uninstrument()
    host_provider = TracerProvider()
    monkeypatch.setattr(trace, "_TRACER_PROVIDER", host_provider)
    monkeypatch.setattr(phoenix, "get_tracing_config", lambda: SimpleNamespace(phoenix=_config(auto_instrument=True)))

    try:
        phoenix.ensure_phoenix_tracing_initialized(_config(auto_instrument=True))
        provider = phoenix._phoenix_tracer_provider
        assert provider is not None

        openinference_tracer = instrumentor._tracer
        assert openinference_tracer._tracer.span_processor is provider._active_span_processor

        tracer_names: list[str] = []
        get_tracer = provider.get_tracer

        def recording_get_tracer(name: str, *args, **kwargs):
            tracer_names.append(name)
            return get_tracer(name, *args, **kwargs)

        monkeypatch.setattr(provider, "get_tracer", recording_get_tracer)
        _install_openinference_runtime(monkeypatch)
        with activate_phoenix_root_context(_root_context()):
            pass

        assert tracer_names == ["deerflow.tracing.phoenix"]
        assert trace.get_tracer_provider() is host_provider
    finally:
        instrumentor.uninstrument()


def test_auto_instrument_uses_langchain_instrumentor_with_deerflow_trace_config(
    monkeypatch,
    _reject_entry_point_enumeration,
):
    from deerflow.tracing import phoenix

    provider = _RecordingProvider()
    fake_instrumentor = _FakeInstrumentor()
    register_calls: list[dict[str, Any]] = []

    def register(**kwargs: Any) -> _RecordingProvider:
        register_calls.append(kwargs)
        return provider

    monkeypatch.setattr("phoenix.otel.register", register)
    monkeypatch.setattr(phoenix, "_validate_openinference_langchain_parent_contract", lambda: None)
    monkeypatch.setattr(phoenix, "_install_openinference_langchain_parent_compat", lambda _provider: None)
    monkeypatch.setattr(phoenix, "_get_langchain_instrumentor", lambda: lambda: fake_instrumentor)

    phoenix.ensure_phoenix_tracing_initialized(_config(auto_instrument=True, capture_content=False))

    assert register_calls[0]["auto_instrument"] is False
    assert register_calls[0]["set_global_tracer_provider"] is False
    assert register_calls[0]["batch"] is True
    assert register_calls[0]["shutdown_on_exit"] is False
    assert fake_instrumentor.providers == [provider]
    assert len(fake_instrumentor.configs) == 1
    passed_config = fake_instrumentor.configs[0]
    assert isinstance(passed_config, DeerFlowTraceConfig)
    assert passed_config._deerflow_capture_content is False
    assert passed_config._deerflow_metadata_allowlist == frozenset(_config().metadata_allowlist)
    assert phoenix._phoenix_owned_langchain_instrumentor is fake_instrumentor
    assert _boundary_io_processors(provider) == []

    phoenix.shutdown_phoenix_tracing()
    assert fake_instrumentor.events == ["instrument", "uninstrument"]
    assert provider.events == [("force_flush", 30_000), ("shutdown", None)]


def test_owned_auto_instrumentor_installs_boundary_io_processor_for_full_capture(
    monkeypatch,
    _reject_entry_point_enumeration,
):
    from deerflow.tracing import phoenix

    provider = _RecordingProvider()
    fake_instrumentor = _FakeInstrumentor()
    monkeypatch.setattr("phoenix.otel.register", lambda **_kwargs: provider)
    monkeypatch.setattr(phoenix, "_validate_openinference_langchain_parent_contract", lambda: None)
    monkeypatch.setattr(phoenix, "_install_openinference_langchain_parent_compat", lambda _provider: None)
    monkeypatch.setattr(phoenix, "_get_langchain_instrumentor", lambda: lambda: fake_instrumentor)

    phoenix.ensure_phoenix_tracing_initialized(_config(auto_instrument=True, capture_content=True))

    processors = _boundary_io_processors(provider)
    assert len(processors) == 1
    assert fake_instrumentor.providers == [provider]


def test_existing_host_langchain_instrumentor_is_left_unchanged(
    monkeypatch,
    _reject_entry_point_enumeration,
    caplog,
):
    from deerflow.tracing import phoenix
    from deerflow.tracing.phoenix import activate_phoenix_root_context

    provider = _RecordingProvider()
    fake_instrumentor = _FakeInstrumentor(instrumented=True)

    monkeypatch.setattr("phoenix.otel.register", lambda **_kwargs: provider)
    monkeypatch.setattr(phoenix, "_install_openinference_langchain_parent_compat", lambda _provider: None)
    monkeypatch.setattr(phoenix, "_get_langchain_instrumentor", lambda: lambda: fake_instrumentor)
    monkeypatch.setattr(phoenix, "get_tracing_config", lambda: SimpleNamespace(phoenix=_config(auto_instrument=True)))
    _install_openinference_runtime(monkeypatch)

    with caplog.at_level("WARNING", logger="deerflow.tracing.phoenix"):
        phoenix.ensure_phoenix_tracing_initialized(_config(auto_instrument=True))

    assert fake_instrumentor.providers == []
    assert fake_instrumentor.uninstrument_calls == 0
    assert phoenix._phoenix_owned_langchain_instrumentor is None
    assert "host-owned LangChain instrumentor unchanged" in caplog.text

    with activate_phoenix_root_context(_root_context()):
        pass
    assert provider.tracer_names == ["deerflow.tracing.phoenix"]

    phoenix.shutdown_phoenix_tracing()
    assert fake_instrumentor.uninstrument_calls == 0
    assert provider.events == [("force_flush", 30_000), ("shutdown", None)]
    assert _boundary_io_processors(provider) == []


def test_manual_only_mode_does_not_install_boundary_io_processor(
    monkeypatch,
    _reject_entry_point_enumeration,
):
    from deerflow.tracing import phoenix

    provider = _RecordingProvider()
    monkeypatch.setattr("phoenix.otel.register", lambda **_kwargs: provider)

    phoenix.ensure_phoenix_tracing_initialized(_config(auto_instrument=False, capture_content=True))

    assert _boundary_io_processors(provider) == []


def test_instrument_failure_closes_provider_and_allows_retry(
    monkeypatch,
    _reject_entry_point_enumeration,
):
    from deerflow.tracing import phoenix

    first_provider = _RecordingProvider()
    second_provider = _RecordingProvider()
    fake_instrumentor = _FakeInstrumentor(fails_during_instrument=True)
    providers = iter((first_provider, second_provider))

    monkeypatch.setattr("phoenix.otel.register", lambda **_kwargs: next(providers))
    monkeypatch.setattr(phoenix, "_validate_openinference_langchain_parent_contract", lambda: None)
    monkeypatch.setattr(phoenix, "_install_openinference_langchain_parent_compat", lambda _provider: None)
    monkeypatch.setattr(phoenix, "_get_langchain_instrumentor", lambda: lambda: fake_instrumentor)

    with pytest.raises(phoenix.PhoenixTracingError, match="instrumentor mutation failed"):
        phoenix.ensure_phoenix_tracing_initialized(_config(auto_instrument=True))

    assert first_provider.events == [("shutdown", None)]
    assert fake_instrumentor.events == []
    assert fake_instrumentor.uninstrument_calls == 0
    assert phoenix._phoenix_tracer_provider is None
    assert phoenix._active_config_key is None

    fake_instrumentor.fails_during_instrument = False
    phoenix.ensure_phoenix_tracing_initialized(_config(auto_instrument=True))
    phoenix.shutdown_phoenix_tracing()

    assert second_provider.events == [("force_flush", 30_000), ("shutdown", None)]
    assert fake_instrumentor.events == ["instrument", "uninstrument"]


def test_post_registration_compatibility_failure_closes_provider_and_allows_retry(monkeypatch):
    from deerflow.tracing import phoenix

    first_provider = _RecordingProvider()
    second_provider = _RecordingProvider()
    providers = iter((first_provider, second_provider))
    attempts = 0

    def install_compat(_provider: TracerProvider) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("compatibility installation failed")

    fake_instrumentor = _FakeInstrumentor()
    monkeypatch.setattr("phoenix.otel.register", lambda **_kwargs: next(providers))
    monkeypatch.setattr(phoenix, "_validate_openinference_langchain_parent_contract", lambda: None)
    monkeypatch.setattr(phoenix, "_install_openinference_langchain_parent_compat", install_compat)
    monkeypatch.setattr(phoenix, "_get_langchain_instrumentor", lambda: lambda: fake_instrumentor)

    with pytest.raises(phoenix.PhoenixTracingError, match="compatibility installation failed"):
        phoenix.ensure_phoenix_tracing_initialized(_config(auto_instrument=True))

    assert first_provider.events == [("shutdown", None)]
    assert fake_instrumentor.events == ["instrument", "uninstrument"]
    assert phoenix._phoenix_tracer_provider is None
    assert phoenix._active_config_key is None
    assert phoenix._parent_compat_tracer is None

    phoenix.ensure_phoenix_tracing_initialized(_config(auto_instrument=True))

    assert attempts == 2
    assert phoenix._phoenix_tracer_provider is second_provider

    phoenix.shutdown_phoenix_tracing()


@pytest.mark.parametrize("flush_result,flush_error", [(False, None), (True, RuntimeError("flush failed"))])
def test_shutdown_flushes_before_shutdown_even_when_flush_fails(
    flush_result,
    flush_error,
    monkeypatch,
    _reject_entry_point_enumeration,
):
    from deerflow.tracing import phoenix

    provider = _RecordingProvider(flush_result=flush_result, flush_error=flush_error)
    monkeypatch.setattr("phoenix.otel.register", lambda **_kwargs: provider)
    phoenix.ensure_phoenix_tracing_initialized(_config())

    phoenix.shutdown_phoenix_tracing(timeout_millis=123)
    phoenix.shutdown_phoenix_tracing(timeout_millis=123)

    assert provider.events == [("force_flush", 123), ("shutdown", None)]
    assert phoenix._phoenix_tracer_provider is None
    assert phoenix._active_config_key is None
    assert phoenix._parent_compat_tracer is None


def test_shutdown_never_inspects_provider_atexit_handler(
    monkeypatch,
    _reject_entry_point_enumeration,
):
    from deerflow.tracing import phoenix

    class _RecordingProviderWithAtexit(_RecordingProvider):
        def __init__(self) -> None:
            object.__setattr__(self, "atexit_reads", [])
            object.__setattr__(self, "atexit_writes", [])
            super().__init__()
            self.atexit_reads.clear()
            self.atexit_writes.clear()

        def __getattribute__(self, name: str) -> Any:
            if name == "_atexit_handler":
                object.__getattribute__(self, "atexit_reads").append(name)
            return object.__getattribute__(self, name)

        def __setattr__(self, name: str, value: Any) -> None:
            if name == "_atexit_handler" and value is not None:
                object.__getattribute__(self, "atexit_writes").append(value)
            object.__setattr__(self, name, value)

    provider = _RecordingProviderWithAtexit()
    monkeypatch.setattr("phoenix.otel.register", lambda **_kwargs: provider)
    phoenix.ensure_phoenix_tracing_initialized(_config())

    phoenix.shutdown_phoenix_tracing(timeout_millis=123)

    assert provider.events == [("force_flush", 123), ("shutdown", None)]
    assert provider.atexit_reads == []
    assert provider.atexit_writes == []


def test_root_context_never_shuts_down_the_process_provider(
    monkeypatch,
    _reject_entry_point_enumeration,
):
    from deerflow.tracing import phoenix
    from deerflow.tracing.phoenix import activate_phoenix_root_context

    provider = _RecordingProvider()
    monkeypatch.setattr("phoenix.otel.register", lambda **_kwargs: provider)
    monkeypatch.setattr(phoenix, "get_tracing_config", lambda: SimpleNamespace(phoenix=_config()))
    _install_openinference_runtime(monkeypatch)
    phoenix.ensure_phoenix_tracing_initialized(_config())

    with activate_phoenix_root_context(_root_context()):
        pass
    with pytest.raises(ValueError, match="run failure"):
        with activate_phoenix_root_context(_root_context()):
            raise ValueError("run failure")

    assert provider.events == []

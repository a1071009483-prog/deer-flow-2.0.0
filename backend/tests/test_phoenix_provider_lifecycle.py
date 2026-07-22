"""Real SDK coverage for Phoenix provider ownership and lifecycle."""

from __future__ import annotations

import importlib.metadata
import os
import sys
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from deerflow.config.tracing_config import PhoenixTracingConfig


def _config(*, auto_instrument: bool = False, capture_content: bool = True) -> PhoenixTracingConfig:
    return PhoenixTracingConfig(
        enabled=True,
        collector_endpoint="http://127.0.0.1:9",
        api_key=None,
        project_name="deer-flow-provider-lifecycle-test",
        auto_instrument=auto_instrument,
        capture_content=capture_content,
        trace_parent_mode="root",
        trace_parent_required=False,
        propagate_baggage=False,
    )


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


class _AtexitRecordingProvider(_RecordingProvider):
    def __init__(self, *, block_flush: bool = False) -> None:
        super().__init__()
        self._atexit_handler = object()
        self.block_flush = block_flush
        self.flush_started = threading.Event()
        self.release_flush = threading.Event()
        self.shutdown_finished = threading.Event()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        assert self._atexit_handler is None
        self.events.append(("force_flush", timeout_millis))
        self.flush_started.set()
        if self.block_flush:
            self.release_flush.wait()
        return True

    def shutdown(self) -> None:
        self.events.append(("shutdown", None))
        self.shutdown_finished.set()


class _FakeInstrumentor:
    def __init__(self, *, instrumented: bool = False, fails_during_instrument: bool = False) -> None:
        self._is_instrumented_by_opentelemetry = instrumented
        self.fails_during_instrument = fails_during_instrument
        self.marker = "host" if instrumented else "clean"
        self.providers: list[Any] = []
        self.events: list[str] = []
        self.uninstrument_calls = 0
        self.partial_rollback_calls = 0

    def instrument(self, *, tracer_provider: Any) -> None:
        self.providers.append(tracer_provider)
        self.marker = "mutating"
        if self.fails_during_instrument:
            raise RuntimeError("instrumentor mutation failed")
        self._is_instrumented_by_opentelemetry = True
        self.marker = "owned"

    def uninstrument(self) -> None:
        self.events.append("uninstrument")
        self.uninstrument_calls += 1
        self._is_instrumented_by_opentelemetry = False
        self.marker = "uninstrumented"

    def _uninstrument(self) -> None:
        self.events.append("partial_rollback")
        self.partial_rollback_calls += 1
        self._is_instrumented_by_opentelemetry = False
        self.marker = "rolled_back"


class _FakeEntryPoint:
    def __init__(self, name: str, instrumentor: _FakeInstrumentor) -> None:
        self.name = name
        self._instrumentor = instrumentor

    def load(self):
        return lambda: self._instrumentor


def _install_fake_openinference_entry_points(
    monkeypatch: pytest.MonkeyPatch,
    *instrumentors: _FakeInstrumentor,
) -> None:
    entry_points = [_FakeEntryPoint(f"fake-{index}", instrumentor) for index, instrumentor in enumerate(instrumentors)]
    monkeypatch.setattr(
        importlib.metadata,
        "entry_points",
        lambda *, group: entry_points if group == "openinference_instrumentor" else [],
    )


def _install_fake_openinference_config(monkeypatch: pytest.MonkeyPatch, env_name: str) -> None:
    openinference_module = type(sys)("openinference")
    instrumentation_module = type(sys)("openinference.instrumentation")
    config_module = type(sys)("openinference.instrumentation.config")
    config_module.OPENINFERENCE_HIDE_INPUTS = env_name
    instrumentation_module.config = config_module
    openinference_module.instrumentation = instrumentation_module
    monkeypatch.setitem(sys.modules, "openinference", openinference_module)
    monkeypatch.setitem(sys.modules, "openinference.instrumentation", instrumentation_module)
    monkeypatch.setitem(sys.modules, "openinference.instrumentation.config", config_module)


@pytest.fixture(autouse=True)
def _reset_phoenix_runtime():
    from deerflow.tracing import phoenix

    phoenix.shutdown_phoenix_tracing()
    phoenix.reset_phoenix_tracing_for_tests()
    yield
    phoenix.shutdown_phoenix_tracing()
    phoenix.reset_phoenix_tracing_for_tests()


def test_registers_an_isolated_batch_provider_and_honors_standard_bsp_environment(monkeypatch):
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
        }
    ]

    processor = provider._active_span_processor._span_processors[0]._batch_processor
    assert processor._max_queue_size == 64
    assert processor._schedule_delay_millis == 31
    assert processor._export_timeout_millis == 47
    assert processor._max_export_batch_size == 16


def test_manual_root_uses_the_saved_phoenix_provider(monkeypatch):
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


def test_auto_instrumented_tracer_and_manual_root_share_the_phoenix_provider(monkeypatch):
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


def test_auto_instrument_registers_provider_before_explicit_entry_point_transition(monkeypatch):
    from deerflow.tracing import phoenix

    provider = _RecordingProvider()
    instrumentor = _FakeInstrumentor()
    register_calls: list[dict[str, Any]] = []

    def register(**kwargs: Any) -> _RecordingProvider:
        register_calls.append(kwargs)
        return provider

    _install_fake_openinference_entry_points(monkeypatch, instrumentor)
    monkeypatch.setattr("phoenix.otel.register", register)
    monkeypatch.setattr(phoenix, "_validate_openinference_langchain_parent_contract", lambda: None)
    monkeypatch.setattr(phoenix, "_install_openinference_langchain_parent_compat", lambda _provider: None)

    phoenix.ensure_phoenix_tracing_initialized(_config(auto_instrument=True))

    assert register_calls[0]["auto_instrument"] is False
    assert register_calls[0]["set_global_tracer_provider"] is False
    assert register_calls[0]["batch"] is True
    assert instrumentor.providers == [provider]

    phoenix.shutdown_phoenix_tracing()
    assert instrumentor.events == ["uninstrument"]


def test_foreign_entry_point_is_untouched_when_initialization_is_rejected(monkeypatch):
    from deerflow.tracing import phoenix

    provider = _RecordingProvider()
    owned_candidate = _FakeInstrumentor()
    foreign = _FakeInstrumentor(instrumented=True)
    _install_fake_openinference_entry_points(monkeypatch, owned_candidate, foreign)
    monkeypatch.setattr("phoenix.otel.register", lambda **_kwargs: provider)
    monkeypatch.setattr(
        phoenix,
        "_validate_openinference_langchain_parent_contract",
        lambda: (_ for _ in ()).throw(AssertionError("foreign state must reject before validation")),
    )

    with pytest.raises(phoenix.PhoenixTracingError, match="foreign provider"):
        phoenix.ensure_phoenix_tracing_initialized(_config(auto_instrument=True))

    assert provider.events == [("shutdown", None)]
    assert owned_candidate.providers == []
    assert foreign.providers == []
    assert foreign.uninstrument_calls == 0
    assert foreign.partial_rollback_calls == 0
    assert foreign._is_instrumented_by_opentelemetry is True
    assert foreign.marker == "host"


def test_partial_entry_point_failure_rolls_back_all_owned_instances_and_allows_retry(monkeypatch):
    from deerflow.tracing import phoenix

    first_provider = _RecordingProvider()
    second_provider = _RecordingProvider()
    first = _FakeInstrumentor()
    second = _FakeInstrumentor(fails_during_instrument=True)
    _install_fake_openinference_entry_points(monkeypatch, first, second)
    providers = iter((first_provider, second_provider))
    monkeypatch.setattr("phoenix.otel.register", lambda **_kwargs: next(providers))
    monkeypatch.setattr(phoenix, "_validate_openinference_langchain_parent_contract", lambda: None)
    monkeypatch.setattr(phoenix, "_install_openinference_langchain_parent_compat", lambda _provider: None)

    with pytest.raises(phoenix.PhoenixTracingError, match="instrumentor mutation failed"):
        phoenix.ensure_phoenix_tracing_initialized(_config(auto_instrument=True))

    assert first_provider.events == [("shutdown", None)]
    assert first.events == ["partial_rollback"]
    assert second.events == ["partial_rollback"]
    assert first._is_instrumented_by_opentelemetry is False
    assert second._is_instrumented_by_opentelemetry is False
    assert first.marker == "clean"
    assert second.marker == "clean"
    assert phoenix._phoenix_tracer_provider is None
    assert phoenix._active_config_key is None

    second.fails_during_instrument = False
    phoenix.ensure_phoenix_tracing_initialized(_config(auto_instrument=True))
    phoenix.shutdown_phoenix_tracing()

    assert second_provider.events == [("force_flush", 30_000), ("shutdown", None)]
    assert first.events == ["partial_rollback", "uninstrument"]
    assert second.events == ["partial_rollback", "uninstrument"]


def test_shutdown_restores_content_capture_environment_and_allows_capture_reinitialization(monkeypatch):
    from deerflow.tracing import phoenix

    env_name = "OPENINFERENCE_HIDE_INPUTS"
    providers = iter((_RecordingProvider(), _RecordingProvider()))
    monkeypatch.setenv(env_name, "host-value")
    _install_fake_openinference_config(monkeypatch, env_name)
    monkeypatch.setattr("phoenix.otel.register", lambda **_kwargs: next(providers))

    phoenix.ensure_phoenix_tracing_initialized(_config(capture_content=False))
    assert os.environ[env_name] == "true"

    phoenix.shutdown_phoenix_tracing()
    assert os.environ[env_name] == "host-value"

    phoenix.ensure_phoenix_tracing_initialized(_config(capture_content=True))
    assert os.environ[env_name] == "host-value"


def test_shutdown_does_not_overwrite_host_content_capture_environment_changes(monkeypatch):
    from deerflow.tracing import phoenix

    env_name = "OPENINFERENCE_HIDE_INPUTS"
    monkeypatch.setenv(env_name, "host-value")
    _install_fake_openinference_config(monkeypatch, env_name)
    monkeypatch.setattr("phoenix.otel.register", lambda **_kwargs: _RecordingProvider())

    phoenix.ensure_phoenix_tracing_initialized(_config(capture_content=False))
    monkeypatch.setenv(env_name, "host-changed")
    phoenix.shutdown_phoenix_tracing()
    phoenix.shutdown_phoenix_tracing()

    assert os.environ[env_name] == "host-changed"


def test_foreign_openinference_instrumentor_fails_fast_and_closes_new_provider(monkeypatch):
    from openinference.instrumentation.langchain import LangChainInstrumentor

    from deerflow.tracing import phoenix

    instrumentor = LangChainInstrumentor()
    instrumentor.uninstrument()
    foreign_provider = TracerProvider()
    phoenix_provider = _RecordingProvider()
    instrumentor.instrument(tracer_provider=foreign_provider)
    monkeypatch.setattr("phoenix.otel.register", lambda **_kwargs: phoenix_provider)

    try:
        with pytest.raises(phoenix.PhoenixTracingError, match="foreign provider"):
            phoenix.ensure_phoenix_tracing_initialized(_config(auto_instrument=True))

        assert phoenix_provider.events == [("shutdown", None)]
        assert phoenix._phoenix_tracer_provider is None
        assert phoenix._active_config_key is None
        assert phoenix._parent_compat_tracer is None
    finally:
        instrumentor.uninstrument()
        foreign_provider.shutdown()


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

    monkeypatch.setattr("phoenix.otel.register", lambda **_kwargs: next(providers))
    monkeypatch.setattr(phoenix, "_install_openinference_langchain_parent_compat", install_compat)

    with pytest.raises(phoenix.PhoenixTracingError, match="compatibility installation failed"):
        phoenix.ensure_phoenix_tracing_initialized(_config(auto_instrument=True))

    assert first_provider.events == [("shutdown", None)]
    assert phoenix._phoenix_tracer_provider is None
    assert phoenix._active_config_key is None
    assert phoenix._parent_compat_tracer is None

    phoenix.ensure_phoenix_tracing_initialized(_config(auto_instrument=True))

    assert attempts == 2
    assert phoenix._phoenix_tracer_provider is second_provider


@pytest.mark.parametrize("flush_result,flush_error", [(False, None), (True, RuntimeError("flush failed"))])
def test_shutdown_flushes_before_shutdown_even_when_flush_fails(flush_result, flush_error, monkeypatch):
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


def test_shutdown_unregisters_provider_atexit_hook_before_flushing(monkeypatch):
    import atexit

    from deerflow.tracing import phoenix

    provider = _AtexitRecordingProvider()
    handler = provider._atexit_handler
    unregistered: list[object] = []
    monkeypatch.setattr("phoenix.otel.register", lambda **_kwargs: provider)
    monkeypatch.setattr(atexit, "unregister", unregistered.append)

    phoenix.ensure_phoenix_tracing_initialized(_config())
    phoenix.shutdown_phoenix_tracing(timeout_millis=123)

    assert provider._atexit_handler is None
    assert unregistered == [handler]
    assert provider.events == [("force_flush", 123), ("shutdown", None)]


def test_root_context_never_shuts_down_the_process_provider(monkeypatch):
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


@pytest.mark.asyncio
async def test_gateway_shuts_down_phoenix_after_inflight_run_drain(monkeypatch):
    import importlib
    from contextlib import asynccontextmanager
    from types import ModuleType

    from fastapi import FastAPI

    import deerflow.tracing as tracing

    gateway_app = importlib.import_module("app.gateway.app")

    events: list[str] = []
    startup_config = SimpleNamespace(
        log_level="INFO",
        memory=SimpleNamespace(token_counting="char"),
    )

    @asynccontextmanager
    async def fake_langgraph_runtime(_app, _config):
        try:
            yield
        finally:
            events.append("runs_drained")

    async def fake_ensure_admin(_app):
        return None

    async def fake_start_channel_service(_config):
        return SimpleNamespace(get_status=lambda: "ready")

    async def fake_stop_channel_service():
        return None

    channel_service = ModuleType("app.channels.service")
    channel_service.start_channel_service = fake_start_channel_service
    channel_service.stop_channel_service = fake_stop_channel_service
    monkeypatch.setattr(gateway_app, "get_app_config", lambda: startup_config)
    monkeypatch.setattr(gateway_app, "get_gateway_config", lambda: SimpleNamespace(host="127.0.0.1", port=2024))
    monkeypatch.setattr(gateway_app, "apply_logging_level", lambda _level: None)
    monkeypatch.setattr(gateway_app, "warn_if_auth_disabled_enabled", lambda: None)
    monkeypatch.setattr(gateway_app, "langgraph_runtime", fake_langgraph_runtime)
    monkeypatch.setattr(gateway_app, "_ensure_admin_user", fake_ensure_admin)
    monkeypatch.setattr(tracing, "shutdown_phoenix_tracing", lambda **_kwargs: events.append("phoenix_shutdown"))
    monkeypatch.setitem(sys.modules, "app.channels.service", channel_service)

    async with gateway_app.lifespan(FastAPI()):
        pass

    assert events.index("runs_drained") < events.index("phoenix_shutdown")


@pytest.mark.asyncio
async def test_gateway_shuts_down_phoenix_after_drain_when_run_lifetime_fails(monkeypatch):
    import importlib
    from contextlib import asynccontextmanager
    from types import ModuleType

    from fastapi import FastAPI

    import deerflow.tracing as tracing

    gateway_app = importlib.import_module("app.gateway.app")
    events: list[str] = []
    startup_config = SimpleNamespace(
        log_level="INFO",
        memory=SimpleNamespace(token_counting="char"),
    )

    @asynccontextmanager
    async def fake_langgraph_runtime(_app, _config):
        try:
            yield
        finally:
            events.append("runs_drained")

    async def fake_ensure_admin(_app):
        return None

    async def fake_start_channel_service(_config):
        return SimpleNamespace(get_status=lambda: "ready")

    async def fake_stop_channel_service():
        return None

    channel_service = ModuleType("app.channels.service")
    channel_service.start_channel_service = fake_start_channel_service
    channel_service.stop_channel_service = fake_stop_channel_service
    monkeypatch.setattr(gateway_app, "get_app_config", lambda: startup_config)
    monkeypatch.setattr(gateway_app, "get_gateway_config", lambda: SimpleNamespace(host="127.0.0.1", port=2024))
    monkeypatch.setattr(gateway_app, "apply_logging_level", lambda _level: None)
    monkeypatch.setattr(gateway_app, "warn_if_auth_disabled_enabled", lambda: None)
    monkeypatch.setattr(gateway_app, "langgraph_runtime", fake_langgraph_runtime)
    monkeypatch.setattr(gateway_app, "_ensure_admin_user", fake_ensure_admin)
    monkeypatch.setattr(tracing, "shutdown_phoenix_tracing", lambda **_kwargs: events.append("phoenix_shutdown"))
    monkeypatch.setitem(sys.modules, "app.channels.service", channel_service)

    with pytest.raises(RuntimeError, match="run lifetime failed"):
        async with gateway_app.lifespan(FastAPI()):
            raise RuntimeError("run lifetime failed")

    assert events == ["runs_drained", "phoenix_shutdown"]


@pytest.mark.asyncio
async def test_gateway_phoenix_cleanup_is_bounded_and_does_not_block_event_loop(monkeypatch, caplog):
    import importlib

    import deerflow.tracing as tracing

    gateway_app = importlib.import_module("app.gateway.app")
    started = threading.Event()
    release = threading.Event()

    def blocking_shutdown(**_kwargs) -> None:
        started.set()
        release.wait()

    monkeypatch.setattr(gateway_app, "_SHUTDOWN_HOOK_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(tracing, "shutdown_phoenix_tracing", blocking_shutdown)
    started_at = time.monotonic()
    try:
        await gateway_app._shutdown_phoenix_tracing_bounded()
    finally:
        release.set()

    assert started.is_set()
    assert time.monotonic() - started_at < 0.5
    assert "Phoenix tracing shutdown exceeded" in caplog.text


@pytest.mark.asyncio
async def test_gateway_timeout_unregisters_provider_atexit_hook_before_blocking_flush(monkeypatch):
    import atexit
    import importlib

    from deerflow.tracing import phoenix

    gateway_app = importlib.import_module("app.gateway.app")
    provider = _AtexitRecordingProvider(block_flush=True)
    handler = provider._atexit_handler
    unregistered: list[object] = []
    monkeypatch.setattr("phoenix.otel.register", lambda **_kwargs: provider)
    monkeypatch.setattr(atexit, "unregister", unregistered.append)
    monkeypatch.setattr(gateway_app, "_SHUTDOWN_HOOK_TIMEOUT_SECONDS", 0.02)
    phoenix.ensure_phoenix_tracing_initialized(_config())

    try:
        await gateway_app._shutdown_phoenix_tracing_bounded()
        assert provider.flush_started.is_set()
        assert provider._atexit_handler is None
        assert unregistered == [handler]
    finally:
        provider.release_flush.set()

    assert provider.shutdown_finished.wait(0.5)


def _install_openinference_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    openinference_module = type(sys)("openinference")
    instrumentation_module = type(sys)("openinference.instrumentation")
    semconv_module = type(sys)("openinference.semconv")
    trace_module = type(sys)("openinference.semconv.trace")

    @contextmanager
    def using_attributes(**_kwargs):
        yield

    class SpanAttributes:
        OPENINFERENCE_SPAN_KIND = "openinference.span.kind"

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

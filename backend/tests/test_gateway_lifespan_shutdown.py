"""Regression tests for Gateway lifespan shutdown.

These tests guard the invariant that lifespan shutdown is *bounded*: a
misbehaving channel whose ``stop()`` blocks forever must not keep the
uvicorn worker alive. A hung worker is the precondition for the
signal-reentrancy deadlock described in
``app.gateway.app._SHUTDOWN_HOOK_TIMEOUT_SECONDS``.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import threading
import time
from contextlib import asynccontextmanager
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI

import deerflow.tracing as tracing


@asynccontextmanager
async def _noop_langgraph_runtime(_app, _startup_config):
    yield


def _make_gateway_app_module(monkeypatch):
    gateway_app = importlib.import_module("app.gateway.app")
    startup_config = SimpleNamespace(
        log_level="INFO",
        memory=SimpleNamespace(token_counting="char"),
    )

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
    monkeypatch.setattr(gateway_app, "langgraph_runtime", _noop_langgraph_runtime)
    monkeypatch.setattr(gateway_app, "_ensure_admin_user", fake_ensure_admin)
    monkeypatch.setitem(sys.modules, "app.channels.service", channel_service)
    return gateway_app


async def _run_lifespan_with_hanging_stop() -> float:
    """Drive the lifespan context with stop_channel_service hanging forever.

    Returns the elapsed wall-clock seconds.
    """
    from app.gateway.app import _SHUTDOWN_HOOK_TIMEOUT_SECONDS, lifespan

    async def hang_forever() -> None:
        await asyncio.sleep(3600)

    app = FastAPI()

    fake_service = MagicMock()
    fake_service.get_status = MagicMock(return_value={})

    async def fake_start():
        return fake_service

    with (
        patch("app.gateway.app.get_app_config"),
        patch("app.gateway.app.get_gateway_config", return_value=MagicMock(host="x", port=0)),
        patch("app.gateway.app.langgraph_runtime", _noop_langgraph_runtime),
        patch("app.channels.service.start_channel_service", side_effect=fake_start),
        patch("app.channels.service.stop_channel_service", side_effect=hang_forever),
    ):
        loop = asyncio.get_event_loop()
        start = loop.time()
        async with lifespan(app):
            pass
        elapsed = loop.time() - start

    assert _SHUTDOWN_HOOK_TIMEOUT_SECONDS < 30.0, "Timeout constant must stay modest"
    return elapsed


def test_shutdown_is_bounded_when_channel_stop_hangs():
    """Lifespan exit must complete near the configured timeout, not hang."""
    from app.gateway.app import _SHUTDOWN_HOOK_TIMEOUT_SECONDS

    elapsed = asyncio.run(_run_lifespan_with_hanging_stop())

    # Generous upper bound: timeout + 2s slack for scheduling overhead.
    assert elapsed < _SHUTDOWN_HOOK_TIMEOUT_SECONDS + 2.0, f"Lifespan shutdown took {elapsed:.2f}s; expected <= {_SHUTDOWN_HOOK_TIMEOUT_SECONDS + 2.0:.1f}s"
    # Lower bound: the wait_for should actually have waited.
    assert elapsed >= _SHUTDOWN_HOOK_TIMEOUT_SECONDS - 0.5, f"Lifespan exited too quickly ({elapsed:.2f}s); wait_for may not have been invoked."


@pytest.mark.asyncio
async def test_gateway_shuts_down_phoenix_after_inflight_run_drain(monkeypatch):
    gateway_app = _make_gateway_app_module(monkeypatch)
    events: list[str] = []

    @asynccontextmanager
    async def fake_langgraph_runtime(_app, _config):
        try:
            yield
        finally:
            events.append("runs_drained")

    monkeypatch.setattr(gateway_app, "langgraph_runtime", fake_langgraph_runtime)
    monkeypatch.setattr(tracing, "shutdown_phoenix_tracing", lambda **_kwargs: events.append("phoenix_shutdown"))

    async with gateway_app.lifespan(FastAPI()):
        pass

    assert events.index("runs_drained") < events.index("phoenix_shutdown")


@pytest.mark.asyncio
async def test_gateway_shuts_down_phoenix_after_drain_when_run_lifetime_fails(monkeypatch):
    gateway_app = _make_gateway_app_module(monkeypatch)
    events: list[str] = []

    @asynccontextmanager
    async def fake_langgraph_runtime(_app, _config):
        try:
            yield
        finally:
            events.append("runs_drained")

    monkeypatch.setattr(gateway_app, "langgraph_runtime", fake_langgraph_runtime)
    monkeypatch.setattr(tracing, "shutdown_phoenix_tracing", lambda **_kwargs: events.append("phoenix_shutdown"))

    with pytest.raises(RuntimeError, match="run lifetime failed"):
        async with gateway_app.lifespan(FastAPI()):
            raise RuntimeError("run lifetime failed")

    assert events == ["runs_drained", "phoenix_shutdown"]


@pytest.mark.asyncio
async def test_gateway_phoenix_cleanup_is_bounded_and_does_not_block_event_loop(monkeypatch, caplog):
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
async def test_gateway_timeout_bounded_flush_does_not_inspect_atexit(monkeypatch):
    from deerflow.config.tracing_config import PhoenixTracingConfig
    from deerflow.tracing import phoenix

    gateway_app = importlib.import_module("app.gateway.app")

    class _BlockingProvider:
        def __init__(self) -> None:
            object.__setattr__(self, "events", [])
            object.__setattr__(self, "atexit_reads", [])
            object.__setattr__(self, "atexit_writes", [])
            super().__init__()
            self.events.clear()
            self.atexit_reads.clear()
            self.atexit_writes.clear()

        def force_flush(self, timeout_millis: int = 30_000) -> bool:
            self.events.append(("force_flush", timeout_millis))
            return True

        def shutdown(self) -> None:
            self.events.append(("shutdown", None))

        def __getattribute__(self, name: str) -> Any:
            if name == "_atexit_handler":
                object.__getattribute__(self, "atexit_reads").append(name)
            return object.__getattribute__(self, name)

        def __setattr__(self, name: str, value: Any) -> None:
            if name == "_atexit_handler" and value is not None:
                object.__getattribute__(self, "atexit_writes").append(value)
            object.__setattr__(self, name, value)

    provider = _BlockingProvider()
    monkeypatch.setattr("phoenix.otel.register", lambda **_kwargs: provider)
    monkeypatch.setattr(gateway_app, "_SHUTDOWN_HOOK_TIMEOUT_SECONDS", 0.02)
    phoenix.ensure_phoenix_tracing_initialized(
        PhoenixTracingConfig(
            enabled=True,
            collector_endpoint="http://127.0.0.1:9",
            api_key=None,
            project_name="gateway-lifecycle-test",
            auto_instrument=False,
            capture_content=True,
            trace_parent_mode="root",
            trace_parent_required=False,
            propagate_baggage=False,
        )
    )

    try:
        await gateway_app._shutdown_phoenix_tracing_bounded()
    finally:
        pass

    assert provider.atexit_reads == []
    assert provider.atexit_writes == []

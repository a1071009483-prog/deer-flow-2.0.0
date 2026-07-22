from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from deerflow.config.app_config import AppConfig, reset_app_config, set_app_config
from deerflow.config.tracing_config import reset_tracing_config
from deerflow.runtime.runs.manager import RunRecord
from deerflow.runtime.runs.schemas import RunStatus


class _FakeRunManager:
    async def create_or_reject(
        self,
        thread_id,
        assistant_id,
        *,
        on_disconnect,
        metadata,
        kwargs,
        multitask_strategy,
        model_name,
        user_id,
    ):
        record = RunRecord(
            run_id="run-1",
            thread_id=thread_id,
            assistant_id=assistant_id,
            status=RunStatus.pending,
            on_disconnect=on_disconnect,
            model_name=model_name,
        )
        record.abort_event = asyncio.Event()
        return record


class _FakeThreadStore:
    async def check_access(self, *_args, **_kwargs):
        return True

    async def get(self, *_args, **_kwargs):
        return {"thread_id": "thread-1"}

    async def create(self, *_args, **_kwargs):
        return None

    async def update_status(self, *_args, **_kwargs):
        return None


class _FakeBridge:
    pass


@pytest.fixture(autouse=True)
def _reset_configs():
    set_app_config(AppConfig.model_validate({"sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider"}}))
    reset_tracing_config()
    yield
    reset_tracing_config()
    reset_app_config()


def _make_request(*, headers: dict[str, str] | None = None):
    state = SimpleNamespace(
        stream_bridge=_FakeBridge(),
        run_manager=_FakeRunManager(),
        checkpointer=SimpleNamespace(),
        store=None,
        run_event_store=object(),
        run_events_config=None,
        thread_store=_FakeThreadStore(),
    )
    return SimpleNamespace(
        headers=headers or {},
        state=SimpleNamespace(user=None),
        app=SimpleNamespace(state=state),
    )


def _make_body():
    return SimpleNamespace(
        assistant_id="lead_agent",
        input={"messages": [{"role": "user", "content": "hi"}]},
        metadata={},
        config=None,
        context=None,
        checkpoint=None,
        checkpoint_id=None,
        on_disconnect="cancel",
        multitask_strategy="reject",
        stream_mode=None,
        stream_subgraphs=False,
        interrupt_before=None,
        interrupt_after=None,
    )


@pytest.mark.asyncio
async def test_start_run_copies_traceparent_headers_to_run_context(monkeypatch):
    from app.gateway import services

    captured: dict[str, object] = {}

    async def fake_run_agent(*args, **kwargs):
        captured["config"] = kwargs["config"]

    monkeypatch.setenv("PHOENIX_TRACING", "true")
    monkeypatch.setenv("PHOENIX_PROPAGATE_BAGGAGE", "true")
    reset_tracing_config()

    request = _make_request(
        headers={
            "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
            "tracestate": "vendor=test",
            "baggage": "user_id=abc,env=dev",
        }
    )

    with (
        patch.object(services, "resolve_agent_factory", return_value=object()),
        patch.object(services, "run_agent", side_effect=fake_run_agent),
    ):
        record = await services.start_run(_make_body(), "thread-1", request)
        await record.task

    context = captured["config"]["context"]
    assert context["__otel_trace_context"] == {
        "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        "tracestate": "vendor=test",
        "baggage": "user_id=abc,env=dev",
    }


@pytest.mark.asyncio
async def test_start_run_omits_baggage_when_disabled(monkeypatch):
    from app.gateway import services

    captured: dict[str, object] = {}

    async def fake_run_agent(*args, **kwargs):
        captured["config"] = kwargs["config"]

    monkeypatch.setenv("PHOENIX_TRACING", "true")
    monkeypatch.setenv("PHOENIX_PROPAGATE_BAGGAGE", "false")
    reset_tracing_config()

    request = _make_request(
        headers={
            "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
            "tracestate": "vendor=test",
            "baggage": "user_id=abc,env=dev",
        }
    )

    with (
        patch.object(services, "resolve_agent_factory", return_value=object()),
        patch.object(services, "run_agent", side_effect=fake_run_agent),
    ):
        record = await services.start_run(_make_body(), "thread-1", request)
        await record.task

    context = captured["config"]["context"]
    assert context["__otel_trace_context"] == {
        "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        "tracestate": "vendor=test",
    }


@pytest.mark.asyncio
async def test_start_run_keeps_baggage_only_header_when_enabled(monkeypatch):
    from app.gateway import services

    captured: dict[str, object] = {}

    async def fake_run_agent(*args, **kwargs):
        captured["config"] = kwargs["config"]

    monkeypatch.setenv("PHOENIX_TRACING", "true")
    monkeypatch.setenv("PHOENIX_PROPAGATE_BAGGAGE", "true")
    reset_tracing_config()
    request = _make_request(headers={"baggage": "tenant=acme"})

    with (
        patch.object(services, "resolve_agent_factory", return_value=object()),
        patch.object(services, "run_agent", side_effect=fake_run_agent),
    ):
        record = await services.start_run(_make_body(), "thread-1", request)
        await record.task

    assert captured["config"]["context"]["__otel_trace_context"] == {"baggage": "tenant=acme"}


@pytest.mark.asyncio
async def test_start_run_drops_baggage_only_header_when_disabled(monkeypatch):
    from app.gateway import services

    captured: dict[str, object] = {}

    async def fake_run_agent(*args, **kwargs):
        captured["config"] = kwargs["config"]

    monkeypatch.setenv("PHOENIX_TRACING", "true")
    monkeypatch.setenv("PHOENIX_PROPAGATE_BAGGAGE", "false")
    reset_tracing_config()
    request = _make_request(headers={"baggage": "tenant=acme"})

    with (
        patch.object(services, "resolve_agent_factory", return_value=object()),
        patch.object(services, "run_agent", side_effect=fake_run_agent),
    ):
        record = await services.start_run(_make_body(), "thread-1", request)
        await record.task

    assert "__otel_trace_context" not in captured["config"].get("context", {})

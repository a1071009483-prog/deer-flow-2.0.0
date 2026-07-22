"""Tests for deerflow.tracing.factory."""

from __future__ import annotations

import sys
import types

import pytest

from deerflow.tracing import factory as tracing_factory


@pytest.fixture(autouse=True)
def clear_tracing_env(monkeypatch):
    from deerflow.config.tracing_config import reset_tracing_config

    for name in (
        "LANGSMITH_TRACING",
        "LANGCHAIN_TRACING_V2",
        "LANGCHAIN_TRACING",
        "LANGSMITH_API_KEY",
        "LANGCHAIN_API_KEY",
        "LANGSMITH_PROJECT",
        "LANGCHAIN_PROJECT",
        "LANGSMITH_ENDPOINT",
        "LANGCHAIN_ENDPOINT",
        "LANGFUSE_TRACING",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "LANGFUSE_BASE_URL",
        "PHOENIX_TRACING",
        "PHOENIX_COLLECTOR_ENDPOINT",
        "PHOENIX_API_KEY",
        "PHOENIX_PROJECT_NAME",
        "PHOENIX_AUTO_INSTRUMENT",
        "PHOENIX_CAPTURE_CONTENT",
        "PHOENIX_TRACE_PARENT_MODE",
        "PHOENIX_TRACE_PARENT_REQUIRED",
        "PHOENIX_PROPAGATE_BAGGAGE",
    ):
        monkeypatch.delenv(name, raising=False)
    reset_tracing_config()
    try:
        from deerflow.tracing.phoenix import reset_phoenix_tracing_for_tests
    except ModuleNotFoundError:
        reset_phoenix_tracing_for_tests = None
    if reset_phoenix_tracing_for_tests is not None:
        reset_phoenix_tracing_for_tests()
    yield
    reset_tracing_config()
    if reset_phoenix_tracing_for_tests is not None:
        reset_phoenix_tracing_for_tests()


def test_build_tracing_callbacks_returns_empty_list_when_disabled(monkeypatch):
    monkeypatch.setattr(tracing_factory, "validate_enabled_tracing_providers", lambda: None)
    monkeypatch.setattr(tracing_factory, "get_enabled_tracing_providers", lambda: [])

    callbacks = tracing_factory.build_tracing_callbacks()

    assert callbacks == []


def test_build_tracing_callbacks_creates_langsmith_and_langfuse(monkeypatch):
    class FakeLangSmithTracer:
        def __init__(self, *, project_name: str):
            self.project_name = project_name

    class FakeLangfuseHandler:
        def __init__(self, *, public_key: str):
            self.public_key = public_key

    monkeypatch.setattr(tracing_factory, "get_enabled_tracing_providers", lambda: ["langsmith", "langfuse"])
    monkeypatch.setattr(tracing_factory, "validate_enabled_tracing_providers", lambda: None)
    monkeypatch.setattr(
        tracing_factory,
        "get_tracing_config",
        lambda: type(
            "Cfg",
            (),
            {
                "langsmith": type("LangSmithCfg", (), {"project": "smith-project"})(),
                "langfuse": type(
                    "LangfuseCfg",
                    (),
                    {
                        "secret_key": "sk-lf-test",
                        "public_key": "pk-lf-test",
                        "host": "https://langfuse.example.com",
                    },
                )(),
            },
        )(),
    )
    monkeypatch.setattr(tracing_factory, "_create_langsmith_tracer", lambda cfg: FakeLangSmithTracer(project_name=cfg.project))
    monkeypatch.setattr(
        tracing_factory,
        "_create_langfuse_handler",
        lambda cfg: FakeLangfuseHandler(public_key=cfg.public_key),
    )

    callbacks = tracing_factory.build_tracing_callbacks()

    assert len(callbacks) == 2
    assert callbacks[0].project_name == "smith-project"
    assert callbacks[1].public_key == "pk-lf-test"


def test_build_tracing_callbacks_initializes_phoenix_without_callback(monkeypatch):
    phoenix_config = type(
        "PhoenixCfg",
        (),
        {
            "enabled": True,
            "collector_endpoint": "http://localhost:6006",
            "api_key": None,
            "project_name": "deer-flow-test",
            "auto_instrument": True,
            "capture_content": False,
        },
    )()
    calls = []

    monkeypatch.setattr(tracing_factory, "get_enabled_tracing_providers", lambda: ["phoenix"])
    monkeypatch.setattr(tracing_factory, "validate_enabled_tracing_providers", lambda: None)
    monkeypatch.setattr(
        tracing_factory,
        "get_tracing_config",
        lambda: type("Cfg", (), {"phoenix": phoenix_config})(),
    )
    monkeypatch.setattr(
        tracing_factory,
        "ensure_phoenix_tracing_initialized",
        lambda config: calls.append(config),
    )

    callbacks = tracing_factory.build_tracing_callbacks()

    assert callbacks == []
    assert calls == [phoenix_config]


def test_build_tracing_callbacks_raises_when_enabled_provider_fails(monkeypatch):
    monkeypatch.setattr(tracing_factory, "get_enabled_tracing_providers", lambda: ["langfuse"])
    monkeypatch.setattr(tracing_factory, "validate_enabled_tracing_providers", lambda: None)
    monkeypatch.setattr(
        tracing_factory,
        "get_tracing_config",
        lambda: type(
            "Cfg",
            (),
            {
                "langfuse": type(
                    "LangfuseCfg",
                    (),
                    {"secret_key": "sk-lf-test", "public_key": "pk-lf-test", "host": "https://langfuse.example.com"},
                )(),
            },
        )(),
    )
    monkeypatch.setattr(tracing_factory, "_create_langfuse_handler", lambda cfg: (_ for _ in ()).throw(RuntimeError("boom")))

    with pytest.raises(RuntimeError, match="Langfuse tracing initialization failed"):
        tracing_factory.build_tracing_callbacks()


def test_build_tracing_callbacks_raises_for_explicitly_enabled_misconfigured_provider(monkeypatch):
    from deerflow.config.tracing_config import reset_tracing_config

    monkeypatch.setenv("LANGFUSE_TRACING", "true")
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    reset_tracing_config()

    with pytest.raises(ValueError, match="LANGFUSE_PUBLIC_KEY"):
        tracing_factory.build_tracing_callbacks()


def test_create_langfuse_handler_initializes_client_before_handler(monkeypatch):
    calls: list[tuple[str, dict]] = []

    class FakeLangfuse:
        def __init__(self, **kwargs):
            calls.append(("client", kwargs))

    class FakeCallbackHandler:
        def __init__(self, **kwargs):
            calls.append(("handler", kwargs))

    fake_langfuse_module = types.ModuleType("langfuse")
    fake_langfuse_module.Langfuse = FakeLangfuse
    fake_langfuse_langchain_module = types.ModuleType("langfuse.langchain")
    fake_langfuse_langchain_module.CallbackHandler = FakeCallbackHandler
    monkeypatch.setitem(sys.modules, "langfuse", fake_langfuse_module)
    monkeypatch.setitem(sys.modules, "langfuse.langchain", fake_langfuse_langchain_module)

    cfg = type(
        "LangfuseCfg",
        (),
        {
            "secret_key": "sk-lf-test",
            "public_key": "pk-lf-test",
            "host": "https://langfuse.example.com",
        },
    )()

    tracing_factory._create_langfuse_handler(cfg)

    assert calls == [
        (
            "client",
            {
                "secret_key": "sk-lf-test",
                "public_key": "pk-lf-test",
                "host": "https://langfuse.example.com",
            },
        ),
        (
            "handler",
            {
                "public_key": "pk-lf-test",
            },
        ),
    ]

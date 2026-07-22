"""Tests for deerflow.config.tracing_config."""

from __future__ import annotations

import pytest

from deerflow.config import tracing_config as tracing_module
from deerflow.config.tracing_config import reset_tracing_config


def _reset_tracing_cache() -> None:
    reset_tracing_config()


@pytest.fixture(autouse=True)
def clear_tracing_env(monkeypatch):
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
        "PHOENIX_METADATA_ALLOWLIST",
        "PHOENIX_TRACE_PARENT_MODE",
        "PHOENIX_TRACE_PARENT_REQUIRED",
        "PHOENIX_PROPAGATE_BAGGAGE",
    ):
        monkeypatch.delenv(name, raising=False)
    _reset_tracing_cache()
    yield
    _reset_tracing_cache()


def test_prefers_langsmith_env_names(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_key")
    monkeypatch.setenv("LANGSMITH_PROJECT", "smith-project")
    monkeypatch.setenv("LANGSMITH_ENDPOINT", "https://smith.example.com")

    _reset_tracing_cache()
    cfg = tracing_module.get_tracing_config()

    assert cfg.langsmith.enabled is True
    assert cfg.langsmith.api_key == "lsv2_key"
    assert cfg.langsmith.project == "smith-project"
    assert cfg.langsmith.endpoint == "https://smith.example.com"
    assert tracing_module.is_tracing_enabled() is True
    assert tracing_module.get_enabled_tracing_providers() == ["langsmith"]


def test_falls_back_to_langchain_env_names(monkeypatch):
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    monkeypatch.delenv("LANGSMITH_PROJECT", raising=False)
    monkeypatch.delenv("LANGSMITH_ENDPOINT", raising=False)

    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    monkeypatch.setenv("LANGCHAIN_API_KEY", "legacy-key")
    monkeypatch.setenv("LANGCHAIN_PROJECT", "legacy-project")
    monkeypatch.setenv("LANGCHAIN_ENDPOINT", "https://legacy.example.com")

    _reset_tracing_cache()
    cfg = tracing_module.get_tracing_config()

    assert cfg.langsmith.enabled is True
    assert cfg.langsmith.api_key == "legacy-key"
    assert cfg.langsmith.project == "legacy-project"
    assert cfg.langsmith.endpoint == "https://legacy.example.com"
    assert tracing_module.is_tracing_enabled() is True
    assert tracing_module.get_enabled_tracing_providers() == ["langsmith"]


def test_langsmith_tracing_false_overrides_langchain_tracing_v2_true(monkeypatch):
    """LANGSMITH_TRACING=false must win over LANGCHAIN_TRACING_V2=true."""
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "some-key")

    _reset_tracing_cache()
    cfg = tracing_module.get_tracing_config()

    assert cfg.langsmith.enabled is False
    assert tracing_module.is_tracing_enabled() is False
    assert tracing_module.get_enabled_tracing_providers() == []


def test_defaults_when_project_not_set(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "yes")
    monkeypatch.setenv("LANGSMITH_API_KEY", "key")
    monkeypatch.delenv("LANGSMITH_PROJECT", raising=False)
    monkeypatch.delenv("LANGCHAIN_PROJECT", raising=False)

    _reset_tracing_cache()
    cfg = tracing_module.get_tracing_config()

    assert cfg.langsmith.project == "deer-flow"


def test_langfuse_config_is_loaded(monkeypatch):
    monkeypatch.setenv("LANGFUSE_TRACING", "true")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://langfuse.example.com")

    _reset_tracing_cache()
    cfg = tracing_module.get_tracing_config()

    assert cfg.langfuse.enabled is True
    assert cfg.langfuse.public_key == "pk-lf-test"
    assert cfg.langfuse.secret_key == "sk-lf-test"
    assert cfg.langfuse.host == "https://langfuse.example.com"
    assert tracing_module.get_enabled_tracing_providers() == ["langfuse"]


def test_dual_provider_config_is_loaded(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_key")
    monkeypatch.setenv("LANGFUSE_TRACING", "true")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")

    _reset_tracing_cache()
    cfg = tracing_module.get_tracing_config()

    assert cfg.langsmith.is_configured is True
    assert cfg.langfuse.is_configured is True
    assert tracing_module.is_tracing_enabled() is True
    assert tracing_module.get_enabled_tracing_providers() == ["langsmith", "langfuse"]


def test_langfuse_enabled_requires_public_and_secret_keys(monkeypatch):
    monkeypatch.setenv("LANGFUSE_TRACING", "true")
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")

    _reset_tracing_cache()

    assert tracing_module.get_tracing_config().is_configured is False
    assert tracing_module.get_enabled_tracing_providers() == []
    assert tracing_module.get_tracing_config().explicitly_enabled_providers == ["langfuse"]

    with pytest.raises(ValueError, match="LANGFUSE_PUBLIC_KEY"):
        tracing_module.validate_enabled_tracing_providers()


def test_phoenix_default_disabled_does_not_enable_provider(monkeypatch):
    _reset_tracing_cache()
    cfg = tracing_module.get_tracing_config()

    assert cfg.phoenix.enabled is False
    assert cfg.phoenix.collector_endpoint == "http://localhost:6006/v1/traces"
    assert cfg.phoenix.api_key is None
    assert cfg.phoenix.project_name == "deer-flow"
    assert cfg.phoenix.auto_instrument is True
    assert cfg.phoenix.capture_content is False
    assert cfg.phoenix.metadata_allowlist == ()
    assert cfg.phoenix.trace_parent_mode == "auto"
    assert cfg.phoenix.trace_parent_required is False
    assert cfg.phoenix.propagate_baggage is False
    assert cfg.phoenix.is_configured is False
    assert tracing_module.get_enabled_tracing_providers() == []
    assert tracing_module.get_explicitly_enabled_tracing_providers() == []


def test_phoenix_local_collector_enabled_without_api_key(monkeypatch):
    monkeypatch.setenv("PHOENIX_TRACING", "true")

    _reset_tracing_cache()
    cfg = tracing_module.get_tracing_config()

    assert cfg.phoenix.enabled is True
    assert cfg.phoenix.collector_endpoint == "http://localhost:6006/v1/traces"
    assert cfg.phoenix.api_key is None
    assert cfg.phoenix.project_name == "deer-flow"
    assert cfg.phoenix.is_configured is True
    assert tracing_module.get_enabled_tracing_providers() == ["phoenix"]
    assert tracing_module.get_explicitly_enabled_tracing_providers() == ["phoenix"]
    tracing_module.validate_enabled_tracing_providers()


def test_phoenix_parent_mode_config(monkeypatch):
    monkeypatch.setenv("PHOENIX_TRACING", "true")
    monkeypatch.setenv("PHOENIX_TRACE_PARENT_MODE", "child")
    monkeypatch.setenv("PHOENIX_TRACE_PARENT_REQUIRED", "yes")
    monkeypatch.setenv("PHOENIX_PROPAGATE_BAGGAGE", "on")

    _reset_tracing_cache()
    cfg = tracing_module.get_tracing_config()

    assert cfg.phoenix.trace_parent_mode == "child"
    assert cfg.phoenix.trace_parent_required is True
    assert cfg.phoenix.propagate_baggage is True


def test_phoenix_metadata_allowlist_strips_whitespace_and_deduplicates(monkeypatch):
    monkeypatch.setenv("PHOENIX_METADATA_ALLOWLIST", " request_id,tenant_id,request_id, , tenant_id ")

    _reset_tracing_cache()
    cfg = tracing_module.get_tracing_config()

    assert cfg.phoenix.metadata_allowlist == ("request_id", "tenant_id")


def test_phoenix_collector_endpoint_base_url_uses_otlp_traces_path(monkeypatch):
    monkeypatch.setenv("PHOENIX_TRACING", "true")
    monkeypatch.setenv("PHOENIX_COLLECTOR_ENDPOINT", "http://127.0.0.1:6006/")

    _reset_tracing_cache()
    cfg = tracing_module.get_tracing_config()

    assert cfg.phoenix.collector_endpoint == "http://127.0.0.1:6006/v1/traces"


def test_phoenix_collector_endpoint_preserves_explicit_path(monkeypatch):
    monkeypatch.setenv("PHOENIX_TRACING", "true")
    monkeypatch.setenv("PHOENIX_COLLECTOR_ENDPOINT", "https://collector.example.com/custom/traces")

    _reset_tracing_cache()
    cfg = tracing_module.get_tracing_config()

    assert cfg.phoenix.collector_endpoint == "https://collector.example.com/custom/traces"


def test_phoenix_invalid_parent_mode_fails_validation(monkeypatch):
    monkeypatch.setenv("PHOENIX_TRACING", "true")
    monkeypatch.setenv("PHOENIX_TRACE_PARENT_MODE", "invalid")

    _reset_tracing_cache()

    with pytest.raises(ValueError, match="PHOENIX_TRACE_PARENT_MODE"):
        tracing_module.validate_enabled_tracing_providers()


def test_three_provider_config_order(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_key")
    monkeypatch.setenv("LANGFUSE_TRACING", "true")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    monkeypatch.setenv("PHOENIX_TRACING", "true")

    _reset_tracing_cache()

    assert tracing_module.get_enabled_tracing_providers() == ["langsmith", "langfuse", "phoenix"]
    assert tracing_module.get_explicitly_enabled_tracing_providers() == ["langsmith", "langfuse", "phoenix"]

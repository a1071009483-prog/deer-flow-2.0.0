import os
import threading
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, Field, field_validator

_config_lock = threading.Lock()
_PHOENIX_TRACE_PARENT_MODES = {"root", "child", "auto"}
_PHOENIX_OTLP_TRACES_PATH = "/v1/traces"


class LangSmithTracingConfig(BaseModel):
    """Configuration for LangSmith tracing."""

    enabled: bool = Field(...)
    api_key: str | None = Field(...)
    project: str = Field(...)
    endpoint: str = Field(...)

    @property
    def is_configured(self) -> bool:
        return self.enabled and bool(self.api_key)

    def validate(self) -> None:
        if self.enabled and not self.api_key:
            raise ValueError("LangSmith tracing is enabled but LANGSMITH_API_KEY (or LANGCHAIN_API_KEY) is not set.")


class LangfuseTracingConfig(BaseModel):
    """Configuration for Langfuse tracing."""

    enabled: bool = Field(...)
    public_key: str | None = Field(...)
    secret_key: str | None = Field(...)
    host: str = Field(...)

    @property
    def is_configured(self) -> bool:
        return self.enabled and bool(self.public_key) and bool(self.secret_key)

    def validate(self) -> None:
        if not self.enabled:
            return
        missing: list[str] = []
        if not self.public_key:
            missing.append("LANGFUSE_PUBLIC_KEY")
        if not self.secret_key:
            missing.append("LANGFUSE_SECRET_KEY")
        if missing:
            raise ValueError(f"Langfuse tracing is enabled but required settings are missing: {', '.join(missing)}")


class PhoenixTracingConfig(BaseModel):
    """Configuration for Phoenix tracing."""

    enabled: bool = Field(...)
    collector_endpoint: str = Field(...)
    api_key: str | None = Field(...)
    project_name: str = Field(...)
    auto_instrument: bool = Field(...)
    capture_content: bool = Field(...)
    metadata_allowlist: tuple[str, ...] = Field(default_factory=tuple)
    trace_parent_mode: Literal["root", "child", "auto"] = Field(...)
    trace_parent_required: bool = Field(...)
    propagate_baggage: bool = Field(...)

    @field_validator("collector_endpoint", mode="before")
    @classmethod
    def _normalize_collector_endpoint(cls, value: str) -> str:
        if not isinstance(value, str):
            return value
        endpoint = value.strip()
        parts = urlsplit(endpoint)
        if parts.scheme in {"http", "https"} and parts.netloc and parts.path in {"", "/"}:
            return urlunsplit((parts.scheme, parts.netloc, _PHOENIX_OTLP_TRACES_PATH, "", ""))
        return endpoint

    @field_validator("trace_parent_mode", mode="before")
    @classmethod
    def _validate_trace_parent_mode(cls, value: str) -> str:
        if value not in _PHOENIX_TRACE_PARENT_MODES:
            raise ValueError("PHOENIX_TRACE_PARENT_MODE must be one of: root, child, auto.")
        return value

    @property
    def is_configured(self) -> bool:
        return self.enabled and bool(self.collector_endpoint) and bool(self.project_name)

    def validate(self) -> None:
        if not self.enabled:
            return
        missing: list[str] = []
        if not self.collector_endpoint:
            missing.append("PHOENIX_COLLECTOR_ENDPOINT")
        if not self.project_name:
            missing.append("PHOENIX_PROJECT_NAME")
        if missing:
            raise ValueError(f"Phoenix tracing is enabled but required settings are missing: {', '.join(missing)}")
        if self.trace_parent_mode not in _PHOENIX_TRACE_PARENT_MODES:
            raise ValueError("PHOENIX_TRACE_PARENT_MODE must be one of: root, child, auto.")


class TracingConfig(BaseModel):
    """Tracing configuration for supported providers."""

    langsmith: LangSmithTracingConfig = Field(...)
    langfuse: LangfuseTracingConfig = Field(...)
    phoenix: PhoenixTracingConfig = Field(...)

    @property
    def is_configured(self) -> bool:
        return bool(self.enabled_providers)

    @property
    def explicitly_enabled_providers(self) -> list[str]:
        enabled: list[str] = []
        if self.langsmith.enabled:
            enabled.append("langsmith")
        if self.langfuse.enabled:
            enabled.append("langfuse")
        if self.phoenix.enabled:
            enabled.append("phoenix")
        return enabled

    @property
    def enabled_providers(self) -> list[str]:
        enabled: list[str] = []
        if self.langsmith.is_configured:
            enabled.append("langsmith")
        if self.langfuse.is_configured:
            enabled.append("langfuse")
        if self.phoenix.is_configured:
            enabled.append("phoenix")
        return enabled

    def validate_enabled(self) -> None:
        self.langsmith.validate()
        self.langfuse.validate()
        self.phoenix.validate()


_tracing_config: TracingConfig | None = None


_TRUTHY_VALUES = {"1", "true", "yes", "on"}


def _env_flag_preferred(*names: str) -> bool:
    """Return the boolean value of the first env var that is present and non-empty."""
    for name in names:
        value = os.environ.get(name)
        if value is not None and value.strip():
            return value.strip().lower() in _TRUTHY_VALUES
    return False


def _env_flag_or_default(name: str, default: bool) -> bool:
    """Return the boolean value of an env var, or a default when it is unset."""
    value = os.environ.get(name)
    if value is not None and value.strip():
        return value.strip().lower() in _TRUTHY_VALUES
    return default


def _first_env_value(*names: str) -> str | None:
    """Return the first non-empty environment value from candidate names."""
    for name in names:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return None


def _comma_separated_env_values(name: str) -> tuple[str, ...]:
    """Parse an env var as ordered, whitespace-trimmed unique values."""
    value = _first_env_value(name)
    if value is None:
        return ()
    return tuple(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))


def _phoenix_trace_parent_mode(enabled: bool) -> Literal["root", "child", "auto"]:
    value = _first_env_value("PHOENIX_TRACE_PARENT_MODE")
    if value is None:
        return "auto"
    if value == "root":
        return "root"
    if value == "child":
        return "child"
    if value == "auto":
        return "auto"
    if enabled:
        raise ValueError("PHOENIX_TRACE_PARENT_MODE must be one of: root, child, auto.")
    return "auto"


def get_tracing_config() -> TracingConfig:
    """Get the current tracing configuration from environment variables."""
    global _tracing_config
    if _tracing_config is not None:
        return _tracing_config
    with _config_lock:
        if _tracing_config is not None:
            return _tracing_config
        phoenix_enabled = _env_flag_preferred("PHOENIX_TRACING")
        _tracing_config = TracingConfig(
            langsmith=LangSmithTracingConfig(
                enabled=_env_flag_preferred("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING"),
                api_key=_first_env_value("LANGSMITH_API_KEY", "LANGCHAIN_API_KEY"),
                project=_first_env_value("LANGSMITH_PROJECT", "LANGCHAIN_PROJECT") or "deer-flow",
                endpoint=_first_env_value("LANGSMITH_ENDPOINT", "LANGCHAIN_ENDPOINT") or "https://api.smith.langchain.com",
            ),
            langfuse=LangfuseTracingConfig(
                enabled=_env_flag_preferred("LANGFUSE_TRACING"),
                public_key=_first_env_value("LANGFUSE_PUBLIC_KEY"),
                secret_key=_first_env_value("LANGFUSE_SECRET_KEY"),
                host=_first_env_value("LANGFUSE_BASE_URL") or "https://cloud.langfuse.com",
            ),
            phoenix=PhoenixTracingConfig(
                enabled=phoenix_enabled,
                collector_endpoint=_first_env_value("PHOENIX_COLLECTOR_ENDPOINT") or "http://localhost:6006",
                api_key=_first_env_value("PHOENIX_API_KEY"),
                project_name=_first_env_value("PHOENIX_PROJECT_NAME") or "deer-flow",
                auto_instrument=_env_flag_or_default("PHOENIX_AUTO_INSTRUMENT", True),
                capture_content=_env_flag_preferred("PHOENIX_CAPTURE_CONTENT"),
                metadata_allowlist=_comma_separated_env_values("PHOENIX_METADATA_ALLOWLIST"),
                trace_parent_mode=_phoenix_trace_parent_mode(phoenix_enabled),
                trace_parent_required=_env_flag_preferred("PHOENIX_TRACE_PARENT_REQUIRED"),
                propagate_baggage=_env_flag_preferred("PHOENIX_PROPAGATE_BAGGAGE"),
            ),
        )
        return _tracing_config


def get_enabled_tracing_providers() -> list[str]:
    """Return the configured tracing providers that are enabled and complete."""
    return get_tracing_config().enabled_providers


def get_explicitly_enabled_tracing_providers() -> list[str]:
    """Return tracing providers explicitly enabled by config, even if incomplete."""
    return get_tracing_config().explicitly_enabled_providers


def validate_enabled_tracing_providers() -> None:
    """Validate that any explicitly enabled providers are fully configured."""
    get_tracing_config().validate_enabled()


def is_tracing_enabled() -> bool:
    """Check if any tracing provider is enabled and fully configured."""
    return get_tracing_config().is_configured


def reset_tracing_config() -> None:
    """Discard the cached :class:`TracingConfig` so the next call rebuilds it.

    Public API so that tests do not have to reach into the private
    ``_tracing_config`` module attribute. A future internal rename would
    silently break callers that mutate the attribute directly.
    """
    global _tracing_config
    with _config_lock:
        _tracing_config = None

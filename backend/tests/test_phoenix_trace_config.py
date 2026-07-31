"""Tests for DeerFlowTraceConfig instance-local safe capture.

Verifies that safe metadata filtering is performed by an explicit
``DeerFlowTraceConfig`` instance rather than by mutating process-level
``OPENINFERENCE_*`` environment variables.
"""

from __future__ import annotations

import json
import os
from unittest.mock import Mock

from openinference.semconv.trace import SpanAttributes

from deerflow.tracing.phoenix import DeerFlowTraceConfig

OPENINFERENCE_HIDE_NAMES = (
    "OPENINFERENCE_HIDE_INPUTS",
    "OPENINFERENCE_HIDE_OUTPUTS",
    "OPENINFERENCE_HIDE_INPUT_MESSAGES",
    "OPENINFERENCE_HIDE_OUTPUT_MESSAGES",
    "OPENINFERENCE_HIDE_PROMPTS",
    "OPENINFERENCE_HIDE_CHOICES",
    "OPENINFERENCE_HIDE_LLM_INVOCATION_PARAMETERS",
    "OPENINFERENCE_HIDE_LLM_TOOLS",
)


def test_safe_config_filters_metadata_without_environment_mutation(monkeypatch):
    before = {name: os.environ.get(name) for name in OPENINFERENCE_HIDE_NAMES}
    config = DeerFlowTraceConfig(
        capture_content=False,
        metadata_allowlist=("request_id", "tenant_id"),
    )

    value = json.dumps(
        {
            "request_id": "r-1",
            "tenant_id": "t-1",
            "private": "secret",
            "langfuse_session_id": "other-provider",
        }
    )
    masked = config.mask(SpanAttributes.METADATA, value)

    assert json.loads(masked) == {"request_id": "r-1", "tenant_id": "t-1"}
    assert {name: os.environ.get(name) for name in OPENINFERENCE_HIDE_NAMES} == before


def test_safe_config_invalid_json_returns_none():
    config = DeerFlowTraceConfig(
        capture_content=False,
        metadata_allowlist=("request_id",),
    )
    assert config.mask(SpanAttributes.METADATA, "not-json") is None


def test_safe_config_top_level_list_returns_none():
    config = DeerFlowTraceConfig(
        capture_content=False,
        metadata_allowlist=("request_id",),
    )
    assert config.mask(SpanAttributes.METADATA, json.dumps([{"request_id": "r-1"}])) is None


def test_safe_config_empty_allowlist_removes_metadata():
    config = DeerFlowTraceConfig(
        capture_content=False,
        metadata_allowlist=(),
    )
    value = json.dumps({"request_id": "r-1"})
    assert config.mask(SpanAttributes.METADATA, value) is None


def test_safe_config_uses_exact_allowlist_keys():
    config = DeerFlowTraceConfig(
        capture_content=False,
        metadata_allowlist=("request",),
    )
    value = json.dumps({"request_id": "r-1", "request": "r-2"})
    masked = config.mask(SpanAttributes.METADATA, value)
    assert json.loads(masked) == {"request": "r-2"}


def test_safe_config_rejects_langfuse_even_if_allowlisted():
    config = DeerFlowTraceConfig(
        capture_content=False,
        metadata_allowlist=("langfuse_session_id",),
    )
    value = json.dumps({"langfuse_session_id": "x"})
    assert config.mask(SpanAttributes.METADATA, value) is None


def test_safe_config_evaluates_callable_value_once():
    config = DeerFlowTraceConfig(
        capture_content=False,
        metadata_allowlist=("request_id",),
    )
    value = Mock(return_value=json.dumps({"request_id": "r-1", "private": "secret"}))
    masked = config.mask(SpanAttributes.METADATA, value)
    assert value.call_count == 1
    assert json.loads(masked) == {"request_id": "r-1"}


def test_full_capture_returns_upstream_metadata_unchanged():
    config = DeerFlowTraceConfig(
        capture_content=True,
        metadata_allowlist=("request_id",),
    )
    value = json.dumps({"request_id": "r-1", "private": "secret"})
    assert config.mask(SpanAttributes.METADATA, value) == value


def test_safe_config_hides_content_attributes():
    config = DeerFlowTraceConfig(
        capture_content=False,
        metadata_allowlist=(),
    )

    assert config.mask(SpanAttributes.INPUT_VALUE, "secret input") == "__REDACTED__"
    assert config.mask(SpanAttributes.OUTPUT_VALUE, "secret output") == "__REDACTED__"
    assert config.mask(SpanAttributes.LLM_INPUT_MESSAGES, json.dumps([{"role": "user", "content": "hi"}])) is None
    assert config.mask(SpanAttributes.LLM_OUTPUT_MESSAGES, json.dumps([{"role": "assistant", "content": "hi"}])) is None
    assert config.mask(SpanAttributes.LLM_PROMPTS, json.dumps(["prompt"])) == "__REDACTED__"
    assert config.mask(SpanAttributes.LLM_INVOCATION_PARAMETERS, json.dumps({"temperature": 0.5})) is None
    assert config.mask(SpanAttributes.LLM_TOOLS, json.dumps([{"name": "tool"}])) is None


def test_full_capture_preserves_content_attributes():
    config = DeerFlowTraceConfig(
        capture_content=True,
        metadata_allowlist=(),
    )

    assert config.mask(SpanAttributes.INPUT_VALUE, "input") == "input"
    assert config.mask(SpanAttributes.OUTPUT_VALUE, "output") == "output"
    assert config.mask(SpanAttributes.LLM_INPUT_MESSAGES, json.dumps([{"role": "user", "content": "hi"}])) == json.dumps([{"role": "user", "content": "hi"}])
    assert config.mask(SpanAttributes.LLM_OUTPUT_MESSAGES, json.dumps([{"role": "assistant", "content": "hi"}])) == json.dumps([{"role": "assistant", "content": "hi"}])
    assert config.mask(SpanAttributes.LLM_PROMPTS, json.dumps(["prompt"])) == json.dumps(["prompt"])
    assert config.mask(SpanAttributes.LLM_INVOCATION_PARAMETERS, json.dumps({"temperature": 0.5})) == json.dumps({"temperature": 0.5})
    assert config.mask(SpanAttributes.LLM_TOOLS, json.dumps([{"name": "tool"}])) == json.dumps([{"name": "tool"}])

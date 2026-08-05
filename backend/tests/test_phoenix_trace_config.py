"""Tests for DeerFlowTraceConfig instance-local safe capture.

Verifies that safe metadata filtering is performed by an explicit
``DeerFlowTraceConfig`` instance rather than by mutating process-level
``OPENINFERENCE_*`` environment variables.
"""

from __future__ import annotations

import json
import os
from unittest.mock import Mock

from openinference.instrumentation import using_attributes
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

OPENINFERENCE_ALL_CONFIG_NAMES = OPENINFERENCE_HIDE_NAMES + (
    "OPENINFERENCE_HIDE_INPUT_IMAGES",
    "OPENINFERENCE_HIDE_INPUT_TEXT",
    "OPENINFERENCE_HIDE_OUTPUT_TEXT",
    "OPENINFERENCE_HIDE_EMBEDDING_VECTORS",
    "OPENINFERENCE_HIDE_EMBEDDINGS_VECTORS",
    "OPENINFERENCE_HIDE_EMBEDDINGS_TEXT",
    "OPENINFERENCE_ENABLE_GENAI_SEMCONV",
    "OPENINFERENCE_BASE64_IMAGE_MAX_LENGTH",
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


def test_safe_config_drops_retrieval_documents():
    config = DeerFlowTraceConfig(capture_content=False, metadata_allowlist=())
    assert config.mask("retrieval.documents.0.document.content", "secret page content") is None
    assert config.mask("retrieval.documents.0.document.metadata", json.dumps({"src": "internal"})) is None


def test_full_capture_preserves_retrieval_documents():
    config = DeerFlowTraceConfig(capture_content=True, metadata_allowlist=())
    assert config.mask("retrieval.documents.0.document.content", "page content") == "page content"
    assert config.mask("retrieval.documents.0.document.metadata", json.dumps({"src": "internal"})) == json.dumps({"src": "internal"})


def test_safe_config_drops_prompt_template_attributes():
    config = DeerFlowTraceConfig(capture_content=False, metadata_allowlist=())
    assert config.mask("llm.prompt_template.template", "Tell me about {topic}") is None
    assert config.mask("llm.prompt_template.variables", json.dumps({"topic": "quantum cats"})) is None


def test_full_capture_preserves_prompt_template_attributes():
    config = DeerFlowTraceConfig(capture_content=True, metadata_allowlist=())
    assert config.mask("llm.prompt_template.template", "Tell me about {topic}") == "Tell me about {topic}"
    assert config.mask("llm.prompt_template.variables", json.dumps({"topic": "quantum cats"})) == json.dumps({"topic": "quantum cats"})


def test_safe_config_drops_top_level_function_call_arguments():
    config = DeerFlowTraceConfig(capture_content=False, metadata_allowlist=())
    value = json.dumps({"name": "get_weather", "arguments": {"city": "Paris"}})
    assert config.mask("llm.function_call", value) is None


def test_full_capture_preserves_top_level_function_call_arguments():
    config = DeerFlowTraceConfig(capture_content=True, metadata_allowlist=())
    value = json.dumps({"name": "get_weather", "arguments": {"city": "Paris"}})
    assert config.mask("llm.function_call", value) == value


def test_session_id_prefers_active_deerflow_context_in_safe_mode():
    config = DeerFlowTraceConfig(capture_content=False, metadata_allowlist=())
    with using_attributes(session_id="deerflow-session"):
        assert config.mask(SpanAttributes.SESSION_ID, "caller-session") == "deerflow-session"


def test_session_id_prefers_active_deerflow_context_in_full_mode():
    config = DeerFlowTraceConfig(capture_content=True, metadata_allowlist=())
    with using_attributes(session_id="deerflow-session"):
        assert config.mask(SpanAttributes.SESSION_ID, "caller-session") == "deerflow-session"


def test_session_id_passes_through_without_deerflow_context():
    config = DeerFlowTraceConfig(capture_content=False, metadata_allowlist=())
    assert config.mask(SpanAttributes.SESSION_ID, "caller-session") == "caller-session"


def test_safe_metadata_context_correlation_wins_over_caller_collision():
    config = DeerFlowTraceConfig(capture_content=False, metadata_allowlist=("request_id",))
    with using_attributes(metadata={"request_id": "deerflow-request"}):
        masked = config.mask(SpanAttributes.METADATA, json.dumps({"request_id": "caller-request", "private": "secret"}))
    assert json.loads(masked) == {"request_id": "deerflow-request"}


def test_full_metadata_context_correlation_wins_over_caller_collision():
    config = DeerFlowTraceConfig(capture_content=True, metadata_allowlist=())
    with using_attributes(metadata={"request_id": "deerflow-request"}):
        masked = config.mask(SpanAttributes.METADATA, json.dumps({"request_id": "caller-request", "private": "kept"}))
    assert json.loads(masked) == {"request_id": "deerflow-request", "private": "kept"}


def test_safe_config_ignores_hostile_openinference_environment(monkeypatch):
    for name in OPENINFERENCE_ALL_CONFIG_NAMES:
        monkeypatch.setenv(name, "false")
    config = DeerFlowTraceConfig(capture_content=False, metadata_allowlist=("request_id",))

    assert config.mask(SpanAttributes.INPUT_VALUE, "secret input") == "__REDACTED__"
    assert config.mask("embedding.embeddings.0.embedding.text", "chunk") == "__REDACTED__"
    assert config.mask("retrieval.documents.0.document.content", "secret page content") is None
    assert config.enable_genai_semconv is False
    assert config.base64_image_max_length == 32_000


def test_full_capture_ignores_hostile_openinference_environment(monkeypatch):
    for name in OPENINFERENCE_ALL_CONFIG_NAMES:
        monkeypatch.setenv(name, "true")
    monkeypatch.setenv("OPENINFERENCE_BASE64_IMAGE_MAX_LENGTH", "0")
    config = DeerFlowTraceConfig(capture_content=True, metadata_allowlist=())

    assert config.mask(SpanAttributes.INPUT_VALUE, "input") == "input"
    assert config.mask("llm.input_messages.0.message.content", "hello") == "hello"
    assert config.mask("embedding.embeddings.0.embedding.text", "chunk") == "chunk"
    assert config.mask("retrieval.documents.0.document.content", "page content") == "page content"
    assert config.enable_genai_semconv is False
    assert config.base64_image_max_length == 32_000

"""Real-exporter regression tests for DeerFlow's safe Phoenix export.

Drives the actual locked ``LangChainInstrumentor`` against an
``InMemorySpanExporter`` to prove that safe mode (``capture_content=False``)
cannot export retriever documents, prompt templates, or function-call
arguments, and that caller metadata cannot forge DeerFlow's trusted
correlation.  ``using_attributes`` simulates the authoritative
``deerflow.run`` boundary context that ``activate_phoenix_root_context``
establishes in production.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from langchain_core.documents import Document
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, Generation, LLMResult
from openinference.instrumentation import using_attributes
from openinference.instrumentation.langchain import LangChainInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from deerflow.tracing.phoenix import DeerFlowTraceConfig

_PROMPT_TEMPLATE_SERIALIZED = {
    "id": ["langchain", "prompts", "prompt", "PromptTemplate"],
    "kwargs": {"template": "Tell me about {topic}", "input_variables": ["topic"]},
}


@pytest.fixture
def export_runtime():
    instrumentor = LangChainInstrumentor()
    try:
        instrumentor.uninstrument()
    except Exception:
        pass
    providers: list[TracerProvider] = []

    def start(*, capture_content: bool, metadata_allowlist: tuple[str, ...] = ("request_id",)):
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        providers.append(provider)
        config = DeerFlowTraceConfig(
            capture_content=capture_content,
            metadata_allowlist=metadata_allowlist,
        )
        instrumentor.instrument(tracer_provider=provider, config=config)
        assert instrumentor.is_instrumented_by_opentelemetry
        return exporter, instrumentor._tracer

    yield start

    instrumentor.uninstrument()
    for provider in providers:
        provider.shutdown()


def _finished(exporter: InMemorySpanExporter, name: str):
    return next(span for span in exporter.get_finished_spans() if span.name == name)


def _drive_retriever(tracer) -> str:
    run_id = uuid4()
    tracer.on_retriever_start({"name": "retriever"}, "query", run_id=run_id, name="retriever")
    tracer.on_retriever_end(
        [Document(page_content="SECRET DOC BODY", metadata={"src": "internal-wiki"})],
        run_id=run_id,
    )
    return "retriever"


def _drive_prompt_template(tracer) -> str:
    run_id = uuid4()
    tracer.on_chain_start(
        _PROMPT_TEMPLATE_SERIALIZED,
        {"topic": "quantum cats"},
        run_id=run_id,
        name="prompt-template",
    )
    tracer.on_chain_end({"output": "Tell me about quantum cats"}, run_id=run_id)
    return "prompt-template"


def _drive_function_call(tracer) -> str:
    run_id = uuid4()
    tracer.on_llm_start({"name": "fc-llm"}, ["hi"], run_id=run_id, name="fc-llm")
    result = LLMResult(
        generations=[
            [
                ChatGeneration(
                    message=AIMessage(
                        content="",
                        additional_kwargs={
                            "function_call": {
                                "name": "get_weather",
                                "arguments": '{"city": "Paris"}',
                            }
                        },
                    )
                ),
                Generation(text="unused"),
            ]
        ]
    )
    tracer.on_llm_end(result, run_id=run_id)
    return "fc-llm"


def _drive_metadata_collision(tracer) -> str:
    run_id = uuid4()
    with using_attributes(
        session_id="deerflow-session",
        user_id="deerflow-user",
        metadata={"request_id": "deerflow-request"},
    ):
        tracer.on_chain_start(
            {"name": "chain"},
            {"input": "x"},
            run_id=run_id,
            name="chain",
            metadata={
                "session_id": "caller-session",
                "conversation_id": "caller-conversation",
                "thread_id": "caller-thread",
                "request_id": "caller-request",
                "private": "caller-private",
            },
        )
        tracer.on_chain_end({"output": "y"}, run_id=run_id)
    return "chain"


def test_safe_export_drops_retriever_document_content(export_runtime):
    exporter, tracer = export_runtime(capture_content=False)
    span = _finished(exporter, _drive_retriever(tracer))
    assert not any("retrieval.documents" in key for key in span.attributes)


def test_full_export_keeps_retriever_document_content(export_runtime):
    exporter, tracer = export_runtime(capture_content=True)
    span = _finished(exporter, _drive_retriever(tracer))
    keys = {key for key in span.attributes if "retrieval.documents" in key}
    assert "retrieval.documents.0.document.content" in keys
    assert "retrieval.documents.0.document.metadata" in keys


def test_safe_export_drops_prompt_template_and_variables(export_runtime):
    exporter, tracer = export_runtime(capture_content=False)
    span = _finished(exporter, _drive_prompt_template(tracer))
    assert not any("llm.prompt_template" in key for key in span.attributes)


def test_full_export_keeps_prompt_template_and_variables(export_runtime):
    exporter, tracer = export_runtime(capture_content=True)
    span = _finished(exporter, _drive_prompt_template(tracer))
    assert span.attributes["llm.prompt_template.template"] == "Tell me about {topic}"
    assert json.loads(span.attributes["llm.prompt_template.variables"]) == {"topic": "quantum cats"}


def test_safe_export_drops_function_call_arguments(export_runtime):
    exporter, tracer = export_runtime(capture_content=False)
    span = _finished(exporter, _drive_function_call(tracer))
    assert "llm.function_call" not in span.attributes
    assert not any("function_call" in key for key in span.attributes)


def test_full_export_keeps_function_call_arguments(export_runtime):
    exporter, tracer = export_runtime(capture_content=True)
    span = _finished(exporter, _drive_function_call(tracer))
    function_call = json.loads(span.attributes["llm.function_call"])
    assert function_call["name"] == "get_weather"
    assert function_call["arguments"] == {"city": "Paris"}


def test_safe_export_caller_metadata_cannot_forge_session_or_correlation(export_runtime):
    exporter, tracer = export_runtime(capture_content=False, metadata_allowlist=("request_id",))
    span = _finished(exporter, _drive_metadata_collision(tracer))

    assert span.attributes["session.id"] == "deerflow-session"
    assert span.attributes["user.id"] == "deerflow-user"
    assert json.loads(span.attributes["metadata"]) == {"request_id": "deerflow-request"}


def test_full_export_caller_metadata_cannot_override_trusted_correlation(export_runtime):
    exporter, tracer = export_runtime(capture_content=True)
    span = _finished(exporter, _drive_metadata_collision(tracer))

    assert span.attributes["session.id"] == "deerflow-session"
    metadata = json.loads(span.attributes["metadata"])
    assert metadata["request_id"] == "deerflow-request"
    assert metadata["private"] == "caller-private"

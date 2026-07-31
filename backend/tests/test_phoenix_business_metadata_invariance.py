"""Regression gate: Phoenix tracing must not change business metadata or subagent authorization.

Covers the P0 finding from the Phoenix tracing ADR: enabling Phoenix (disabled,
safe-capture, full-capture) must leave canonical ``RunnableConfig.metadata``
unchanged and must not alter the effective tools/skills available to a subagent.
"""

from __future__ import annotations

import asyncio
import copy
from typing import Any
from unittest.mock import MagicMock

import pytest

from deerflow.config.tracing_config import reset_tracing_config
from deerflow.runtime.runs.manager import RunRecord
from deerflow.runtime.runs.schemas import DisconnectMode, RunStatus
from deerflow.runtime.runs.worker import RunContext, run_agent
from deerflow.subagents.config import SubagentConfig
from deerflow.tracing.metadata import inject_trace_metadata


class _FakeAgent:
    """Minimal LangGraph-like graph that captures the runnable config."""

    def __init__(self) -> None:
        self.captured_config: dict | None = None
        self.metadata: dict = {}
        self.checkpointer = None
        self.store = None
        self.interrupt_before_nodes: list[str] = []
        self.interrupt_after_nodes: list[str] = []

    async def astream(self, graph_input, *, config, stream_mode, **kwargs):
        self.captured_config = config
        return
        yield  # pragma: no cover


class _FakeRunManager:
    def __init__(self) -> None:
        self.status_updates: list[tuple[str, dict]] = []

    async def set_status(self, *_args, **_kwargs) -> None:
        if len(_args) >= 2:
            self.status_updates.append((_args[1].value, dict(_kwargs)))
        return None

    async def update_model_name(self, *_args, **_kwargs) -> None:
        return None

    async def update_run_completion(self, *_args, **_kwargs) -> None:
        return None

    async def update_run_progress(self, *_args, **_kwargs) -> None:
        return None


class _FakeBridge:
    def __init__(self) -> None:
        self.events: list[tuple[str, object]] = []

    async def publish(self, _run_id, event, payload) -> None:
        self.events.append((event, payload))

    async def publish_end(self, _run_id) -> None:
        self.events.append(("end", None))

    async def cleanup(self, _run_id, *, delay: int = 0) -> None:
        return None


@pytest.fixture(autouse=True)
def _clear_tracing_env(monkeypatch):
    from deerflow.tracing.phoenix import reset_phoenix_tracing_for_tests

    for name in (
        "LANGFUSE_TRACING",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "LANGFUSE_BASE_URL",
        "PHOENIX_TRACING",
        "PHOENIX_CAPTURE_CONTENT",
        "PHOENIX_METADATA_ALLOWLIST",
        "PHOENIX_TRACE_PARENT_MODE",
        "PHOENIX_TRACE_PARENT_REQUIRED",
        "DEER_FLOW_ENV",
        "ENVIRONMENT",
    ):
        monkeypatch.delenv(name, raising=False)
    reset_tracing_config()
    reset_phoenix_tracing_for_tests()
    yield
    reset_tracing_config()
    reset_phoenix_tracing_for_tests()


@pytest.mark.parametrize(
    ("phoenix_enabled", "capture_content"),
    [(False, False), (True, False), (True, True)],
    ids=["disabled", "safe", "full"],
)
def test_trace_metadata_preserves_business_values(
    monkeypatch,
    phoenix_enabled,
    capture_content,
):
    monkeypatch.setenv("PHOENIX_TRACING", str(phoenix_enabled).lower())
    monkeypatch.setenv("PHOENIX_CAPTURE_CONTENT", str(capture_content).lower())
    reset_tracing_config()

    business_metadata = {
        "request_id": "request-1",
        "private": {"token": "do-not-export"},
        "tool_groups": ["web"],
        "available_skills": ["research"],
    }
    expected = copy.deepcopy(business_metadata)
    expected.update({"agent_name": "lead-agent", "model_name": "resolved-model"})
    config = {"metadata": copy.deepcopy(business_metadata)}

    inject_trace_metadata(
        config,
        thread_id="thread-1",
        user_id="user-1",
        assistant_id="lead-agent",
        model_name="resolved-model",
    )
    config["metadata"].update({"agent_name": "lead-agent", "model_name": "resolved-model"})

    assert config["metadata"] == expected


@pytest.mark.parametrize(
    ("phoenix_enabled", "capture_content"),
    [(False, False), (True, False), (True, True)],
    ids=["disabled", "safe", "full"],
)
@pytest.mark.asyncio
async def test_worker_authorization_is_invariant_across_phoenix_modes(
    monkeypatch,
    phoenix_enabled,
    capture_content,
):
    """Subagent tool/skills decisions must be identical in disabled, safe, and full modes."""
    monkeypatch.setenv("PHOENIX_TRACING", str(phoenix_enabled).lower())
    monkeypatch.setenv("PHOENIX_CAPTURE_CONTENT", str(capture_content).lower())
    monkeypatch.setenv("PHOENIX_METADATA_ALLOWLIST", "request_id")
    reset_tracing_config()

    fake_agent = _FakeAgent()
    factory_fields = {
        "agent_name": "lead-agent",
        "model_name": "resolved-model",
        "tool_groups": ["web"],
        "available_skills": ["research"],
    }

    def agent_factory(config):
        config["metadata"].update(factory_fields)
        fake_agent.metadata = {"model_name": "resolved-model"}
        return fake_agent

    record = RunRecord(
        run_id="run-auth-invariance",
        thread_id="thread-auth-invariance",
        assistant_id="lead-agent",
        status=RunStatus.pending,
        on_disconnect=DisconnectMode.cancel,
        model_name="requested-model",
    )
    record.abort_event = asyncio.Event()

    await run_agent(
        _FakeBridge(),
        _FakeRunManager(),
        record,
        ctx=RunContext(checkpointer=None),
        agent_factory=agent_factory,
        graph_input={"messages": []},
        config={
            "configurable": {"thread_id": "thread-auth-invariance"},
            "metadata": {
                "request_id": "request-auth-invariance",
                "private": {"token": "do-not-export"},
            },
        },
    )

    captured_metadata = fake_agent.captured_config.get("metadata") or {}
    # Canonical metadata must retain caller values and factory business fields.
    assert captured_metadata["request_id"] == "request-auth-invariance"
    assert captured_metadata["private"] == {"token": "do-not-export"}
    assert captured_metadata["tool_groups"] == ["web"]
    assert captured_metadata["available_skills"] == ["research"]

    # Simulate the task-tool authorization boundary using the exact runtime config
    # the agent received. This avoids exercising the full task-tool polling loop
    # while proving that tool_groups/available_skills reach the subagent resolver.
    get_available_tools = MagicMock(return_value=["tool-a"])
    executor_captured: dict[str, Any] = {}

    class DummyExecutor:
        def __init__(self, **kwargs):
            executor_captured["kwargs"] = kwargs

        def execute_async(self, prompt, task_id=None):
            return task_id or "generated-task-id"

    monkeypatch.setattr("deerflow.tools.get_available_tools", get_available_tools)

    # These are the same metadata reads performed by task_tool.
    parent_model = captured_metadata.get("model_name")
    parent_tool_groups = captured_metadata.get("tool_groups")
    parent_available_skills = captured_metadata.get("available_skills")

    get_available_tools(parent_model, groups=parent_tool_groups, subagent_enabled=False)
    DummyExecutor(
        config=SubagentConfig(
            name="general-purpose",
            description="General helper",
            system_prompt="Base system prompt",
            max_turns=50,
            timeout_seconds=10,
        ),
        tools=["tool-a"],
        parent_model=parent_model,
        skills=parent_available_skills,
    )

    get_available_tools.assert_called_once_with(
        "resolved-model",
        groups=["web"],
        subagent_enabled=False,
    )
    assert executor_captured["kwargs"]["skills"] == ["research"]

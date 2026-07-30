"""Core behavior tests for task tool orchestration."""

import asyncio
import importlib
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from deerflow.subagents.config import SubagentConfig
from deerflow.subagents.delegation import (
    DelegationParentContext,
    DelegationPolicy,
    DelegationRequest,
    ResolvedDelegation,
    intersect_allowlists,
)

# Use module import so tests can patch the exact symbols referenced inside task_tool().
task_tool_module = importlib.import_module("deerflow.tools.builtins.task_tool")


class FakeSubagentStatus(Enum):
    # Match production enum values so branch comparisons behave identically.
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


def _make_runtime(*, app_config=None) -> SimpleNamespace:
    # Minimal ToolRuntime-like object; task_tool only reads these three attributes.
    context = {"thread_id": "thread-1"}
    if app_config is not None:
        context["app_config"] = app_config
    return SimpleNamespace(
        state={
            "sandbox": {"sandbox_id": "local"},
            "thread_data": {
                "workspace_path": "/tmp/workspace",
                "uploads_path": "/tmp/uploads",
                "outputs_path": "/tmp/outputs",
            },
        },
        context=context,
        config={"metadata": {"model_name": "ark-model", "trace_id": "trace-1"}},
    )


def _make_subagent_config(name: str = "general-purpose") -> SubagentConfig:
    return SubagentConfig(
        name=name,
        description="General helper",
        system_prompt="Base system prompt",
        max_turns=50,
        timeout_seconds=10,
    )


def _make_result(
    status: FakeSubagentStatus,
    *,
    ai_messages: list[dict] | None = None,
    result: str | None = None,
    error: str | None = None,
    token_usage_records: list[dict] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        status=status,
        ai_messages=ai_messages or [],
        result=result,
        error=error,
        token_usage_records=token_usage_records or [],
        usage_reported=False,
    )


def _run_task_tool(*, delegation_policy: DelegationPolicy | None = None, parent_model: str | None = None, **kwargs) -> str:
    """Execute the task tool across LangChain sync/async wrapper variants."""
    policy = delegation_policy or DelegationPolicy(tool_groups=None, available_skills=None)
    task_tool = task_tool_module.build_task_tool(DelegationParentContext(policy=policy, model_name=parent_model))
    coroutine = getattr(task_tool, "coroutine", None)
    if coroutine is not None:
        return asyncio.run(coroutine(**kwargs))
    return task_tool.func(**kwargs)


def _resolve_for_test(parent_policy: DelegationPolicy, request: DelegationRequest) -> ResolvedDelegation:
    return ResolvedDelegation(
        parent_policy=parent_policy,
        request=request,
        effective_skills=intersect_allowlists(parent_policy.available_skills, request.requested_skills),
        tools=(),
        parent_policy_fingerprint="sha256:test-parent",
        delegation_decision_fingerprint="sha256:test-decision",
        tool_catalog_fingerprint="sha256:test-catalog",
    )


@pytest.fixture(autouse=True)
def _resolved_delegation_stub(monkeypatch):
    def resolve(*, parent_policy, request, **_kwargs):
        return _resolve_for_test(parent_policy, request)

    monkeypatch.setattr(task_tool_module, "resolve_delegation", resolve)

    async def inline_to_thread(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(task_tool_module.asyncio, "to_thread", inline_to_thread)


@pytest.fixture(autouse=True)
def _fallback_app_config_stub(monkeypatch):
    """Keep orchestration unit tests independent of a real ``config.yaml``.

    ``_run_task`` falls back to ``get_app_config()`` when the runtime carries
    no app_config; without this stub the tests would implicitly depend on a
    config file existing in the repository root. Tests that exercise the
    production fallback path re-patch ``get_app_config`` themselves.
    """
    stub = SimpleNamespace(token_usage=SimpleNamespace(enabled=False))
    monkeypatch.setattr(task_tool_module, "get_app_config", lambda: stub)


async def _no_sleep(_: float) -> None:
    return None


class _DummyScheduledTask:
    def add_done_callback(self, _callback):
        return None


def test_task_tool_returns_error_for_unknown_subagent(monkeypatch):
    monkeypatch.setattr(task_tool_module, "get_subagent_config", lambda _: None)
    monkeypatch.setattr(task_tool_module, "get_available_subagent_names", lambda: ["general-purpose"])

    result = _run_task_tool(
        runtime=None,
        description="执行任务",
        prompt="do work",
        subagent_type="general-purpose",
        tool_call_id="tc-1",
    )

    assert result == "Error: Unknown subagent type 'general-purpose'. Available: general-purpose"


def test_task_tool_rejects_bash_subagent_when_host_bash_disabled(monkeypatch):
    monkeypatch.setattr(task_tool_module, "get_subagent_config", lambda _: _make_subagent_config())
    monkeypatch.setattr(task_tool_module, "is_host_bash_allowed", lambda: False)

    result = _run_task_tool(
        runtime=_make_runtime(),
        description="执行任务",
        prompt="run commands",
        subagent_type="bash",
        tool_call_id="tc-bash",
    )

    assert result.startswith("Error: Bash subagent is disabled")


def test_task_tool_threads_runtime_app_config_to_subagent_dependencies(monkeypatch):
    app_config = object()
    config = _make_subagent_config(name="bash")
    runtime = _make_runtime(app_config=app_config)
    events = []
    captured = {}

    class DummyExecutor:
        def __init__(self, **kwargs):
            captured["executor_kwargs"] = kwargs

        def execute_async(self, prompt, task_id=None):
            captured["prompt"] = prompt
            return task_id or "generated-task-id"

    def fake_get_available_subagent_names(*, app_config):
        captured["names_app_config"] = app_config
        return ["bash"]

    def fake_get_subagent_config(name, *, app_config):
        captured["config_lookup"] = (name, app_config)
        return config

    def fake_is_host_bash_allowed(config):
        captured["bash_gate_app_config"] = config
        return True

    def fake_resolve_delegation(*, parent_policy, request, app_config, parent_model):
        captured["resolver_kwargs"] = {
            "parent_policy": parent_policy,
            "request": request,
            "app_config": app_config,
            "parent_model": parent_model,
        }
        return _resolve_for_test(parent_policy, request)

    monkeypatch.setattr(task_tool_module, "SubagentStatus", FakeSubagentStatus)
    monkeypatch.setattr(task_tool_module, "SubagentExecutor", DummyExecutor)
    monkeypatch.setattr(task_tool_module, "get_available_subagent_names", fake_get_available_subagent_names)
    monkeypatch.setattr(task_tool_module, "get_subagent_config", fake_get_subagent_config)
    monkeypatch.setattr(task_tool_module, "is_host_bash_allowed", fake_is_host_bash_allowed)
    monkeypatch.setattr(task_tool_module, "resolve_delegation", fake_resolve_delegation)
    monkeypatch.setattr(
        task_tool_module,
        "get_background_task_result",
        lambda _: _make_result(FakeSubagentStatus.COMPLETED, result="done"),
    )
    monkeypatch.setattr(task_tool_module, "get_stream_writer", lambda: events.append)
    monkeypatch.setattr(task_tool_module.asyncio, "sleep", _no_sleep)

    output = _run_task_tool(
        parent_model="bound-parent-model",
        runtime=runtime,
        description="运行命令",
        prompt="inspect files",
        subagent_type="bash",
        tool_call_id="tc-explicit-config",
    )

    assert output == "Task Succeeded. Result: done"
    assert captured["names_app_config"] is app_config
    assert captured["config_lookup"] == ("bash", app_config)
    assert captured["bash_gate_app_config"] is app_config
    assert captured["resolver_kwargs"]["app_config"] is app_config
    # The resolver must receive the build-time bound parent model, never the
    # caller/tracing-visible ``metadata.model_name`` ("ark-model").
    assert captured["resolver_kwargs"]["parent_model"] == "bound-parent-model"
    assert captured["executor_kwargs"]["app_config"] is app_config
    assert captured["executor_kwargs"]["parent_model"] == "bound-parent-model"
    assert captured["executor_kwargs"]["resolved_delegation"] == _resolve_for_test(captured["resolver_kwargs"]["parent_policy"], captured["resolver_kwargs"]["request"])


def test_task_tool_emits_running_and_completed_events(monkeypatch):
    config = _make_subagent_config()
    runtime = _make_runtime()
    events = []
    captured = {}

    class DummyExecutor:
        def __init__(self, **kwargs):
            captured["executor_kwargs"] = kwargs

        def execute_async(self, prompt, task_id=None):
            captured["prompt"] = prompt
            captured["task_id"] = task_id
            return task_id or "generated-task-id"

    # Simulate two polling rounds: first running (with one message), then completed.
    responses = iter(
        [
            _make_result(FakeSubagentStatus.RUNNING, ai_messages=[{"id": "m1", "content": "phase-1"}]),
            _make_result(
                FakeSubagentStatus.COMPLETED,
                ai_messages=[{"id": "m1", "content": "phase-1"}, {"id": "m2", "content": "phase-2"}],
                result="all done",
            ),
        ]
    )

    monkeypatch.setattr(task_tool_module, "SubagentStatus", FakeSubagentStatus)
    monkeypatch.setattr(task_tool_module, "SubagentExecutor", DummyExecutor)
    monkeypatch.setattr(task_tool_module, "get_subagent_config", lambda _: config)

    monkeypatch.setattr(task_tool_module, "get_background_task_result", lambda _: next(responses))
    monkeypatch.setattr(task_tool_module, "get_stream_writer", lambda: events.append)
    monkeypatch.setattr(task_tool_module.asyncio, "sleep", _no_sleep)

    output = _run_task_tool(
        parent_model="bound-parent-model",
        runtime=runtime,
        description="运行子任务",
        prompt="collect diagnostics",
        subagent_type="general-purpose",
        tool_call_id="tc-123",
    )

    assert output == "Task Succeeded. Result: all done"
    assert captured["prompt"] == "collect diagnostics"
    assert captured["task_id"] == "tc-123"
    assert captured["executor_kwargs"]["thread_id"] == "thread-1"
    assert captured["executor_kwargs"]["parent_model"] == "bound-parent-model"
    assert captured["executor_kwargs"]["config"].max_turns == config.max_turns
    # Skills are no longer appended to system_prompt; they are loaded per-session
    # by SubagentExecutor and injected as conversation items (Codex pattern).
    assert captured["executor_kwargs"]["config"].system_prompt == "Base system prompt"

    assert captured["executor_kwargs"]["resolved_delegation"].parent_policy == DelegationPolicy(
        tool_groups=None,
        available_skills=None,
    )

    event_types = [e["type"] for e in events]
    assert event_types == ["task_started", "task_running", "task_running", "task_completed"]
    assert events[-1]["result"] == "all done"


def _install_terminal_task(monkeypatch, *, config, status=FakeSubagentStatus.COMPLETED, result="done", error=None):
    captured = {}
    events = []

    class DummyExecutor:
        def __init__(self, **kwargs):
            captured["executor_kwargs"] = kwargs

        def execute_async(self, prompt, task_id=None):
            return task_id or "generated-task-id"

    monkeypatch.setattr(task_tool_module, "SubagentStatus", FakeSubagentStatus)
    monkeypatch.setattr(task_tool_module, "SubagentExecutor", DummyExecutor)
    monkeypatch.setattr(task_tool_module, "get_subagent_config", lambda _: config)
    monkeypatch.setattr(
        task_tool_module,
        "get_background_task_result",
        lambda _: _make_result(status, result=result, error=error),
    )
    monkeypatch.setattr(task_tool_module, "get_stream_writer", lambda: events.append)
    monkeypatch.setattr(task_tool_module.asyncio, "sleep", _no_sleep)
    return captured, events


def test_task_tool_uses_bound_policy_and_ignores_policy_metadata(monkeypatch):
    config = _make_subagent_config()
    runtime = _make_runtime()
    runtime.config["metadata"].update({"tool_groups": ["bash"], "available_skills": ["attacker-skill"]})
    policy = DelegationPolicy(tool_groups=("web",), available_skills=frozenset({"safe-skill"}))
    captured, _ = _install_terminal_task(monkeypatch, config=config)

    def fake_resolve(*, parent_policy, request, **_kwargs):
        captured["resolver_policy"] = parent_policy
        return _resolve_for_test(parent_policy, request)

    monkeypatch.setattr(task_tool_module, "resolve_delegation", fake_resolve)

    output = _run_task_tool(
        delegation_policy=policy,
        runtime=runtime,
        description="执行任务",
        prompt="file work only",
        subagent_type="general-purpose",
        tool_call_id="tc-groups",
    )

    assert output == "Task Succeeded. Result: done"
    assert captured["resolver_policy"] is policy
    assert captured["executor_kwargs"]["resolved_delegation"].parent_policy is policy


def test_task_tool_factories_create_distinct_policy_bound_tools(monkeypatch):
    config = _make_subagent_config()
    captured, _ = _install_terminal_task(monkeypatch, config=config)
    policies = [
        DelegationPolicy(("web",), frozenset({"research"})),
        DelegationPolicy((), frozenset()),
    ]
    seen = []

    def fake_resolve(*, parent_policy, request, **_kwargs):
        seen.append(parent_policy)
        return _resolve_for_test(parent_policy, request)

    monkeypatch.setattr(task_tool_module, "resolve_delegation", fake_resolve)
    first_tool = task_tool_module.build_task_tool(DelegationParentContext(policy=policies[0]))
    second_tool = task_tool_module.build_task_tool(DelegationParentContext(policy=policies[1]))

    assert first_tool is not second_tool
    assert first_tool.name == second_tool.name == "task"
    for index, policy in enumerate(policies):
        output = _run_task_tool(
            delegation_policy=policy,
            runtime=_make_runtime(),
            description="执行任务",
            prompt="delegated work",
            subagent_type="general-purpose",
            tool_call_id=f"tc-policy-{index}",
        )
        assert output == "Task Succeeded. Result: done"

    assert seen == policies
    assert captured["executor_kwargs"]["resolved_delegation"].parent_policy is policies[-1]


def test_task_tool_passes_bound_parent_model_to_single_resolver(monkeypatch):
    """The resolver receives the build-time bound parent model, not metadata."""
    config = SubagentConfig(
        name="general-purpose",
        description="General helper",
        system_prompt="Base system prompt",
        model="vision-subagent-model",
        max_turns=50,
        timeout_seconds=10,
    )
    runtime = _make_runtime()
    runtime.config["metadata"]["model_name"] = "spoofed-metadata-model"
    captured, _ = _install_terminal_task(monkeypatch, config=config)

    def fake_resolve(*, parent_policy, request, parent_model, **_kwargs):
        captured["parent_model"] = parent_model
        captured["request"] = request
        return _resolve_for_test(parent_policy, request)

    monkeypatch.setattr(task_tool_module, "resolve_delegation", fake_resolve)

    output = _run_task_tool(
        parent_model="bound-parent-model",
        runtime=runtime,
        description="inspect image",
        prompt="inspect the uploaded image",
        subagent_type="general-purpose",
        tool_call_id="tc-issue-2543",
    )

    assert output == "Task Succeeded. Result: done"
    assert captured["parent_model"] == "bound-parent-model"
    assert captured["executor_kwargs"]["parent_model"] == "bound-parent-model"
    assert captured["request"].subagent_type == "general-purpose"


def test_task_tool_missing_metadata_model_name_keeps_bound_parent_model(monkeypatch):
    """A metadata dict without ``model_name`` must not change authorization."""
    config = _make_subagent_config()
    runtime = _make_runtime()
    runtime.config["metadata"].pop("model_name", None)
    captured, _ = _install_terminal_task(monkeypatch, config=config)

    def fake_resolve(*, parent_policy, request, parent_model, **_kwargs):
        captured["parent_model"] = parent_model
        return _resolve_for_test(parent_policy, request)

    monkeypatch.setattr(task_tool_module, "resolve_delegation", fake_resolve)

    output = _run_task_tool(
        parent_model="bound-parent-model",
        runtime=runtime,
        description="执行任务",
        prompt="do work",
        subagent_type="general-purpose",
        tool_call_id="tc-missing-metadata-model",
    )

    assert output == "Task Succeeded. Result: done"
    assert captured["parent_model"] == "bound-parent-model"
    assert captured["executor_kwargs"]["parent_model"] == "bound-parent-model"


def test_task_tool_never_reads_model_name_from_runtime_metadata(monkeypatch):
    """Spoofed metadata must not reach the resolver, executor, or trace inputs."""
    config = _make_subagent_config()
    runtime = _make_runtime()
    runtime.config["metadata"]["model_name"] = "attacker-controlled-model"
    captured, _ = _install_terminal_task(monkeypatch, config=config)

    def fake_resolve(*, parent_policy, request, parent_model, **_kwargs):
        captured["parent_model"] = parent_model
        return _resolve_for_test(parent_policy, request)

    monkeypatch.setattr(task_tool_module, "resolve_delegation", fake_resolve)

    output = _run_task_tool(
        parent_model="trusted-text-model",
        runtime=runtime,
        description="执行任务",
        prompt="do work",
        subagent_type="general-purpose",
        tool_call_id="tc-spoofed-metadata-model",
    )

    assert output == "Task Succeeded. Result: done"
    assert captured["parent_model"] == "trusted-text-model"
    assert captured["executor_kwargs"]["parent_model"] == "trusted-text-model"


def test_task_tool_ignores_parent_skill_metadata(monkeypatch):
    config = _make_subagent_config()
    runtime = _make_runtime()
    runtime.config["metadata"]["available_skills"] = ["attacker-skill"]
    policy = DelegationPolicy(tool_groups=None, available_skills=frozenset({"safe-skill"}))
    captured, _ = _install_terminal_task(monkeypatch, config=config)

    output = _run_task_tool(
        delegation_policy=policy,
        runtime=runtime,
        description="执行任务",
        prompt="use skills",
        subagent_type="general-purpose",
        tool_call_id="tc-skills",
    )

    assert output == "Task Succeeded. Result: done"
    assert captured["executor_kwargs"]["resolved_delegation"].effective_skills == ("safe-skill",)


def test_task_tool_intersects_parent_and_requested_skills(monkeypatch):
    config = SubagentConfig(
        name="general-purpose",
        description="General helper",
        system_prompt="Base system prompt",
        max_turns=50,
        timeout_seconds=10,
        skills=["safe-skill", "other-skill"],
    )
    runtime = _make_runtime()
    runtime.config["metadata"]["available_skills"] = ["other-skill"]
    policy = DelegationPolicy(tool_groups=None, available_skills=frozenset({"safe-skill"}))
    captured, _ = _install_terminal_task(monkeypatch, config=config)

    output = _run_task_tool(
        delegation_policy=policy,
        runtime=runtime,
        description="执行任务",
        prompt="use skills",
        subagent_type="general-purpose",
        tool_call_id="tc-skills-intersection",
    )

    assert output == "Task Succeeded. Result: done"
    assert captured["executor_kwargs"]["resolved_delegation"].effective_skills == ("safe-skill",)


def test_task_tool_unrestricted_policy_is_explicit(monkeypatch):
    config = _make_subagent_config()
    captured, _ = _install_terminal_task(monkeypatch, config=config, result="ok")

    output = _run_task_tool(
        runtime=_make_runtime(),
        description="执行任务",
        prompt="normal work",
        subagent_type="general-purpose",
        tool_call_id="tc-no-groups",
    )

    assert output == "Task Succeeded. Result: ok"
    assert captured["executor_kwargs"]["resolved_delegation"].parent_policy == DelegationPolicy(None, None)


def _install_model_aware_real_resolver(monkeypatch, record: dict) -> None:
    """Swap in the real resolver backed by a model-aware in-memory catalog.

    The fake catalog mirrors production behavior: ``view_image`` is only
    present when the *effective* model supports vision. If the task tool ever
    resumes reading ``metadata.model_name``, a spoofed vision model would make
    ``view_image`` appear in the delegated tool set and these tests fail.
    """
    from langchain_core.tools import tool as lc_tool

    from deerflow.subagents import delegation as delegation_module
    from deerflow.subagents.delegation import resolve_delegation as real_resolve_delegation
    from deerflow.tools.builtins import present_file_tool, view_image_tool
    from deerflow.tools.tools import ToolCatalogEntry, ToolCatalogSnapshot

    @lc_tool
    def safe_tool(query: str) -> str:
        """Safe configured tool."""
        return query

    def fake_load_tool_catalog(**kwargs):
        model_name = kwargs.get("model_name")
        record.setdefault("catalog_model_names", []).append(model_name)
        entries = [
            ToolCatalogEntry(tool=safe_tool, source="configured", configured_group="web"),
            ToolCatalogEntry(tool=present_file_tool, source="builtin", configured_group=None),
        ]
        if model_name == "vision-model":
            entries.append(ToolCatalogEntry(tool=view_image_tool, source="builtin", configured_group=None))
        return ToolCatalogSnapshot(
            entries=tuple(entries),
            known_tool_names=frozenset({"safe_tool", "present_files", "view_image", "task"}),
            known_groups=frozenset({"web"}),
        )

    monkeypatch.setattr(task_tool_module, "resolve_delegation", real_resolve_delegation)
    monkeypatch.setattr(delegation_module, "_load_tool_catalog", fake_load_tool_catalog)
    monkeypatch.setattr(delegation_module, "_load_enabled_skills", lambda _app_config: [])
    monkeypatch.setattr(delegation_module, "get_available_subagent_names", lambda **_kwargs: ["general-purpose"])
    monkeypatch.setattr(delegation_module, "get_subagent_config", lambda *_args, **_kwargs: SimpleNamespace(model="inherit"))


def _tool_names(executor_kwargs: dict) -> list[str]:
    return sorted(tool.name for tool in executor_kwargs["resolved_delegation"].tools)


def test_task_tool_spoofed_metadata_model_name_does_not_change_effective_tools(monkeypatch):
    """bound=text-model + metadata.model_name=vision-model → no vision tools."""
    config = _make_subagent_config()
    record: dict = {}
    _install_model_aware_real_resolver(monkeypatch, record)
    captured, _ = _install_terminal_task(monkeypatch, config=config)

    spoofed_runtime = _make_runtime()
    spoofed_runtime.config["metadata"]["model_name"] = "vision-model"
    output = _run_task_tool(
        parent_model="text-model",
        runtime=spoofed_runtime,
        description="执行任务",
        prompt="do work",
        subagent_type="general-purpose",
        tool_call_id="tc-spoofed-vision",
    )
    assert output == "Task Succeeded. Result: done"
    assert record["catalog_model_names"] == ["text-model"]
    spoofed_tools = _tool_names(captured["executor_kwargs"])
    assert "view_image" not in spoofed_tools
    spoofed_fingerprint = captured["executor_kwargs"]["resolved_delegation"].delegation_decision_fingerprint

    # Control: metadata carries the same text model — outcome must be identical.
    control_runtime = _make_runtime()
    control_runtime.config["metadata"]["model_name"] = "text-model"
    output = _run_task_tool(
        parent_model="text-model",
        runtime=control_runtime,
        description="执行任务",
        prompt="do work",
        subagent_type="general-purpose",
        tool_call_id="tc-control-text",
    )
    assert output == "Task Succeeded. Result: done"
    assert record["catalog_model_names"] == ["text-model", "text-model"]
    assert _tool_names(captured["executor_kwargs"]) == spoofed_tools
    assert captured["executor_kwargs"]["resolved_delegation"].delegation_decision_fingerprint == spoofed_fingerprint


def test_task_tool_missing_metadata_model_name_keeps_bound_vision_tools(monkeypatch):
    """bound=vision-model + metadata 缺失 model_name → vision tools 保留。"""
    config = _make_subagent_config()
    record: dict = {}
    _install_model_aware_real_resolver(monkeypatch, record)
    captured, _ = _install_terminal_task(monkeypatch, config=config)

    missing_runtime = _make_runtime()
    missing_runtime.config["metadata"].pop("model_name", None)
    output = _run_task_tool(
        parent_model="vision-model",
        runtime=missing_runtime,
        description="执行任务",
        prompt="do work",
        subagent_type="general-purpose",
        tool_call_id="tc-missing-vision",
    )
    assert output == "Task Succeeded. Result: done"
    assert record["catalog_model_names"] == ["vision-model"]
    missing_tools = _tool_names(captured["executor_kwargs"])
    assert "view_image" in missing_tools
    missing_fingerprint = captured["executor_kwargs"]["resolved_delegation"].delegation_decision_fingerprint

    # Control: metadata repeats the bound model — outcome must be identical.
    control_runtime = _make_runtime()
    control_runtime.config["metadata"]["model_name"] = "vision-model"
    output = _run_task_tool(
        parent_model="vision-model",
        runtime=control_runtime,
        description="执行任务",
        prompt="do work",
        subagent_type="general-purpose",
        tool_call_id="tc-control-vision",
    )
    assert output == "Task Succeeded. Result: done"
    assert record["catalog_model_names"] == ["vision-model", "vision-model"]
    assert _tool_names(captured["executor_kwargs"]) == missing_tools
    assert captured["executor_kwargs"]["resolved_delegation"].delegation_decision_fingerprint == missing_fingerprint


def _write_skill(tmp_path, name: str, allowed_tools: list[str] | None = None):
    """Create a real on-disk skill so catalog fingerprints can hash its file."""
    from deerflow.skills.types import Skill, SkillCategory

    skill_dir = tmp_path / name
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
    return Skill(
        name=name,
        description=name,
        license=None,
        skill_dir=skill_dir,
        skill_file=skill_dir / "SKILL.md",
        relative_path=Path(name),
        category=SkillCategory.PUBLIC,
        allowed_tools=allowed_tools,
        enabled=True,
    )


def _install_skill_composition_resolver(monkeypatch, skills):
    """Real resolver + in-memory catalog + on-disk skills (no config.yaml)."""
    from langchain_core.tools import tool as lc_tool

    from deerflow.subagents import delegation as delegation_module
    from deerflow.subagents.delegation import resolve_delegation as real_resolve_delegation
    from deerflow.tools.builtins import present_file_tool
    from deerflow.tools.tools import ToolCatalogEntry, ToolCatalogSnapshot

    @lc_tool
    def safe_tool(query: str) -> str:
        """Safe configured tool."""
        return query

    @lc_tool
    def unsafe_tool(query: str) -> str:
        """Unsafe configured tool."""
        return query

    catalog = ToolCatalogSnapshot(
        entries=(
            ToolCatalogEntry(tool=safe_tool, source="configured", configured_group="web"),
            ToolCatalogEntry(tool=unsafe_tool, source="configured", configured_group="web"),
            ToolCatalogEntry(tool=present_file_tool, source="builtin", configured_group=None),
        ),
        known_tool_names=frozenset({"safe_tool", "unsafe_tool", "present_files", "task"}),
        known_groups=frozenset({"web"}),
    )

    monkeypatch.setattr(task_tool_module, "resolve_delegation", real_resolve_delegation)
    monkeypatch.setattr(delegation_module, "_load_tool_catalog", lambda **_kwargs: catalog)
    monkeypatch.setattr(delegation_module, "_load_enabled_skills", lambda _app_config: skills)
    monkeypatch.setattr(delegation_module, "get_available_subagent_names", lambda **_kwargs: ["general-purpose"])
    monkeypatch.setattr(delegation_module, "get_subagent_config", lambda *_args, **_kwargs: SimpleNamespace(model="inherit"))


def _minimal_typed_app_config():
    """Temporary typed AppConfig — never reads a real config.yaml."""
    from deerflow.config.app_config import AppConfig
    from deerflow.config.sandbox_config import SandboxConfig

    return AppConfig(sandbox=SandboxConfig(use="test"))


def test_task_tool_composition_intersects_skills_and_filters_tools_without_mocking_resolver(monkeypatch, tmp_path):
    """End-to-end through the task tool with the real resolver:

    parent skills=["safe"], child skills=["safe","unsafe"],
    metadata.available_skills=["unsafe"] → effective_skills == ("safe",) and
    only the safe skill's allowed tools survive. metadata.available_skills
    must not influence the outcome.
    """
    skills = [
        _write_skill(tmp_path, "safe", allowed_tools=["safe_tool"]),
        _write_skill(tmp_path, "unsafe", allowed_tools=["unsafe_tool"]),
    ]
    _install_skill_composition_resolver(monkeypatch, skills)

    config = SubagentConfig(
        name="general-purpose",
        description="General helper",
        system_prompt="Base system prompt",
        max_turns=50,
        timeout_seconds=10,
        skills=["safe", "unsafe"],
    )
    captured, _ = _install_terminal_task(monkeypatch, config=config)

    policy = DelegationPolicy(tool_groups=None, available_skills=frozenset({"safe"}))
    runtime = _make_runtime(app_config=_minimal_typed_app_config())
    runtime.config["metadata"]["available_skills"] = ["unsafe"]
    # A runtime app_config makes the task tool call the registry with kwargs.
    monkeypatch.setattr(task_tool_module, "get_subagent_config", lambda _name, **_kwargs: config)
    monkeypatch.setattr(task_tool_module, "get_available_subagent_names", lambda **_kwargs: ["general-purpose"])

    output = _run_task_tool(
        delegation_policy=policy,
        parent_model="model-a",
        runtime=runtime,
        description="执行任务",
        prompt="use skills",
        subagent_type="general-purpose",
        tool_call_id="tc-composition-intersection",
    )

    assert output == "Task Succeeded. Result: done"
    resolved = captured["executor_kwargs"]["resolved_delegation"]
    assert resolved.effective_skills == ("safe",)
    # The safe skill declares allowed-tools=["safe_tool"], so skill policy
    # filters every projected tool (including builtins) down to that set.
    assert _tool_names(captured["executor_kwargs"]) == ["safe_tool"]


def test_task_tool_composition_empty_parent_skills_yields_no_effective_skills(monkeypatch, tmp_path):
    """parent available_skills=[] + child requested=["safe"] → effective_skills == ()."""
    skills = [_write_skill(tmp_path, "safe", allowed_tools=["safe_tool"])]
    _install_skill_composition_resolver(monkeypatch, skills)

    config = SubagentConfig(
        name="general-purpose",
        description="General helper",
        system_prompt="Base system prompt",
        max_turns=50,
        timeout_seconds=10,
        skills=["safe"],
    )
    captured, _ = _install_terminal_task(monkeypatch, config=config)

    policy = DelegationPolicy(tool_groups=None, available_skills=frozenset())
    runtime = _make_runtime(app_config=_minimal_typed_app_config())
    # A runtime app_config makes the task tool call the registry with kwargs.
    monkeypatch.setattr(task_tool_module, "get_subagent_config", lambda _name, **_kwargs: config)
    monkeypatch.setattr(task_tool_module, "get_available_subagent_names", lambda **_kwargs: ["general-purpose"])

    output = _run_task_tool(
        delegation_policy=policy,
        parent_model="model-a",
        runtime=runtime,
        description="执行任务",
        prompt="use skills",
        subagent_type="general-purpose",
        tool_call_id="tc-composition-empty-skills",
    )

    assert output == "Task Succeeded. Result: done"
    resolved = captured["executor_kwargs"]["resolved_delegation"]
    assert resolved.effective_skills == ()


def test_task_tool_runtime_none_uses_fallback_app_config(monkeypatch):
    config = _make_subagent_config()
    captured, _ = _install_terminal_task(monkeypatch, config=config, result="ok")
    fallback_app_config = SimpleNamespace(models=[SimpleNamespace(name="default-model")])
    monkeypatch.setattr(task_tool_module, "get_app_config", lambda: fallback_app_config)

    def fake_resolve(*, parent_policy, request, app_config, parent_model):
        captured["resolver_app_config"] = app_config
        captured["parent_model"] = parent_model
        return _resolve_for_test(parent_policy, request)

    monkeypatch.setattr(task_tool_module, "resolve_delegation", fake_resolve)

    output = _run_task_tool(
        runtime=None,
        description="执行任务",
        prompt="no runtime",
        subagent_type="general-purpose",
        tool_call_id="tc-no-runtime",
    )

    assert output == "Task Succeeded. Result: ok"
    assert captured["resolver_app_config"] is fallback_app_config
    assert captured["parent_model"] is None


def test_task_tool_returns_failed_message(monkeypatch):
    config = _make_subagent_config()
    _, events = _install_terminal_task(
        monkeypatch,
        config=config,
        status=FakeSubagentStatus.FAILED,
        result=None,
        error="subagent crashed",
    )

    output = _run_task_tool(
        runtime=_make_runtime(),
        description="执行任务",
        prompt="do fail",
        subagent_type="general-purpose",
        tool_call_id="tc-fail",
    )

    assert output == "Task failed. Error: subagent crashed"
    assert events[-1]["type"] == "task_failed"
    assert events[-1]["error"] == "subagent crashed"


def test_task_tool_returns_timed_out_message(monkeypatch):
    config = _make_subagent_config()
    events = []

    monkeypatch.setattr(task_tool_module, "SubagentStatus", FakeSubagentStatus)
    monkeypatch.setattr(
        task_tool_module,
        "SubagentExecutor",
        type("DummyExecutor", (), {"__init__": lambda self, **kwargs: None, "execute_async": lambda self, prompt, task_id=None: task_id}),
    )
    monkeypatch.setattr(task_tool_module, "get_subagent_config", lambda _: config)

    monkeypatch.setattr(
        task_tool_module,
        "get_background_task_result",
        lambda _: _make_result(FakeSubagentStatus.TIMED_OUT, error="timeout"),
    )
    monkeypatch.setattr(task_tool_module, "get_stream_writer", lambda: events.append)
    monkeypatch.setattr(task_tool_module.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr("deerflow.tools.get_available_tools", lambda **kwargs: [])

    output = _run_task_tool(
        runtime=_make_runtime(),
        description="执行任务",
        prompt="do timeout",
        subagent_type="general-purpose",
        tool_call_id="tc-timeout",
    )

    assert output == "Task timed out. Error: timeout"
    assert events[-1]["type"] == "task_timed_out"
    assert events[-1]["error"] == "timeout"


def test_task_tool_polling_safety_timeout(monkeypatch):
    config = _make_subagent_config()
    # Keep max_poll_count small for test speed: (1 + 60) // 5 = 12
    config.timeout_seconds = 1
    events = []

    monkeypatch.setattr(task_tool_module, "SubagentStatus", FakeSubagentStatus)
    monkeypatch.setattr(
        task_tool_module,
        "SubagentExecutor",
        type("DummyExecutor", (), {"__init__": lambda self, **kwargs: None, "execute_async": lambda self, prompt, task_id=None: task_id}),
    )
    monkeypatch.setattr(task_tool_module, "get_subagent_config", lambda _: config)

    monkeypatch.setattr(
        task_tool_module,
        "get_background_task_result",
        lambda _: _make_result(FakeSubagentStatus.RUNNING, ai_messages=[]),
    )
    monkeypatch.setattr(task_tool_module, "get_stream_writer", lambda: events.append)
    monkeypatch.setattr(task_tool_module.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr("deerflow.tools.get_available_tools", lambda **kwargs: [])

    output = _run_task_tool(
        runtime=_make_runtime(),
        description="执行任务",
        prompt="never finish",
        subagent_type="general-purpose",
        tool_call_id="tc-safety-timeout",
    )

    assert output.startswith("Task polling timed out after 0 minutes")
    assert events[0]["type"] == "task_started"
    assert events[-1]["type"] == "task_timed_out"


def test_cleanup_called_on_completed(monkeypatch):
    """Verify cleanup_background_task is called when task completes."""
    config = _make_subagent_config()
    events = []
    cleanup_calls = []

    monkeypatch.setattr(task_tool_module, "SubagentStatus", FakeSubagentStatus)
    monkeypatch.setattr(
        task_tool_module,
        "SubagentExecutor",
        type("DummyExecutor", (), {"__init__": lambda self, **kwargs: None, "execute_async": lambda self, prompt, task_id=None: task_id}),
    )
    monkeypatch.setattr(task_tool_module, "get_subagent_config", lambda _: config)

    monkeypatch.setattr(
        task_tool_module,
        "get_background_task_result",
        lambda _: _make_result(FakeSubagentStatus.COMPLETED, result="done"),
    )
    monkeypatch.setattr(task_tool_module, "get_stream_writer", lambda: events.append)
    monkeypatch.setattr(task_tool_module.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr("deerflow.tools.get_available_tools", lambda **kwargs: [])
    monkeypatch.setattr(
        task_tool_module,
        "cleanup_background_task",
        lambda task_id: cleanup_calls.append(task_id),
    )

    output = _run_task_tool(
        runtime=_make_runtime(),
        description="执行任务",
        prompt="complete task",
        subagent_type="general-purpose",
        tool_call_id="tc-cleanup-completed",
    )

    assert output == "Task Succeeded. Result: done"
    assert cleanup_calls == ["tc-cleanup-completed"]


def test_cleanup_called_on_failed(monkeypatch):
    """Verify cleanup_background_task is called when task fails."""
    config = _make_subagent_config()
    events = []
    cleanup_calls = []

    monkeypatch.setattr(task_tool_module, "SubagentStatus", FakeSubagentStatus)
    monkeypatch.setattr(
        task_tool_module,
        "SubagentExecutor",
        type("DummyExecutor", (), {"__init__": lambda self, **kwargs: None, "execute_async": lambda self, prompt, task_id=None: task_id}),
    )
    monkeypatch.setattr(task_tool_module, "get_subagent_config", lambda _: config)

    monkeypatch.setattr(
        task_tool_module,
        "get_background_task_result",
        lambda _: _make_result(FakeSubagentStatus.FAILED, error="error"),
    )
    monkeypatch.setattr(task_tool_module, "get_stream_writer", lambda: events.append)
    monkeypatch.setattr(task_tool_module.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr("deerflow.tools.get_available_tools", lambda **kwargs: [])
    monkeypatch.setattr(
        task_tool_module,
        "cleanup_background_task",
        lambda task_id: cleanup_calls.append(task_id),
    )

    output = _run_task_tool(
        runtime=_make_runtime(),
        description="执行任务",
        prompt="fail task",
        subagent_type="general-purpose",
        tool_call_id="tc-cleanup-failed",
    )

    assert output == "Task failed. Error: error"
    assert cleanup_calls == ["tc-cleanup-failed"]


def test_cleanup_called_on_timed_out(monkeypatch):
    """Verify cleanup_background_task is called when task times out."""
    config = _make_subagent_config()
    events = []
    cleanup_calls = []

    monkeypatch.setattr(task_tool_module, "SubagentStatus", FakeSubagentStatus)
    monkeypatch.setattr(
        task_tool_module,
        "SubagentExecutor",
        type("DummyExecutor", (), {"__init__": lambda self, **kwargs: None, "execute_async": lambda self, prompt, task_id=None: task_id}),
    )
    monkeypatch.setattr(task_tool_module, "get_subagent_config", lambda _: config)

    monkeypatch.setattr(
        task_tool_module,
        "get_background_task_result",
        lambda _: _make_result(FakeSubagentStatus.TIMED_OUT, error="timeout"),
    )
    monkeypatch.setattr(task_tool_module, "get_stream_writer", lambda: events.append)
    monkeypatch.setattr(task_tool_module.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr("deerflow.tools.get_available_tools", lambda **kwargs: [])
    monkeypatch.setattr(
        task_tool_module,
        "cleanup_background_task",
        lambda task_id: cleanup_calls.append(task_id),
    )

    output = _run_task_tool(
        runtime=_make_runtime(),
        description="执行任务",
        prompt="timeout task",
        subagent_type="general-purpose",
        tool_call_id="tc-cleanup-timedout",
    )

    assert output == "Task timed out. Error: timeout"
    assert cleanup_calls == ["tc-cleanup-timedout"]


def test_cleanup_not_called_on_polling_safety_timeout(monkeypatch):
    """Verify cleanup_background_task is NOT called directly on polling safety timeout.

    The task is still RUNNING so it cannot be safely removed yet. Instead,
    cooperative cancellation is requested and a deferred cleanup is scheduled.
    """
    config = _make_subagent_config()
    # Keep max_poll_count small for test speed: (1 + 60) // 5 = 12
    config.timeout_seconds = 1
    events = []
    cleanup_calls = []
    cancel_requests = []
    scheduled_cleanups = []

    class DummyCleanupTask:
        def add_done_callback(self, _callback):
            return None

    def fake_create_task(coro):
        scheduled_cleanups.append(coro)
        coro.close()
        return DummyCleanupTask()

    monkeypatch.setattr(task_tool_module, "SubagentStatus", FakeSubagentStatus)
    monkeypatch.setattr(
        task_tool_module,
        "SubagentExecutor",
        type("DummyExecutor", (), {"__init__": lambda self, **kwargs: None, "execute_async": lambda self, prompt, task_id=None: task_id}),
    )
    monkeypatch.setattr(task_tool_module, "get_subagent_config", lambda _: config)

    monkeypatch.setattr(
        task_tool_module,
        "get_background_task_result",
        lambda _: _make_result(FakeSubagentStatus.RUNNING, ai_messages=[]),
    )
    monkeypatch.setattr(task_tool_module, "get_stream_writer", lambda: events.append)
    monkeypatch.setattr(task_tool_module.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(task_tool_module.asyncio, "create_task", fake_create_task)
    monkeypatch.setattr("deerflow.tools.get_available_tools", lambda **kwargs: [])
    monkeypatch.setattr(
        task_tool_module,
        "cleanup_background_task",
        lambda task_id: cleanup_calls.append(task_id),
    )
    monkeypatch.setattr(
        task_tool_module,
        "request_cancel_background_task",
        lambda task_id: cancel_requests.append(task_id),
    )

    output = _run_task_tool(
        runtime=_make_runtime(),
        description="执行任务",
        prompt="never finish",
        subagent_type="general-purpose",
        tool_call_id="tc-no-cleanup-safety-timeout",
    )

    assert output.startswith("Task polling timed out after 0 minutes")
    # cleanup_background_task must NOT be called directly (task is still RUNNING)
    assert cleanup_calls == []
    # cooperative cancellation must be requested
    assert cancel_requests == ["tc-no-cleanup-safety-timeout"]
    # a deferred cleanup coroutine must be scheduled
    assert len(scheduled_cleanups) == 1


def test_cleanup_scheduled_on_cancellation(monkeypatch):
    """Verify cancellation handler synchronously cleans up after shielded wait."""
    config = _make_subagent_config()
    events = []
    cleanup_calls = []
    poll_count = 0

    def get_result(_: str):
        nonlocal poll_count
        poll_count += 1
        # Main loop polls RUNNING twice, then shielded wait gets COMPLETED
        if poll_count <= 2:
            return _make_result(FakeSubagentStatus.RUNNING, ai_messages=[])
        return _make_result(FakeSubagentStatus.COMPLETED, result="done")

    sleep_count = 0

    async def cancel_on_second_sleep(_: float) -> None:
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(task_tool_module, "SubagentStatus", FakeSubagentStatus)
    monkeypatch.setattr(
        task_tool_module,
        "SubagentExecutor",
        type("DummyExecutor", (), {"__init__": lambda self, **kwargs: None, "execute_async": lambda self, prompt, task_id=None: task_id}),
    )
    monkeypatch.setattr(task_tool_module, "get_subagent_config", lambda _: config)

    monkeypatch.setattr(task_tool_module, "get_background_task_result", get_result)
    monkeypatch.setattr(task_tool_module, "get_stream_writer", lambda: events.append)
    monkeypatch.setattr(task_tool_module.asyncio, "sleep", cancel_on_second_sleep)
    monkeypatch.setattr("deerflow.tools.get_available_tools", lambda **kwargs: [])
    monkeypatch.setattr(
        task_tool_module,
        "cleanup_background_task",
        lambda task_id: cleanup_calls.append(task_id),
    )

    with pytest.raises(asyncio.CancelledError):
        _run_task_tool(
            runtime=_make_runtime(),
            description="执行任务",
            prompt="cancel task",
            subagent_type="general-purpose",
            tool_call_id="tc-cancelled-cleanup",
        )

    # Cleanup happens synchronously within the cancellation handler
    assert cleanup_calls == ["tc-cancelled-cleanup"]


def test_cancelled_cleanup_stops_after_timeout(monkeypatch):
    """Verify cancellation handler survives a shielded-wait timeout gracefully.

    When the subagent never reaches a terminal state, the shielded wait times
    out (or is interrupted), the handler reports whatever usage it can, calls
    cleanup (which is a no-op for non-terminal tasks), and re-raises.
    """
    config = _make_subagent_config()
    events = []
    report_calls = []
    cleanup_calls = []
    scheduled_cleanups = []

    # Always return RUNNING — subagent never finishes
    monkeypatch.setattr(
        task_tool_module,
        "get_background_task_result",
        lambda _: _make_result(FakeSubagentStatus.RUNNING, ai_messages=[]),
    )

    async def cancel_on_first_sleep(_: float) -> None:
        raise asyncio.CancelledError

    def fake_report_subagent_usage(runtime, result):
        report_calls.append((runtime, result))

    class DummyCleanupTask:
        def __init__(self, coro):
            self.coro = coro

        def add_done_callback(self, callback):
            self.callback = callback

    def fake_create_task(coro):
        scheduled_cleanups.append(coro)
        coro.close()
        return DummyCleanupTask(coro)

    monkeypatch.setattr(task_tool_module, "SubagentStatus", FakeSubagentStatus)
    monkeypatch.setattr(
        task_tool_module,
        "SubagentExecutor",
        type("DummyExecutor", (), {"__init__": lambda self, **kwargs: None, "execute_async": lambda self, prompt, task_id=None: task_id}),
    )
    monkeypatch.setattr(task_tool_module, "get_subagent_config", lambda _: config)
    monkeypatch.setattr(task_tool_module, "get_stream_writer", lambda: events.append)
    monkeypatch.setattr(task_tool_module.asyncio, "sleep", cancel_on_first_sleep)
    monkeypatch.setattr(task_tool_module.asyncio, "create_task", fake_create_task)
    monkeypatch.setattr(task_tool_module, "_report_subagent_usage", fake_report_subagent_usage)
    monkeypatch.setattr("deerflow.tools.get_available_tools", lambda **kwargs: [])
    monkeypatch.setattr(
        task_tool_module,
        "cleanup_background_task",
        lambda task_id: cleanup_calls.append(task_id),
    )

    with pytest.raises(asyncio.CancelledError):
        _run_task_tool(
            runtime=_make_runtime(),
            description="执行任务",
            prompt="cancel task",
            subagent_type="general-purpose",
            tool_call_id="tc-cancelled-timeout",
        )

    # Non-terminal tasks cannot be cleaned immediately; a deferred cleanup
    # keeps polling after the parent cancellation path exits.
    assert cleanup_calls == []
    assert len(scheduled_cleanups) == 1
    # _report_subagent_usage is called (but skips because result has no records)
    assert len(report_calls) == 1


def test_cancellation_wait_uses_subagent_polling_budget(monkeypatch):
    """Cancelled parent waits on the existing subagent polling budget, not a fixed timeout."""
    config = _make_subagent_config()
    events = []
    report_calls = []
    cleanup_calls = []
    sleep_count = 0
    result_polls = 0
    terminal_result = _make_result(FakeSubagentStatus.COMPLETED, result="done")

    def get_result(_: str):
        nonlocal result_polls
        result_polls += 1
        if result_polls < 5:
            return _make_result(FakeSubagentStatus.RUNNING, ai_messages=[])
        return terminal_result

    async def cancel_then_continue(_: float) -> None:
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count == 1:
            raise asyncio.CancelledError

    def fake_report_subagent_usage(runtime, result):
        report_calls.append((runtime, result))

    async def fail_on_fixed_timeout(awaitable, *, timeout=None):
        raise AssertionError(f"cancellation wait should not use fixed timeout={timeout}")

    monkeypatch.setattr(task_tool_module, "SubagentStatus", FakeSubagentStatus)
    monkeypatch.setattr(
        task_tool_module,
        "SubagentExecutor",
        type("DummyExecutor", (), {"__init__": lambda self, **kwargs: None, "execute_async": lambda self, prompt, task_id=None: task_id}),
    )
    monkeypatch.setattr(task_tool_module, "get_subagent_config", lambda _: config)
    monkeypatch.setattr(task_tool_module, "get_background_task_result", get_result)
    monkeypatch.setattr(task_tool_module, "get_stream_writer", lambda: events.append)
    monkeypatch.setattr(task_tool_module.asyncio, "sleep", cancel_then_continue)
    monkeypatch.setattr(task_tool_module.asyncio, "wait_for", fail_on_fixed_timeout)
    monkeypatch.setattr(task_tool_module, "_report_subagent_usage", fake_report_subagent_usage)
    monkeypatch.setattr("deerflow.tools.get_available_tools", lambda **kwargs: [])
    monkeypatch.setattr(
        task_tool_module,
        "cleanup_background_task",
        lambda task_id: cleanup_calls.append(task_id),
    )

    with pytest.raises(asyncio.CancelledError):
        _run_task_tool(
            runtime=_make_runtime(),
            description="执行任务",
            prompt="cancel task",
            subagent_type="general-purpose",
            tool_call_id="tc-cancel-budget",
        )

    assert report_calls == [(_make_runtime(), terminal_result)]
    assert cleanup_calls == ["tc-cancel-budget"]


def test_cancellation_calls_request_cancel(monkeypatch):
    """Verify CancelledError path calls request_cancel_background_task(task_id)."""
    config = _make_subagent_config()
    events = []
    cancel_requests = []

    async def cancel_on_first_sleep(_: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(task_tool_module, "SubagentStatus", FakeSubagentStatus)
    monkeypatch.setattr(
        task_tool_module,
        "SubagentExecutor",
        type("DummyExecutor", (), {"__init__": lambda self, **kwargs: None, "execute_async": lambda self, prompt, task_id=None: task_id}),
    )
    monkeypatch.setattr(task_tool_module, "get_subagent_config", lambda _: config)

    monkeypatch.setattr(
        task_tool_module,
        "get_background_task_result",
        lambda _: _make_result(FakeSubagentStatus.RUNNING, ai_messages=[]),
    )
    monkeypatch.setattr(task_tool_module, "get_stream_writer", lambda: events.append)
    monkeypatch.setattr(task_tool_module.asyncio, "sleep", cancel_on_first_sleep)
    monkeypatch.setattr("deerflow.tools.get_available_tools", lambda **kwargs: [])
    monkeypatch.setattr(
        task_tool_module,
        "request_cancel_background_task",
        lambda task_id: cancel_requests.append(task_id),
    )
    monkeypatch.setattr(
        task_tool_module,
        "cleanup_background_task",
        lambda task_id: None,
    )

    with pytest.raises(asyncio.CancelledError):
        _run_task_tool(
            runtime=_make_runtime(),
            description="执行任务",
            prompt="cancel me",
            subagent_type="general-purpose",
            tool_call_id="tc-cancel-request",
        )

    assert cancel_requests == ["tc-cancel-request"]


def test_task_tool_returns_cancelled_message(monkeypatch):
    """Verify polling a CANCELLED result emits task_cancelled event and returns message."""
    config = _make_subagent_config()
    events = []
    cleanup_calls = []

    # First poll: RUNNING, second poll: CANCELLED
    responses = iter(
        [
            _make_result(FakeSubagentStatus.RUNNING, ai_messages=[]),
            _make_result(FakeSubagentStatus.CANCELLED, error="Cancelled by user"),
        ]
    )

    monkeypatch.setattr(task_tool_module, "SubagentStatus", FakeSubagentStatus)
    monkeypatch.setattr(
        task_tool_module,
        "SubagentExecutor",
        type("DummyExecutor", (), {"__init__": lambda self, **kwargs: None, "execute_async": lambda self, prompt, task_id=None: task_id}),
    )
    monkeypatch.setattr(task_tool_module, "get_subagent_config", lambda _: config)

    monkeypatch.setattr(task_tool_module, "get_background_task_result", lambda _: next(responses))
    monkeypatch.setattr(task_tool_module, "get_stream_writer", lambda: events.append)
    monkeypatch.setattr(task_tool_module.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr("deerflow.tools.get_available_tools", lambda **kwargs: [])
    monkeypatch.setattr(
        task_tool_module,
        "cleanup_background_task",
        lambda task_id: cleanup_calls.append(task_id),
    )

    output = _run_task_tool(
        runtime=_make_runtime(),
        description="执行任务",
        prompt="some task",
        subagent_type="general-purpose",
        tool_call_id="tc-poll-cancelled",
    )

    assert output == "Task cancelled by user."
    assert any(e.get("type") == "task_cancelled" for e in events)
    assert cleanup_calls == ["tc-poll-cancelled"]


def test_cancellation_reports_subagent_usage(monkeypatch):
    """Verify cancellation handler waits (shielded) for subagent terminal state,
    then reports the final token usage before re-raising CancelledError.

    The report must happen synchronously within the cancellation handler so
    the parent worker's finally block sees the updated journal totals.
    """
    config = _make_subagent_config()
    events = []
    report_calls = []
    cleanup_calls = []

    # Terminal result with token usage collected after cancellation processing
    cancel_result = _make_result(FakeSubagentStatus.CANCELLED, error="Cancelled by user")
    cancel_result.token_usage_records = [{"source_run_id": "sub-run-1", "caller": "subagent:gp", "input_tokens": 50, "output_tokens": 25, "total_tokens": 75}]
    cancel_result.usage_reported = False

    poll_count = 0

    def get_result(_: str):
        nonlocal poll_count
        poll_count += 1
        # Main loop polls 3 times (RUNNING each time to keep looping)
        if poll_count <= 3:
            running = _make_result(FakeSubagentStatus.RUNNING, ai_messages=[])
            running.token_usage_records = []
            running.usage_reported = False
            return running
        # Shielded wait poll gets the terminal result
        return cancel_result

    sleep_count = 0

    async def cancel_on_third_sleep(_: float) -> None:
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count == 3:
            raise asyncio.CancelledError

    def fake_report_subagent_usage(runtime, result):
        report_calls.append((runtime, result))

    monkeypatch.setattr(task_tool_module, "SubagentStatus", FakeSubagentStatus)
    monkeypatch.setattr(
        task_tool_module,
        "SubagentExecutor",
        type("DummyExecutor", (), {"__init__": lambda self, **kwargs: None, "execute_async": lambda self, prompt, task_id=None: task_id}),
    )
    monkeypatch.setattr(task_tool_module, "get_subagent_config", lambda _: config)
    monkeypatch.setattr(task_tool_module, "get_background_task_result", get_result)
    monkeypatch.setattr(task_tool_module, "get_stream_writer", lambda: events.append)
    monkeypatch.setattr(task_tool_module.asyncio, "sleep", cancel_on_third_sleep)
    monkeypatch.setattr(task_tool_module, "_report_subagent_usage", fake_report_subagent_usage)
    monkeypatch.setattr("deerflow.tools.get_available_tools", lambda **kwargs: [])
    monkeypatch.setattr(task_tool_module, "request_cancel_background_task", lambda _: None)
    monkeypatch.setattr(
        task_tool_module,
        "cleanup_background_task",
        lambda task_id: cleanup_calls.append(task_id),
    )

    with pytest.raises(asyncio.CancelledError):
        _run_task_tool(
            runtime=_make_runtime(),
            description="执行任务",
            prompt="cancel me",
            subagent_type="general-purpose",
            tool_call_id="tc-cancel-report",
        )

    # _report_subagent_usage is called synchronously within the cancellation
    # handler (after the shielded wait), before CancelledError is re-raised.
    assert len(report_calls) == 1
    assert report_calls[0][1] is cancel_result
    assert cleanup_calls == ["tc-cancel-report"]


@pytest.mark.parametrize(
    "status, expected_type",
    [
        (FakeSubagentStatus.COMPLETED, "task_completed"),
        (FakeSubagentStatus.FAILED, "task_failed"),
        (FakeSubagentStatus.CANCELLED, "task_cancelled"),
        (FakeSubagentStatus.TIMED_OUT, "task_timed_out"),
    ],
)
def test_terminal_events_include_usage(monkeypatch, status, expected_type):
    """Terminal task events include a usage summary from token_usage_records."""
    config = _make_subagent_config()
    runtime = _make_runtime()
    events = []

    records = [
        {"source_run_id": "r1", "caller": "subagent:general-purpose", "input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
        {"source_run_id": "r2", "caller": "subagent:general-purpose", "input_tokens": 200, "output_tokens": 80, "total_tokens": 280},
    ]
    result = _make_result(status, result="ok" if status == FakeSubagentStatus.COMPLETED else None, error="err" if status != FakeSubagentStatus.COMPLETED else None, token_usage_records=records)

    monkeypatch.setattr(task_tool_module, "SubagentStatus", FakeSubagentStatus)
    monkeypatch.setattr(task_tool_module, "get_subagent_config", lambda _: config)
    monkeypatch.setattr(task_tool_module, "get_background_task_result", lambda _: result)
    monkeypatch.setattr(task_tool_module, "get_stream_writer", lambda: events.append)
    monkeypatch.setattr(task_tool_module.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(task_tool_module, "_report_subagent_usage", lambda *_: None)
    monkeypatch.setattr(task_tool_module, "cleanup_background_task", lambda _: None)
    monkeypatch.setattr("deerflow.tools.get_available_tools", MagicMock(return_value=[]))

    _run_task_tool(
        runtime=runtime,
        description="test",
        prompt="do work",
        subagent_type="general-purpose",
        tool_call_id="tc-usage",
    )

    terminal_events = [e for e in events if e["type"] == expected_type]
    assert len(terminal_events) == 1
    assert terminal_events[0]["usage"] == {
        "input_tokens": 300,
        "output_tokens": 130,
        "total_tokens": 430,
    }


def test_terminal_event_usage_none_when_no_records(monkeypatch):
    """Terminal event has usage=None when token_usage_records is empty."""
    config = _make_subagent_config()
    runtime = _make_runtime()
    events = []

    result = _make_result(FakeSubagentStatus.COMPLETED, result="done", token_usage_records=[])

    monkeypatch.setattr(task_tool_module, "SubagentStatus", FakeSubagentStatus)
    monkeypatch.setattr(task_tool_module, "get_subagent_config", lambda _: config)
    monkeypatch.setattr(task_tool_module, "get_background_task_result", lambda _: result)
    monkeypatch.setattr(task_tool_module, "get_stream_writer", lambda: events.append)
    monkeypatch.setattr(task_tool_module.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(task_tool_module, "_report_subagent_usage", lambda *_: None)
    monkeypatch.setattr(task_tool_module, "cleanup_background_task", lambda _: None)
    monkeypatch.setattr("deerflow.tools.get_available_tools", MagicMock(return_value=[]))

    _run_task_tool(
        runtime=runtime,
        description="test",
        prompt="do work",
        subagent_type="general-purpose",
        tool_call_id="tc-no-records",
    )

    completed = [e for e in events if e["type"] == "task_completed"]
    assert len(completed) == 1
    assert completed[0]["usage"] is None


def test_subagent_usage_cache_is_skipped_when_config_file_is_missing(monkeypatch):
    monkeypatch.setattr(
        task_tool_module,
        "get_app_config",
        MagicMock(side_effect=FileNotFoundError("missing config")),
    )

    assert task_tool_module._token_usage_cache_enabled(None) is False


def test_subagent_usage_cache_is_skipped_when_token_usage_is_disabled(monkeypatch):
    config = _make_subagent_config()
    app_config = SimpleNamespace(token_usage=SimpleNamespace(enabled=False))
    runtime = _make_runtime(app_config=app_config)
    records = [{"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}]
    result = _make_result(FakeSubagentStatus.COMPLETED, result="done", token_usage_records=records)

    task_tool_module._subagent_usage_cache.clear()
    monkeypatch.setattr(task_tool_module, "SubagentStatus", FakeSubagentStatus)
    monkeypatch.setattr(task_tool_module, "get_available_subagent_names", lambda *, app_config: ["general-purpose"])
    monkeypatch.setattr(task_tool_module, "get_subagent_config", lambda _, *, app_config: config)
    monkeypatch.setattr(
        task_tool_module,
        "SubagentExecutor",
        type("DummyExecutor", (), {"__init__": lambda self, **kwargs: None, "execute_async": lambda self, prompt, task_id=None: task_id}),
    )
    monkeypatch.setattr(task_tool_module, "get_background_task_result", lambda _: result)
    monkeypatch.setattr(task_tool_module, "get_stream_writer", lambda: lambda _: None)
    monkeypatch.setattr(task_tool_module, "_report_subagent_usage", lambda *_: None)
    monkeypatch.setattr(task_tool_module, "cleanup_background_task", lambda _: None)
    monkeypatch.setattr("deerflow.tools.get_available_tools", MagicMock(return_value=[]))

    _run_task_tool(
        runtime=runtime,
        description="test",
        prompt="do work",
        subagent_type="general-purpose",
        tool_call_id="tc-disabled-cache",
    )

    assert task_tool_module.pop_cached_subagent_usage("tc-disabled-cache") is None


@pytest.mark.parametrize(
    ("phoenix_enabled", "expected_capture"),
    [
        (True, "phoenix"),
        (False, "ambient"),
    ],
)
def test_task_tool_selects_provider_aware_otel_carrier(
    monkeypatch,
    phoenix_enabled,
    expected_capture,
):
    from deerflow.tracing import TraceContextCarrier

    config = _make_subagent_config()
    runtime = _make_runtime(app_config=SimpleNamespace(token_usage=SimpleNamespace(enabled=False)))
    captured = {}
    carriers = {
        "ambient": TraceContextCarrier(
            traceparent="00-0123456789abcdef0123456789abcdef-0123456789abcdef-01",
            tracestate="ambient=value",
            baggage="ambient=true",
        ),
        "phoenix": TraceContextCarrier(
            traceparent="00-fedcba9876543210fedcba9876543210-fedcba9876543210-01",
            tracestate="phoenix=value",
            baggage="phoenix=true",
        ),
    }

    class DummyExecutor:
        def __init__(self, **kwargs):
            captured["executor_kwargs"] = kwargs

        def execute_async(self, prompt, task_id=None):
            return task_id or "generated-task-id"

    def fake_capture_current_trace_context(*, include_baggage):
        captured.setdefault("capture_calls", []).append(("ambient", include_baggage))
        return carriers["ambient"]

    def fake_capture_current_phoenix_trace_context(*, include_baggage):
        captured.setdefault("capture_calls", []).append(("phoenix", include_baggage))
        return carriers["phoenix"]

    monkeypatch.setattr(task_tool_module, "SubagentStatus", FakeSubagentStatus)
    monkeypatch.setattr(task_tool_module, "get_available_subagent_names", lambda *, app_config: ["general-purpose"])
    monkeypatch.setattr(task_tool_module, "get_subagent_config", lambda _, *, app_config: config)
    monkeypatch.setattr(task_tool_module, "SubagentExecutor", DummyExecutor)
    monkeypatch.setattr(task_tool_module, "capture_current_trace_context", fake_capture_current_trace_context)
    monkeypatch.setattr(
        task_tool_module,
        "capture_current_phoenix_trace_context",
        fake_capture_current_phoenix_trace_context,
        raising=False,
    )
    monkeypatch.setattr(
        task_tool_module,
        "get_tracing_config",
        lambda: SimpleNamespace(
            phoenix=SimpleNamespace(
                enabled=phoenix_enabled,
                propagate_baggage=True,
            )
        ),
    )
    monkeypatch.setattr(
        task_tool_module,
        "get_background_task_result",
        lambda _: _make_result(FakeSubagentStatus.COMPLETED, result="done"),
    )
    monkeypatch.setattr(task_tool_module, "get_stream_writer", lambda: lambda _: None)
    monkeypatch.setattr(task_tool_module.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(task_tool_module, "_report_subagent_usage", lambda *_: None)
    monkeypatch.setattr(task_tool_module, "cleanup_background_task", lambda _: None)
    monkeypatch.setattr("deerflow.tools.get_available_tools", MagicMock(return_value=[]))

    output = _run_task_tool(
        runtime=runtime,
        description="test",
        prompt="do work",
        subagent_type="general-purpose",
        tool_call_id="tc-otel",
    )

    assert output == "Task Succeeded. Result: done"
    assert captured["capture_calls"] == [(expected_capture, True)]
    assert captured["executor_kwargs"]["otel_trace_context"] == carriers[expected_capture]


def test_subagent_usage_cache_is_cleared_when_polling_raises(monkeypatch):
    config = _make_subagent_config()
    app_config = SimpleNamespace(token_usage=SimpleNamespace(enabled=True))
    runtime = _make_runtime(app_config=app_config)

    task_tool_module._subagent_usage_cache["tc-error"] = {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
    monkeypatch.setattr(task_tool_module, "SubagentStatus", FakeSubagentStatus)
    monkeypatch.setattr(task_tool_module, "get_available_subagent_names", lambda *, app_config: ["general-purpose"])
    monkeypatch.setattr(task_tool_module, "get_subagent_config", lambda _, *, app_config: config)
    monkeypatch.setattr(
        task_tool_module,
        "SubagentExecutor",
        type("DummyExecutor", (), {"__init__": lambda self, **kwargs: None, "execute_async": lambda self, prompt, task_id=None: task_id}),
    )
    monkeypatch.setattr(task_tool_module, "get_background_task_result", MagicMock(side_effect=RuntimeError("poll failed")))
    monkeypatch.setattr(task_tool_module, "get_stream_writer", lambda: lambda _: None)
    monkeypatch.setattr("deerflow.tools.get_available_tools", MagicMock(return_value=[]))

    with pytest.raises(RuntimeError, match="poll failed"):
        _run_task_tool(
            runtime=runtime,
            description="test",
            prompt="do work",
            subagent_type="general-purpose",
            tool_call_id="tc-error",
        )

    assert task_tool_module.pop_cached_subagent_usage("tc-error") is None

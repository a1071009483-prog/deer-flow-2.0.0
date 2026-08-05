# Phoenix PR1 Review Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:receiving-code-review` first, then use `superpowers:executing-plans` to implement this plan task-by-task. Use `superpowers:test-driven-development` for every behavior change and `superpowers:verification-before-completion` before the final handoff. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the four confirmed review gaps in `remediation/phoenix-pr1-correctness` without expanding PR1 beyond Phoenix correctness, privacy, non-interference, and its existing authorization regression gate.

**Architecture:** Keep canonical `RunnableConfig.metadata` and all delegation behavior unchanged. Make the Phoenix-owned export view preserve server correlation after caller filtering, make caller export values best-effort and JSON-stable, include the allowlist in the Phoenix initialization identity, and make the cross-mode authorization test invoke the real production `task_tool` path. Do not introduce new span attributes, new tracing abstractions, or new authorization code.

**Tech Stack:** Python 3.12, pytest, pytest-asyncio, OpenTelemetry SDK, OpenInference `LangChainInstrumentor`, Pydantic tracing configuration, git.

## Global Constraints

- Execute in the existing worktree `.worktrees/phoenix-pr1-correctness` on branch `remediation/phoenix-pr1-correctness`; do not implement these changes on `main`.
- Start from commit `bc120d5e0c76207a9a3c692a5bdb663f9d69cf70` or a descendant containing the same PR1 fixes. If the branch has moved, inspect the intervening commits before continuing and do not discard them.
- Preserve all unrelated user changes. If `git status --short` is non-empty before work begins, identify ownership before editing overlapping files.
- Production changes are limited to:
  - `backend/packages/harness/deerflow/tracing/phoenix.py`
  - `backend/packages/harness/deerflow/tracing/metadata.py`
- Test changes are limited to:
  - `backend/tests/test_phoenix_safe_export.py`
  - `backend/tests/test_tracing_metadata.py`
  - `backend/tests/test_phoenix_provider_lifecycle.py`
  - `backend/tests/test_phoenix_business_metadata_invariance.py`
- Do not modify `task_tool.py`, delegation rules, `SUBAGENT_TOOLS`, tool loading, skill loading, caches, or subagent configuration semantics.
- Do not rebuild, filter, replace, or mutate canonical `RunnableConfig.metadata`. Every new filter applies only to the Phoenix export copy.
- Do not add `deerflow.*` span attributes, a span processor, a metadata envelope, a reserved-key framework, or another tracing provider abstraction.
- Do not change W3C parent modes, manual `deerflow.run` spans, generator lifetimes, provider ownership, shutdown behavior, OpenInference environment handling, dependency declarations, or public configuration names.
- Safe mode must export configured caller allowlist fields plus server-owned correlation fields. Caller data must not override the server-owned values.
- A bad caller metadata value may be omitted from Phoenix export, but it must never change the business request result or exception.
- Use one focused commit per task. Do not squash tasks during implementation. Do not push, update the PR, or submit a GitHub review unless the user separately authorizes it.

## File Responsibility Map

- `backend/packages/harness/deerflow/tracing/phoenix.py`: DeerFlow-owned OpenInference masking and process initialization identity. It owns trusted-context precedence and `_config_key()`.
- `backend/packages/harness/deerflow/tracing/metadata.py`: Builds a detached, bounded Phoenix export copy from caller metadata and server correlation fields.
- `backend/tests/test_phoenix_safe_export.py`: Real `LangChainInstrumentor` plus `InMemorySpanExporter` assertions against final exported span attributes.
- `backend/tests/test_tracing_metadata.py`: Unit coverage for export-copy isolation and uncopyable/unserializable caller values.
- `backend/tests/test_phoenix_provider_lifecycle.py`: Phoenix initialization/configuration lifecycle tests, including the configuration fingerprint.
- `backend/tests/test_phoenix_business_metadata_invariance.py`: Cross-mode worker-to-production-`task_tool` authorization regression gate.

---

### Task 0: Confirm the execution baseline

**Files:**

- Read only: the files listed in the File Responsibility Map

**Interfaces:**

- Consumes: existing PR1 branch at `bc120d5e...` or a reviewed descendant.
- Produces: a clean, known starting point and baseline test evidence.

- [ ] **Step 1: Enter the PR worktree and confirm the branch**

Run from the repository root:

```bash
cd .worktrees/phoenix-pr1-correctness
git branch --show-current
git rev-parse HEAD
git status --short
```

Expected:

- branch is `remediation/phoenix-pr1-correctness`;
- HEAD is `bc120d5e0c76207a9a3c692a5bdb663f9d69cf70` or a reviewed descendant;
- status is empty, unless the user has intentionally supplied changes that must be preserved.

- [ ] **Step 2: Run the focused baseline suite**

Run from `.worktrees/phoenix-pr1-correctness/backend`:

```bash
PYTHONPATH=. .venv/bin/pytest \
  tests/test_phoenix_safe_export.py \
  tests/test_phoenix_trace_config.py \
  tests/test_tracing_metadata.py \
  tests/test_phoenix_provider_lifecycle.py \
  tests/test_phoenix_business_metadata_invariance.py \
  tests/test_task_tool_core_logic.py -q
```

Expected before adding new tests: PASS. Record the count. At plan-writing time the narrower relevant suite reported `56 passed`; a changed count is acceptable when the branch has legitimate newer tests.

- [ ] **Step 3: Do not commit baseline-only work**

No files should have changed. Confirm:

```bash
git status --short
```

Expected: empty.

---

### Task 1: Preserve server-owned correlation after safe caller filtering

**Files:**

- Modify: `backend/tests/test_phoenix_safe_export.py`
- Modify: `backend/packages/harness/deerflow/tracing/phoenix.py:27,96-111`

**Interfaces:**

- Consumes: `DeerFlowTraceConfig.mask(key: str, value: Any) -> Any` and OpenInference context metadata supplied by `using_attributes(metadata=...)`.
- Produces: safe-mode metadata equal to `allowlisted caller export fields + active DeerFlow root correlation`, with active context values taking final precedence.

- [ ] **Step 1: Add a complete server-correlation fixture to the real-exporter test**

In `backend/tests/test_phoenix_safe_export.py`, add this constant below `_PROMPT_TEMPLATE_SERIALIZED`:

```python
_SERVER_CORRELATION = {
    "session_id": "deerflow-session",
    "thread_id": "deerflow-thread",
    "user_id": "deerflow-user",
    "assistant_id": "deerflow-assistant",
    "model_name": "deerflow-model",
    "environment": "test",
    "root_run_name": "deerflow.run",
    "run_id": "deerflow-run",
}
```

Change `_drive_metadata_collision()` so the context metadata is an explicit argument and caller metadata attempts to forge every trusted key:

```python
def _drive_metadata_collision(tracer, *, context_metadata: dict[str, str]) -> str:
    run_id = uuid4()
    with using_attributes(
        session_id="deerflow-session",
        user_id="deerflow-user",
        metadata=context_metadata,
    ):
        tracer.on_chain_start(
            {"name": "chain"},
            {"input": "x"},
            run_id=run_id,
            name="chain",
            metadata={
                "session_id": "caller-session",
                "thread_id": "caller-thread",
                "user_id": "caller-user",
                "assistant_id": "caller-assistant",
                "model_name": "caller-model",
                "environment": "caller-env",
                "root_run_name": "caller-root",
                "run_id": "caller-run",
                "request_id": "caller-request",
                "private": "caller-private",
            },
        )
        tracer.on_chain_end({"output": "y"}, run_id=run_id)
    return "chain"
```

Update the existing safe/full tests to pass `context_metadata`. For the existing allowlisted safe case, use:

```python
trusted = {**_SERVER_CORRELATION, "request_id": "deerflow-request"}
span = _finished(
    exporter,
    _drive_metadata_collision(tracer, context_metadata=trusted),
)
assert span.attributes["session.id"] == "deerflow-session"
assert span.attributes["user.id"] == "deerflow-user"
assert json.loads(span.attributes["metadata"]) == trusted
```

For the full-capture case, pass the same `trusted` mapping and assert:

```python
metadata = json.loads(span.attributes["metadata"])
assert metadata["private"] == "caller-private"
for key, value in trusted.items():
    assert metadata[key] == value
```

- [ ] **Step 2: Add the empty-allowlist real-exporter regression**

Add this test:

```python
def test_safe_export_keeps_server_correlation_with_empty_allowlist(export_runtime):
    exporter, tracer = export_runtime(
        capture_content=False,
        metadata_allowlist=(),
    )
    span = _finished(
        exporter,
        _drive_metadata_collision(
            tracer,
            context_metadata=dict(_SERVER_CORRELATION),
        ),
    )

    assert span.attributes["session.id"] == "deerflow-session"
    assert span.attributes["user.id"] == "deerflow-user"
    assert json.loads(span.attributes["metadata"]) == _SERVER_CORRELATION
```

The context deliberately excludes `request_id`: in production, `build_phoenix_correlation_metadata()` excludes caller fields when the allowlist is empty, while still adding the server fields.

- [ ] **Step 3: Run the new exporter tests and verify RED**

Run:

```bash
PYTHONPATH=. .venv/bin/pytest \
  tests/test_phoenix_safe_export.py::test_safe_export_caller_metadata_cannot_forge_session_or_correlation \
  tests/test_phoenix_safe_export.py::test_safe_export_keeps_server_correlation_with_empty_allowlist \
  tests/test_phoenix_safe_export.py::test_full_export_caller_metadata_cannot_override_trusted_correlation -q
```

Expected on the current implementation: safe cases FAIL because `_mask_metadata()` filters server fields through `PHOENIX_METADATA_ALLOWLIST`; the empty-allowlist span may have no `metadata` attribute.

- [ ] **Step 4: Implement filter-then-trusted precedence**

In `DeerFlowTraceConfig._mask_metadata()`, do not merge `trusted` into `decoded` before the safe allowlist filter. Replace the method body after the JSON/dict validation with this structure:

```python
trusted = self._context_metadata() or {}

if self._deerflow_capture_content:
    if trusted:
        decoded = {**decoded, **trusted}
        return json.dumps(decoded, sort_keys=True, separators=(",", ":"))
    return masked

filtered = {
    name: item
    for name, item in decoded.items()
    if name in self._deerflow_metadata_allowlist
    and not name.startswith("langfuse_")
}
filtered.update(trusted)
return (
    json.dumps(filtered, sort_keys=True, separators=(",", ":"))
    if filtered
    else None
)
```

Keep the existing invalid-JSON and non-dict behavior unchanged. Do not add a second allowlist for server fields: the active DeerFlow root context is the trusted source, and it must win after caller filtering.

- [ ] **Step 5: Run focused and neighboring tests and verify GREEN**

Run:

```bash
PYTHONPATH=. .venv/bin/pytest \
  tests/test_phoenix_safe_export.py \
  tests/test_phoenix_trace_config.py -q
```

Expected: PASS. In particular, existing hostile-environment, content hiding, exact allowlist, full-capture, and caller-collision tests must remain green.

- [ ] **Step 6: Commit Task 1**

Run from the worktree root:

```bash
git add \
  backend/packages/harness/deerflow/tracing/phoenix.py \
  backend/tests/test_phoenix_safe_export.py
git diff --cached --check
git commit -m "fix(tracing): preserve trusted Phoenix correlation"
```

Expected: one commit containing only the two listed files.

---

### Task 2: Make caller export values best-effort and JSON-stable

**Files:**

- Modify: `backend/tests/test_tracing_metadata.py`
- Modify: `backend/packages/harness/deerflow/tracing/metadata.py:17-25,93-135`

**Interfaces:**

- Consumes: arbitrary values from `caller_metadata: dict[str, Any] | None`.
- Produces: detached JSON-compatible values for Phoenix export; a value that cannot be copied or serialized is omitted without raising.

- [ ] **Step 1: Add an uncopyable-value regression test**

In `backend/tests/test_tracing_metadata.py`, add:

```python
class _Uncopyable:
    def __deepcopy__(self, memo):
        raise RuntimeError("copy boom")


def test_phoenix_export_builder_skips_uncopyable_allowlisted_value(monkeypatch):
    _enable_phoenix(monkeypatch)
    monkeypatch.setenv("PHOENIX_CAPTURE_CONTENT", "false")
    monkeypatch.setenv("PHOENIX_METADATA_ALLOWLIST", "request_id,bad")
    from deerflow.config.tracing_config import reset_tracing_config

    reset_tracing_config()
    bad = _Uncopyable()
    source = {"request_id": "request-1", "bad": bad}

    exported = tracing_metadata.build_phoenix_correlation_metadata(
        thread_id="thread-1",
        caller_metadata=source,
    )

    assert exported["request_id"] == "request-1"
    assert "bad" not in exported
    assert source["bad"] is bad
    assert exported["thread_id"] == "thread-1"
```

- [ ] **Step 2: Add an unserializable-value regression test**

Add `import json` at the top of the test file and add:

```python
def test_phoenix_export_builder_skips_circular_full_capture_value(monkeypatch):
    _enable_phoenix(monkeypatch)
    monkeypatch.setenv("PHOENIX_CAPTURE_CONTENT", "true")
    from deerflow.config.tracing_config import reset_tracing_config

    reset_tracing_config()
    circular: dict[str, object] = {}
    circular["self"] = circular
    source = {"request_id": "request-1", "circular": circular}

    exported = tracing_metadata.build_phoenix_correlation_metadata(
        thread_id="thread-1",
        caller_metadata=source,
    )

    assert exported["request_id"] == "request-1"
    assert "circular" not in exported
    assert source["circular"] is circular
    json.dumps(exported, default=str, ensure_ascii=False)
```

This covers the serialization stage that OpenInference `using_attributes()` performs after the builder returns.

- [ ] **Step 3: Run both tests and verify RED**

Run:

```bash
PYTHONPATH=. .venv/bin/pytest \
  tests/test_tracing_metadata.py::test_phoenix_export_builder_skips_uncopyable_allowlisted_value \
  tests/test_tracing_metadata.py::test_phoenix_export_builder_skips_circular_full_capture_value -q
```

Expected on the current implementation:

- the uncopyable case raises `RuntimeError: copy boom`;
- the circular case retains the unsafe value or fails the JSON-stability assertion.

- [ ] **Step 4: Add one narrow copy-and-normalize helper**

In `backend/packages/harness/deerflow/tracing/metadata.py`, add `json` and `logging` imports, a module logger, one private sentinel, and this helper:

```python
import json
import logging

logger = logging.getLogger(__name__)
_UNEXPORTABLE = object()


def _copy_phoenix_export_value(key: str, value: Any) -> Any:
    try:
        copied = copy.deepcopy(value)
        serialized = json.dumps(copied, default=str, ensure_ascii=False)
        return json.loads(serialized)
    except Exception:
        logger.warning(
            "Skipping Phoenix metadata field %s because %s cannot be copied or serialized.",
            key,
            type(value).__name__,
            exc_info=True,
        )
        return _UNEXPORTABLE
```

The JSON round trip is intentional: it converts a detached value to the same JSON-compatible shape that OpenInference will export, so the later `using_attributes()` serialization does not see caller-owned custom objects or circular references.

- [ ] **Step 5: Use the helper only for caller-controlled export fields**

Replace both direct `copy.deepcopy(...)` assignments in `build_phoenix_correlation_metadata()` with the same identity check:

```python
copied = _copy_phoenix_export_value(key, value)
if copied is not _UNEXPORTABLE:
    metadata[key] = copied
```

For safe mode, bind the selected value first:

```python
value = caller_metadata[key]
copied = _copy_phoenix_export_value(key, value)
if copied is not _UNEXPORTABLE:
    metadata[key] = copied
```

Do not wrap the entire builder in one broad `try/except`: one bad caller field must not discard valid caller fields or server correlation. Do not apply the helper to the server-generated scalar fields in `metadata.update(...)`.

- [ ] **Step 6: Run metadata and call-site non-interference tests**

Run:

```bash
PYTHONPATH=. .venv/bin/pytest \
  tests/test_tracing_metadata.py \
  tests/test_worker_langfuse_metadata.py \
  tests/test_client_langfuse_metadata.py \
  tests/test_subagent_executor.py -q
```

Expected: PASS. Existing nested-copy isolation must remain green, proving the export view cannot write through to canonical metadata.

- [ ] **Step 7: Commit Task 2**

Run:

```bash
git add \
  backend/packages/harness/deerflow/tracing/metadata.py \
  backend/tests/test_tracing_metadata.py
git diff --cached --check
git commit -m "fix(tracing): isolate invalid Phoenix metadata values"
```

Expected: one commit containing only the builder and its unit tests.

---

### Task 3: Include the metadata allowlist in Phoenix initialization identity

**Files:**

- Modify: `backend/tests/test_phoenix_provider_lifecycle.py:19-30`
- Modify: `backend/packages/harness/deerflow/tracing/phoenix.py:27,548-557`

**Interfaces:**

- Consumes: `PhoenixTracingConfig.metadata_allowlist: tuple[str, ...]`.
- Produces: `_config_key(config)` values that differ whenever the effective initialized allowlist differs.

- [ ] **Step 1: Let the lifecycle config factory accept an allowlist**

Change the test helper signature and constructor argument:

```python
def _config(
    *,
    auto_instrument: bool = False,
    capture_content: bool = True,
    metadata_allowlist: tuple[str, ...] = (),
) -> PhoenixTracingConfig:
    return PhoenixTracingConfig(
        enabled=True,
        collector_endpoint="http://127.0.0.1:9",
        api_key=None,
        project_name="deer-flow-provider-lifecycle-test",
        auto_instrument=auto_instrument,
        capture_content=capture_content,
        metadata_allowlist=metadata_allowlist,
        trace_parent_mode="root",
        trace_parent_required=False,
        propagate_baggage=False,
    )
```

- [ ] **Step 2: Add the configuration-key regression**

Add this test near the initialization tests:

```python
def test_config_key_changes_when_metadata_allowlist_changes():
    from deerflow.tracing import phoenix

    without_allowlist = phoenix._config_key(_config(metadata_allowlist=()))
    with_allowlist = phoenix._config_key(
        _config(metadata_allowlist=("request_id",))
    )

    assert without_allowlist != with_allowlist
```

- [ ] **Step 3: Run the test and verify RED**

Run:

```bash
PYTHONPATH=. .venv/bin/pytest \
  tests/test_phoenix_provider_lifecycle.py::test_config_key_changes_when_metadata_allowlist_changes -q
```

Expected on the current implementation: FAIL because both keys are equal.

- [ ] **Step 4: Extend the configuration-key type and value**

Change the alias to:

```python
type _PhoenixConfigKey = tuple[
    str,
    str,
    bool,
    bool,
    bool,
    str | None,
    tuple[str, ...],
]
```

Append this final entry in `_config_key()`:

```python
tuple(config.metadata_allowlist),
```

Do not add runtime-only parent-mode or baggage fields to this key in this task. The allowlist belongs here because it is captured inside the initialized `DeerFlowTraceConfig`; the other runtime fields are evaluated outside that instance.

- [ ] **Step 5: Run lifecycle tests and verify GREEN**

Run:

```bash
PYTHONPATH=. .venv/bin/pytest tests/test_phoenix_provider_lifecycle.py -q
```

Expected: PASS, including initialization retry, host-owned instrumentor, public shutdown, and the new key test.

- [ ] **Step 6: Commit Task 3**

Run:

```bash
git add \
  backend/packages/harness/deerflow/tracing/phoenix.py \
  backend/tests/test_phoenix_provider_lifecycle.py
git diff --cached --check
git commit -m "fix(tracing): fingerprint Phoenix metadata allowlist"
```

Expected: one focused commit. It is valid for `phoenix.py` to appear in both Task 1 and Task 3 commits because the changes affect separate functions.

---

### Task 4: Make the cross-mode authorization gate invoke production `task_tool`

**Files:**

- Modify: `backend/tests/test_phoenix_business_metadata_invariance.py:7-15,104-200`
- Do not modify: `backend/packages/harness/deerflow/tools/builtins/task_tool.py`

**Interfaces:**

- Consumes: the exact `RunnableConfig` captured from `run_agent()` and the decorated production `task_tool.coroutine`.
- Produces: a disabled/safe/full regression gate that observes the real production reads of `model_name`, `tool_groups`, and `available_skills`, and the real production construction of `SubagentExecutor`.

- [ ] **Step 1: Import the production task-tool module and runtime support types**

Add these imports:

```python
import importlib
from types import SimpleNamespace
```

After the DeerFlow imports, bind the module, not a copied function:

```python
task_tool_module = importlib.import_module("deerflow.tools.builtins.task_tool")
```

Keep `SubagentConfig`; the test supplies a deterministic existing-style subagent configuration.

- [ ] **Step 2: Delete the simulated authorization block**

Delete the block beginning with the comment:

```python
# Simulate the task-tool authorization boundary using the exact runtime config
```

Delete through the final assertion against `executor_captured`. In particular, remove these copied production operations:

```python
parent_model = captured_metadata.get("model_name")
parent_tool_groups = captured_metadata.get("tool_groups")
parent_available_skills = captured_metadata.get("available_skills")
get_available_tools(...)
DummyExecutor(...)
```

The repaired test must never read the three authorization keys itself when deciding tool or skill behavior.

- [ ] **Step 3: Build a runtime from the exact config received by the fake agent**

Immediately after the canonical metadata assertions, add:

```python
runtime = SimpleNamespace(
    state={
        "sandbox": {"sandbox_id": "local"},
        "thread_data": {"workspace_path": "/tmp/workspace"},
    },
    context={"thread_id": "thread-auth-invariance"},
    config=fake_agent.captured_config,
)
```

Do not create a new metadata mapping. `runtime.config` must be the exact object captured from `run_agent()`.

- [ ] **Step 4: Install deterministic external seams around the real task tool**

Add:

```python
subagent_config = SubagentConfig(
    name="general-purpose",
    description="General helper",
    system_prompt="Base system prompt",
    max_turns=50,
    timeout_seconds=10,
)
get_available_tools = MagicMock(return_value=["tool-a"])
executor_captured: dict[str, Any] = {}


class DummyExecutor:
    def __init__(self, **kwargs):
        executor_captured["kwargs"] = kwargs

    def execute_async(self, prompt, task_id=None):
        executor_captured["prompt"] = prompt
        return task_id or "generated-task-id"


completed = SimpleNamespace(
    status=task_tool_module.SubagentStatus.COMPLETED,
    ai_messages=[],
    result="done",
    error=None,
    token_usage_records=[],
    usage_reported=False,
)

monkeypatch.setattr(task_tool_module, "SubagentExecutor", DummyExecutor)
monkeypatch.setattr(
    task_tool_module,
    "get_subagent_config",
    lambda _name: subagent_config,
)
monkeypatch.setattr(
    task_tool_module,
    "get_available_subagent_names",
    lambda: ["general-purpose"],
)
monkeypatch.setattr(
    task_tool_module,
    "get_background_task_result",
    lambda _task_id: completed,
)
monkeypatch.setattr(task_tool_module, "cleanup_background_task", lambda _task_id: None)
monkeypatch.setattr(task_tool_module, "get_stream_writer", lambda: lambda _event: None)
monkeypatch.setattr(
    task_tool_module,
    "_token_usage_cache_enabled",
    lambda _app_config: False,
)
monkeypatch.setattr("deerflow.tools.get_available_tools", get_available_tools)
```

These mocks prevent real subagent work and polling. They do not duplicate the authorization decisions: production `task_tool` still reads metadata, resolves the effective skills, calls `get_available_tools()`, and constructs `SubagentExecutor` itself.

- [ ] **Step 5: Invoke the decorated production coroutine and assert its observed decisions**

Add:

```python
task_coroutine = task_tool_module.task_tool.coroutine
assert task_coroutine is not None
result = await task_coroutine(
    runtime=runtime,
    description="验证授权",
    prompt="perform authorized work",
    subagent_type="general-purpose",
    tool_call_id="tc-auth-invariance",
)

assert result == "Task Succeeded. Result: done"
get_available_tools.assert_called_once_with(
    model_name="resolved-model",
    groups=["web"],
    subagent_enabled=False,
)
assert executor_captured["kwargs"]["tools"] == ["tool-a"]
assert executor_captured["kwargs"]["config"].skills == ["research"]
assert executor_captured["kwargs"]["parent_model"] == "resolved-model"
```

Because the test is parameterized over disabled, safe, and full modes, the same literal assertions prove effective authorization is invariant without sharing mutable state between parameter cases.

- [ ] **Step 6: Prove the repaired test catches production-path regressions**

Before making any production change, run:

```bash
PYTHONPATH=. .venv/bin/pytest \
  tests/test_phoenix_business_metadata_invariance.py::test_worker_authorization_is_invariant_across_phoenix_modes -q
```

Expected after the test rewrite: PASS for all three modes.

Then perform one temporary local mutation only to validate the test: change production `task_tool.py` locally so `parent_tool_groups = None`, rerun the command, and confirm all relevant cases FAIL at the `groups=["web"]` assertion. Immediately restore only that one temporary line using `apply_patch`; do not use `git checkout`, `git restore`, or `git reset` because they could discard user work. Rerun and confirm PASS.

The temporary mutation must not be staged or committed. Confirm with:

```bash
git diff -- backend/packages/harness/deerflow/tools/builtins/task_tool.py
```

Expected: no output.

- [ ] **Step 7: Run neighboring task-tool coverage**

Run:

```bash
PYTHONPATH=. .venv/bin/pytest \
  tests/test_phoenix_business_metadata_invariance.py \
  tests/test_task_tool_core_logic.py -q
```

Expected: PASS. Existing task-tool tests still independently cover tool-group propagation, skill inheritance/intersection, model override, polling, and events.

- [ ] **Step 8: Commit Task 4**

Run:

```bash
git add backend/tests/test_phoenix_business_metadata_invariance.py
git diff --cached --check
git commit -m "test(tracing): exercise task tool authorization path"
```

Expected: test-only commit; `task_tool.py` must not be staged or changed.

---

### Task 5: Run the complete merge gate and prepare evidence

**Files:**

- Read only: all changed files and test output
- Do not modify repository files solely to make this checklist pass

**Interfaces:**

- Consumes: Tasks 1-4.
- Produces: reproducible merge evidence and a clean branch ready for a new review.

- [ ] **Step 1: Inspect the final diff for scope**

Run from the worktree root:

```bash
git status --short
git diff --stat bc120d5e0c76207a9a3c692a5bdb663f9d69cf70..HEAD
git diff --check bc120d5e0c76207a9a3c692a5bdb663f9d69cf70..HEAD
git log --oneline bc120d5e0c76207a9a3c692a5bdb663f9d69cf70..HEAD
```

Expected:

- working tree is clean;
- only the six permitted files changed;
- four focused commits exist, one per remediation task;
- no whitespace errors.

- [ ] **Step 2: Run the complete focused Phoenix and authorization suite**

Run from `backend/`:

```bash
PYTHONPATH=. .venv/bin/pytest \
  tests/test_phoenix_safe_export.py \
  tests/test_phoenix_trace_config.py \
  tests/test_tracing_metadata.py \
  tests/test_phoenix_provider_lifecycle.py \
  tests/test_phoenix_business_metadata_invariance.py \
  tests/test_task_tool_core_logic.py \
  tests/test_worker_langfuse_metadata.py \
  tests/test_client_langfuse_metadata.py \
  tests/test_subagent_executor.py -q
```

Expected: PASS with zero failed/skipped tests in this explicit list. Warnings must be recorded but are not failures unless they come from the new code.

- [ ] **Step 3: Run backend lint**

Run:

```bash
make lint
```

Expected: exit code 0.

- [ ] **Step 4: Run the full backend test suite**

Run:

```bash
make test
```

Expected: exit code 0. Do not describe skipped CI workflows as passing tests. If the command fails because of an unrelated pre-existing environment problem, capture the exact command, exit code, and first actionable error; do not weaken or delete tests.

- [ ] **Step 5: Produce the handoff summary**

Write the handoff with these exact sections and only observed results:

- `Branch`: state `remediation/phoenix-pr1-correctness`.
- `Changes`: state that safe export now filters caller metadata before restoring server correlation; invalid caller export values are skipped per field without mutating business metadata; the Phoenix initialization identity includes `metadata_allowlist`; and the cross-mode authorization test invokes production `task_tool`.
- `Verification`: copy the exact passed count from the focused suite, then record the actual exit/result of `make lint` and `make test`.
- `Commits`: copy the SHA and subject for each of the four task commits from `git log --oneline`.
- `Git status`: state `clean` only if `git status --short` emitted no output; otherwise list every remaining path.
- `Not performed`: state that no push, PR mutation, merge, dependency change, or unrelated refactor was performed.

If any required verification is not run, state `NOT RUN` and the exact reason; never infer success from an earlier run or from GitHub's `mergeable=true` flag.

# Phoenix Remediation PR 1: Correctness and Side Effects Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure Phoenix tracing cannot change business metadata or subagent authorization, and remove the process-global privacy/instrumentation side effects introduced by `bcd4c409`.

**Architecture:** Preserve canonical `RunnableConfig.metadata` and construct a separate Phoenix export view. Give the one explicit `LangChainInstrumentor` an instance-local `DeerFlowTraceConfig`; retain the existing DeerFlow-owned non-global provider, but manage it and the owned instrumentor only through public APIs. This PR intentionally leaves call-site containment and exact-parentage deletion to PR 2 so the correctness fix can ship first.

**Tech Stack:** Python 3.12, LangChain/LangGraph, Phoenix OTel, OpenInference LangChain instrumentation, OpenTelemetry SDK, pytest, ruff.

## Global Constraints

- This PR fixes Findings 3.1-3.3 and the public-lifecycle portion of 3.6 in the ADR.
- Do not change the pre-existing `task_tool`, delegation rules, `SUBAGENT_TOOLS`, tool/skill loaders, or caches.
- Do not add policy objects, resolvers, catalogs, fingerprints, identity modes, diagnostics registries, carrier limits, or service containers.
- Phoenix must never remove, replace, or overwrite canonical `RunnableConfig.metadata` or nested values.
- Existing Langfuse metadata injection and callback ordering must remain unchanged.
- `PHOENIX_CAPTURE_CONTENT=false` must still hide input/output/prompt/tool content and filter exported metadata to exact `PHOENIX_METADATA_ALLOWLIST` keys.
- No `OPENINFERENCE_*` environment variable may be written, removed, or restored by DeerFlow.
- Initialize only `LangChainInstrumentor`; never enumerate `openinference_instrumentor` entry points.
- Do not inspect, rebind, restore, reject, or un-instrument a host-owned active LangChain instrumentor.
- Provider cleanup may use only public `force_flush()` and `shutdown()`; instrumentor cleanup may use only public `uninstrument()`.
- Preserve the current W3C parent modes, generator behavior, manual `deerflow.run` span, DeerFlow-owned exact-parentage path, and mandatory dependency layout until PR 2. Do not preserve exact behavior by touching a host-owned instrumentor.

---

## Task 1: Add the metadata and authorization regression gate

**Files:**

- Modify: `backend/tests/test_worker_langfuse_metadata.py`
- Modify: `backend/tests/test_client_langfuse_metadata.py`
- Modify: `backend/tests/test_subagent_executor.py`
- Modify: `backend/tests/test_task_tool_core_logic.py`
- Create: `backend/tests/test_phoenix_business_metadata_invariance.py`

**Interfaces:**

- Consumes: existing `run_agent()`, `DeerFlowClient.stream()`, `inject_trace_metadata()`, and `task_tool` behavior.
- Produces: a failing-before-fix contract that compares canonical metadata and effective delegation across Phoenix modes.

- [ ] **Step 1: Replace tests that currently require destructive metadata rebuilding**

In `test_worker_langfuse_metadata.py`, replace
`test_safe_mode_rebuilds_astream_metadata_after_effective_model_resolution`
with a preservation test. Reuse the existing `_FakeAgent`, `_FakeBridge`,
`_FakeRunManager`, and fake OpenInference runtime fixtures. The factory must
append the same business fields as production:

```python
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
```

After `run_agent()`, assert the exact config passed to `astream()` contains the
original caller entries plus `factory_fields`; it must not equal the filtered
Phoenix export mapping. Keep separate assertions against the captured Phoenix
span metadata so the test proves the execution and export views differ.

- [ ] **Step 2: Add a cross-mode invariance test**

In `test_phoenix_business_metadata_invariance.py`, parameterize the three
relevant states:

```python
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
    config["metadata"].update(
        {"agent_name": "lead-agent", "model_name": "resolved-model"}
    )

    assert config["metadata"] == expected
```

In the worker-level case, capture the metadata object delivered to the fake
agent. Then invoke the
existing task-tool test helper with that runtime config while
`get_available_tools()` and `SubagentExecutor` are mocked. Assert:

```python
assert captured_metadata == business_metadata_with_factory_fields
get_available_tools.assert_called_once_with(
    model_name="resolved-model",
    groups=["web"],
    subagent_enabled=False,
)
assert captured_subagent_config.skills == ["research"]
```

Store the disabled-mode result and compare the safe/full effective tool names
and skills to that literal baseline. Do not introduce a new delegation API to
make the test easier.

- [ ] **Step 3: Cover embedded and subagent metadata preservation**

Add focused assertions to the existing client and subagent tests:

```python
original = {
    "request_id": "request-embedded",
    "private": {"values": [1, 2, 3]},
}
before = copy.deepcopy(original)

# Execute the existing client/subagent fixture with safe Phoenix enabled.

assert captured_config["metadata"]["request_id"] == "request-embedded"
assert captured_config["metadata"]["private"] == before["private"]
assert original == before
```

The Phoenix export assertion must exclude `private` unless explicitly
allowlisted. The test must prove nested input was not mutated.

- [ ] **Step 4: Run the regression tests and verify RED**

Run from `backend/`:

```bash
PYTHONPATH=. uv run pytest \
  tests/test_phoenix_business_metadata_invariance.py \
  tests/test_worker_langfuse_metadata.py \
  tests/test_client_langfuse_metadata.py \
  tests/test_task_tool_core_logic.py \
  tests/test_subagent_executor.py -q
```

Expected: the safe-mode cases fail because `inject_trace_metadata()` and the
worker post-factory rebuild replace canonical metadata. Existing disabled/full
cases remain useful controls.

- [ ] **Step 5: Commit the failing regression gate**

```bash
git add backend/tests/test_phoenix_business_metadata_invariance.py \
  backend/tests/test_worker_langfuse_metadata.py \
  backend/tests/test_client_langfuse_metadata.py \
  backend/tests/test_task_tool_core_logic.py \
  backend/tests/test_subagent_executor.py
git commit -m "test(tracing): expose Phoenix metadata authorization regression"
```

## Task 2: Separate canonical metadata from Phoenix export metadata

**Files:**

- Modify: `backend/packages/harness/deerflow/tracing/metadata.py`
- Modify: `backend/packages/harness/deerflow/runtime/runs/worker.py`
- Modify: `backend/packages/harness/deerflow/client.py`
- Modify: `backend/packages/harness/deerflow/subagents/executor.py`
- Modify: `backend/tests/test_tracing_metadata.py`
- Modify: `backend/tests/test_phoenix_business_metadata_invariance.py`

**Interfaces:**

- Preserves: `inject_langfuse_metadata()` and its pre-Phoenix `setdefault` behavior.
- Preserves: `build_phoenix_correlation_metadata()` as a separate `dict[str, Any]` export-value builder.
- Removes: production uses of `build_trace_metadata()` and `inject_trace_metadata()`.

- [ ] **Step 1: Add direct copy/non-mutation tests for the export builder**

Add this contract in `test_tracing_metadata.py` using the existing environment
fixture:

```python
def test_phoenix_export_builder_does_not_mutate_business_metadata(monkeypatch):
    _enable_phoenix(monkeypatch)
    monkeypatch.setenv("PHOENIX_CAPTURE_CONTENT", "false")
    monkeypatch.setenv("PHOENIX_METADATA_ALLOWLIST", "request_id")
    reset_tracing_config()

    source = {
        "request_id": "request-1",
        "private": {"values": [1, 2]},
        "tool_groups": ["web"],
        "available_skills": ["research"],
    }
    before = copy.deepcopy(source)

    exported = build_phoenix_correlation_metadata(
        thread_id="thread-1",
        user_id="user-1",
        assistant_id="lead-agent",
        caller_metadata=source,
    )

    assert source == before
    assert exported["request_id"] == "request-1"
    assert "private" not in exported
    assert "tool_groups" not in exported
    assert "available_skills" not in exported
```

- [ ] **Step 2: Remove Phoenix mutation from the shared metadata helper**

Restore provider ownership of metadata helpers:

- keep `build_langfuse_trace_metadata()` and `inject_langfuse_metadata()`;
- keep `build_phoenix_correlation_metadata()` only for an adapter/root export
  copy;
- delete `build_trace_metadata()` and `inject_trace_metadata()` once all
  production callers are migrated;
- after the worker-level cross-mode test from Task 1 is active, delete the
  temporary direct test that calls `inject_trace_metadata()` and retain the
  worker/task effective-authorization test as the durable regression gate;
- ensure `build_phoenix_correlation_metadata()` copies caller values and never
  writes through a nested reference during conversion.

Do not add an authorization-aware replacement helper.

- [ ] **Step 3: Restore call sites to Langfuse-only metadata injection**

In worker, client, and subagent executor:

```python
inject_langfuse_metadata(
    config,
    thread_id=thread_id,
    user_id=effective_user_id,
    assistant_id=assistant_id,
    model_name=model_name,
    environment=environment,
)
```

Continue constructing the existing `PhoenixRootContext` with a separate
`correlation_metadata` value for this PR. Do not write that value back into
`config["metadata"]`.

Delete the worker's `caller_metadata` snapshot and the complete safe-mode block
that reassigns both `config["metadata"]` and
`runnable_config["metadata"]` after agent construction. Read the resolved model
for root export without reconstructing the factory's metadata.

- [ ] **Step 4: Run metadata and authorization tests and verify GREEN**

```bash
PYTHONPATH=. uv run pytest \
  tests/test_tracing_metadata.py \
  tests/test_phoenix_business_metadata_invariance.py \
  tests/test_worker_langfuse_metadata.py \
  tests/test_client_langfuse_metadata.py \
  tests/test_task_tool_core_logic.py \
  tests/test_subagent_executor.py -q
```

Expected: all cases pass; safe Phoenix export excludes private metadata while
the agent/task runtime still receives it and retains the same restricted tools
and skills as the disabled baseline.

- [ ] **Step 5: Run static checks and commit**

```bash
uv run ruff check \
  packages/harness/deerflow/tracing/metadata.py \
  packages/harness/deerflow/runtime/runs/worker.py \
  packages/harness/deerflow/client.py \
  packages/harness/deerflow/subagents/executor.py \
  tests/test_tracing_metadata.py \
  tests/test_phoenix_business_metadata_invariance.py
git add backend/packages/harness/deerflow/tracing/metadata.py \
  backend/packages/harness/deerflow/runtime/runs/worker.py \
  backend/packages/harness/deerflow/client.py \
  backend/packages/harness/deerflow/subagents/executor.py \
  backend/tests/test_tracing_metadata.py \
  backend/tests/test_phoenix_business_metadata_invariance.py \
  backend/tests/test_worker_langfuse_metadata.py \
  backend/tests/test_client_langfuse_metadata.py \
  backend/tests/test_task_tool_core_logic.py \
  backend/tests/test_subagent_executor.py
git commit -m "fix(tracing): preserve canonical execution metadata"
```

## Task 3: Apply safe capture through an instance-local TraceConfig

**Files:**

- Modify: `backend/packages/harness/deerflow/tracing/phoenix.py`
- Modify: `backend/tests/test_phoenix_root_runtime.py`
- Modify: `backend/tests/test_tracing_metadata.py`
- Create: `backend/tests/test_phoenix_trace_config.py`

**Interfaces:**

- Produces: `DeerFlowTraceConfig(TraceConfig)` inside the Phoenix implementation module.
- Produces: exact top-level allowlist filtering for `SpanAttributes.METADATA` without changing the source mapping.
- Removes: `_disable_openinference_content_capture()` and `_restore_openinference_content_capture()`.

- [ ] **Step 1: Write failing instance-isolation and mask tests**

Create `test_phoenix_trace_config.py` with tests for:

```python
def test_safe_config_filters_metadata_without_environment_mutation(monkeypatch):
    before = {name: os.environ.get(name) for name in OPENINFERENCE_HIDE_NAMES}
    config = DeerFlowTraceConfig(
        capture_content=False,
        metadata_allowlist=("request_id", "tenant_id"),
    )

    value = json.dumps({
        "request_id": "r-1",
        "tenant_id": "t-1",
        "private": "secret",
        "langfuse_session_id": "other-provider",
    })
    masked = config.mask(SpanAttributes.METADATA, value)

    assert json.loads(masked) == {"request_id": "r-1", "tenant_id": "t-1"}
    assert {name: os.environ.get(name) for name in OPENINFERENCE_HIDE_NAMES} == before
```

Also assert:

- invalid JSON and a top-level list return `None` in safe mode;
- an empty allowlist removes the metadata attribute;
- exact keys are used (`request` does not match `request_id`);
- `langfuse_*` is rejected even if allowlisted;
- a callable value is evaluated once by the upstream mask path;
- full capture returns upstream metadata behavior unchanged;
- input/output/prompt/tool attributes follow the existing OpenInference hide
  behavior when safe capture is active.

- [ ] **Step 2: Run the new tests and verify RED**

```bash
PYTHONPATH=. uv run pytest tests/test_phoenix_trace_config.py -q
```

Expected: import or construction fails because `DeerFlowTraceConfig` does not
exist and safe capture still depends on environment mutation.

- [ ] **Step 3: Implement the minimal non-dataclass subclass**

Implement in `phoenix.py` without adding dataclass fields to the upstream
dataclass:

```python
class DeerFlowTraceConfig(TraceConfig):
    def __init__(
        self,
        *,
        capture_content: bool,
        metadata_allowlist: tuple[str, ...],
    ) -> None:
        hidden = not capture_content
        super().__init__(
            hide_inputs=hidden,
            hide_outputs=hidden,
            hide_input_messages=hidden,
            hide_output_messages=hidden,
            hide_prompts=hidden,
            hide_choices=hidden,
            hide_llm_invocation_parameters=hidden,
            hide_llm_tools=hidden,
        )
        object.__setattr__(self, "_deerflow_capture_content", capture_content)
        object.__setattr__(self, "_deerflow_metadata_allowlist", frozenset(metadata_allowlist))

    def mask(self, key: str, value: Any) -> Any:
        masked = super().mask(key, value)
        if masked is None or self._deerflow_capture_content:
            return masked
        if key != SpanAttributes.METADATA:
            return masked
        try:
            decoded = json.loads(masked)
        except (TypeError, ValueError):
            return None
        if not isinstance(decoded, dict):
            return None
        filtered = {
            name: item
            for name, item in decoded.items()
            if name in self._deerflow_metadata_allowlist
            and not name.startswith("langfuse_")
        }
        return json.dumps(filtered, sort_keys=True, separators=(",", ":")) if filtered else None
```

Use the real `SpanAttributes.METADATA` constant. Keep the implementation local
to Phoenix; do not move privacy policy into canonical metadata helpers.

- [ ] **Step 4: Remove all content-capture environment bookkeeping**

Delete:

- `_content_capture_environment`;
- calls to `_disable_openinference_content_capture()`;
- calls to `_restore_openinference_content_capture()`;
- both helper functions;
- rollback/reset assertions that require DeerFlow to own process environment.

Initialization must construct one `DeerFlowTraceConfig` and pass that exact
instance to the LangChain instrumentor in Task 4.

- [ ] **Step 5: Run focused tests and commit**

```bash
PYTHONPATH=. uv run pytest \
  tests/test_phoenix_trace_config.py \
  tests/test_phoenix_root_runtime.py \
  tests/test_tracing_metadata.py -q
uv run ruff check \
  packages/harness/deerflow/tracing/phoenix.py \
  tests/test_phoenix_trace_config.py
git add backend/packages/harness/deerflow/tracing/phoenix.py \
  backend/tests/test_phoenix_trace_config.py \
  backend/tests/test_phoenix_root_runtime.py \
  backend/tests/test_tracing_metadata.py
git commit -m "fix(tracing): localize Phoenix content filtering"
```

## Task 4: Instrument only LangChain and use public lifecycle APIs

**Files:**

- Modify: `backend/packages/harness/deerflow/tracing/phoenix.py`
- Modify: `backend/tests/test_phoenix_provider_lifecycle.py`
- Modify: `backend/tests/test_phoenix_root_runtime.py`
- Modify: `backend/tests/test_gateway_lifespan_shutdown.py`

**Interfaces:**

- Preserves: `ensure_phoenix_tracing_initialized(config=None) -> None` for PR 1 call sites.
- Preserves: `shutdown_phoenix_tracing(timeout_millis=30_000) -> None` for PR 1 gateway cleanup.
- Owns: one non-global provider and at most one explicitly initialized `LangChainInstrumentor`.

- [ ] **Step 1: Replace entry-point lifecycle tests with explicit ownership tests**

Rewrite the affected cases in `test_phoenix_provider_lifecycle.py` to assert:

```python
entry_points = monkeypatch.setattr(
    importlib.metadata,
    "entry_points",
    Mock(side_effect=AssertionError("must not enumerate instrumentors")),
)
```

and cover these states:

1. inactive LangChain instrumentor: `instrument()` receives the saved provider
   and exact `DeerFlowTraceConfig`, then owned shutdown calls `uninstrument()`;
2. already-active LangChain instrumentor: no instrument/rebind/restore occurs,
   manual root tracing remains available, shutdown does not uninstrument it,
   and a warning is logged;
3. explicit instrumentation failure: the new provider is shut down, only an
   actually owned instrumentor is rolled back, and retry succeeds;
4. unrelated fake entry points are never loaded;
5. provider registration receives `set_global_tracer_provider=False`,
   `auto_instrument=False`, `batch=True`, and `shutdown_on_exit=False`;
6. shutdown calls `force_flush(timeout_millis=timeout_millis)` before `shutdown()` and
   never reads or assigns `_atexit_handler`.

- [ ] **Step 2: Run lifecycle tests and verify RED**

```bash
PYTHONPATH=. uv run pytest \
  tests/test_phoenix_provider_lifecycle.py \
  tests/test_phoenix_root_runtime.py \
  tests/test_gateway_lifespan_shutdown.py -q
```

Expected: tests fail because the implementation enumerates entry points,
rejects active instrumentors, stores private snapshots, and manipulates the
provider's private exit hook.

- [ ] **Step 3: Implement explicit LangChain ownership**

Replace `_phoenix_owned_instrumentors` with one nullable owned reference:

```python
_phoenix_owned_langchain_instrumentor: Any | None = None
```

Initialization logic must follow this order:

```python
provider = register(
    project_name=config.project_name,
    endpoint=config.collector_endpoint,
    api_key=config.api_key,
    auto_instrument=False,
    set_global_tracer_provider=False,
    batch=True,
    shutdown_on_exit=False,
)

instrumentor = LangChainInstrumentor()
if config.auto_instrument and not instrumentor.is_instrumented_by_opentelemetry:
    instrumentor.instrument(tracer_provider=provider, config=trace_config)
    owned_instrumentor = instrumentor
    _install_openinference_langchain_parent_compat(provider)
elif config.auto_instrument:
    logger.warning(
        "Phoenix tracing left an existing host-owned LangChain instrumentor unchanged; "
        "only DeerFlow manual run spans are guaranteed on the Phoenix provider."
    )
```

Check the public property after `instrument()` and treat a false value as
initialization failure. Preserve current lock/idempotency/config-key behavior.
Until PR 2 deletes exact compatibility, install it only inside the
DeerFlow-owned branch shown above. Never inspect or patch the already-active
host branch. Do not add a general ownership type.

- [ ] **Step 4: Delete snapshot, foreign-entry-point, and private-provider code**

Delete `_InstrumentorSnapshot` and all snapshot/restore/entry-point helpers.
Delete `_relinquish_provider_atexit_hook()` and the `atexit` import.

On failure or shutdown:

```python
if owned_instrumentor is not None:
    owned_instrumentor.uninstrument()
provider.force_flush(timeout_millis=timeout_millis)
provider.shutdown()
```

Keep existing exception logging and gateway deadline behavior. Do not mutate a
host-owned instrumentor even if provider shutdown or initialization fails.

- [ ] **Step 5: Run lifecycle, metadata, and parentage regression tests**

```bash
PYTHONPATH=. uv run pytest \
  tests/test_phoenix_provider_lifecycle.py \
  tests/test_phoenix_root_runtime.py \
  tests/test_phoenix_trace_config.py \
  tests/test_phoenix_parent_modes_task_7_5_2.py \
  tests/test_phoenix_generator_scope.py \
  tests/test_gateway_lifespan_shutdown.py -q
```

Expected: all pass. Exact-parentage tests remain unchanged for this PR and are
removed in PR 2.

- [ ] **Step 6: Run source guards and commit**

```bash
rg -n '_atexit_handler|openinference_instrumentor|entry_points\(|OPENINFERENCE_.*os\.environ|__dict__' \
  packages/harness/deerflow/tracing/phoenix.py
uv run ruff check packages/harness/deerflow/tracing/phoenix.py \
  tests/test_phoenix_provider_lifecycle.py \
  tests/test_phoenix_root_runtime.py
git add backend/packages/harness/deerflow/tracing/phoenix.py \
  backend/tests/test_phoenix_provider_lifecycle.py \
  backend/tests/test_phoenix_root_runtime.py \
  backend/tests/test_gateway_lifespan_shutdown.py
git commit -m "fix(tracing): isolate Phoenix instrumentation ownership"
```

Expected `rg` result: no matches. The command exits 1 when the guard succeeds;
record that as success rather than a test failure.

## Task 5: Update the supported contract and run the PR gate

**Files:**

- Modify: `README.md`
- Modify: `backend/README.md`
- Modify: `backend/CLAUDE.md`
- Modify: `openspec/specs/phoenix-tracing-provider/spec.md`

**Interfaces:**

- Documents: non-mutating safe metadata, exact allowlist export, explicit LangChain-only instrumentation, and host-owned coexistence.
- Defers: neutral facade, optional packaging, and exact-parentage deletion to PR 2.

- [ ] **Step 1: Correct documentation that currently requires metadata rebuilding**

Replace statements that Phoenix rebuilds `RunnableConfig.metadata` with this
contract:

```text
PHOENIX_CAPTURE_CONTENT=false filters only attributes exported by the
Phoenix-owned OpenInference instrumentor and manual run span. The RunnableConfig
used by LangGraph, tools, skills, LangSmith, Langfuse, and custom callbacks is
not filtered or replaced.
```

Document that the allowlist controls Phoenix export only and is not an
authorization mechanism.

- [ ] **Step 2: Correct instrumentation and ownership documentation**

State that DeerFlow initializes only `LangChainInstrumentor`. If a host already
owns that instrumentor, DeerFlow leaves it unchanged and guarantees only manual
`deerflow.run` spans on its provider. Remove all claims about entry-point
enumeration, snapshot restoration, and private atexit manipulation.

Do not yet remove exact-parentage documentation; PR 2 owns that behavior change.

- [ ] **Step 3: Run the complete focused PR test gate**

Run from `backend/`:

```bash
PYTHONPATH=. uv run pytest \
  tests/test_tracing_config.py \
  tests/test_tracing_factory.py \
  tests/test_tracing_metadata.py \
  tests/test_phoenix_business_metadata_invariance.py \
  tests/test_phoenix_trace_config.py \
  tests/test_phoenix_root_runtime.py \
  tests/test_phoenix_provider_lifecycle.py \
  tests/test_phoenix_parent_modes_task_7_5_2.py \
  tests/test_phoenix_generator_scope.py \
  tests/test_gateway_phoenix_context.py \
  tests/test_worker_langfuse_metadata.py \
  tests/test_client_langfuse_metadata.py \
  tests/test_task_tool_core_logic.py \
  tests/test_subagent_executor.py -q
```

Then run repository checks:

```bash
uv run ruff check .
uv run ruff format --check .
PYTHONPATH=. uv run pytest tests/ -q
```

Expected: all pass. If the complete suite exposes a pre-existing unrelated
failure, record its exact test/node and reproduce it on the PR base; do not
expand this PR to fix it.

- [ ] **Step 4: Verify the scope boundary**

From the repository root:

```bash
git diff --name-only bcd4c409..HEAD
git diff --stat bcd4c409..HEAD
rg -n 'DelegationPolicy|ResolvedDelegation|fingerprint_|ApplicationServices|TraceRuntime|IDENTITY_MODE' \
  backend/packages/harness backend/tests
```

Expected: production changes are limited to the files listed in this plan;
there are no matches for newly introduced out-of-scope architecture.

- [ ] **Step 5: Commit the documentation and final gate**

```bash
git add README.md backend/README.md backend/CLAUDE.md \
  openspec/specs/phoenix-tracing-provider/spec.md
git commit -m "docs(tracing): define non-mutating Phoenix safety boundary"
```

## PR 1 Exit Gate

- [ ] Phoenix disabled/safe/full modes produce identical effective subagent tools and skills.
- [ ] Canonical metadata and nested values are unchanged by Phoenix filtering.
- [ ] Safe export continues to exclude non-allowlisted metadata and caller tags.
- [ ] Langfuse metadata and callbacks retain their pre-Phoenix behavior.
- [ ] No `OPENINFERENCE_*` environment mutation remains.
- [ ] No OpenInference entry-point enumeration or unrelated instrumentation remains.
- [ ] A host-owned LangChain instrumentor is left untouched.
- [ ] Only a DeerFlow-owned instrumentor is uninstrumented.
- [ ] Provider lifecycle uses public APIs and the gateway cleanup remains bounded.
- [ ] No delegation, cache, identity-policy, diagnostics, packaging, or service-container redesign entered the PR.

## Rollout and Rollback

Deploy this PR before re-enabling Phoenix. Exercise the disabled/safe/full
authorization matrix in staging and inspect one safe export for allowlist
behavior. If provider initialization or export regresses, disable
`PHOENIX_TRACING` and restart backend processes. Do not roll back the canonical
metadata fix; it is independent of whether Phoenix remains enabled.

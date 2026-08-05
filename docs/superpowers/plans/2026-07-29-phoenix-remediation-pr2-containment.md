# Phoenix Remediation PR 2: Containment and Deletion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce the Phoenix change to a small optional tracing integration by hiding provider details behind a neutral facade and deleting private exact-parentage compatibility, mandatory pins, and implementation-coupled tests.

**Architecture:** Add one small `deerflow.tracing.api` facade containing only carrier/run values and lazy lifecycle/scope helpers. Migrate the core files touched by `bcd4c409` to that facade, then delete callback-registry parent repair and keep public W3C/ambient OpenTelemetry continuity. Move Phoenix dependencies to one optional extra; do not create a runtime framework or an exact-compatibility package.

**Tech Stack:** Python 3.12, LangChain/LangGraph, Phoenix OTel, OpenInference LangChain instrumentation, OpenTelemetry SDK, pytest, ruff, uv.

## Global Constraints

- PR 1 is a required base; its metadata and ownership invariants must remain green.
- This PR fixes Findings 3.4-3.7 in the ADR and removes the remaining invasive mechanisms added by `bcd4c409`.
- Strict callback-derived direct parent IDs are intentionally unsupported after this PR.
- Preserve `PHOENIX_TRACE_PARENT_MODE=root|auto|child`, `PHOENIX_TRACE_PARENT_REQUIRED`, and `PHOENIX_PROPAGATE_BAGGAGE`.
- Preserve the manual `deerflow.run` boundary, W3C continuity, isolated-subagent carrier handoff, generator attach/detach, completion/error status, and bounded gateway shutdown.
- Do not add `prefer_exact`, `require_exact`, an exact extra, compatibility status/counters, a bounded parent registry, or fallback telemetry.
- Do not add `TraceRuntime` protocols, provider registries, no-op implementation classes, `ApplicationServices`, a replacement `RunContext`, Studio/direct bootstrap modules, or dependency-injection infrastructure.
- Do not change delegation, tools, skills, caches, identity export policy, metadata/tag/baggage limits, or unrelated application code.
- `build_tracing_callbacks()` remains responsible only for LangSmith and Langfuse callbacks.
- Core files may import neutral names from `deerflow.tracing`; they may not import Phoenix, OpenInference, OpenTelemetry, or `deerflow.tracing.phoenix`.
- With tracing disabled, importing core execution modules must not import optional provider packages.

---

## Task 1: Add the minimal neutral tracing facade

**Files:**

- Create: `backend/packages/harness/deerflow/tracing/api.py`
- Modify: `backend/packages/harness/deerflow/tracing/__init__.py`
- Modify: `backend/packages/harness/deerflow/tracing/factory.py`
- Modify: `backend/packages/harness/deerflow/tracing/otel_context.py`
- Modify: `backend/packages/harness/deerflow/tracing/phoenix.py`
- Create: `backend/tests/test_tracing_api.py`
- Modify: `backend/tests/test_tracing_factory.py`
- Modify: `backend/tests/test_phoenix_root_runtime.py`

**Interfaces:**

- Produces: `TraceContextCarrier`, `TraceRunContext`, `TraceRunHandle`, `TracingInitializationError`, and `TraceParentRequiredError`.
- Produces: `initialize_tracing()`, `shutdown_tracing()`, carrier extract/serialize/deserialize/config-attachment helpers, `capture_current_trace_context()`, `trace_run()`, and `trace_sync_iterator()`.
- Removes: Phoenix initialization from `build_tracing_callbacks()`.

- [ ] **Step 1: Write failing facade purity and behavior tests**

In `test_tracing_api.py`, install a temporary import guard that raises for
top-level `phoenix`, `openinference`, and `opentelemetry` imports. Clear any of
those modules already present in `sys.modules`, disable Phoenix, then assert:

```python
def test_disabled_neutral_api_imports_without_provider_sdks(block_provider_imports):
    import deerflow.tracing
    from deerflow.runtime.runs import worker
    from deerflow import client
    from deerflow.tools.builtins import task_tool
    from deerflow.subagents import executor

    assert deerflow.tracing.TraceRunContext
    assert worker and client and task_tool and executor
```

Add behavior tests for:

- disabled `initialize_tracing()` and `shutdown_tracing()` are no-ops;
- disabled `trace_run()` yields a handle whose `mark_complete()` is inert;
- disabled `trace_sync_iterator()` yields exactly the wrapped iterator values
  and forwards its exception;
- header extraction/serialization/config attachment copies only non-empty
  `traceparent`, `tracestate`, and configured `baggage` values into
  `TraceContextCarrier` without importing OTel;
- enabled initialization with provider imports unavailable raises
  `TracingInitializationError` containing `pip install
  "deerflow-harness[phoenix]"` and no raw environment/config values.

In `test_tracing_factory.py`, add:

```python
def test_callback_factory_never_initializes_phoenix(monkeypatch):
    initialize = Mock(side_effect=AssertionError("not a callback concern"))
    monkeypatch.setattr(tracing_factory, "ensure_phoenix_tracing_initialized", initialize, raising=False)
    build_tracing_callbacks()
    initialize.assert_not_called()
```

- [ ] **Step 2: Run tests and verify RED**

```bash
PYTHONPATH=. uv run pytest tests/test_tracing_api.py tests/test_tracing_factory.py -q
```

Expected: the neutral API does not exist, the current facade eagerly imports
Phoenix/OTel, and the callback factory initializes Phoenix.

- [ ] **Step 3: Implement pure values and lazy dispatch**

`api.py` may import the standard library and tracing configuration, but no
provider SDK at module import time. Define the values as:

```python
@dataclass(frozen=True)
class TraceContextCarrier:
    traceparent: str | None = None
    tracestate: str | None = None
    baggage: str | None = None


@dataclass(frozen=True)
class TraceRunContext:
    run_name: str
    session_id: str | None
    user_id: str | None
    agent_name: str
    model_name: str | None
    environment: str | None
    run_id: str | None
    metadata: Mapping[str, Any]
    tags: tuple[str, ...]
    upstream_context: TraceContextCarrier | None = None
```

`TraceRunHandle` exposes only `mark_complete()`. It delegates to a private
adapter handle or performs a no-op; core code cannot obtain a provider span.

Every provider operation must check the cached tracing configuration and then
lazy-import `deerflow.tracing.phoenix`. Convert a missing optional import to
`TracingInitializationError`. Do not catch ordinary enabled-provider
configuration/initialization errors before work begins.

Keep carrier header copying, serialization, deserialization, and config
attachment free of provider SDK imports. `attach_trace_context_to_config()` and
`capture_current_trace_context()` apply the configured baggage policy
internally so gateway/task call sites do not read Phoenix settings.
Move OTel parse/inject/capture work behind the lazy provider call;
`otel_context.py` may temporarily re-export the new neutral value until Task 2
removes old imports.

Add narrow adapter entry points in `phoenix.py` that accept `TraceRunContext`
and return the private scope/handle needed by the facade. The adapter, not
`api.py`, constructs `PhoenixRootContext` and Phoenix correlation metadata.
Map the existing strict-parent rejection to neutral `TraceParentRequiredError`;
leave all other pre-execution initialization failures visible.

- [ ] **Step 4: Centralize scope and sync-iterator behavior**

Implement `trace_run()` as a context manager that returns a neutral handle. On
successful provider initialization, it delegates to the Phoenix root context;
when disabled it yields an inert handle.

Implement `trace_sync_iterator()` so the provider context is active only while
advancing the wrapped iterator:

```python
while True:
    try:
        with scope.activate():
            item = next(iterator)
    except StopIteration:
        scope.close(None, completed=True)
        return
    except GeneratorExit as exc:
        scope.close(exc, completed=False)
        raise
    except BaseException as exc:
        scope.close(exc, completed=False)
        raise
    yield item
```

The actual implementation must close the wrapped iterator on early close,
restore the caller context after every advancement, and make final cleanup
idempotent. It must never catch or suppress the wrapped iterator's business
exception. After successful initialization, provider-only `Exception` values
during scope creation/activation/close are logged and converted to no-op
tracing, except `TraceParentRequiredError`, which remains an intentional
pre-execution rejection.

- [ ] **Step 5: Make callback construction callback-only**

Delete the Phoenix branch and Phoenix import from `factory.py`.
`build_tracing_callbacks()` must return only LangSmith/Langfuse handlers.
Phoenix initialization will be called explicitly by gateway startup or lazily
by `trace_run()`/`trace_sync_iterator()`.

- [ ] **Step 6: Run tests, lint, and commit**

```bash
PYTHONPATH=. uv run pytest \
  tests/test_tracing_api.py \
  tests/test_tracing_factory.py \
  tests/test_phoenix_generator_scope.py \
  tests/test_phoenix_otel_context.py -q
uv run ruff check packages/harness/deerflow/tracing tests/test_tracing_api.py
git add backend/packages/harness/deerflow/tracing/api.py \
  backend/packages/harness/deerflow/tracing/__init__.py \
  backend/packages/harness/deerflow/tracing/factory.py \
  backend/packages/harness/deerflow/tracing/otel_context.py \
  backend/packages/harness/deerflow/tracing/phoenix.py \
  backend/tests/test_tracing_api.py \
  backend/tests/test_tracing_factory.py \
  backend/tests/test_phoenix_root_runtime.py \
  backend/tests/test_phoenix_generator_scope.py \
  backend/tests/test_phoenix_otel_context.py
git commit -m "refactor(tracing): add minimal provider-neutral facade"
```

## Task 2: Migrate core call sites and restore their business flow

**Files:**

- Modify: `backend/app/gateway/app.py`
- Modify: `backend/app/gateway/services.py`
- Modify: `backend/packages/harness/deerflow/runtime/runs/worker.py`
- Modify: `backend/packages/harness/deerflow/client.py`
- Modify: `backend/packages/harness/deerflow/subagents/executor.py`
- Modify: `backend/packages/harness/deerflow/tools/builtins/task_tool.py`
- Modify: `backend/packages/harness/deerflow/models/factory.py`
- Modify: `backend/packages/harness/deerflow/agents/lead_agent/agent.py`
- Create: `backend/tests/test_tracing_boundary.py`
- Modify: `backend/tests/test_gateway_phoenix_context.py`
- Modify: `backend/tests/test_worker_langfuse_metadata.py`
- Modify: `backend/tests/test_client_langfuse_metadata.py`
- Modify: `backend/tests/test_subagent_executor.py`

**Interfaces:**

- Consumes: the Task 1 neutral facade only.
- Removes: all Phoenix-specific types/functions/config branches from core execution files.
- Establishes: explicit gateway initialization and neutral shutdown.

- [ ] **Step 1: Add a focused source boundary test**

In `test_tracing_boundary.py`, parse exactly the core files listed above with
`ast`. Fail imports whose module starts with `phoenix`, `openinference`,
`opentelemetry`, or `deerflow.tracing.phoenix`. Also fail imported/used names:

```python
FORBIDDEN_NAMES = {
    "PhoenixRootContext",
    "PhoenixRootScope",
    "PhoenixRunBoundary",
    "PhoenixTracingError",
    "activate_phoenix_root_context",
    "open_phoenix_root_scope",
    "bind_phoenix_graph_root_parent",
    "capture_current_phoenix_trace_context",
    "ensure_phoenix_tracing_initialized",
    "shutdown_phoenix_tracing",
}
```

Do not scan the entire repository or impose a new architecture on files not
touched by `bcd4c409`.

- [ ] **Step 2: Run the boundary test and verify RED**

```bash
PYTHONPATH=. uv run pytest tests/test_tracing_boundary.py -q
```

Expected: worker, client, executor, task tool, gateway, and model/callback paths
contain forbidden Phoenix names.

- [ ] **Step 3: Migrate gateway ingress and process lifecycle**

In `services.py`, import neutral header extraction/attachment only. Call
`attach_trace_context_to_config(config, carrier)` without reading Phoenix
configuration; the facade applies the baggage policy.

In gateway lifespan, call `initialize_tracing()` immediately before entering
the existing `langgraph_runtime(app, startup_config)` block. Keep the complete
existing runtime/admin/channel body unchanged. In the existing outer `finally`,
replace `_shutdown_phoenix_tracing_bounded()` with
`_shutdown_tracing_bounded()`.

Rename the bounded helper and thread name from Phoenix-specific to tracing-
neutral names. The neutral shutdown must remain after in-flight run drain and
under the existing five-second deadline. Fix failure logging to pass an actual
`(type, value, traceback)` tuple instead of a bare exception object to
`exc_info`.

- [ ] **Step 4: Migrate worker, subagent, and task handoff**

Construct only `TraceRunContext` in worker/subagent code. The adapter, not the
caller, builds Phoenix correlation metadata.

For each of the worker's two existing stream branches, replace only the
provider-specific context manager:

```python
with trace_run(trace_context) as trace:
    async for item in agent.astream(
        graph_input,
        config=runnable_config,
        stream_mode=lg_modes,
        subgraphs=stream_subgraphs,
    ):
        mode, chunk = _unpack_stream_item(item, lg_modes, stream_subgraphs)
        if mode is not None:
            await bridge.publish(
                run_id,
                _lg_mode_to_sse_event(mode),
                serialize(chunk, mode=mode),
            )
    if not record.abort_event.is_set():
        trace.mark_complete()
```

Retain the existing abort check and LLM-error fallback assignment in that loop;
the snippet shows the tracing boundary and existing publish operation, not a
replacement for those business statements. Apply the same boundary to the
single-mode branch without changing its serialization path.

Subagent usage is the same without `graph_run_id` or
`bind_phoenix_graph_root_parent()`. `task_tool` captures the standard ambient
carrier with argument-free `capture_current_trace_context()` and passes it to
the executor; the facade applies baggage policy and does not inspect a Phoenix
callback registry.

- [ ] **Step 5: Restore the embedded client's original stream loop**

Build `TraceRunContext`, call the agent's original stream method once, and wrap
only iterator advancement:

```python
inner = self._agent.stream(
    state,
    config=config,
    context=context,
    stream_mode=["values", "messages", "custom"],
)
for item in trace_sync_iterator(inner, trace_context):
    if isinstance(item, tuple) and len(item) == 2:
        mode, chunk = item
        mode = str(mode)
    else:
        mode, chunk = "values", item
```

After this normalization, retain the current `custom`, `messages`, and `values`
branches byte-for-byte; only their indentation changes. Compare the result with
`git show bcd4c409^:backend/packages/harness/deerflow/client.py` and the current
post-`2a8c0778` behavior. Do not rewrite event synthesis, usage accounting,
message de-duplication, or tool-call serialization while moving tracing.

- [ ] **Step 6: Remove model/callback initialization side effects**

`attach_tracing=True` on `create_chat_model()` may attach LangSmith/Langfuse
callbacks but must not initialize Phoenix. Update comments in the model factory
and lead-agent module accordingly. Direct users who want Phoenix call the
neutral initializer or execute through a traced run/client.

- [ ] **Step 7: Update cross-path tests and verify GREEN**

Change monkeypatch targets and assertions from Phoenix symbols to neutral
facade behavior. Preserve assertions for:

- gateway header carrier handoff;
- worker completion versus abort status;
- client activation only during iterator advancement;
- client early close and exception propagation;
- isolated-loop subagent carrier handoff;
- metadata/authorization invariance from PR 1.

Run:

```bash
PYTHONPATH=. uv run pytest \
  tests/test_tracing_boundary.py \
  tests/test_tracing_api.py \
  tests/test_gateway_lifespan_shutdown.py \
  tests/test_gateway_phoenix_context.py \
  tests/test_worker_langfuse_metadata.py \
  tests/test_client_langfuse_metadata.py \
  tests/test_subagent_executor.py \
  tests/test_phoenix_business_metadata_invariance.py \
  tests/test_phoenix_generator_scope.py -q
```

- [ ] **Step 8: Lint and commit**

```bash
uv run ruff check app/gateway \
  packages/harness/deerflow/tracing \
  packages/harness/deerflow/runtime/runs/worker.py \
  packages/harness/deerflow/client.py \
  packages/harness/deerflow/subagents/executor.py \
  packages/harness/deerflow/tools/builtins/task_tool.py \
  packages/harness/deerflow/models/factory.py \
  tests/test_tracing_boundary.py
git add backend/app/gateway/app.py \
  backend/app/gateway/services.py \
  backend/packages/harness/deerflow/tracing/api.py \
  backend/packages/harness/deerflow/tracing/__init__.py \
  backend/packages/harness/deerflow/tracing/factory.py \
  backend/packages/harness/deerflow/tracing/otel_context.py \
  backend/packages/harness/deerflow/runtime/runs/worker.py \
  backend/packages/harness/deerflow/client.py \
  backend/packages/harness/deerflow/subagents/executor.py \
  backend/packages/harness/deerflow/tools/builtins/task_tool.py \
  backend/packages/harness/deerflow/models/factory.py \
  backend/packages/harness/deerflow/agents/lead_agent/agent.py \
  backend/tests/test_tracing_boundary.py \
  backend/tests/test_gateway_phoenix_context.py \
  backend/tests/test_worker_langfuse_metadata.py \
  backend/tests/test_client_langfuse_metadata.py \
  backend/tests/test_subagent_executor.py
git commit -m "refactor(tracing): contain provider wiring behind facade"
```

Before committing, inspect `git diff --cached --name-only`; unstage any
unrelated DeerFlow files accidentally included by the broad directory paths.

## Task 3: Delete exact parentage and private compatibility

**Files:**

- Modify: `backend/packages/harness/deerflow/tracing/phoenix.py`
- Delete: `backend/tests/test_phoenix_parent_compat.py`
- Rename: `backend/tests/test_phoenix_parent_modes_task_7_5_2.py` to `backend/tests/test_phoenix_parent_modes.py`
- Modify: `backend/tests/test_phoenix_root_runtime.py`
- Modify: `backend/tests/test_phoenix_provider_lifecycle.py`
- Modify: `backend/tests/test_subagent_executor.py`
- Delete: `openspec/specs/phoenix-subagent-parentage/spec.md`

**Interfaces:**

- Removes: private callback-parent lookup, graph-root binding, compatibility validation, dynamic class replacement, and exact parent-ID guarantees.
- Preserves: public W3C parent modes, baggage behavior, ambient context capture, boundary status, and context restoration.

- [ ] **Step 1: Freeze the replacement public parentage contract**

Rename the W3C parent-mode test file and retain its cases for:

- root isolation from ambient context;
- valid sampled and unsampled W3C parents;
- missing versus invalid fallback;
- strict child rejection before execution;
- baggage on/off;
- restoration after success and exception.

Add one standard-continuity integration assertion per main/embedded/subagent
entry path:

```python
assert boundary.context.trace_id == descendant.context.trace_id
```

Do not assert `descendant.parent.span_id` against a private callback/run-tree
span ID. For isolated subagents, assert the carrier trace ID matches the active
public OTel trace and that the caller context is restored.

- [ ] **Step 2: Delete private compatibility production code**

Remove from `phoenix.py`:

- `_PARENT_COMPAT_DEPENDENCY_VERSIONS`;
- `_parent_compat_tracer`, `_parent_compat_base_class`, and
  `_parent_compat_class`;
- `_graph_root_parent_overrides` and its lock;
- `capture_current_phoenix_trace_context()`;
- `bind_phoenix_graph_root_parent()` and override consume/cleanup helpers;
- `_validate_openinference_langchain_parent_contract()`;
- `_install_openinference_langchain_parent_compat()`;
- all imports of `_parse_dotted_order`, `OpenInferenceTracer`, `_as_utc_nano`,
  `audit_timing`, `_SUPPRESS_INSTRUMENTATION_KEY`, provider processor internals,
  and class reassignment.

Initialization must stop validating exact package versions and installing a
parent compatibility class. Scope creation must use only public OTel parent
context and the explicit LangChain instrumentor configured in PR 1.

- [ ] **Step 3: Delete and prune implementation-coupled tests**

Delete `test_phoenix_parent_compat.py`; do not port its exact-ID, private-slot,
version-contract, registry, or class-patching tests.

From root/provider/subagent tests remove cases whose only subject is:

- parent compatibility installation/residual state;
- exact dependency mismatch;
- graph-root override registration/consumption;
- callback registry preference over ambient context;
- provider private processor equality.

Keep tests for manual span attributes, public provider identity, initialization
idempotency, W3C parent resolution, owned cleanup, abort status, and isolated
ambient carrier handoff.

- [ ] **Step 4: Remove the obsolete canonical exact-parentage spec**

Delete `openspec/specs/phoenix-subagent-parentage/spec.md`. Do not edit the
archived change under `openspec/changes/archive/`; it remains a historical
record of the removed design. PR documentation in Task 5 will explain the
replacement standard contract.

- [ ] **Step 5: Run public parentage and source guards**

```bash
PYTHONPATH=. uv run pytest \
  tests/test_phoenix_parent_modes.py \
  tests/test_phoenix_otel_context.py \
  tests/test_phoenix_generator_scope.py \
  tests/test_phoenix_root_runtime.py \
  tests/test_phoenix_provider_lifecycle.py \
  tests/test_gateway_phoenix_context.py \
  tests/test_subagent_executor.py -q
rg -n '_parse_dotted_order|OpenInferenceTracer|_as_utc_nano|audit_timing|_SUPPRESS_INSTRUMENTATION_KEY|__class__\s*=|_graph_root_parent_overrides|bind_phoenix_graph_root_parent|capture_current_phoenix_trace_context' \
  packages/harness/deerflow
test ! -e tests/test_phoenix_parent_compat.py
```

Expected: tests pass and `rg` returns no matches. Archived OpenSpec paths are
excluded intentionally.

- [ ] **Step 6: Commit the deletion**

```bash
git add -A backend/packages/harness/deerflow/tracing/phoenix.py \
  backend/tests/test_phoenix_parent_compat.py \
  backend/tests/test_phoenix_parent_modes_task_7_5_2.py \
  backend/tests/test_phoenix_parent_modes.py \
  backend/tests/test_phoenix_root_runtime.py \
  backend/tests/test_phoenix_provider_lifecycle.py \
  backend/tests/test_subagent_executor.py \
  openspec/specs/phoenix-subagent-parentage/spec.md
git commit -m "refactor(tracing): remove private exact parentage"
```

## Task 4: Make Phoenix a single optional dependency surface

**Files:**

- Modify: `backend/packages/harness/pyproject.toml`
- Modify: `backend/pyproject.toml`
- Modify: `backend/uv.lock`
- Modify: `backend/packages/harness/deerflow/tracing/api.py`
- Create: `backend/tests/test_phoenix_optional_imports.py`
- Create: `backend/tests/test_phoenix_packaging.py`

**Interfaces:**

- Produces: `deerflow-harness[phoenix]` and backend workspace extra `phoenix`.
- Restores: base `langchain>=1.2.15` and transitive ownership of LangChain Core/LangSmith.
- Removes: mandatory Phoenix/OpenInference dependencies and exact version pins.

- [ ] **Step 1: Write failing dependency and optional-import tests**

Parse both TOML files with `tomllib` and assert:

```python
assert "langchain>=1.2.15" in harness["project"]["dependencies"]
assert not any(item.startswith("langchain-core") for item in base_dependencies)
assert not any(item.startswith("langsmith") for item in base_dependencies)
assert not any("phoenix" in item or "openinference" in item for item in base_dependencies)
assert harness["project"]["optional-dependencies"]["phoenix"] == [
    "arize-phoenix-otel>=0.16.0",
    "openinference-instrumentation-langchain>=0.1.67,<0.2",
]
assert root["project"]["optional-dependencies"]["phoenix"] == [
    "deerflow-harness[phoenix]",
]
```

In `test_phoenix_optional_imports.py`, use the Task 1 import blocker to verify
the base core imports and disabled no-op path. Enable Phoenix under the same
blocker and assert `TracingInitializationError` names the single extra.

- [ ] **Step 2: Run packaging tests and verify RED**

```bash
PYTHONPATH=. uv run pytest \
  tests/test_phoenix_packaging.py \
  tests/test_phoenix_optional_imports.py -q
```

Expected: Phoenix remains mandatory, exact pins remain, and the facade import
path loads provider SDKs eagerly.

- [ ] **Step 3: Move dependencies and restore ranges**

In the harness package:

```toml
dependencies = [
  # existing base dependencies
  "langchain>=1.2.15",
]

[project.optional-dependencies]
phoenix = [
  "arize-phoenix-otel>=0.16.0",
  "openinference-instrumentation-langchain>=0.1.67,<0.2",
]
```

Remove the direct exact `langchain-core` and `langsmith` dependencies. In the
backend root expose:

```toml
[project.optional-dependencies]
phoenix = ["deerflow-harness[phoenix]"]
```

Do not introduce an exact extra or retain exact pins as constraints.

- [ ] **Step 4: Finish lazy optional imports and regenerate the lock**

Ensure `deerflow.tracing.__init__`, `api.py`, `factory.py`, and `metadata.py` do
not import provider SDKs at module import time. Provider imports occur only
after enabled configuration selects Phoenix.

From `backend/`:

```bash
uv lock
uv sync --extra phoenix
```

Inspect the lock to verify Phoenix packages are associated with the optional
extra and exact LangChain/LangSmith requirements are absent from harness base
requirements.

- [ ] **Step 5: Run base-path and installed-extra verification**

```bash
PYTHONPATH=. uv run pytest \
  tests/test_phoenix_packaging.py \
  tests/test_phoenix_optional_imports.py \
  tests/test_tracing_api.py -q
PYTHONPATH=. uv run --extra phoenix pytest \
  tests/test_phoenix_trace_config.py \
  tests/test_phoenix_provider_lifecycle.py \
  tests/test_phoenix_parent_modes.py \
  tests/test_phoenix_generator_scope.py -q
```

This is one base import surface plus one Phoenix surface. Do not create three
temporary environments or an exact-compatibility matrix.

- [ ] **Step 6: Commit packaging changes**

```bash
git add backend/pyproject.toml backend/packages/harness/pyproject.toml \
  backend/uv.lock backend/packages/harness/deerflow/tracing/api.py \
  backend/tests/test_phoenix_packaging.py \
  backend/tests/test_phoenix_optional_imports.py
git commit -m "build(tracing): make Phoenix dependencies optional"
```

## Task 5: Replace exact-topology documentation and run the final gate

**Files:**

- Modify: `README.md`
- Modify: `backend/README.md`
- Modify: `backend/CLAUDE.md`
- Modify: `backend/docs/phoenix-tracing-spike.md`
- Modify: `docs/porting/phoenix-v2.0.0.md`
- Modify: `openspec/specs/phoenix-tracing-provider/spec.md`

**Interfaces:**

- Documents: the neutral facade, explicit lifecycle, optional install, and standard W3C parentage.
- Removes: exact callback topology, private API/version contract, entry-point ownership, and automatic standalone-model initialization claims.

- [ ] **Step 1: Update operator installation and lifecycle instructions**

Document the single install surface:

```bash
cd backend
uv sync --extra phoenix
```

State that gateway startup initializes enabled Phoenix tracing before serving,
embedded runs initialize lazily, and non-gateway process owners call neutral
`shutdown_tracing()` for deterministic flush. Remove claims that Phoenix is a
mandatory harness dependency or is initialized as a model/callback side effect.

- [ ] **Step 2: Replace exact topology with the supported public contract**

Document:

```text
upstream W3C parent (auto/child when valid)
└── deerflow.run
    └── LangChain/OpenInference spans connected by public ambient propagation
```

Clarify that same-trace W3C continuity is supported but exact direct callback
parent IDs and every middleware wrapper are not. Remove private dependency
versions, dotted-order behavior, callback registry lookup, graph-root binding,
and requalification instructions.

In the canonical Phoenix provider spec, retain root/auto/child, baggage,
safe-export, manual-root, generator, ownership, and lifecycle requirements.
Delete requirements that enumerate all instrumentors, inspect private state, or
require exact callback parent IDs.

- [ ] **Step 3: Run focused Phoenix and boundary tests**

From `backend/` with the Phoenix extra installed:

```bash
PYTHONPATH=. uv run --extra phoenix pytest \
  tests/test_tracing_config.py \
  tests/test_tracing_factory.py \
  tests/test_tracing_metadata.py \
  tests/test_tracing_api.py \
  tests/test_tracing_boundary.py \
  tests/test_phoenix_optional_imports.py \
  tests/test_phoenix_packaging.py \
  tests/test_phoenix_business_metadata_invariance.py \
  tests/test_phoenix_trace_config.py \
  tests/test_phoenix_root_runtime.py \
  tests/test_phoenix_provider_lifecycle.py \
  tests/test_phoenix_parent_modes.py \
  tests/test_phoenix_generator_scope.py \
  tests/test_phoenix_otel_context.py \
  tests/test_gateway_lifespan_shutdown.py \
  tests/test_gateway_phoenix_context.py \
  tests/test_worker_langfuse_metadata.py \
  tests/test_client_langfuse_metadata.py \
  tests/test_task_tool_core_logic.py \
  tests/test_subagent_executor.py -q
```

- [ ] **Step 4: Run full repository verification**

```bash
uv run ruff check .
uv run ruff format --check .
PYTHONPATH=. uv run --extra phoenix pytest tests/ -q
```

Expected: all pass. Reproduce an unrelated failure on the PR base instead of
expanding scope.

- [ ] **Step 5: Run final deletion and scope guards**

From the repository root:

```bash
rg -n '_parse_dotted_order|OpenInferenceTracer|_as_utc_nano|audit_timing|_SUPPRESS_INSTRUMENTATION_KEY|__class__\s*=|_graph_root_parent_overrides|bind_phoenix_graph_root_parent|capture_current_phoenix_trace_context|prefer_exact|require_exact|phoenix-exact-parentage' \
  backend/packages/harness README.md backend/README.md \
  backend/CLAUDE.md backend/docs docs/porting openspec/specs
test ! -e backend/tests/test_phoenix_parent_compat.py
rg -n 'from (phoenix|openinference|opentelemetry)|import (phoenix|openinference|opentelemetry)|PhoenixRoot|activate_phoenix|open_phoenix|shutdown_phoenix' \
  backend/app/gateway/app.py \
  backend/app/gateway/services.py \
  backend/packages/harness/deerflow/runtime/runs/worker.py \
  backend/packages/harness/deerflow/client.py \
  backend/packages/harness/deerflow/subagents/executor.py \
  backend/packages/harness/deerflow/tools/builtins/task_tool.py \
  backend/packages/harness/deerflow/models/factory.py
git diff --stat bcd4c409..HEAD
```

Expected: both searches return no matches; production tracing line count and
test line count are materially below `bcd4c409`; no out-of-scope subsystem has
been added.

- [ ] **Step 6: Commit documentation and final verification**

```bash
git add README.md backend/README.md backend/CLAUDE.md \
  backend/docs/phoenix-tracing-spike.md docs/porting/phoenix-v2.0.0.md \
  openspec/specs/phoenix-tracing-provider/spec.md
git commit -m "docs(tracing): document contained Phoenix integration"
```

## PR 2 Exit Gate

- [ ] Core execution files contain only neutral tracing names.
- [ ] `build_tracing_callbacks()` has no Phoenix behavior.
- [ ] Gateway initialization happens before work is accepted; embedded initialization remains lazy.
- [ ] The embedded business event loop is restored and tracing wraps only iterator advancement.
- [ ] Exact callback-parent compatibility code and tests are deleted, not relocated.
- [ ] No private LangSmith/OpenInference/provider symbols or exact dependency pins remain.
- [ ] Root/auto/child, baggage, W3C continuity, isolated handoff, generator restoration, and completion/error status tests pass.
- [ ] Base imports succeed without provider SDK imports.
- [ ] One `phoenix` extra installs and runs the integration.
- [ ] No runtime framework, service container, exact mode, status subsystem, new privacy policy, or delegation redesign was introduced.

## Rollout and Rollback

Release notes must call out two intentional changes: install the `phoenix`
extra, and stop relying on exact callback direct-parent IDs. Deploy with Phoenix
disabled, verify base imports, then enable it in a canary and validate W3C trace
continuity, safe metadata export, isolated subagent handoff, and shutdown. If
the contained adapter regresses, disable Phoenix and restart processes; PR 1's
metadata and ownership fixes remain in place.

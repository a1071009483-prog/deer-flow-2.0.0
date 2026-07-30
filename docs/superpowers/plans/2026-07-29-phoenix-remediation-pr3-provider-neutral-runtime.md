# Phoenix Remediation PR 3: Provider-Neutral Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every core execution path depend only on a small provider-neutral tracing contract, with one process-level runtime and invocation-local carriers/scopes.

**Architecture:** Introduce pure protocol/value modules under `deerflow.tracing.runtime`, an inert implementation, and an OpenTelemetry implementation selected only at composition roots. Application bootstrap creates one thread-safe runtime per owning process; request/run contexts borrow it and carry invocation-local inbound context. Gateway, worker, client, task tool, executor, model factory, and callback builder lose every Phoenix/OpenInference/OpenTelemetry import and Phoenix configuration branch in the same PR.

**Tech Stack:** Python 3.12, typing protocols, contextvars, OpenTelemetry API/SDK behind adapters, LangGraph streaming, pytest, Python AST, Ruff, OpenSpec.

## Global Constraints

- This PR depends on PR 1 and PR 2.
- `TraceRuntime` is process-level; `TraceCarrier`, `TraceRunSpec`, `TraceRunScope`, activation tokens, and outcomes are invocation-level.
- Runtime construction and shutdown happen only at documented composition roots, never in request handlers, factories, model constructors, tools, or callbacks.
- Core execution modules import tracing values only from `deerflow.tracing.runtime`.
- `deerflow.tracing.runtime` and `deerflow.tracing.__init__` cannot import a runtime implementation, adapter, Phoenix, OpenInference, or OpenTelemetry.
- `NoOpTraceRuntime` absorbs disabled tracing without core `is_enabled` branches.
- A scope is already started but detached when returned by `open_run_scope()`.
- Scope creation uses `tracer.start_span(..., context=parent_context)`, never `start_as_current_span()` followed by detach.
- Generator and async-generator context is activated for each advancement and restored before yielding control to the caller.
- Strict `OwnedTraceRunScope` ownership errors are internal; public `FailOpenTraceRunScope` activation/close catches tracing-only `Exception` values and preserves business behavior.
- External carriers enforce 512-byte `traceparent`, 512-byte `tracestate`, 8,192-byte `baggage`, and 9,216-byte combined UTF-8 limits before parsing or handoff.
- `TraceRunScope.close()` and `TraceRuntime.shutdown()` are idempotent.
- Each scope belongs to one invocation/task and cannot be concurrently activated from another owner.
- Successful-start tracing failures degrade to no-op and cannot alter business state or results.
- PR 3 deletes the old production paths in the same change; no dual tracing abstraction remains.
- Exact-parentage mechanics may remain adapter-internal for compatibility, but core code has no exact-specific method or type.
- Update `backend/README.md` and `backend/CLAUDE.md` with runtime ownership and dependency rules.
- Run test/lint commands from `backend/`. Run Git, OpenSpec, and repository architecture/search commands from the repository root.
- Commit blocks mark suggested reviewable behavior boundaries; combine adjacent task commits when they cannot be reviewed independently instead of mechanically creating one commit per checkbox.

## Preflight Symbol and Call-Path Inventory

Run from the repository root before editing and record the summary in the PR description:

```bash
git status --short
rg -n 'PhoenixRootContext|PhoenixRunBoundary|capture_current_phoenix|bind_phoenix|initialize_phoenix|PHOENIX_' backend/app backend/packages/harness/deerflow backend/tests
rg -n 'astream|stream\(|yield|__anext__|GeneratorExit|CancelledError' backend/packages/harness/deerflow/runtime backend/packages/harness/deerflow/client.py backend/packages/harness/deerflow/subagents backend/tests
rg -n 'RunContext|lifespan|build_tracing_callbacks|create_chat_model|make_lead_agent' backend/app backend/packages/harness/deerflow backend/tests
```

Inventory gateway, worker, embedded/direct, Studio, task/executor, model, and callback paths before applying the module map.

---

### Task 1: Define and test the neutral runtime contract

**Files:**
- Create: `backend/packages/harness/deerflow/tracing/runtime.py`
- Create: `backend/tests/test_trace_runtime_contract.py`
- Modify: `backend/packages/harness/deerflow/tracing/__init__.py`

**Interfaces:**
- Produces: `TraceCarrierRejectionReason`, `TraceCarrierRejection`, `TraceCarrier`, `TraceRunSpec`, `TraceRunOutcome`, `TraceRunScope`, `TraceRuntime`.
- Produces: `TraceScopeOwnershipError` for cross-owner or concurrent activation.

- [ ] **Step 1: Write failing protocol and value tests**

Pin the public surface:

```python
class TraceCarrierRejectionReason(StrEnum):
    NON_STRING = "non_string"
    FIELD_TOO_LARGE = "field_too_large"
    TOTAL_TOO_LARGE = "total_too_large"


@dataclass(frozen=True, slots=True)
class TraceCarrierRejection:
    field: Literal["traceparent", "tracestate", "baggage", "total"]
    reason_code: TraceCarrierRejectionReason
    encoded_bytes: int | None


@dataclass(frozen=True, slots=True)
class TraceCarrier:
    traceparent: str | None = None
    tracestate: str | None = None
    baggage: str | None = None
    rejections: tuple[TraceCarrierRejection, ...] = ()

    @classmethod
    def from_headers(cls, headers: Mapping[str, object]) -> TraceCarrier | None: ...


@dataclass(frozen=True, slots=True)
class TraceRunSpec:
    run_id: str
    thread_id: str | None
    user_id: str | None
    session_id: str | None
    agent_name: str
    run_name: str
    model_name: str | None
    caller_metadata: Mapping[str, Any]
    caller_tags: tuple[str, ...]


class TraceRunOutcome(StrEnum):
    COMPLETED = "completed"
    ERROR = "error"
    CANCELLED = "cancelled"
    EARLY_CLOSED = "early_closed"
```

Use structural fake implementations to assert the protocols require:

```python
class TraceRunScope(Protocol):
    def activate(self) -> ContextManager[None]: ...
    def close(self, outcome: TraceRunOutcome, error: BaseException | None = None) -> None: ...


class TraceRuntime(Protocol):
    def capture_carrier(self) -> TraceCarrier | None: ...
    def open_run_scope(self, spec: TraceRunSpec, parent: TraceCarrier | None) -> TraceRunScope: ...
    def shutdown(self, timeout_millis: int) -> None: ...
```

Define `TraceCarrierRejectionReason` values `NON_STRING`, `FIELD_TOO_LARGE`, and `TOTAL_TOO_LARGE`, plus a rejection value containing only field, reason code, and optional byte count. Assert `TraceCarrier.from_headers()` extracts only `traceparent`, `tracestate`, and `baggage`, performs case-insensitive lookup, and returns `None` when all are absent. Test exact-limit acceptance, one-byte-over rejection, multi-byte UTF-8 accounting, non-string values, combined overflow, and partial rejection. Rejected raw values must be absent from the result and logs. The contract must not import OpenTelemetry or perform vendor/W3C semantic validation.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
uv run pytest tests/test_trace_runtime_contract.py -q
```

Expected: collection fails because `deerflow.tracing.runtime` does not exist.

- [ ] **Step 3: Implement the pure contract module**

Use standard-library imports only. Make carrier fields strings rather than vendor context objects. Enforce constants `TRACEPARENT_MAX_BYTES=512`, `TRACESTATE_MAX_BYTES=512`, `BAGGAGE_MAX_BYTES=8192`, and `CARRIER_MAX_BYTES=9216` before copying a value. Retain only bounded rejection records so the runtime can count them; never retain the rejected input. Normalize caller mappings into read-only snapshots when constructing `TraceRunSpec`. Document that `activate()` returns a short-lived context manager and that a scope itself is not a context manager.

Keep `deerflow.tracing.__init__` either empty or limited to lazy-free re-exports from `runtime.py`; do not re-export `NoOpTraceRuntime`, adapter factories, or OpenTelemetry implementations.

- [ ] **Step 4: Run import-purity and focused tests**

Run:

```bash
uv run pytest tests/test_trace_runtime_contract.py -q
python -c 'import sys; import deerflow.tracing.runtime; forbidden=("phoenix", "openinference", "opentelemetry"); assert not any(name.startswith(forbidden) for name in sys.modules)'
uv run ruff check packages/harness/deerflow/tracing/runtime.py packages/harness/deerflow/tracing/__init__.py tests/test_trace_runtime_contract.py
```

Expected: all commands pass and importing the contract loads no telemetry implementation.

- [ ] **Step 5: Commit Task 1**

```bash
git add backend/packages/harness/deerflow/tracing/runtime.py backend/packages/harness/deerflow/tracing/__init__.py backend/tests/test_trace_runtime_contract.py
git commit -m "refactor: define provider-neutral tracing contract"
```

---

### Task 2: Implement no-op and OpenTelemetry scopes

**Files:**
- Create: `backend/packages/harness/deerflow/tracing/noop.py`
- Create: `backend/packages/harness/deerflow/tracing/otel.py`
- Create: `backend/packages/harness/deerflow/tracing/adapters/phoenix/runtime.py`
- Modify: `backend/packages/harness/deerflow/tracing/adapters/phoenix/instrumentation.py`
- Create: `backend/tests/test_noop_trace_runtime.py`
- Create: `backend/tests/test_otel_trace_runtime.py`
- Modify: `backend/tests/test_phoenix_generator_scope.py`

**Interfaces:**
- Produces: `NoOpTraceRuntime` and inert idempotent scope.
- Produces: `OpenTelemetryTraceRuntime`, strict internal `OwnedTraceRunScope`, and public `FailOpenTraceRunScope`.
- Produces: Phoenix adapter attribute/privacy hooks supplied to the generic OTel runtime.

- [ ] **Step 1: Write failing no-op and scope lifecycle tests**

Cover:

```text
NoOp capture -> None
NoOp scope activation -> no context change
NoOp close/shutdown repeated -> no error
OTel open via start_span(context=...) -> span started and never transiently current
owned activate enter -> span is current
owned activate exit -> prior context restored
owned concurrent/re-entrant/cross-owner activate -> TraceScopeOwnershipError
public wrapper sees ownership/enter/exit failure -> inert activation and unchanged business result/error
normal/error/cancelled/early-close -> matching span status/events
public close twice -> exporter observes one completion
public close internal failure -> swallowed, counted, business path unchanged
carrier rejection records -> bounded counters; rejected values never parsed
runtime tracing failure -> inert scope and stable diagnostic reason
shutdown twice -> provider lifecycle called once
```

Use an in-memory span exporter and fake clock/diagnostics. Do not contact Phoenix.

- [ ] **Step 2: Write generator activation regression tests**

For synchronous and asynchronous generators, wrap each `next`/`send`/`throw`/`close` or `__anext__` call in `scope.activate()`. Assert the run span is current inside generator code and the caller's previous context is current immediately after every yielded item. Test cancellation and early close.

- [ ] **Step 3: Run tests and verify RED**

Run:

```bash
uv run pytest tests/test_noop_trace_runtime.py tests/test_otel_trace_runtime.py tests/test_phoenix_generator_scope.py -q
```

Expected: collection fails because the runtime implementations do not exist.

- [ ] **Step 4: Implement the inert runtime**

Use one immutable/inert scope whose methods have no telemetry imports or side effects. Make `shutdown()` accept every non-negative timeout and remain idempotent. The no-op implementation lives outside the contract module so core paths receive it through composition rather than importing it.

- [ ] **Step 5: Implement detached OTel scope ownership and outcomes**

Parse/inject W3C headers only inside `otel.py`. Drop/record carrier rejections before parsing and apply the existing required-parent policy after size and semantic validation. `open_run_scope()` calls `tracer.start_span(..., context=parent_context)` directly; it never calls `start_as_current_span()`, so no creation context needs detaching. `OwnedTraceRunScope.activate()` attaches the span context for one advancement and restores the previous context in `finally`.

Record the creating thread/task identity in `OwnedTraceRunScope`. Reject simultaneous activation, re-entrant activation, or activation by a different owner with `TraceScopeOwnershipError`. Permit sequential activation by the same invocation owner across repeated generator advancement. Close maps cancellation to a stable cancellation event, application errors to `ERROR` with privacy-filtered exception recording, and early close to its own bounded event; safe diagnostics never contain raw exception messages.

Wrap every owned scope before returning it from the public runtime. `FailOpenTraceRunScope.activate()` catches tracing-only `Exception` values from owned activation construction, enter, and exit, records the stable reason, and yields inertly. It must neither suppress nor replace an exception/cancellation from the business block. Its `close()` catches owned close failures under the same rule. Internal tests call the owned scope directly; production integration tests call only the wrapper.

Guard public runtime methods and shutdown state with locks. Track open scopes weakly or by stable IDs so shutdown can stop accepting new scopes, close remaining spans with `EARLY_CLOSED`, flush, and release registry entries deterministically.

- [ ] **Step 6: Connect Phoenix privacy and instrumentation hooks**

`adapters/phoenix/runtime.py` composes the generic runtime with the Task 2 privacy policy and Task 4 instrumentation state from PR 2. The adapter converts `TraceRunSpec` to authoritative and filtered attributes. Exact graph-root correlation remains private to the adapter and keys only from neutral fields such as `run_id`; do not add an exact-binding method to the core protocol.

- [ ] **Step 7: Run focused tests and lint**

Run:

```bash
uv run pytest tests/test_noop_trace_runtime.py tests/test_otel_trace_runtime.py tests/test_phoenix_generator_scope.py tests/test_phoenix_runtime_failure_semantics.py -q
uv run ruff check packages/harness/deerflow/tracing/noop.py packages/harness/deerflow/tracing/otel.py packages/harness/deerflow/tracing/adapters/phoenix/runtime.py tests/test_noop_trace_runtime.py tests/test_otel_trace_runtime.py
```

Expected: all commands pass.

- [ ] **Step 8: Commit Task 2**

```bash
git add backend/packages/harness/deerflow/tracing/noop.py backend/packages/harness/deerflow/tracing/otel.py backend/packages/harness/deerflow/tracing/adapters/phoenix/runtime.py backend/packages/harness/deerflow/tracing/adapters/phoenix/instrumentation.py backend/tests/test_noop_trace_runtime.py backend/tests/test_otel_trace_runtime.py backend/tests/test_phoenix_generator_scope.py
git commit -m "refactor: implement neutral tracing runtimes"
```

---

### Task 3: Establish process-level composition ownership

**Files:**
- Create: `backend/packages/harness/deerflow/tracing/bootstrap.py`
- Create: `backend/packages/harness/deerflow/bootstrap.py`
- Create: `backend/packages/harness/deerflow/studio.py`
- Modify: `backend/app/gateway/app.py`
- Modify: `backend/app/gateway/deps.py`
- Modify: `backend/app/gateway/services.py`
- Modify: `backend/packages/harness/deerflow/runtime/runs/worker.py`
- Modify: `backend/packages/harness/deerflow/client.py`
- Modify: `backend/langgraph.json`
- Create: `backend/tests/test_trace_runtime_composition.py`
- Modify: `backend/tests/test_gateway_lifespan_shutdown.py`
- Modify: `backend/tests/test_gateway_phoenix_context.py`
- Modify: `backend/tests/test_client.py`

**Interfaces:**
- Produces: `ApplicationServices(trace_runtime: TraceRuntime)`.
- Extends: run-local `RunContext` with `trace_runtime` and `inbound_trace_carrier`.
- Produces: explicit owning factories for gateway, worker, embedded/direct client, and Studio.

- [ ] **Step 1: Write failing composition ownership tests**

Pin the lifecycle table:

```text
Gateway: app bootstrap creates once; app shutdown closes once; carrier from HTTP headers
Worker: worker bootstrap receives/creates once; worker shutdown closes only owned runtime; carrier from task envelope
Embedded client: caller may inject borrowed runtime; client closes only one it created
Direct: direct bootstrap owns runtime until explicit close/command termination
Studio: Studio composition module owns runtime for Studio lifecycle; carrier from Studio request/ambient context
```

Send two gateway requests and assert one runtime/provider construction. Create two run contexts and assert both reference the same runtime but hold distinct carriers. Send oversized/non-string trace headers and assert values are dropped before run-context/task-envelope storage, bounded rejection records reach runtime diagnostics, and the business request continues unless required-parent mode is active. Inject a fake borrowed runtime into `DeerFlowClient`, close the client, and assert borrowed shutdown was not invoked.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
uv run pytest tests/test_trace_runtime_composition.py tests/test_gateway_lifespan_shutdown.py tests/test_gateway_phoenix_context.py tests/test_client.py -q
```

Expected: tests fail because runtime ownership is implicit and request contexts do not hold the neutral carrier/runtime pair.

- [ ] **Step 3: Implement one explicit adapter factory**

`tracing/bootstrap.py` is a composition-only factory. It may import `NoOpTraceRuntime` and the Phoenix adapter. It validates tracing configuration and returns a runtime plus an ownership token/state. Disabled tracing returns the no-op runtime. No core execution module imports this factory.

`deerflow/bootstrap.py` exposes embedded/direct application construction and close ownership without importing Phoenix at module import time; perform optional adapter loading inside the owning factory after configuration selects it.

- [ ] **Step 4: Wire gateway and worker ownership**

Gateway lifespan calls the bootstrap once, stores `ApplicationServices` on app state, and shuts it down once after run draining. Request dependency construction calls the pure `TraceCarrier.from_headers(request.headers)` and creates a run context that references the process runtime. Remove request-level provider creation.

Worker composition receives the process runtime and task-envelope carrier rather than rebuilding a provider. If the worker process has a standalone bootstrap, keep its ownership token at the worker lifecycle root rather than inside `_execute_run()`.

- [ ] **Step 5: Wire embedded/direct and Studio ownership**

Require core `DeerFlowClient` construction to receive a borrowed `TraceRuntime`; update internal and test call sites to pass the process runtime explicitly. Preserve a convenient public construction path through `deerflow.bootstrap`, whose factory supplies `NoOpTraceRuntime` when tracing is disabled or creates the selected configured runtime, records ownership, and exposes explicit `close()`/async close behavior. The core client never imports or constructs the no-op implementation.

Point `backend/langgraph.json` to the Studio composition function in `deerflow.studio`. That function obtains one Studio-lifecycle runtime and passes it into graph construction without exposing adapter types to agent code.

- [ ] **Step 6: Run focused tests and lint**

Run:

```bash
uv run pytest tests/test_trace_runtime_composition.py tests/test_gateway_lifespan_shutdown.py tests/test_gateway_phoenix_context.py tests/test_client.py tests/test_client_e2e.py -q
uv run ruff check packages/harness/deerflow/tracing/bootstrap.py packages/harness/deerflow/bootstrap.py packages/harness/deerflow/studio.py app/gateway/app.py app/gateway/deps.py app/gateway/services.py packages/harness/deerflow/runtime/runs/worker.py packages/harness/deerflow/client.py tests/test_trace_runtime_composition.py
```

Expected: all commands pass.

- [ ] **Step 7: Commit Task 3**

```bash
git add backend/packages/harness/deerflow/tracing/bootstrap.py backend/packages/harness/deerflow/bootstrap.py backend/packages/harness/deerflow/studio.py backend/app/gateway/app.py backend/app/gateway/deps.py backend/app/gateway/services.py backend/packages/harness/deerflow/runtime/runs/worker.py backend/packages/harness/deerflow/client.py backend/langgraph.json backend/tests/test_trace_runtime_composition.py backend/tests/test_gateway_lifespan_shutdown.py backend/tests/test_gateway_phoenix_context.py backend/tests/test_client.py
git commit -m "refactor: compose tracing at process lifecycle roots"
```

---

### Task 4: Migrate every core execution path and delete old branches

**Files:**
- Modify: `backend/packages/harness/deerflow/runtime/runs/worker.py`
- Modify: `backend/packages/harness/deerflow/client.py`
- Modify: `backend/packages/harness/deerflow/tools/builtins/task_tool.py`
- Modify: `backend/packages/harness/deerflow/subagents/executor.py`
- Modify: `backend/packages/harness/deerflow/agents/lead_agent/agent.py`
- Modify: `backend/packages/harness/deerflow/models/factory.py`
- Modify: `backend/packages/harness/deerflow/tracing/factory.py`
- Modify: `backend/app/gateway/app.py`
- Modify: `backend/app/gateway/deps.py`
- Modify: `backend/app/gateway/services.py`
- Delete: `backend/packages/harness/deerflow/tracing/otel_context.py`
- Modify: `backend/tests/test_phoenix_root_runtime.py`
- Modify: `backend/tests/test_phoenix_otel_context.py`
- Modify: `backend/tests/test_tracing_factory.py`
- Create: `backend/tests/test_core_trace_runtime_integration.py`

**Interfaces:**
- Core sees only: `TraceRuntime`, `TraceCarrier`, `TraceRunSpec`, `TraceRunScope`, `TraceRunOutcome`.
- `build_tracing_callbacks()` returns callback handlers only and performs no provider/instrumentor initialization.

- [ ] **Step 1: Write failing cross-path runtime tests**

Use a recording fake runtime and assert each path calls only the neutral contract:

```text
worker opens one run scope with envelope parent
client stream opens one run scope with supplied/ambient parent
task tool captures one carrier before subagent handoff
executor opens/activates the child scope without provider-specific context
gateway injects runtime/carrier but never initializes per request
model factory creates models without initializing tracing
callback builder creates LangSmith/Langfuse callbacks without initializing tracing
```

Add generator checks for normal completion, graph error, cancellation, and caller early close with the exact `TraceRunOutcome` passed to `close()`. Inject `TraceScopeOwnershipError` and ordinary tracing exceptions from internal activation enter/exit/close; assert the production wrapper preserves successful business values and preserves the identity/type of business exceptions and cancellation.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
uv run pytest tests/test_core_trace_runtime_integration.py tests/test_tracing_factory.py tests/test_phoenix_root_runtime.py tests/test_phoenix_otel_context.py -q
```

Expected: current core files call Phoenix helpers and callback construction initializes Phoenix as a side effect.

- [ ] **Step 3: Replace Phoenix context handoff in worker, client, task, and executor**

At each run boundary create a neutral `TraceRunSpec`, call `open_run_scope()`, and wrap each stream/generator advancement with `scope.activate()`. Close once with the mapped outcome. At delegation handoff call `capture_carrier()` and pass `TraceCarrier | None` through `ResolvedDelegation`/executor inputs without inspecting it.

Delete all `PhoenixRootContext`, `PhoenixRunBoundary`, Phoenix carrier dictionaries, `bind_phoenix_graph_root_parent`, `capture_current_phoenix_trace_context`, and `if phoenix_enabled` branches from these modules. Authorization remains sourced only from the PR 1 policy closure/resolver.

- [ ] **Step 4: Remove initialization from factories and models**

Make `build_tracing_callbacks()` build only LangSmith/Langfuse callbacks. Model and lead-agent factories accept/borrow the neutral runtime through stable construction inputs or run context; they do not read `PHOENIX_*`, initialize providers, or bind exact parents. Remove `otel_context.py` after its W3C behavior is covered by `TraceCarrier` plus `otel.py`.

- [ ] **Step 5: Update compatibility tests to assert behavior through the neutral contract**

Rename test descriptions and imports so they instantiate fake/OTel runtimes rather than Phoenix root/context helpers. Adapter-specific tests remain under Phoenix test modules; core integration tests may not import the adapter.

- [ ] **Step 6: Run the migrated-path suite and source guard**

Run tests/lint from `backend/`:

```bash
uv run pytest tests/test_core_trace_runtime_integration.py tests/test_trace_runtime_composition.py tests/test_tracing_factory.py tests/test_phoenix_root_runtime.py tests/test_phoenix_otel_context.py tests/test_client.py tests/test_client_e2e.py tests/test_subagent_skills_config.py -q
uv run ruff check packages/harness/deerflow/runtime packages/harness/deerflow/agents packages/harness/deerflow/tools packages/harness/deerflow/subagents packages/harness/deerflow/models packages/harness/deerflow/client.py app/gateway
```

Run the source guard from the repository root:

```bash
! rg -n 'PhoenixRootContext|PhoenixRunBoundary|bind_phoenix|capture_current_phoenix|initialize_phoenix|PHOENIX_' backend/packages/harness/deerflow/runtime backend/packages/harness/deerflow/agents backend/packages/harness/deerflow/tools backend/packages/harness/deerflow/subagents backend/packages/harness/deerflow/models backend/packages/harness/deerflow/client.py backend/app/gateway/deps.py backend/app/gateway/services.py
```

Expected: tests and lint pass; the guard finds no provider-specific core symbol or environment read.

- [ ] **Step 7: Commit Task 4**

```bash
git add backend/packages/harness/deerflow/runtime backend/packages/harness/deerflow/client.py backend/packages/harness/deerflow/tools/builtins/task_tool.py backend/packages/harness/deerflow/subagents/executor.py backend/packages/harness/deerflow/agents/lead_agent/agent.py backend/packages/harness/deerflow/models/factory.py backend/packages/harness/deerflow/tracing/factory.py backend/app/gateway/app.py backend/app/gateway/deps.py backend/app/gateway/services.py backend/tests/test_core_trace_runtime_integration.py backend/tests/test_phoenix_root_runtime.py backend/tests/test_phoenix_otel_context.py backend/tests/test_tracing_factory.py
git commit -m "refactor: migrate core execution to TraceRuntime"
```

---

### Task 5: Enforce the import allowlist in AST and import smoke tests

**Files:**
- Create: `backend/tests/architecture/test_tracing_import_boundaries.py`
- Create: `backend/tests/architecture/test_tracing_optional_imports.py`
- Modify: `backend/packages/harness/deerflow/tracing/__init__.py`

**Interfaces:**
- Enforces: core execution files may import tracing only from `deerflow.tracing.runtime`.
- Enforces: composition roots may additionally import `deerflow.tracing.bootstrap` but not adapters directly.
- Enforces: contract/facade import succeeds while Phoenix/OpenInference/OpenTelemetry imports are blocked.

- [ ] **Step 1: Write the AST allowlist test**

Parse `ast.Import` and `ast.ImportFrom`, plus literal-string calls to `importlib.import_module()` and `__import__()`, for:

```text
packages/harness/deerflow/runtime
packages/harness/deerflow/agents
packages/harness/deerflow/tools
packages/harness/deerflow/subagents
packages/harness/deerflow/models
packages/harness/deerflow/client.py
app/gateway/deps.py
app/gateway/services.py
```

If an imported module begins with `deerflow.tracing`, require the exact module `deerflow.tracing.runtime`. Independently forbid prefixes `phoenix`, `openinference`, `opentelemetry`, `deerflow.tracing.otel`, and `deerflow.tracing.adapters`. For `app/gateway/app.py`, `deerflow/bootstrap.py`, and `deerflow/studio.py`, allow `deerflow.tracing.bootstrap` but continue to forbid adapter imports.

Parse `tracing/runtime.py` and `tracing/__init__.py` under the strictest rule: standard library plus neutral local contract only.

- [ ] **Step 2: Write a dynamic optional-import smoke test**

In a subprocess, install an import blocker for `phoenix`, `openinference`, `opentelemetry`, and `deerflow.tracing.adapters.phoenix`, then import:

```python
import deerflow
import deerflow.tracing
import deerflow.tracing.runtime
import deerflow.client
import deerflow.agents
```

Then call `deerflow.tracing.bootstrap.create_trace_runtime()` with a tracing-disabled settings fixture and assert it returns `NoOpTraceRuntime` without triggering the adapter blocker. Assert all imports succeed and no forbidden module appears in `sys.modules`. This catches eager re-exports and disabled-mode adapter loading that AST prefix checks alone can miss.

- [ ] **Step 3: Run tests and verify RED or detect remaining leaks**

Run:

```bash
uv run pytest tests/architecture/test_tracing_import_boundaries.py tests/architecture/test_tracing_optional_imports.py -q
```

Expected: the first run identifies any core import or eager re-export not removed in Task 4; keep fixing until it passes.

- [ ] **Step 4: Tighten facade imports**

Remove every eager adapter/runtime implementation re-export from `deerflow.tracing.__init__`. Composition uses `deerflow.tracing.bootstrap` explicitly. Core type imports use `deerflow.tracing.runtime` explicitly so a future adapter cannot enter through an innocent-looking top-level facade.

- [ ] **Step 5: Run architecture tests and lint**

Run:

```bash
uv run pytest tests/architecture/test_tracing_import_boundaries.py tests/architecture/test_tracing_optional_imports.py -q
uv run ruff check tests/architecture packages/harness/deerflow/tracing/__init__.py
```

Expected: all commands pass.

- [ ] **Step 6: Commit Task 5**

```bash
git add backend/tests/architecture/test_tracing_import_boundaries.py backend/tests/architecture/test_tracing_optional_imports.py backend/packages/harness/deerflow/tracing/__init__.py
git commit -m "test: enforce tracing dependency boundaries"
```

---

### Task 6: Document and verify the architecture boundary

**Files:**
- Modify: `backend/README.md`
- Modify: `backend/CLAUDE.md`
- Modify: `openspec/specs/phoenix-tracing-provider/spec.md`
- Modify: `openspec/specs/phoenix-subagent-parentage/spec.md`

- [ ] **Step 1: Document process/runtime ownership and extension rules**

Add the composition ownership table for Gateway, worker, embedded client, direct, and Studio. Document the scope activation contract for yielding generators, tracing-only runtime failure behavior, and the rule that core imports only `deerflow.tracing.runtime`.

- [ ] **Step 2: Update OpenSpec scenarios**

Add scenarios for one runtime per process owner, distinct run-local carriers, borrowed-runtime shutdown, callback-builder purity, generator context restoration, cancellation/early-close outcomes, and AST/import boundary enforcement. Remove scenarios that expose Phoenix helper types as core APIs.

- [ ] **Step 3: Run complete PR 3 verification**

Run:

```bash
uv run pytest tests/test_trace_runtime_contract.py tests/test_noop_trace_runtime.py tests/test_otel_trace_runtime.py tests/test_trace_runtime_composition.py tests/test_core_trace_runtime_integration.py tests/architecture/test_tracing_import_boundaries.py tests/architecture/test_tracing_optional_imports.py tests/test_phoenix_generator_scope.py tests/test_tracing_factory.py tests/test_client.py tests/test_client_e2e.py -q
uv run pytest -q
uv run ruff check
cd ..
openspec validate --all --strict
git diff --check
```

Expected: focused tests, full suite, Ruff, strict OpenSpec, and whitespace checks pass.

- [ ] **Step 4: Verify old production paths are gone**

Run from the repository root:

```bash
! rg -n 'PhoenixRootContext|OpenInferenceTracer|LangChainInstrumentor|PHOENIX_|bind_phoenix|capture_current_phoenix' backend/packages/harness/deerflow/runtime backend/packages/harness/deerflow/agents backend/packages/harness/deerflow/tools backend/packages/harness/deerflow/subagents backend/packages/harness/deerflow/models backend/packages/harness/deerflow/client.py backend/app/gateway/deps.py backend/app/gateway/services.py
! test -e backend/packages/harness/deerflow/tracing/otel_context.py
```

Expected: no core provider-specific reference remains and the obsolete context helper is deleted.

- [ ] **Step 5: Commit documentation and spec updates**

```bash
git add backend/README.md backend/CLAUDE.md openspec/specs/phoenix-tracing-provider/spec.md openspec/specs/phoenix-subagent-parentage/spec.md
git commit -m "docs: define provider-neutral tracing runtime"
```

## PR 3 Exit Gate

Do not merge until all conditions hold:

- Every supported entry point has one documented runtime owner and run-local contexts only borrow it.
- Core files see only `TraceRuntime`, `TraceCarrier`, `TraceRunSpec`, `TraceRunScope`, and `TraceRunOutcome` from the contract module.
- Callback and model/agent factories have no instrumentation side effects.
- Generator context is restored on yield, error, cancellation, and early close.
- Strict ownership misuse fails in `OwnedTraceRunScope` tests but is inert and reason-counted through the production `FailOpenTraceRunScope`.
- Carrier byte limits and non-string rejection occur before OpenTelemetry parsing, run-context storage, or task handoff.
- Scope creation uses explicit `start_span(context=...)` with no transient ambient-context mutation.
- Old Phoenix production helpers and branches are removed in this PR.
- AST allowlist and blocked-optional-import smoke tests pass.
- The full backend suite, Ruff, and strict OpenSpec validation pass.

## Rollout and Rollback

Deploy after PR 2 with tracing disabled, then verify gateway, worker, embedded client, direct, and Studio lifecycle smokes. A rollback must return to PR 2 as a whole; do not selectively restore individual Phoenix branches into the neutral runtime release.

# Phoenix Remediation PR 4: Exact Parentage and Packaging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make strict Phoenix parentage an opt-in compatibility layer and remove Phoenix/OpenInference/private-version pressure from the base installation and standard tracing path.

**Architecture:** Standard mode uses only the public provider-neutral/OpenTelemetry path from PR 3. An optional `ExactParentageDecorator` lives entirely under the Phoenix adapter, loads only for `prefer_exact` or `require_exact`, verifies pinned private contracts at startup, and never mutates host-owned instrumentation. Package extras separate normal Phoenix telemetry from exact-parentage pins, while base imports and operation remain valid with neither extra installed.

**Tech Stack:** Python 3.12, OpenTelemetry, optional OpenInference LangChain instrumentation, LangChain/LangSmith private compatibility isolated behind an extra, uv dependency locking, pytest, Ruff, OpenSpec.

## Global Constraints

- This PR depends on PR 1, PR 2, and PR 3.
- `PHOENIX_PARENTAGE_MODE=standard` is the default.
- Standard mode does not import, inspect, or validate the exact-parentage module.
- `prefer_exact` emits a structured degradation status and continues in standard mode when exact cannot activate.
- `require_exact` validates at startup and fails startup when exact cannot activate.
- `require_exact` is a startup capability guarantee, not a per-request topology SLA; post-start exact failures remain fail-open for business execution.
- Tracing disabled returns no-op for every valid parentage mode, does not import or validate exact code, and does not fail startup.
- Exact compatibility never patches, replaces, rebinds, uninstruments, or shuts down a host-owned LangChain instrumentor.
- Private LangChain/LangSmith/OpenInference imports and version pins exist only in the exact module/extra.
- Exact registries are thread-safe, bounded, and delete entries on success, error, cancellation, early close, timeout, and runtime shutdown.
- Base installation imports and runs with Phoenix, OpenInference, and OpenTelemetry packages absent.
- Installing standard Phoenix tracing does not impose exact LangChain/LangSmith pins.
- `deerflow.tracing.runtime` and core execution paths remain unchanged by this PR.
- Update `backend/README.md` and `backend/CLAUDE.md` with install and mode semantics.
- Run test/lint commands from `backend/`. Run Git, OpenSpec, dependency inspection, and repository architecture/search commands from the repository root.
- Commit blocks mark suggested reviewable behavior boundaries; combine adjacent task commits when they cannot be reviewed independently instead of mechanically creating one commit per checkbox.

## Preflight Symbol, Dependency, and Call-Path Inventory

Run from the repository root before editing and record the summary in the PR description:

```bash
git status --short
rg -n '_parse_dotted_order|_SUPPRESS_INSTRUMENTATION_KEY|_as_utc_nano|_spans_by_run|_start_trace|__class__' backend/packages/harness/deerflow/tracing backend/tests
rg -n 'PHOENIX_.*PARENT|ParentageStatus|exact_parentage|ExactParentage' backend/packages/harness/deerflow backend/tests backend/README.md
rg -n 'arize-phoenix|opentelemetry|openinference|langchain-core|langsmith' backend/pyproject.toml backend/packages/harness/pyproject.toml backend/uv.lock
```

Verify current private symbols and lockfile versions before applying compatibility checks or package ranges.

---

### Task 1: Define parentage mode, status, and startup matrix

**Files:**
- Modify: `backend/packages/harness/deerflow/config/tracing_config.py`
- Modify: `backend/packages/harness/deerflow/tracing/adapters/phoenix/types.py`
- Modify: `backend/packages/harness/deerflow/tracing/adapters/phoenix/runtime.py`
- Modify: `backend/tests/test_tracing_config.py`
- Create: `backend/tests/test_phoenix_parentage_matrix.py`

**Interfaces:**
- Produces: `ParentageMode`, `ParentageDegradationReason`, startup `ParentageStatus`, and counter-bearing `ParentageRuntimeStatus`.
- Replaces: overlapping exact/required boolean semantics with one exact-topology mode while retaining the separate root/child/auto inbound W3C policy.
- Produces: adapter status suitable for health/debug reporting.

- [ ] **Step 1: Write the full configuration matrix as failing tests**

Pin these outcomes:

| Tracing | Requested mode | Auto instrument | Instrumentor | Exact extra/contract | Outcome |
| --- | --- | --- | --- | --- | --- |
| off | `standard` | any | any | any | no-op, active `disabled` |
| off | `prefer_exact` | any | any | any | no-op, active `disabled`, exact not imported |
| off | `require_exact` | any | any | any | no-op, active `disabled`, exact not imported |
| on | `standard` | false | none/host | any | active `standard`, exact not imported |
| on | `prefer_exact` | false | none | valid | degrade `AUTO_INSTRUMENT_DISABLED` |
| on | `require_exact` | false | none | valid | startup failure |
| on | `prefer_exact` | true | host | valid | degrade `HOST_OWNED_INSTRUMENTOR` |
| on | `require_exact` | true | host | valid | startup failure |
| on | `prefer_exact` | true | DeerFlow | missing | degrade `EXTRA_NOT_INSTALLED` |
| on | `require_exact` | true | DeerFlow | missing | startup failure |
| on | `prefer_exact` | true | DeerFlow | version mismatch | degrade `VERSION_MISMATCH` |
| on | `require_exact` | true | DeerFlow | version mismatch | startup failure |
| on | exact mode | true | DeerFlow | private contract fails | degrade/fail with `PRIVATE_API_CONTRACT_FAILED` |
| on | exact mode | true | DeerFlow | valid | active `exact` |

Also assert `standard` never invokes the exact-loader fake and that every degraded result has a stable reason code plus a bounded detail string without exception messages or metadata.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
uv run pytest tests/test_tracing_config.py tests/test_phoenix_parentage_matrix.py -q
```

Expected: tests fail because current configuration uses legacy parent-mode flags and lacks structured final status.

- [ ] **Step 3: Implement the mode and status values**

Define:

```python
class ParentageMode(StrEnum):
    STANDARD = "standard"
    PREFER_EXACT = "prefer_exact"
    REQUIRE_EXACT = "require_exact"


class ParentageDegradationReason(StrEnum):
    EXTRA_NOT_INSTALLED = "extra_not_installed"
    VERSION_MISMATCH = "version_mismatch"
    HOST_OWNED_INSTRUMENTOR = "host_owned_instrumentor"
    AUTO_INSTRUMENT_DISABLED = "auto_instrument_disabled"
    PRIVATE_API_CONTRACT_FAILED = "private_api_contract_failed"
    EXACT_REGISTRY_CAPACITY = "exact_registry_capacity"
    RUNTIME_COMPATIBILITY_FAILURE = "runtime_compatibility_failure"


@dataclass(frozen=True, slots=True)
class ParentageStatus:
    requested_mode: ParentageMode
    active_mode: Literal["disabled", "standard", "exact"]
    degraded: bool
    reason_code: ParentageDegradationReason | None
    reason_detail: str | None


@dataclass(frozen=True, slots=True)
class ParentageRuntimeStatus:
    startup: ParentageStatus
    exact_scope_total: int
    exact_runtime_fallback_total: int
    fallback_counts: Mapping[ParentageDegradationReason, int]
```

Read exact-topology preference only from `PHOENIX_PARENTAGE_MODE`. Remove legacy exact/required booleans after migration documentation is in place. Retain the separate inbound W3C policy in `PHOENIX_TRACE_PARENT_MODE=root|child|auto` and `PHOENIX_TRACE_PARENT_REQUIRED`; standard and exact runtimes must both honor it.

- [ ] **Step 4: Implement matrix resolution before serving requests**

When tracing is disabled, return disabled status before inspecting exact mode dependencies. Otherwise resolve ownership and auto-instrument state before attempting an exact import. Return standard status for `standard` without touching the loader. For `prefer_exact`, catch only typed compatibility errors, record one structured degradation event, and return standard status. For `require_exact`, convert the same typed error to a startup failure.

Never pass a host-owned instrumentor into the exact loader. Expose an immutable `ParentageRuntimeStatus` snapshot through the process-level runtime status accessor for composition/operations code; adding a new public endpoint is outside this remediation. Startup matrix tests assert the nested `startup` value, while runtime tests assert counters independently.

- [ ] **Step 5: Run focused tests and lint**

Run:

```bash
uv run pytest tests/test_tracing_config.py tests/test_phoenix_parentage_matrix.py -q
uv run ruff check packages/harness/deerflow/config/tracing_config.py packages/harness/deerflow/tracing/adapters/phoenix/types.py packages/harness/deerflow/tracing/adapters/phoenix/runtime.py tests/test_phoenix_parentage_matrix.py
```

Expected: all commands pass.

- [ ] **Step 6: Commit Task 1**

```bash
git add backend/packages/harness/deerflow/config/tracing_config.py backend/packages/harness/deerflow/tracing/adapters/phoenix/types.py backend/packages/harness/deerflow/tracing/adapters/phoenix/runtime.py backend/tests/test_tracing_config.py backend/tests/test_phoenix_parentage_matrix.py
git commit -m "refactor: define Phoenix parentage modes"
```

---

### Task 2: Isolate and validate the exact-parentage decorator

**Files:**
- Create: `backend/packages/harness/deerflow/tracing/adapters/phoenix/exact_parentage.py`
- Modify: `backend/packages/harness/deerflow/tracing/adapters/phoenix/runtime.py`
- Delete: `backend/packages/harness/deerflow/tracing/phoenix.py`
- Modify: `backend/tests/test_phoenix_parent_compat.py`
- Modify: `backend/tests/test_phoenix_parent_modes_task_7_5_2.py`
- Create: `backend/tests/test_phoenix_exact_parentage_loading.py`

**Interfaces:**
- Produces: `ExactParentageDecorator` implementing `TraceRuntime` by delegation.
- Produces: `load_exact_parentage(base_runtime, instrumentation_state, config) -> ExactParentageDecorator`.
- Produces: typed `ExactParentageCompatibilityError` with stable reason code.

- [ ] **Step 1: Write failing import-isolation and compatibility tests**

Assert:

```text
standard adapter import -> exact module absent from sys.modules
standard runtime construction -> exact loader not called
exact extra missing -> typed EXTRA_NOT_INSTALLED
distribution version mismatch -> typed VERSION_MISMATCH
required private symbol absent -> typed PRIVATE_API_CONTRACT_FAILED
host-owned instrumentor -> loader rejects before class replacement/patch
compatible DeerFlow-owned instrumentor -> decorator activates
```

Patch each private dependency independently: `langsmith.run_trees._parse_dotted_order`, OpenInference suppression/as-nano helpers, private tracer slots/registry, and `_start_trace`. The loader must validate all required contracts before mutating the DeerFlow-owned instrumentor.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
uv run pytest tests/test_phoenix_exact_parentage_loading.py tests/test_phoenix_parent_compat.py tests/test_phoenix_parent_modes_task_7_5_2.py -q
```

Expected: private compatibility currently lives in the broad Phoenix module and can be loaded by the standard path.

- [ ] **Step 3: Move private code into one optional module**

Move private imports, compatibility tracer logic, and exact span-tree algorithms into `exact_parentage.py`. Keep all private imports inside `load_exact_parentage()` or functions invoked only after an exact mode is selected. Do not re-export private classes from the adapter package.

The decorator implements only neutral runtime methods. It uses neutral `TraceRunSpec.run_id` and internal adapter state to coordinate exact graph roots; the core protocol gains no `bind_exact_*` operation.

- [ ] **Step 4: Validate before mutation and contain rollback**

Perform distribution version checks and symbol/signature/slot contract checks first. Only after all checks pass may the loader adapt a DeerFlow-owned instrumentor/tracer. Capture the original DeerFlow-owned class/state and restore it if a later initialization step fails. Never execute mutation or restoration against a host-owned instance.

Delete the old broad `tracing/phoenix.py` after all standard runtime/privacy/instrumentation behavior has moved to adapter modules in PR 2 and PR 3. Update tests to import only adapter internals where exact behavior is under test.

- [ ] **Step 5: Run focused tests and standard-mode source guard**

Run:

```bash
uv run pytest tests/test_phoenix_exact_parentage_loading.py tests/test_phoenix_parent_compat.py tests/test_phoenix_parent_modes_task_7_5_2.py tests/test_phoenix_parentage_matrix.py -q
python -c 'import sys; import deerflow.tracing.adapters.phoenix.runtime; assert "deerflow.tracing.adapters.phoenix.exact_parentage" not in sys.modules'
! test -e packages/harness/deerflow/tracing/phoenix.py
uv run ruff check packages/harness/deerflow/tracing/adapters/phoenix/exact_parentage.py packages/harness/deerflow/tracing/adapters/phoenix/runtime.py tests/test_phoenix_exact_parentage_loading.py
```

Expected: tests and lint pass, standard import does not load the exact module, and the obsolete Phoenix module is absent.

- [ ] **Step 6: Commit Task 2**

```bash
git add backend/packages/harness/deerflow/tracing/adapters/phoenix/exact_parentage.py backend/packages/harness/deerflow/tracing/adapters/phoenix/runtime.py backend/packages/harness/deerflow/tracing/phoenix.py backend/tests/test_phoenix_parent_compat.py backend/tests/test_phoenix_parent_modes_task_7_5_2.py backend/tests/test_phoenix_exact_parentage_loading.py
git commit -m "refactor: isolate exact Phoenix parentage adapter"
```

---

### Task 3: Bound exact registries and concurrency behavior

**Files:**
- Modify: `backend/packages/harness/deerflow/tracing/adapters/phoenix/exact_parentage.py`
- Modify: `backend/packages/harness/deerflow/config/tracing_config.py`
- Create: `backend/tests/test_phoenix_exact_parentage_registry.py`
- Modify: `backend/tests/test_phoenix_root_runtime.py`

**Interfaces:**
- Adds: `PHOENIX_EXACT_REGISTRY_MAX_ENTRIES`, default `4096`.
- Guarantees: deterministic registry cleanup on every terminal path and runtime shutdown.
- Guarantees: parallel subagent parent binding is thread-safe and capacity-bounded.

- [ ] **Step 1: Write failing capacity and cleanup tests**

Use a small configured capacity and assert:

```text
register up to capacity -> succeeds
next registration -> exact binding degrades/fails according to requested mode with stable reason
normal completion -> entry removed
application error -> entry removed
task cancellation -> entry removed
generator early close -> entry removed
binding timeout -> entry removed
runtime shutdown -> all entries removed
duplicate terminal callbacks -> no error and no negative count
parallel siblings -> distinct correct parents and empty registry after join
prefer_exact/require_exact runtime failure -> business result unchanged, fallback counter/attribute updated
require_exact runtime failure -> startup.active_mode remains exact
```

Run a multithreaded stress test with a deterministic barrier and an asyncio sibling-subagent test. Assert registry size never exceeds the configured cap.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
uv run pytest tests/test_phoenix_exact_parentage_registry.py tests/test_phoenix_root_runtime.py -q
```

Expected: current private registry behavior lacks the complete capacity/terminal-path contract.

- [ ] **Step 3: Implement one bounded registry owner**

Encapsulate registry entries behind a lock-protected class with `register`, `resolve`, `discard`, `close_all`, and `size`. Reject new entries at capacity without evicting an active unrelated run. Store only the minimum IDs/context needed for binding; do not retain prompts, metadata, tool payloads, or application exception objects.

Return an idempotent registration token/context manager so every run terminal path executes `discard` in `finally`. Schedule binding timeouts on the runtime's lifecycle owner and cancel them during normal cleanup.

- [ ] **Step 4: Map capacity failure to parentage policy**

Use the existing `EXACT_REGISTRY_CAPACITY` degradation reason. In both `prefer_exact` and `require_exact`, use standard OTel for the affected scope and record a bounded event. Attach `deerflow.trace_parent_fallback=exact_registry_capacity` when a fallback span exists, increment `exact_runtime_fallback_total` and the reason counter, and leave `ParentageRuntimeStatus.startup.active_mode="exact"` unchanged. If span creation failed, counters/events remain mandatory even though no attribute can be attached. A per-request capacity/runtime failure never becomes an application failure.

- [ ] **Step 5: Run concurrency tests repeatedly and lint**

Run:

```bash
for iteration in {1..10}; do uv run pytest tests/test_phoenix_exact_parentage_registry.py tests/test_phoenix_root_runtime.py -q || exit 1; done
uv run ruff check packages/harness/deerflow/tracing/adapters/phoenix/exact_parentage.py packages/harness/deerflow/config/tracing_config.py tests/test_phoenix_exact_parentage_registry.py
```

Expected: all ten repetitions pass without leaks or flakes and no test-only repeat plugin is added to base dependencies.

- [ ] **Step 6: Commit Task 3**

```bash
git add backend/packages/harness/deerflow/tracing/adapters/phoenix/exact_parentage.py backend/packages/harness/deerflow/config/tracing_config.py backend/tests/test_phoenix_exact_parentage_registry.py backend/tests/test_phoenix_root_runtime.py
git commit -m "fix: bound exact parentage runtime state"
```

---

### Task 4: Split base, Phoenix, and exact dependencies

**Files:**
- Modify: `backend/packages/harness/pyproject.toml`
- Modify: `backend/pyproject.toml`
- Modify: `backend/uv.lock`
- Create: `backend/tests/architecture/test_base_install_without_telemetry.py`
- Create: `backend/tests/architecture/test_phoenix_extra_contract.py`
- Modify: `backend/tests/architecture/test_tracing_optional_imports.py`

**Interfaces:**
- Produces: `phoenix` optional extra for public standard telemetry dependencies.
- Produces: `phoenix-exact-parentage` optional extra for exact private compatibility pins.
- Restores: base LangChain constraints without exact-only direct pins.

- [ ] **Step 1: Write failing package metadata tests**

Parse both `pyproject.toml` files and assert:

```text
base dependencies exclude arize-phoenix-otel, OpenInference, OTel SDK/exporter, direct langchain-core/langsmith exact pins
phoenix extra includes OTel SDK, OTLP HTTP exporter, OpenInference base, OpenInference LangChain
exact extra ensures Phoenix dependencies and exact LangChain/langchain-core/langsmith/OpenInference LangChain pins
root backend extras forward to harness extras
standard Phoenix extra does not require exact pins
```

Pin the intended ranges:

```toml
phoenix = [
  "opentelemetry-sdk>=1.41.1,<2",
  "opentelemetry-exporter-otlp-proto-http>=1.41.1,<2",
  "openinference-instrumentation>=0.1.54,<0.2",
  "openinference-instrumentation-langchain>=0.1.67,<0.2",
]

phoenix-exact-parentage = [
  "opentelemetry-sdk>=1.41.1,<2",
  "opentelemetry-exporter-otlp-proto-http>=1.41.1,<2",
  "openinference-instrumentation>=0.1.54,<0.2",
  "langchain==1.2.15",
  "langchain-core==1.3.3",
  "langsmith==0.8.18",
  "openinference-instrumentation-langchain==0.1.67",
]
```

- [ ] **Step 2: Run metadata tests and verify RED**

Run:

```bash
uv run pytest tests/architecture/test_base_install_without_telemetry.py tests/architecture/test_phoenix_extra_contract.py tests/architecture/test_tracing_optional_imports.py -q
```

Expected: current required dependencies and exact pins violate the new package boundary.

- [ ] **Step 3: Move dependencies to extras and restore base ranges**

Remove `arize-phoenix-otel`; the adapter constructs standard OTel components directly. Move OTel/OpenInference packages into `phoenix`. Move exact private pins into `phoenix-exact-parentage`. Restore base `langchain>=1.2.15` and remove exact-only direct `langchain-core`/`langsmith` pins unless another non-tracing feature independently requires them and has its own tested constraint.

Add root-project forwarding extras so operators can install:

```bash
uv sync --extra phoenix
uv sync --extra phoenix-exact-parentage
```

The exact extra explicitly repeats every standard Phoenix dependency and then narrows the private-compatibility packages. The package metadata test compares normalized requirement names so later edits cannot omit a standard dependency from the exact extra.

- [ ] **Step 4: Make bootstrap report missing optional dependencies explicitly**

Keep base module imports lazy. If tracing is enabled without `phoenix`, raise a startup configuration error naming the required extra. If an exact mode is requested with only `phoenix`, apply the Task 1 `prefer_exact` degradation or `require_exact` startup failure. Do not make disabled/base operation import or resolve adapter packages.

- [ ] **Step 5: Regenerate the lockfile and run package tests**

Run:

```bash
uv lock
uv run pytest tests/architecture/test_base_install_without_telemetry.py tests/architecture/test_phoenix_extra_contract.py tests/architecture/test_tracing_optional_imports.py tests/test_phoenix_parentage_matrix.py -q
uv run ruff check tests/architecture packages/harness/deerflow/tracing/bootstrap.py
```

Expected: lockfile generation, package-boundary tests, and lint pass.

- [ ] **Step 6: Verify clean environments for all three install surfaces**

Create three temporary virtual environments outside the repository checkout and install the local harness source as:

```text
base only
[phoenix]
[phoenix-exact-parentage]
```

Use one validated temporary root and explicit environment paths:

```bash
smoke_root=$(mktemp -d /tmp/deerflow-phoenix-smoke.XXXXXX)
uv venv "$smoke_root/base"
uv venv "$smoke_root/phoenix"
uv venv "$smoke_root/exact"
uv pip install --python "$smoke_root/base/bin/python" ./packages/harness
uv pip install --python "$smoke_root/phoenix/bin/python" './packages/harness[phoenix]'
uv pip install --python "$smoke_root/exact/bin/python" './packages/harness[phoenix-exact-parentage]'
```

For base, import harness/core modules and run a no-op trace smoke while an import blocker rejects Phoenix/OpenInference/OpenTelemetry. For `phoenix`, initialize standard mode against an in-memory/fake exporter and assert exact private distributions are not required by project metadata. For exact, run the compatibility loader contract test. Delete only the explicitly created temporary directories after each smoke.

- [ ] **Step 7: Commit Task 4**

```bash
git add backend/packages/harness/pyproject.toml backend/pyproject.toml backend/uv.lock backend/packages/harness/deerflow/tracing/bootstrap.py backend/tests/architecture/test_base_install_without_telemetry.py backend/tests/architecture/test_phoenix_extra_contract.py backend/tests/architecture/test_tracing_optional_imports.py
git commit -m "build: make Phoenix tracing optional"
```

---

### Task 5: Freeze status access and migration documentation

**Files:**
- Modify: `backend/packages/harness/deerflow/tracing/adapters/phoenix/runtime.py`
- Create: `backend/tests/test_phoenix_parentage_status.py`
- Modify: `backend/README.md`
- Modify: `backend/CLAUDE.md`
- Modify: `openspec/specs/phoenix-tracing-provider/spec.md`
- Modify: `openspec/specs/phoenix-subagent-parentage/spec.md`

**Interfaces:**
- Exposes: immutable `ParentageStatus` through the process-level adapter/runtime status accessor.
- Documents: base, standard Phoenix, and exact installation/configuration paths.

- [ ] **Step 1: Write a failing immutable-status accessor test**

For disabled, standard, degraded, and exact initialization, assert the process runtime returns an immutable `ParentageRuntimeStatus` snapshot without recomputing or loading compatibility code. After injected runtime fallback, assert the next snapshot preserves the nested startup status and increases only the runtime counters. A sanitized operational projection contains only startup mode fields plus numeric totals and reason-code counts:

```json
{
  "requested_mode": "prefer_exact",
  "active_mode": "standard",
  "degraded": true,
  "reason_code": "version_mismatch",
  "exact_scope_total": 0,
  "exact_runtime_fallback_total": 0,
  "fallback_counts": {}
}
```

Assert the projection omits `reason_detail`, installed versions, filesystem paths, exception messages, collector headers/API keys, raw metadata, tags, and identifiers. This PR does not add a public health/debug endpoint; a future authenticated operational consumer may use the projection without changing the adapter contract.

- [ ] **Step 2: Run the status test and verify RED**

Run:

```bash
uv run pytest tests/test_phoenix_parentage_status.py -q
```

Expected: the adapter does not yet expose a stable status accessor/projection.

- [ ] **Step 3: Add stable process-level status access**

Store startup `ParentageStatus` when the process runtime initializes. Guard exact-scope and per-reason runtime counters with the runtime lock and return immutable snapshots. Provide an internal sanitized projection helper that serializes requested mode, active mode, degraded flag, reason code, numeric totals, and reason-code counts only. Keep bounded `reason_detail` for internal startup diagnostics and never use it as a metric label.

- [ ] **Step 4: Document install and migration paths**

Document:

```bash
uv sync
uv sync --extra phoenix
uv sync --extra phoenix-exact-parentage
```

Explain `standard`, `prefer_exact`, and `require_exact`; ownership restrictions; host-owned degradation; auto-instrument-disabled behavior; disabled tracing behavior; safe-mode identity policy; and the removal of legacy exact/required booleans. State verbatim that `require_exact` is a startup capability guarantee, not a per-request topology SLA, and document runtime fallback attributes/counters plus unchanged startup status. Document that `PHOENIX_TRACE_PARENT_MODE` and `PHOENIX_TRACE_PARENT_REQUIRED` remain the separate inbound W3C parent policy. State that standard mode remains supported across normal LangChain upgrades, while exact mode is tied to its compatibility pins.

- [ ] **Step 5: Update OpenSpec and run focused tests**

Add exact matrix, standard no-import, structured degradation status, bounded registry, and three install-surface scenarios. Then run:

```bash
uv run pytest tests/test_phoenix_parentage_status.py tests/test_phoenix_parentage_matrix.py tests/test_phoenix_exact_parentage_loading.py tests/test_phoenix_exact_parentage_registry.py tests/architecture/test_base_install_without_telemetry.py tests/architecture/test_phoenix_extra_contract.py -q
uv run ruff check packages/harness/deerflow/tracing/adapters/phoenix/runtime.py tests/test_phoenix_parentage_status.py
cd ..
openspec validate --all --strict
```

Expected: tests, lint, and OpenSpec validation pass.

- [ ] **Step 6: Commit Task 5**

```bash
git add backend/packages/harness/deerflow/tracing/adapters/phoenix/runtime.py backend/tests/test_phoenix_parentage_status.py backend/README.md backend/CLAUDE.md openspec/specs/phoenix-tracing-provider/spec.md openspec/specs/phoenix-subagent-parentage/spec.md
git commit -m "docs: publish Phoenix parentage and install status"
```

---

### Task 6: Run the final compatibility and packaging gate

**Files:**
- Verify only; fix failures in the owning task's files and amend that task's commit.

- [ ] **Step 1: Run standard-mode and exact-mode focused suites**

Run:

```bash
uv run pytest tests/test_phoenix_parentage_matrix.py tests/test_phoenix_parentage_status.py tests/test_phoenix_exact_parentage_loading.py tests/test_phoenix_exact_parentage_registry.py tests/test_phoenix_parent_compat.py tests/test_phoenix_parent_modes_task_7_5_2.py tests/test_phoenix_root_runtime.py -q
uv run pytest tests/architecture/test_base_install_without_telemetry.py tests/architecture/test_phoenix_extra_contract.py tests/architecture/test_tracing_optional_imports.py tests/architecture/test_tracing_import_boundaries.py -q
```

Expected: both suites pass.

- [ ] **Step 2: Run the complete repository verification**

Run:

```bash
uv run pytest -q
uv run ruff check
cd ..
openspec validate --all --strict
git diff --check
```

Expected: full tests, Ruff, strict OpenSpec, and whitespace checks pass.

- [ ] **Step 3: Verify dependency/import invariants**

Run from the repository root:

```bash
! rg -n 'langsmith\.run_trees|_parse_dotted_order|_SUPPRESS_INSTRUMENTATION_KEY|_as_utc_nano|_spans_by_run|_start_trace' backend/packages/harness/deerflow/tracing --glob '!adapters/phoenix/exact_parentage.py'
! rg -n '^\s*(from|import)\s+(phoenix|openinference|opentelemetry)' backend/packages/harness/deerflow/tracing/runtime.py backend/packages/harness/deerflow/tracing/__init__.py
uv run pytest tests/architecture/test_phoenix_extra_contract.py -q
```

Expected: private APIs appear only in the exact module, neutral imports remain pure, and the parsed package metadata test proves exact dependencies are absent from base/standard declarations.

- [ ] **Step 4: Run an optional real Phoenix smoke**

When a collector is available, run one standard-mode gateway trace and one exact-mode parallel-subagent trace. Confirm run/session correlation, privacy policy, shutdown flush, final `ParentageStatus`, and no context leak between requests. Record the collector/runtime versions and smoke result in the PR description; do not store API keys or exported content in the repository.

- [ ] **Step 5: Review rollback boundaries**

Confirm deployment can return from PR 4 to PR 3 standard tracing without reverting PR 1 security, PR 2 side-effect isolation, or PR 3 runtime abstraction. Exact-mode startup failures must be resolved by changing the mode/extra or rolling back PR 4 as a unit, never by reintroducing private code into core modules.

## PR 4 Exit Gate

Do not merge until all conditions hold:

- `standard` does not import exact compatibility code.
- `prefer_exact` degrades with a stable status and `require_exact` fails at startup for every unsupported matrix case.
- `require_exact` runtime failures preserve requests, set fallback attributes when possible, increment reason counters, and do not rewrite startup `active_mode="exact"`.
- Exact compatibility never mutates host-owned instrumentation.
- Exact registry capacity, cleanup, cancellation, shutdown, and concurrency tests pass.
- Base, `phoenix`, and `phoenix-exact-parentage` install surfaces pass clean-environment smoke tests.
- Base and standard extras contain no exact private pins.
- Core/contract import boundaries from PR 3 remain unchanged and pass.
- Full backend tests, Ruff, strict OpenSpec, and package metadata tests pass.

## Rollout and Rollback

Deploy standard mode first. Enable `prefer_exact` only on a canary with status monitoring, then use `require_exact` only where strict topology is an operational contract. Roll back PR 4 to PR 3 if compatibility or packaging fails; retain all preceding security and architecture changes.

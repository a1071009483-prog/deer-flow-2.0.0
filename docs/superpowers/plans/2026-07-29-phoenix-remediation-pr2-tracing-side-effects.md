# Phoenix Remediation PR 2: Tracing Side-Effect Boundary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop Phoenix tracing from mutating canonical business metadata or process-global instrumentation state while preserving the trace output needed before the provider-neutral runtime migration.

**Architecture:** Keep `RunnableConfig.metadata` canonical and move Phoenix privacy decisions to an instance-local OpenInference `TraceConfig` and authoritative span-attribute builder. Replace environment-variable mutation, entry-point scanning, and private provider lifecycle access with an explicitly owned OpenTelemetry provider and one explicitly initialized `LangChainInstrumentor`. This PR deliberately preserves existing Phoenix call sites; PR 3 removes those call sites behind the neutral runtime contract.

**Tech Stack:** Python 3.12, OpenTelemetry SDK and OTLP HTTP exporter, OpenInference instrumentation, LangChain instrumentation, pytest, Ruff, OpenSpec.

## Global Constraints

- This PR depends on PR 1 and must preserve all delegation authorization invariants.
- `RunnableConfig.metadata`, `callbacks`, `configurable`, and runtime context are business state; tracing must not delete, replace, or reinterpret them.
- Safe-mode filtering applies only at the OpenInference/span attribute boundary.
- Caller metadata never overrides authoritative `session.id`, `user.id`, `deerflow.*`, or OpenInference semantic attributes.
- Safe-mode raw `deerflow.run_id` is allowed only for a DeerFlow-generated random opaque UUID; caller-supplied or identity-derived values are replaced by an internal correlation UUID before export.
- Safe mode defaults to omitting user, session, and thread identifiers unless the configured identity policy pseudonymizes them.
- W3C baggage is propagation-only and is never copied into span attributes.
- DeerFlow initializes only `LangChainInstrumentor`; it does not scan `openinference_instrumentor` entry points.
- DeerFlow never uninstrument or shuts down host-owned components.
- Provider and instrumentor ownership are tracked independently.
- Provider construction uses `shutdown_on_exit=False`; no private `_atexit_handler` access is allowed.
- Successful-start runtime tracing failures become a no-op for that operation and never fail the business request.
- Diagnostics use bounded reason codes and numeric facts only; they never log rejected values, identifiers, metadata, tags, baggage, prompts, tool payloads, or exception messages.
- Update `backend/README.md` and `backend/CLAUDE.md` with the privacy and ownership contract.
- Run test/lint commands from `backend/`. Run Git, OpenSpec, and repository architecture/search commands from the repository root.
- Commit blocks mark suggested reviewable behavior boundaries; combine adjacent task commits when they cannot be reviewed independently instead of mechanically creating one commit per checkbox.

## Preflight Symbol and Call-Path Inventory

Run from the repository root before editing and record the summary in the PR description:

```bash
git status --short
rg -n 'inject_trace_metadata|build_phoenix_correlation_metadata|capture_content|metadata_allowlist' backend/packages/harness/deerflow backend/tests
rg -n 'entry_points|OPENINFERENCE_|_atexit_handler|shutdown_on_exit|LangChainInstrumentor' backend/packages/harness/deerflow/tracing backend/tests
rg -n 'initialize_phoenix|shutdown_phoenix|build_tracing_callbacks' backend/app backend/packages/harness/deerflow backend/tests
```

Use current symbols/callers as authoritative; plan line numbers are navigation hints only.

---

### Task 1: Freeze privacy and ownership configuration

**Files:**
- Modify: `backend/packages/harness/deerflow/config/tracing_config.py`
- Create: `backend/packages/harness/deerflow/tracing/adapters/__init__.py`
- Create: `backend/packages/harness/deerflow/tracing/adapters/phoenix/__init__.py`
- Create: `backend/packages/harness/deerflow/tracing/adapters/phoenix/types.py`
- Create: `backend/packages/harness/deerflow/tracing/adapters/phoenix/diagnostics.py`
- Modify: `backend/tests/test_tracing_config.py`
- Create: `backend/tests/test_phoenix_diagnostics.py`

**Interfaces:**
- Produces: `IdentityExportMode`, `Ownership`, `TelemetryOwnership`, and stable diagnostic reason codes.
- Extends: `PhoenixTracingConfig` with identity, metadata-size, tag-count, tag-size, and diagnostic-rate settings.
- Produces: `TracingDiagnostics.record(reason_code, **numeric_facts) -> None`.

- [ ] **Step 1: Write failing configuration and diagnostic tests**

Pin these defaults and validation rules:

```python
def test_safe_mode_defaults_to_omitting_identifiers(monkeypatch):
    monkeypatch.setenv("PHOENIX_TRACING", "true")
    monkeypatch.setenv("PHOENIX_CAPTURE_CONTENT", "false")
    config = load_tracing_config().phoenix
    assert config.identity_export_mode is IdentityExportMode.OMIT


def test_content_capture_defaults_to_raw_identifiers(monkeypatch):
    monkeypatch.setenv("PHOENIX_TRACING", "true")
    monkeypatch.setenv("PHOENIX_CAPTURE_CONTENT", "true")
    monkeypatch.delenv("PHOENIX_IDENTITY_MODE", raising=False)
    assert load_tracing_config().phoenix.identity_export_mode is IdentityExportMode.RAW


def test_hmac_identity_mode_requires_a_key(monkeypatch):
    monkeypatch.setenv("PHOENIX_IDENTITY_MODE", "hmac_sha256")
    monkeypatch.delenv("PHOENIX_IDENTITY_HASH_KEY", raising=False)
    with pytest.raises(ValueError, match="PHOENIX_IDENTITY_HASH_KEY"):
        load_tracing_config()


def test_diagnostic_log_contains_no_rejected_value(caplog):
    diagnostics = TracingDiagnostics(logger=logging.getLogger("test"), interval_seconds=60)
    diagnostics.record(TracingDiagnosticReason.METADATA_INVALID_JSON, encoded_bytes=42)
    assert "METADATA_INVALID_JSON" in caplog.text
    assert "secret-value" not in caplog.text
```

Also test invalid identity modes, an explicit `raw` selection in safe mode producing a value-free privacy warning, non-positive byte/count limits, one warning per reason per interval, and a counter increment for every suppressed occurrence.

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
uv run pytest tests/test_tracing_config.py tests/test_phoenix_diagnostics.py -q
```

Expected: tests fail because the new enums, fields, and diagnostics class do not exist.

- [ ] **Step 3: Add typed values and configuration**

Define:

```python
class IdentityExportMode(StrEnum):
    OMIT = "omit"
    HMAC_SHA256 = "hmac_sha256"
    RAW = "raw"


class Ownership(StrEnum):
    DEERFLOW = "deerflow"
    HOST = "host"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class TelemetryOwnership:
    provider: Ownership
    langchain_instrumentor: Ownership
```

Add configuration for:

```text
PHOENIX_IDENTITY_MODE=omit|hmac_sha256|raw
PHOENIX_IDENTITY_HASH_KEY
PHOENIX_METADATA_MAX_BYTES=16384
PHOENIX_TAG_MAX_COUNT=32
PHOENIX_TAG_MAX_BYTES=128
PHOENIX_DIAGNOSTIC_INTERVAL_SECONDS=60
```

Reject `hmac_sha256` without a non-empty key. Default to `omit` in safe mode and `raw` when content capture is enabled. Treat safe-mode `raw` as an explicit operator override and emit a value-free warning.

- [ ] **Step 4: Implement bounded diagnostics**

Make `TracingDiagnostics` thread-safe. Track counters by reason code and guard the last-emitted timestamps with one lock. Accept only integer numeric facts at the public method; format no exception string or caller value. Expose a snapshot for tests and future metrics integration.

- [ ] **Step 5: Run focused tests and lint**

Run:

```bash
uv run pytest tests/test_tracing_config.py tests/test_phoenix_diagnostics.py -q
uv run ruff check packages/harness/deerflow/config/tracing_config.py packages/harness/deerflow/tracing/adapters/phoenix tests/test_tracing_config.py tests/test_phoenix_diagnostics.py
```

Expected: all commands pass.

- [ ] **Step 6: Commit Task 1**

```bash
git add backend/packages/harness/deerflow/config/tracing_config.py backend/packages/harness/deerflow/tracing/adapters backend/tests/test_tracing_config.py backend/tests/test_phoenix_diagnostics.py
git commit -m "refactor: define Phoenix privacy and ownership policy"
```

---

### Task 2: Filter OpenInference attributes with an instance-local config

**Files:**
- Create: `backend/packages/harness/deerflow/tracing/adapters/phoenix/privacy.py`
- Create: `backend/tests/test_phoenix_privacy.py`
- Modify: `backend/tests/test_phoenix_attribute_types.py`

**Interfaces:**
- Produces: non-dataclass `DeerFlowTraceConfig(TraceConfig)`.
- Produces: `filter_metadata_json(value: str, policy: PhoenixPrivacyPolicy) -> str | None`.
- Produces: `build_authoritative_attributes(spec, policy) -> Mapping[str, AttributeValue]`.
- Produces: `filter_caller_tags(tags, policy) -> tuple[str, ...]`.

- [ ] **Step 1: Write the metadata-mask matrix as failing tests**

Parameterize these cases:

```text
valid object + exact allowlisted top-level key -> stable filtered JSON
valid object + nested dict/list value -> nested value retained unchanged
invalid JSON -> None
JSON array/scalar/null -> None
reserved langfuse_* key -> omitted even if allowlisted
authoritative attribute name -> omitted from caller metadata
encoded input above limit -> None
filtered output above limit -> None
callable value -> evaluated exactly once by TraceConfig.mask
```

Add identity tests for `omit`, deterministic HMAC-SHA256 pseudonyms using a fixed test key, and explicit `raw`. Add run-ID tests proving gateway/worker UUIDs export raw, embedded/direct paths generate an equivalent random opaque UUID, caller metadata cannot spoof it, and user/session/thread values are not inputs to its generation. Add tag tests for safe-mode omission, full-mode count/UTF-8 byte caps, stable order, and no raw rejected value in diagnostics. Add a baggage test that proves propagated baggage is absent from produced attributes.

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
uv run pytest tests/test_phoenix_privacy.py tests/test_phoenix_attribute_types.py -q
```

Expected: collection fails because `privacy.py` and its public values do not exist.

- [ ] **Step 3: Implement a non-dataclass TraceConfig subclass**

Use a normal subclass so OpenInference's dataclass `TraceConfig.__post_init__()` does not inspect DeerFlow-only fields:

```python
class DeerFlowTraceConfig(TraceConfig):
    def __init__(self, *, policy: PhoenixPrivacyPolicy, diagnostics: TracingDiagnostics, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        object.__setattr__(self, "_deerflow_policy", policy)
        object.__setattr__(self, "_deerflow_diagnostics", diagnostics)

    def mask(self, key: str, value: Any) -> Any:
        masked = super().mask(key, value)
        if masked is None or key != SpanAttributes.METADATA:
            return masked
        return filter_metadata_json(masked, self._deerflow_policy, self._deerflow_diagnostics)
```

Do not decorate this subclass with `@dataclass` and do not override `__post_init__`.

- [ ] **Step 4: Implement fail-closed JSON filtering and authoritative attributes**

Parse the metadata string once, require a JSON object, retain exact top-level allowlist keys only, reject provider-reserved and authoritative names, and serialize with sorted keys and compact separators. Check the UTF-8 byte limit before parsing and after filtering. Return `None` on parsing/type/size failure and record only the stable reason and byte count.

Build authoritative attributes separately from caller metadata. Under safe mode export only `deerflow.run_id`, `deerflow.span.role`, `deerflow.run.outcome`, `deerflow.trace_parent_mode`, optional `deerflow.trace_parent_fallback`, and the OpenInference span kind; apply `IdentityExportMode` to user/session/thread values. Treat `deerflow.run_id` as an opaque operational correlation identifier: accept only the trusted server-side run/root-context UUID, never a same-named metadata field, and generate a new random UUID at embedded/direct composition when no trusted run ID exists. PR 3 carries the same trusted value in `TraceRunSpec`. Export agent name, root run name, and model name only when content capture is enabled. Compute each pseudonym as HMAC-SHA256 over `b"deerflow-phoenix-identity-v1\0" + attribute_name.encode() + b"\0" + value.encode()` and never include the key in a span or log.

- [ ] **Step 5: Run focused tests and lint**

Run:

```bash
uv run pytest tests/test_phoenix_privacy.py tests/test_phoenix_attribute_types.py -q
uv run ruff check packages/harness/deerflow/tracing/adapters/phoenix/privacy.py tests/test_phoenix_privacy.py tests/test_phoenix_attribute_types.py
```

Expected: all commands pass.

- [ ] **Step 6: Commit Task 2**

```bash
git add backend/packages/harness/deerflow/tracing/adapters/phoenix/privacy.py backend/tests/test_phoenix_privacy.py backend/tests/test_phoenix_attribute_types.py
git commit -m "refactor: filter Phoenix attributes at instrumentation boundary"
```

---

### Task 3: Preserve canonical execution metadata

**Files:**
- Modify: `backend/packages/harness/deerflow/tracing/metadata.py`
- Modify: `backend/packages/harness/deerflow/runtime/runs/worker.py:248-305`
- Modify: `backend/packages/harness/deerflow/client.py`
- Modify: `backend/packages/harness/deerflow/subagents/executor.py`
- Modify: `backend/tests/test_tracing_metadata.py`
- Modify: `backend/tests/test_worker_langfuse_metadata.py`
- Modify: `backend/tests/test_client_langfuse_metadata.py`
- Create: `backend/tests/test_tracing_business_metadata_invariance.py`

**Interfaces:**
- Preserves: `inject_langfuse_metadata()` for the existing callback contract.
- Replaces: Phoenix-safe metadata reconstruction with an export-only `PhoenixExportAttributes` value built from a read-only snapshot.

- [ ] **Step 1: Write business-metadata invariance tests**

Build one config containing `tool_groups`, `available_skills`, `langfuse_session_id`, a caller key, and attempted authoritative-key overrides. Exercise worker, embedded client, direct graph, and subagent setup with:

```text
tracing disabled
Phoenix content capture enabled
Phoenix safe mode
Phoenix safe mode with auto-instrument disabled
```

Build the tracing-disabled result first as `business_baseline`. For every Phoenix mode assert:

```python
assert runnable_config["metadata"] == business_baseline["metadata"]
assert runnable_config["callbacks"] is original_callbacks
assert runnable_config["configurable"] == business_baseline["configurable"]
```

This comparison permits the existing provider-neutral/Langfuse business augmentation to exist in both baselines while proving Phoenix adds no mutation. Also assert Langfuse fields remain available to Langfuse callback preparation, Phoenix export excludes `langfuse_*`, and caller attempts cannot replace authoritative exported attributes.

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
uv run pytest tests/test_tracing_business_metadata_invariance.py tests/test_tracing_metadata.py tests/test_worker_langfuse_metadata.py tests/test_client_langfuse_metadata.py -q
```

Expected: safe-mode cases fail because the worker and metadata helper currently rebuild `RunnableConfig.metadata`.

- [ ] **Step 3: Separate business metadata from export attributes**

Change `metadata.py` so `inject_trace_metadata()` never removes caller fields. Keep Langfuse's existing provider contract intact. Move Phoenix-specific allowlisting and authoritative-attribute construction behind the adapter API from Task 2, passing a copy/read-only view into that API rather than assigning its result to `config["metadata"]`.

Delete the worker's post-factory safe-mode reconstruction at lines 289-305. Remove equivalent Phoenix sanitization assignments from client and executor paths. Do not delete `langfuse_*` or policy fields from canonical metadata in this PR; PR 1 has already removed authorization's dependency on those policy fields.

- [ ] **Step 4: Prove trace conversion cannot mutate nested business data**

Add a regression case with nested dictionaries and lists. Snapshot using `copy.deepcopy`, run attribute conversion, and compare the canonical object to the snapshot. Use immutable copies inside the privacy adapter before normalization so nested values cannot be rewritten in place.

- [ ] **Step 5: Run focused and existing Phoenix metadata tests**

Run:

```bash
uv run pytest tests/test_tracing_business_metadata_invariance.py tests/test_tracing_metadata.py tests/test_worker_langfuse_metadata.py tests/test_client_langfuse_metadata.py tests/test_gateway_phoenix_context.py -q
uv run ruff check packages/harness/deerflow/tracing/metadata.py packages/harness/deerflow/runtime/runs/worker.py packages/harness/deerflow/client.py packages/harness/deerflow/subagents/executor.py tests/test_tracing_business_metadata_invariance.py
```

Expected: all commands pass.

- [ ] **Step 6: Commit Task 3**

```bash
git add backend/packages/harness/deerflow/tracing/metadata.py backend/packages/harness/deerflow/runtime/runs/worker.py backend/packages/harness/deerflow/client.py backend/packages/harness/deerflow/subagents/executor.py backend/tests/test_tracing_metadata.py backend/tests/test_worker_langfuse_metadata.py backend/tests/test_client_langfuse_metadata.py backend/tests/test_tracing_business_metadata_invariance.py
git commit -m "fix: keep tracing from rewriting business metadata"
```

---

### Task 4: Initialize only explicitly owned telemetry components

**Files:**
- Create: `backend/packages/harness/deerflow/tracing/adapters/phoenix/instrumentation.py`
- Modify: `backend/packages/harness/deerflow/tracing/phoenix.py`
- Modify: `backend/tests/test_phoenix_provider_lifecycle.py`
- Create: `backend/tests/test_phoenix_instrumentation_ownership.py`
- Modify: `backend/tests/test_tracing_factory.py`

**Interfaces:**
- Produces: `initialize_phoenix_instrumentation(config) -> PhoenixInstrumentationState`.
- Produces: `PhoenixInstrumentationState.ownership: TelemetryOwnership`.
- Produces: idempotent `PhoenixInstrumentationState.shutdown(timeout_millis) -> None`.
- Produces: `DiagnosticSpanExporter`, a transparent wrapper that converts delegate exceptions or `SpanExportResult.FAILURE` into bounded diagnostics without exposing span content.

- [ ] **Step 1: Write ownership and scope tests**

Cover this matrix:

```text
no provider + no instrumentor -> DeerFlow owns both
host provider + no instrumentor -> host provider, DeerFlow instrumentor
DeerFlow provider + host instrumentor -> DeerFlow provider, host instrumentor
host provider + host instrumentor -> host owns both
auto-instrument false -> provider ownership independent, instrumentor NONE/HOST
```

Patch `importlib.metadata.entry_points` to raise if called. Assert initialization succeeds without reading it. Register a fake unrelated OpenInference instrumentor and assert DeerFlow neither instruments nor uninstalls it. Assert the exact `LangChainInstrumentor` receives `tracer_provider=provider` and `config=DeerFlowTraceConfig(...)`.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
uv run pytest tests/test_phoenix_instrumentation_ownership.py tests/test_phoenix_provider_lifecycle.py tests/test_tracing_factory.py -q
```

Expected: tests fail because current initialization scans all entry points and does not represent ownership independently.

- [ ] **Step 3: Construct standard OpenTelemetry components directly**

In `instrumentation.py`, create a dedicated `TracerProvider(shutdown_on_exit=False, resource=...)`, `BatchSpanProcessor`, and OTLP HTTP span exporter when DeerFlow owns the provider. Wrap the exporter in `DiagnosticSpanExporter` before handing it to the processor. Supply endpoint, headers/API key, and project resource attributes from validated `PhoenixTracingConfig` without writing environment variables.

Detect an already instrumented LangChain instrumentor only through its public `is_instrumented_by_opentelemetry` property. If host-owned, record ownership and leave it untouched. If DeerFlow-owned, call only:

```python
LangChainInstrumentor().instrument(
    tracer_provider=provider,
    config=DeerFlowTraceConfig(...),
)
```

Do not use `phoenix.otel.register()`, import entry points, or set any `OPENINFERENCE_*`/`OTEL_*` environment variable.

- [ ] **Step 4: Implement ownership-aware lifecycle**

On export, the diagnostic wrapper returns the delegate's result; it catches delegate `Exception`, records `EXPORTER_EXCEPTION`, and returns `SpanExportResult.FAILURE`, while an ordinary failure result records `EXPORTER_FAILURE`. It never formats spans or exception text. On shutdown, uninstrument only a DeerFlow-owned LangChain instrumentor and flush/shutdown only a DeerFlow-owned provider. Make shutdown thread-safe and idempotent. A host-owned provider may be force-flushed only if the host explicitly passed a callback that grants that operation; default behavior is no lifecycle call.

Keep a compatibility delegation in `tracing/phoenix.py` so existing PR 2 call sites use the new state object. Remove entry-point scanning, environment save/restore, and all `_atexit_handler` inspection/unregistration from provider initialization. Do not move the exact compatibility layer's dynamic class replacement into `instrumentation.py`; it remains legacy adapter-internal code until PR 4 isolates it in `exact_parentage.py` and may not own provider lifecycle.

- [ ] **Step 5: Run focused tests and source guards**

Run tests/lint from `backend/`:

```bash
uv run pytest tests/test_phoenix_instrumentation_ownership.py tests/test_phoenix_provider_lifecycle.py tests/test_tracing_factory.py -q
uv run ruff check packages/harness/deerflow/tracing/adapters/phoenix/instrumentation.py packages/harness/deerflow/tracing/phoenix.py tests/test_phoenix_instrumentation_ownership.py tests/test_phoenix_provider_lifecycle.py
```

Run the source guard from the repository root:

```bash
! rg -n 'entry_points\(|_atexit_handler|OPENINFERENCE_.*=.*|tracer\.__class__\s*=' backend/packages/harness/deerflow/tracing/adapters/phoenix/instrumentation.py
```

Expected: tests and lint pass; the source guard returns success because no forbidden match exists in the new instrumentation module.

- [ ] **Step 6: Commit Task 4**

```bash
git add backend/packages/harness/deerflow/tracing/adapters/phoenix/instrumentation.py backend/packages/harness/deerflow/tracing/phoenix.py backend/tests/test_phoenix_provider_lifecycle.py backend/tests/test_phoenix_instrumentation_ownership.py backend/tests/test_tracing_factory.py
git commit -m "refactor: own Phoenix instrumentation explicitly"
```

---

### Task 5: Freeze startup, runtime-failure, and shutdown behavior

**Files:**
- Modify: `backend/packages/harness/deerflow/tracing/phoenix.py`
- Modify: `backend/app/gateway/app.py`
- Modify: `backend/packages/harness/deerflow/client.py`
- Modify: `backend/tests/test_gateway_lifespan_shutdown.py`
- Modify: `backend/tests/test_phoenix_provider_lifecycle.py`
- Create: `backend/tests/test_phoenix_runtime_failure_semantics.py`

**Interfaces:**
- Produces: typed startup configuration errors before serving requests.
- Guarantees: post-start span/attribute/export setup errors return a no-op scope and record diagnostics.
- Guarantees: shutdown is bounded, public-API-only, and idempotent.

- [ ] **Step 1: Write failing lifecycle and failure-semantic tests**

Assert:

```text
invalid endpoint/config -> startup fails before gateway serves
required inbound parent missing -> existing explicit request policy still rejects
span creation raises after startup -> business callable runs and returns unchanged value
attribute conversion raises after startup -> no-op trace, unchanged business config/result
exporter worker reports failure -> request result is unchanged and diagnostic counter increments
two shutdown calls -> owned components each shut down once
shutdown racing an in-flight request -> request is not cancelled by tracing
```

Use fakes for provider, processor, exporter, and scope; do not require a live Phoenix server.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
uv run pytest tests/test_phoenix_runtime_failure_semantics.py tests/test_gateway_lifespan_shutdown.py tests/test_phoenix_provider_lifecycle.py -q
```

Expected: runtime exceptions currently escape or lack stable diagnostics and lifecycle assertions.

- [ ] **Step 3: Implement the failure boundary**

Validate configuration and required startup contracts before returning initialized state. After successful initialization, catch tracing-only `Exception` values at span creation, attribute conversion, exact binding, and context activation boundaries. Do not catch `BaseException` process-control/cancellation values. Return the existing inert/no-op scope, record a reason code, and preserve the original business exception/result path. Do not catch authorization, graph, tool, model, or application exceptions as tracing failures.

Keep the configured inbound-parent-required rejection explicit because it is an operator-selected request admission rule, not an exporter failure.

- [ ] **Step 4: Implement bounded public shutdown**

Gateway lifespan owns the state it created and invokes its idempotent shutdown with the configured timeout. Use public `force_flush(timeout_millis)` and `shutdown()` methods only. Do not inspect or unregister SDK atexit handlers. Embedded/direct owners close only states they created; borrowed host state is untouched.

- [ ] **Step 5: Run focused tests and lint**

Run:

```bash
uv run pytest tests/test_phoenix_runtime_failure_semantics.py tests/test_gateway_lifespan_shutdown.py tests/test_phoenix_provider_lifecycle.py tests/test_gateway_run_drain_shutdown.py -q
uv run ruff check packages/harness/deerflow/tracing/phoenix.py packages/harness/deerflow/client.py app/gateway/app.py tests/test_phoenix_runtime_failure_semantics.py
```

Expected: all commands pass.

- [ ] **Step 6: Commit Task 5**

```bash
git add backend/packages/harness/deerflow/tracing/phoenix.py backend/packages/harness/deerflow/client.py backend/app/gateway/app.py backend/tests/test_gateway_lifespan_shutdown.py backend/tests/test_phoenix_provider_lifecycle.py backend/tests/test_phoenix_runtime_failure_semantics.py
git commit -m "fix: isolate Phoenix runtime failures from requests"
```

---

### Task 6: Document and verify the side-effect boundary

**Files:**
- Modify: `backend/README.md`
- Modify: `backend/CLAUDE.md`
- Modify: `openspec/specs/phoenix-tracing-provider/spec.md`
- Modify: `openspec/specs/phoenix-subagent-parentage/spec.md`

- [ ] **Step 1: Update operator and maintainer documentation**

Document safe-mode identity behavior, HMAC key requirements, caller metadata/tag/baggage handling, provider-versus-instrumentor ownership, host-owned privacy limitations, post-start fail-open tracing semantics, and explicit process shutdown. State that PR 2 still contains compatibility call sites scheduled for removal by PR 3.

- [ ] **Step 2: Update the canonical OpenSpec requirements**

Replace any requirement that describes global environment mutation, all-entry-point instrumentation, or business metadata rebuilding. Add scenarios for canonical metadata invariance, host-owned instrumentation, safe identifiers/tags/baggage, and tracing-only runtime failure.

- [ ] **Step 3: Run the complete PR 2 verification**

Run:

```bash
uv run pytest tests/test_tracing_config.py tests/test_phoenix_diagnostics.py tests/test_phoenix_privacy.py tests/test_phoenix_attribute_types.py tests/test_tracing_business_metadata_invariance.py tests/test_tracing_metadata.py tests/test_worker_langfuse_metadata.py tests/test_client_langfuse_metadata.py tests/test_phoenix_instrumentation_ownership.py tests/test_phoenix_provider_lifecycle.py tests/test_phoenix_runtime_failure_semantics.py tests/test_gateway_lifespan_shutdown.py tests/test_gateway_run_drain_shutdown.py -q
uv run pytest -q
uv run ruff check
cd ..
openspec validate --all --strict
git diff --check
```

Expected: the focused suite, full backend suite, Ruff, OpenSpec, and whitespace check pass.

- [ ] **Step 4: Verify exit criteria with source searches**

Run from the repository root:

```bash
! rg -n 'entry_points\(group="openinference_instrumentor"|_atexit_handler|tracer\.__class__\s*=' backend/packages/harness/deerflow/tracing/adapters/phoenix/instrumentation.py
! rg -n 'os\.environ\[.*(OPENINFERENCE|OTEL)|os\.environ\.update' backend/packages/harness/deerflow/tracing
rg -n 'shutdown_on_exit=False|LangChainInstrumentor' backend/packages/harness/deerflow/tracing/adapters/phoenix/instrumentation.py
```

Expected: the first two guards find no forbidden code; the final search finds explicit provider lifecycle and LangChain-only instrumentation.

- [ ] **Step 5: Commit documentation and spec updates**

```bash
git add backend/README.md backend/CLAUDE.md openspec/specs/phoenix-tracing-provider/spec.md openspec/specs/phoenix-subagent-parentage/spec.md
git commit -m "docs: define Phoenix tracing side-effect boundary"
```

## PR 2 Exit Gate

Do not merge until all conditions hold:

- Tracing never removes or overwrites canonical business metadata.
- Safe-mode filtering is exercised at the OpenInference/span attribute boundary.
- Identifier, tag, baggage, size-limit, and value-free diagnostic tests pass.
- No process-global privacy environment variable is written.
- No unrelated OpenInference instrumentor is discovered or modified.
- Provider and LangChain instrumentor ownership are independent and honored at shutdown.
- Provider lifecycle uses `shutdown_on_exit=False` and no private atexit field.
- Post-start tracing-only failures preserve business return values and exceptions.
- The full backend suite, Ruff, and strict OpenSpec validation pass.

## Rollout and Rollback

Deploy with `PHOENIX_TRACING=false`, then validate the required in-memory exporter suite. PR 2 may enter a standard Phoenix canary after its privacy/ownership gate and a real collector smoke pass; PR 3 is not required for that safety canary. If PR 2 must be rolled back, retain PR 1: the authorization repair is independent and must not be reverted with tracing.

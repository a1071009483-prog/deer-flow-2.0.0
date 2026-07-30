# ADR: Re-establish Provider-Neutral Tracing Boundaries

- **Date:** 2026-07-29
- **Status:** Accepted
- **Implementation status:** Planned
- **Owner:** DeerFlow harness maintainers
- **Deciders:** Harness, gateway/runtime, security, and observability maintainers
- **Decision scope:** Follow-up remediation for commit `bcd4c409` (`feat(tracing): add Phoenix OpenTelemetry tracing`)
- **Supersedes:** Conflicting architecture, privacy, ownership, and default-parentage decisions in `openspec/specs/phoenix-tracing-provider/spec.md`, `openspec/specs/phoenix-subagent-parentage/spec.md`, `openspec/changes/archive/2026-07-21-add-phoenix-tracing-provider/`, `openspec/changes/archive/2026-07-21-fix-phoenix-subagent-parentage/`, `backend/docs/phoenix-tracing-spike.md`, and `docs/porting/phoenix-v2.0.0.md`. Historical test and production-trace evidence remain valid records of what was observed.

## 1. Context

Commit `bcd4c409` added Phoenix/OpenTelemetry tracing with a strict target topology:

```text
task
└── deerflow.run
    └── subagent graph
        └── descendants
```

The implementation correctly addressed several hard tracing problems: non-global provider ownership, W3C propagation, baggage isolation, generator-yield context restoration, bounded shutdown, parallel subagent binding, and separation from `RunJournal`.

The implementation also crossed the observability boundary in five material ways:

1. Phoenix safe mode rebuilds `RunnableConfig.metadata` after the lead-agent factory runs. That removes `tool_groups` and `available_skills`, although `task_tool` uses those fields as the parent delegation policy. Missing `tool_groups` becomes `groups=None`, which means no configured-tool group filter. Missing `available_skills` can allow the subagent to load every enabled skill. Tracing can therefore widen subagent capabilities.
2. Exact parentage depends on private LangSmith and OpenInference APIs, exact dependency versions, private provider state, and dynamic replacement of a third-party tracer instance's class.
3. Initialization scans every `openinference_instrumentor` entry point, instruments unrelated integrations, and rejects any pre-instrumented entry point as foreign-owned.
4. Safe content capture mutates process-global `OPENINFERENCE_*` environment variables for the lifetime of the provider.
5. Phoenix-specific types, configuration branches, context capture, root scopes, and lifecycle calls appear throughout gateway, worker, client, model, task, and subagent execution paths.

The first issue is a release blocker. The remaining issues make Phoenix tracing disproportionately expensive to maintain and difficult to embed in an existing OpenTelemetry process.

Until PR 1 from this ADR is deployed, Phoenix must remain disabled in production:

```bash
PHOENIX_TRACING=false
```

Every backend gateway and worker process must be restarted after changing this setting because tracing configuration and instrumentation are process-scoped and cached.

## 2. Decision Drivers

The remediation must satisfy these drivers, in priority order:

1. Tracing must never change authorization, tool selection, skill inheritance, or other business execution semantics.
2. Privacy filtering must affect only the data presented to Phoenix/OpenInference, never the canonical business configuration.
3. Provider, instrumentor, exporter, and shutdown ownership must be explicit and process-scoped.
4. Core execution paths may depend on tracing concepts, but not on Phoenix, OpenInference, OTLP, or exact-parentage internals.
5. Standard tracing must tolerate ordinary LangChain upgrades and coexist with unrelated OpenInference integrations.
6. Strict callback-derived parentage must be opt-in and must expose whether it is active or degraded.
7. A base harness installation must import and run without Phoenix/OpenInference dependencies.
8. Existing W3C propagation, generator scope isolation, completion status, batch flush, and `RunJournal` separation must be preserved.

## 3. Options Considered

### Option A: Patch the safe-mode allowlist

Keep `tool_groups` and `available_skills` in `RunnableConfig.metadata`, but preserve or allowlist them when Phoenix content capture is disabled.

**Advantages:** Smallest code change and quickest local repair.

**Rejected because:** Authorization remains coupled to tracing metadata, policy becomes tracer-visible, future metadata filtering can recreate the same vulnerability, and the other lifecycle and dependency problems remain unchanged.

### Option B: Revert Phoenix and rebuild it in one replacement change

Revert `bcd4c409`, then introduce a minimal OTLP/OpenInference implementation from scratch.

**Advantages:** Produces the cleanest intermediate tree and avoids carrying compatibility code through the refactor.

**Rejected as the default migration path because:** The commit is already the main branch tip, contains validated context and lifecycle behavior worth preserving, and a large revert/reintroduction makes it harder to isolate and review the security fix. This remains an operational option if no downstream deployment depends on the commit.

### Option C: Four sequentially releasable remediation PRs

First remove authorization from tracing metadata, then remove privacy and process-global side effects, then isolate provider-neutral runtime boundaries, and finally make exact parentage and Phoenix dependencies optional.

**Accepted because:** It fixes the blocker first, keeps every stage testable and deployable, preserves behavior that remains inside the new boundaries, and prevents the architecture cleanup from delaying the security repair. The PRs are ordered dependencies, not independently cherry-pickable changes:

```text
PR 1
└── PR 2
    └── PR 3
        └── PR 4
```

## 4. Decision Summary

The remediation will ship as four PRs:

1. **Security boundary:** Bind an immutable per-agent delegation policy to a per-agent `task` tool. Remove policy fields from tracing metadata and remove the global unrestricted production task tool.
2. **Side-effect boundary:** Preserve canonical execution metadata, filter Phoenix attributes through an instance-local `TraceConfig`, explicitly instrument only LangChain, and use public provider lifecycle APIs with independent provider/instrumentor ownership.
3. **Core architecture boundary:** Introduce a minimal provider-neutral tracing runtime and remove every Phoenix/OpenInference type and condition from core production paths in one change.
4. **Compatibility and packaging boundary:** Make exact parentage an optional decorator with explicit activation status, move its version pins into an exact extra, and make the ordinary Phoenix stack optional to the base harness.

PR 1 and PR 2 make the implementation safe. PR 3 is the acceptance point for reduced core-code invasiveness. PR 4 removes private compatibility and packaging pressure from normal tracing.

## 5. Delegation Security Boundary

### 5.1 Policy model

Authorization policy is immutable business data:

```python
@dataclass(frozen=True, slots=True)
class DelegationPolicy:
    tool_groups: tuple[str, ...] | None
    available_skills: frozenset[str] | None


@dataclass(frozen=True, slots=True)
class DelegationRequest:
    subagent_type: str
    requested_tools: tuple[str, ...] | None
    disallowed_tools: tuple[str, ...]
    requested_skills: tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class ResolvedDelegation:
    parent_policy: DelegationPolicy
    request: DelegationRequest
    effective_skills: tuple[str, ...] | None
    tools: tuple[BaseTool, ...]
    parent_policy_fingerprint: str
    delegation_decision_fingerprint: str
    tool_catalog_fingerprint: str
```

`None` means unrestricted within the enclosing application configuration. An empty collection means no values are permitted.

| Parent | Child | Effective |
|---|---|---|
| `None` | `None` | unrestricted |
| `None` | `["a"]` | `["a"]` |
| `["a"]` | `None` | `["a"]` |
| `["a"]` | `["a", "b"]` | `["a"]` |
| `[]` | any value | `[]` |

Tool-group restrictions apply to configured tools. Safe built-in tools remain governed by their existing independent assembly and security rules; tests must not claim that `tool_groups=["web"]` means the final tool list contains only web tools.

### 5.2 One resolver

Exactly one function interprets delegation policy:

```python
def resolve_delegation(
    *,
    parent_policy: DelegationPolicy,
    request: DelegationRequest,
    app_config: AppConfig,
    parent_model: str | None,
) -> ResolvedDelegation:
    ...
```

It owns `None`/empty semantics, parent-child intersection, unknown-value rejection, model-aware configured-tool resolution, skill resolution, skill `allowed-tools` filtering, and fingerprint generation. `parent_model` selects the effective inherited model and its model-dependent tool catalog; it is catalog input, not caller-controlled authorization policy. Agent factories and executors do not reimplement intersection rules.

The resolver reuses the repository's existing tool and skill loading functions. PR 1 does not introduce general-purpose `ToolRegistry` or `SkillRegistry` frameworks.

### 5.3 Per-agent task tool

Every production agent receives a fresh task tool built with its trusted policy:

```python
build_task_tool(
    delegation_policy=policy,
)
```

The target task tool obtains the provider-neutral runtime reference from its invocation context. The authorization closure never captures Phoenix configuration or another tracing implementation detail. PR 1 introduces the policy-bound factory while preserving the then-current tracing handoff; PR 3 replaces that handoff with the runtime contract without changing the policy source.

When `subagent_enabled=True`, tool assembly requires an explicit `DelegationPolicy`; omission raises `DelegationPolicyError`. Production tool assembly no longer appends a module-level `SUBAGENT_TOOLS = [task_tool]` list and does not retain a global unrestricted task tool as a compatibility default.

The task tool invokes `resolve_delegation()` and passes the resulting `ResolvedDelegation` to `SubagentExecutor`. The executor consumes the result and records it for audit. Missing or inconsistent authorization data raises `DelegationPolicyError`; Python `assert` is never used as a security control.

### 5.4 Fingerprints

The implementation defines three distinct SHA-256 fingerprints over deterministic compact JSON:

- `parent_policy_fingerprint` identifies the immutable policy captured by a per-agent task tool. It contains `policy_version`, normalized `tool_groups`, and normalized `available_skills` only.
- `delegation_decision_fingerprint` identifies the normalized authorization result for one parent policy and child request. It contains the parent-policy fingerprint, `subagent_type`, normalized `requested_tools`, `disallowed_tools`, `requested_skills`, effective skill names, and effective qualified tool names.
- `tool_catalog_fingerprint` identifies the resolution-time catalog against which the decision was made. It contains source-qualified tool identities, callable JSON schemas, policy-relevant tool metadata, deferred-tool mode/catalog revision, and skill identities plus their content revision and normalized `allowed-tools` declarations.

The canonical delegation-decision payload is shaped as follows:

```json
{
  "delegation_decision_version": 1,
  "disallowed_tools": [],
  "effective_skills": ["research"],
  "effective_tools": ["configured:web_search"],
  "parent_policy_fingerprint": "sha256:...",
  "requested_skills": ["research", "writer"],
  "requested_tools": null,
  "subagent_type": "general-purpose"
}
```

Lists are sorted and duplicate-free before serialization. Catalog inputs are computed after MCP hot-reload and after configured, built-in, MCP, ACP, skill, and deferred-tool resolution. A skill content revision is a content digest, not an mtime alone. The hash never includes `BaseTool` objects, object addresses, `repr()` output, closures, filesystem paths, credentials, API keys, or other secret configuration values.

No fingerprint is a proof of tool-object integrity; `ResolvedDelegation` remains the trusted result produced by the single resolver, and the executor does not reconstruct or reinterpret it. A cache that retains a parent task-tool closure includes `parent_policy_fingerprint`. A cache that retains a resolved child agent or tool set includes both `delegation_decision_fingerprint` and `tool_catalog_fingerprint`. An unchanged authorization policy combined with a changed MCP, configured, deferred, or skill catalog must rebuild the affected cache entry.

## 6. Process and Run Lifecycles

### 6.1 Process-level services

Provider, instrumentor, exporter, ownership state, parentage status, and shutdown state belong to one process-level `TraceRuntime` created by application bootstrap.

The repository's existing `RunContext` already groups per-run infrastructure dependencies. The remediation makes the process/run distinction explicit:

```python
@dataclass(frozen=True, slots=True)
class ApplicationServices:
    trace_runtime: TraceRuntime


@dataclass(frozen=True, slots=True)
class RunInvocationContext:
    trace_runtime: TraceRuntime
    inbound_trace_carrier: TraceCarrier | None
    checkpointer: Any
    store: Any | None
    event_store: Any | None
    app_config: AppConfig
```

`ApplicationServices` owns process-lifetime services. Each `RunInvocationContext` references its `trace_runtime` and combines it with the current request's carrier, hot-reloaded `AppConfig`, and run dependencies. The implementation may retain the existing `RunContext` name for `RunInvocationContext`, but it must preserve the distinction:

```text
process bootstrap
└── create TraceRuntime once
    ├── provider
    ├── instrumentor
    ├── exporter
    ├── ownership
    └── parentage status

request/run creation
└── create RunInvocationContext
    ├── reference ApplicationServices.trace_runtime
    ├── store only this run's inbound carrier
    └── bind the current run dependencies/configuration
```

No request may re-instrument LangChain, create a replacement provider, or acquire independent exporter ownership.

Composition ownership is explicit for every supported entry point:

| Entry point | Runtime creator | Shutdown owner | Carrier source |
|---|---|---|---|
| Gateway | application bootstrap | application shutdown, after in-flight run drain | inbound HTTP headers |
| Worker | worker bootstrap or enclosing application services | the creator, after worker drain | task envelope |
| Embedded client | `deerflow.bootstrap` factory or injecting application | explicit `close()` / context exit by the creator | explicit caller carrier or ambient context captured at call time |
| Direct execution | direct-command bootstrap | command termination | explicit carrier or ambient context |
| Studio | Studio composition root | Studio lifecycle | Studio request context |

An injected runtime is borrowed and is never shut down by the consumer. A factory that creates both an embedded client and its runtime marks that runtime as owned and exposes deterministic `close()` and context-manager cleanup. The core `DeerFlowClient` implementation only stores and invokes the provider-neutral runtime; adapter selection remains in the composition factory.

### 6.2 Run-local state

`TraceCarrier`, `TraceRunSpec`, `TraceRunScope`, current spans, completion state, and exact graph-root bindings are run-local. They are returned or created per invocation and are never written back into mutable process-runtime state except for narrowly scoped, lock-protected exact-parentage registries owned by the optional decorator.

Gateway bootstrap owns the process runtime and shutdown. Request handling only extracts an inbound carrier and references that runtime. `DeerFlowClient`, direct execution, and Studio execution use explicit composition paths; core code does not locate a runtime through a service locator.

## 7. Provider-Neutral Runtime Contract

The core contract remains intentionally small:

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


class TraceRuntime(Protocol):
    def capture_carrier(self) -> TraceCarrier | None: ...

    def open_run_scope(
        self,
        spec: TraceRunSpec,
        parent: TraceCarrier | None,
    ) -> TraceRunScope: ...

    def shutdown(self, timeout_millis: int) -> None: ...


class TraceRunOutcome(StrEnum):
    COMPLETED = "completed"
    ERROR = "error"
    CANCELLED = "cancelled"
    EARLY_CLOSED = "early_closed"


class TraceRunScope(Protocol):
    def activate(self) -> ContextManager[None]: ...

    def close(
        self,
        outcome: TraceRunOutcome,
        error: BaseException | None = None,
    ) -> None: ...
```

`TraceCarrier.from_headers()` is a bounded, provider-neutral input boundary. It does not perform vendor or full W3C semantic parsing, but it enforces these UTF-8 byte limits before copying values into run context or a task envelope:

| Input | Maximum bytes |
|---|---:|
| `traceparent` | 512 |
| `tracestate` | 512 |
| `baggage` | 8,192 |
| Combined values | 9,216 |

Non-string values and over-limit fields are dropped. If the combined limit is exceeded, all three values are dropped. `TraceCarrierRejection` contains only the field name, a stable reason code (`NON_STRING`, `FIELD_TOO_LARGE`, or `TOTAL_TOO_LARGE`), and an observed byte count when one can be computed; it never contains the rejected value. A carrier containing only rejections is retained long enough for the runtime to increment bounded diagnostics, then no rejected value is propagated or parsed.

The OpenTelemetry adapter performs W3C semantic validation later. Invalid or rejected input is fail-open for business execution and behaves as no parent. The existing explicit `PHOENIX_TRACE_PARENT_REQUIRED=true` policy remains the only exception: if it requires a valid parent, missing, rejected, or semantically invalid `traceparent` rejects that invocation through the existing typed ingress error.

The public scope returned to core code is always a `FailOpenTraceRunScope`. The OpenTelemetry implementation uses two layers:

```python
class OwnedTraceRunScope:
    """Strict internal scope; misuse raises TraceScopeOwnershipError."""


class FailOpenTraceRunScope:
    """Production wrapper; tracing failures become inert operations."""
```

`OwnedTraceRunScope` owns span/context state and raises `TraceScopeOwnershipError` for concurrent, re-entrant, or cross-owner activation. `FailOpenTraceRunScope.activate()` catches tracing-only `Exception` values raised while constructing, entering, or exiting the internal activation; it records a stable reason code and yields an inert activation while preserving the business block's return value, exception, and cancellation. It ignores an internal context manager's suppression return so tracing cannot suppress a business exception. `close()` applies the same fail-open wrapper. Process-control `BaseException` values originating from business code are never swallowed.

Internal unit tests target `OwnedTraceRunScope` and assert strict errors. Core integration tests target the public runtime and assert the same misuse or injected internal failure cannot change business behavior.

`open_run_scope()` returns an already-started scope with no context left attached to the caller. `OpenTelemetryTraceRuntime` creates it with `tracer.start_span(..., context=parent_context)`; it does not call `start_as_current_span()` and then detach, so scope creation cannot transiently pollute the caller context. `TraceRunScope` is intentionally not itself a context manager: a long-lived `with scope` around a yielding generator would leak the current span across caller-visible suspension points. Instead, every synchronous `next()`/`send()`/`throw()`/`close()` or asynchronous iterator advancement is wrapped independently:

```python
scope = trace_runtime.open_run_scope(spec, parent)
try:
    with scope.activate():
        item = next(iterator)
except StopIteration:
    scope.close(TraceRunOutcome.COMPLETED)
    raise
except GeneratorExit:
    scope.close(TraceRunOutcome.EARLY_CLOSED)
    raise
except asyncio.CancelledError as exc:
    scope.close(TraceRunOutcome.CANCELLED, exc)
    raise
except BaseException as exc:
    scope.close(TraceRunOutcome.ERROR, exc)
    raise
```

The contract is:

- `activate()` attaches only the scope's saved context and always restores the previous context on exit, including exceptions and cancellation.
- `close()` is idempotent. The first call wins; later calls do nothing.
- `COMPLETED` maps to OTel `OK`; `ERROR` records a privacy-filtered exception and maps to `ERROR`; `CANCELLED` and `EARLY_CLOSED` retain `UNSET` status and emit only a controlled outcome attribute.
- An owned scope belongs to one invocation and one owning thread or asyncio task. It may be activated repeatedly but never concurrently, re-entrantly, or from a different owner. Internal misuse raises `TraceScopeOwnershipError`; the public fail-open wrapper converts that tracing error to inert activation. Cross-thread/subagent handoff captures a `TraceCarrier` and opens a new scope instead of sharing a scope object.
- `TraceRunSpec.run_id` is sufficient for the optional decorator to perform graph-root correlation internally. The core contract has no exact-parentage or provider-specific bind method.
- Application shutdown first stops admission and drains in-flight runs. If the drain deadline expires, the runtime closes its remaining scopes as `EARLY_CLOSED`, performs bounded flush/shutdown, and makes later scope operations idempotent no-ops.

### 7.1 Implementations

Only these implementations are introduced:

1. `NoOpTraceRuntime`
2. `OpenTelemetryTraceRuntime`
3. `ExactParentageDecorator`, enabled only by the Phoenix adapter

There is no provider registry, dynamic plugin loader, or multi-exporter orchestration framework in this remediation.

Core production files import `TraceRuntime`, `TraceCarrier`, `TraceRunSpec`, and `TraceRunScope` only from `deerflow.tracing.runtime`. They may not import Phoenix, OpenInference, OpenTelemetry, OTLP exporter classes, runtime implementations, parent-compatibility helpers, or Phoenix environment configuration.

### 7.2 Failure semantics

Authorization and observability use different failure policies:

| Failure class | Required behavior |
|---|---|
| Delegation policy, unknown tool/skill, or catalog resolution failure | Fail closed with `DelegationPolicyError`; do not construct or run the subagent. |
| Phoenix explicitly enabled with invalid endpoint, privacy, ownership, or parentage configuration | Fail application startup with a typed configuration error. Never silently reinterpret invalid configuration. |
| `require_exact` startup validation failure | Fail startup before accepting work. |
| `PHOENIX_TRACE_PARENT_MODE=child` with `PHOENIX_TRACE_PARENT_REQUIRED=true` and missing/invalid parent | Reject that invocation with the existing typed required-parent error; this is an operator-selected ingress rule. |
| `capture_carrier()`, carrier rejection reporting, span creation, attribute conversion, scope ownership/activation, exporter, processor, or exact correlation failure after successful startup | Preserve business execution. Return/use a no-op scope or skip the failed tracing operation, then emit a rate-limited structured event and counter. |
| Shutdown flush/export timeout | Log and count the timeout, abandon tracing cleanup after the configured bound, and do not delay process termination indefinitely. |

After successful startup, tracing implementation errors must not alter tools, skills, metadata, callbacks, graph inputs, return values, exception behavior, or cancellation behavior. Runtime fallback catches ordinary instrumentation `Exception` values at the adapter boundary; it does not swallow process-control `BaseException` values raised by the business operation. Exporter worker failures are contained by the span processor and never surface through request execution.

Operational events use stable reason codes and bounded cardinality. Repeated warnings are limited to one event per reason code per process per 60-second window; counters still record every occurrence. In safe mode they record no raw metadata, tag, baggage, identifier, prompt, tool payload, or exception message. A tracing failure may change `ParentageStatus` to degraded but cannot mutate business state.

### 7.3 Concurrency and idempotency

All public `TraceRuntime` methods are thread-safe. Initialization and `shutdown()` are idempotent, and only the component recorded as owner may shut down or uninstrument a resource. Concurrent calls racing with shutdown either complete against the live runtime or receive a no-op scope; they never access a half-closed provider.

The exact decorator's run-ID registry is lock-protected, removes entries on consumption and on every scope-close path, and has a finite `PHOENIX_EXACT_REGISTRY_MAX_ENTRIES` capacity whose default is 4,096. Capacity exhaustion is a post-start tracing failure under both `prefer_exact` and `require_exact`: standard/manual tracing continues, exact correlation for that invocation is skipped, `deerflow.trace_parent_fallback=exact_registry_capacity` is attached when a fallback span exists, and the rate-limited event plus runtime counters are updated. Startup `active_mode` remains `exact`.

## 8. Privacy Boundary

### 8.1 Canonical metadata remains canonical

Phoenix/OpenTelemetry never removes, replaces, or filters caller entries in `RunnableConfig.metadata`. The safe-mode post-factory rebuild is deleted.

Existing Langfuse behavior remains in scope: the Langfuse callback path may append its documented `langfuse_*` reserved keys without deleting caller metadata. Phoenix privacy configuration must reject those keys from Phoenix export without removing them from the business config.

### 8.2 Authoritative attributes are not automatically privacy-safe

Server-owned correlation fields are represented as dedicated attributes rather than caller metadata:

```text
session.id
user.id
deerflow.run_id
deerflow.thread_id
deerflow.agent_name
deerflow.root_run_name
deerflow.model_name
```

`SpanAttributes.METADATA` contains only filtered caller metadata. Caller fields named like authoritative attributes are rejected from Phoenix metadata and cannot override the dedicated server-owned attributes.

Authority prevents spoofing; it does not make an identifier non-sensitive. `user.id`, `session.id`, and `deerflow.thread_id` are governed by:

```text
PHOENIX_IDENTITY_MODE=omit
PHOENIX_IDENTITY_MODE=hmac_sha256
PHOENIX_IDENTITY_MODE=raw
```

- If unset, the default is `omit` when `PHOENIX_CAPTURE_CONTENT=false` and `raw` when content capture is enabled.
- `omit` exports none of the three identity attributes.
- `hmac_sha256` exports domain-separated HMAC-SHA256 pseudonyms and requires a non-empty `PHOENIX_IDENTITY_HASH_KEY`; a missing key fails startup. The key is never logged, fingerprinted, or exported.
- `raw` exports the authoritative values. Selecting it in safe content mode is allowed only as an explicit operator choice and emits a structured privacy warning without the values.

Safe content mode always exports only these controlled run attributes: `deerflow.run_id`, `deerflow.span.role`, `deerflow.run.outcome`, `deerflow.trace_parent_mode`, `deerflow.trace_parent_fallback` when present, and the OpenInference span kind. Agent name, root run name, and model name are exported only in content-capture mode; deployments needing them in safe mode add separately reviewed, dedicated configuration rather than smuggling them through caller metadata.

`deerflow.run_id` is classified as an opaque operational correlation identifier, not an identity attribute. Safe mode may export it raw only when DeerFlow generated it as a random UUID (gateway/worker) or the embedded/direct composition root generated an equivalent random opaque value. Caller metadata can neither supply nor override it. A caller-supplied, semantic, identity-derived, or externally reusable run identifier is not eligible for this exception and must be replaced by a DeerFlow-generated trace correlation ID before export. Tests cover UUID generation, spoof rejection, and the absence of user/session/thread derivation.

### 8.3 Tags, baggage, and diagnostics

Caller tags are omitted in safe content mode. In content-capture mode they preserve caller order after deduplication, with at most 32 tags and at most 128 UTF-8 bytes per tag; excess or overlong tags are dropped rather than truncated. Provider-reserved or DeerFlow-internal tags are created through dedicated adapter fields, never accepted as authoritative caller tags.

W3C baggage is propagation-only. `PHOENIX_PROPAGATE_BAGGAGE=false` remains the default; when enabled, baggage can be forwarded in an outbound carrier but DeerFlow never converts baggage entries into span attributes, metadata, tags, logs, or metrics. Crossing a trust boundary with baggage remains an explicit operator responsibility.

Privacy and degradation warnings contain only a stable reason code, component name, and bounded numeric facts such as encoded byte count or dropped-tag count. They never contain raw metadata, tags, baggage, identifiers, HMAC keys, rejected values, tool payloads, prompts, or exception messages.

### 8.4 Instance-local OpenInference configuration

DeerFlow uses a non-dataclass subclass of the public `TraceConfig`:

```python
class DeerFlowTraceConfig(TraceConfig):
    def __init__(
        self,
        *,
        metadata_allowlist: tuple[str, ...] | None,
        metadata_max_bytes: int,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        object.__setattr__(self, "_deerflow_metadata_allowlist", metadata_allowlist)
        object.__setattr__(self, "_deerflow_metadata_max_bytes", metadata_max_bytes)

    def mask(self, key: str, value: Any) -> Any:
        ...
```

It is intentionally not a dataclass with additional fields. The pinned upstream `TraceConfig.__post_init__()` iterates all dataclass fields and expects each field to define `env_var` and `default_value` metadata; adding ordinary dataclass fields would break that contract.

Metadata masking always performs these steps:

1. Call `super().mask()` exactly once, which also evaluates a callable value exactly once.
2. For keys other than `SpanAttributes.METADATA`, return the upstream result.
3. Parse the metadata JSON string.
4. Reject invalid JSON or a non-object top-level value by returning `None`.
5. Reject authoritative keys and provider-reserved prefixes, including `langfuse_*`.
6. When `metadata_allowlist` is not `None`, keep exact top-level allowlist keys only.
7. Serialize deterministic compact JSON in sorted-key order.
8. If the UTF-8 encoded result exceeds 16,384 bytes, drop the complete metadata attribute and emit a rate-limited warning; never return an unfiltered or partially truncated value.

Nested dict/list values are permitted only as the complete value of a retained top-level key and remain subject to the total 16,384-byte limit. Safe mode supplies the configured allowlist. Content-capture mode supplies `None`, which keeps non-reserved caller metadata but still enforces reserved-key rejection, JSON validity, and the total size limit. For non-metadata attributes, content-capture mode preserves upstream OpenInference behavior.

The same `DeerFlowTraceConfig` instance is passed to the explicit LangChain instrumentor and reused by the Phoenix adapter when it filters metadata for DeerFlow's manual run span. A raw OTel `TracerProvider` does not receive OpenInference configuration. No `OPENINFERENCE_*` environment variable is modified.

## 9. Instrumentation and Ownership

Initialization explicitly creates only `LangChainInstrumentor`. It does not enumerate the `openinference_instrumentor` entry-point group and never instruments or un-instruments unrelated integrations.

Provider and LangChain instrumentor ownership are independent:

```python
class Ownership(Enum):
    DEERFLOW = "deerflow"
    HOST = "host"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class TelemetryOwnership:
    provider: Ownership
    langchain_instrumentor: Ownership
```

Valid states include a DeerFlow-owned manual-span provider with a host-owned LangChain instrumentor. Shutdown is called only for a DeerFlow-owned provider. Uninstrumentation is called only for a DeerFlow-owned LangChain instrumentor.

When `PHOENIX_AUTO_INSTRUMENT=false`, LangChain instrumentor ownership is `NONE` and only DeerFlow's manual run spans are produced. When it is enabled, bootstrap either creates one DeerFlow-owned `LangChainInstrumentor` or records an already active instance as `HOST`; it never rebinds that instance to DeerFlow's provider.

In host-owned instrumentor mode:

- DeerFlow does not replace, rebind, restore, or unload the host instrumentor.
- DeerFlow can still export its manual `deerflow.run` spans through its owned provider.
- `PHOENIX_CAPTURE_CONTENT=false` does not guarantee privacy for LangChain spans created by the host-owned instrumentor.
- Startup emits a clear structured warning describing that privacy boundary.

The standard adapter constructs the OTel SDK `TracerProvider`, batch span processor, and OTLP/HTTP exporter directly rather than relying on `phoenix.otel.register()`. Provider construction uses the public `shutdown_on_exit=False` option. Gateway/application shutdown explicitly performs bounded `force_flush()` and `shutdown()`. Code no longer reads, unregisters, or assigns the private `_atexit_handler` field.

## 10. Parentage Modes

Parentage behavior uses one enum rather than overlapping booleans:

```text
PHOENIX_PARENTAGE_MODE=standard
PHOENIX_PARENTAGE_MODE=prefer_exact
PHOENIX_PARENTAGE_MODE=require_exact
```

- `standard` is the default. It uses normal OTel ambient/W3C parentage or span links and never imports the exact module.
- `prefer_exact` tries the locked private compatibility adapter at process startup. Incompatibility produces a structured warning and activates standard mode.
- `require_exact` validates and installs exact compatibility at process startup. Any incompatibility fails startup before requests are accepted.

`require_exact` is a startup capability guarantee, not a per-request topology SLA. It guarantees that the exact adapter and its private compatibility contract activated successfully before the process accepted work. After startup, registry capacity, exporter, context, or private runtime failures still follow the global fail-open observability rule: the affected request continues with standard/no-op tracing. Making business availability depend on exact topology would conflict with this ADR and requires a separate decision that explicitly reverses the fail-open priority.

Initialization records the requested and active state:

```python
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

`reason_detail` is bounded diagnostic text and must not be used as a metric label. `prefer_exact` startup degradation must be observable through structured startup logging and a counter keyed by `reason_code`. The runtime status accessor returns an immutable `ParentageRuntimeStatus` snapshot. Runtime fallbacks increment `exact_runtime_fallback_total` and the matching `fallback_counts` entry without changing `startup.active_mode="exact"`; the startup fact remains true. When a standard fallback span exists, it carries `deerflow.trace_parent_fallback=<reason_code>`. If span construction itself failed, the counters and bounded diagnostic event remain the observable signal. Adding a public health/debug endpoint is outside this remediation unless an existing operational consumer requires it.

This enum controls callback-derived exact topology only. It does not replace the existing inbound W3C parent policy (`PHOENIX_TRACE_PARENT_MODE` and `PHOENIX_TRACE_PARENT_REQUIRED`); the standard and exact implementations both honor that separate policy.

The exact adapter contains all uses of private LangSmith/OpenInference APIs, dependency validation, tracer registry lookup, graph-root override state, and dynamic class compatibility. It may patch only a DeerFlow-owned LangChain instrumentor; it never validates private state on, replaces the class of, rebinds, or otherwise mutates a host-owned instrumentor. Model, tool, chain, retriever, and LLM parentage tests remain contract tests for that optional adapter rather than requirements imposed on standard mode.

The configuration matrix is frozen as follows:

| Tracing | Auto-instrument | Instrumentor ownership | Requested mode | Exact extra/contract | Result |
|---|---|---|---|---|---|
| disabled | any | `NONE` | any valid mode, including `require_exact` | absent or present | `active_mode=disabled`; do not import or validate exact code and do not fail startup |
| enabled | any | any | `standard` | absent or present | standard mode; do not import exact code |
| enabled | false | `NONE` | `prefer_exact` | any | standard mode, degraded with `AUTO_INSTRUMENT_DISABLED` |
| enabled | false | `NONE` | `require_exact` | any | startup failure |
| enabled | true | `HOST` | `prefer_exact` | any | standard mode, degraded with `HOST_OWNED_INSTRUMENTOR` |
| enabled | true | `HOST` | `require_exact` | any | startup failure |
| enabled | true | `DEERFLOW` | `prefer_exact` | extra absent | standard mode, degraded with `EXTRA_NOT_INSTALLED` |
| enabled | true | `DEERFLOW` | `require_exact` | extra absent | startup failure |
| enabled | true | `DEERFLOW` | `prefer_exact` | version/private contract mismatch | standard mode, degraded with `VERSION_MISMATCH` or `PRIVATE_API_CONTRACT_FAILED` |
| enabled | true | `DEERFLOW` | `require_exact` | version/private contract mismatch | startup failure |
| enabled | true | `DEERFLOW` | `prefer_exact` or `require_exact` | compatible | exact mode |

An already active but version-incompatible host instrumentor follows the `HOST` rows; DeerFlow does not inspect or patch it in order to obtain a more specific version reason.

## 11. Callback and Initialization Boundaries

`build_tracing_callbacks()` builds only callback providers such as LangSmith and Langfuse. It never creates a TracerProvider, instruments LangChain, reads Phoenix settings, mutates environment variables, or owns shutdown state.

OpenTelemetry runtime initialization occurs only at an application composition root or an explicit embedded/direct bootstrap. Model factories, agent factories, callback builders, and individual requests cannot trigger process instrumentation as an incidental side effect.

Tracing may append its owned callback handlers, but it must preserve caller handlers and their relative order. Phoenix/OpenTelemetry must not remove or replace business metadata, callbacks, configurable values, or runtime context values.

## 12. Packaging Boundary

The base harness removes Phoenix/OpenInference packages and exact LangChain/LangSmith pins from mandatory dependencies.

```toml
[project.optional-dependencies]
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

These lower bounds are the versions validated in the current lockfile. The base dependency restores `langchain>=1.2.15` and removes direct `langchain-core` and `langsmith` pins; exact compatibility owns the stricter versions shown above. `arize-phoenix-otel` is not required because the standard adapter constructs public OTel components directly.

The exact extra repeats the ordinary Phoenix dependencies so it is installable by itself; it does not silently activate exact mode. Operators install the exact extra when they need exact parentage:

```bash
pip install "deerflow-harness[phoenix-exact-parentage]"
```

Installing both extras explicitly is also supported. Exact compatibility never narrows dependency ranges in the base installation or standard-only extra.

`deerflow.tracing.__init__` and `deerflow.tracing.runtime` do not eagerly import the Phoenix adapter. Without extras installed, core imports and `NoOpTraceRuntime` execution must work.

## 13. Target Module Boundary

The target structure is:

```text
deerflow/tracing/
├── __init__.py                 # provider-neutral exports only
├── runtime.py                  # protocols and provider-neutral dataclasses
├── noop.py                     # NoOpTraceRuntime
├── otel.py                     # generic OTel carrier/scope implementation
├── bootstrap.py                # composition-only lazy adapter selection
└── adapters/
    └── phoenix/
        ├── runtime.py          # Phoenix configuration and composition
        ├── instrumentation.py  # provider/instrumentor ownership
        ├── privacy.py          # DeerFlowTraceConfig
        └── exact_parentage.py  # optional private compatibility
```

The final placement may reuse existing files where a split would create trivial modules, but the dependency direction is mandatory:

```text
core execution ───────────────→ tracing runtime contract
composition root → bootstrap → selected adapter ─implements→ runtime contract
selected adapter ─X──────────→ core business modules
```

Deleting `deerflow/tracing/adapters/phoenix` and selecting `NoOpTraceRuntime` must not require edits to worker, client, agent, task, subagent, model, or gateway execution logic.

## 14. Architecture Enforcement

CI adds an AST import allowlist test. The following are core execution paths:

```text
deerflow/runtime/
deerflow/agents/
deerflow/tools/
deerflow/subagents/
deerflow/models/
deerflow/client.py
app/gateway/ except app.py
```

Within those paths, the only permitted tracing import target is exactly:

```text
deerflow.tracing.runtime
```

The test fails all direct, re-exported, and obvious literal dynamic imports of implementation packages, including:

```text
phoenix.*
openinference.*
opentelemetry.*
deerflow.tracing.noop
deerflow.tracing.otel
deerflow.tracing.bootstrap
deerflow.tracing.adapters.*
```

`app/gateway/app.py`, `deerflow.bootstrap`, and explicit direct/Studio bootstrap modules are composition roots. They may import `deerflow.tracing.bootstrap`, but they still do not import provider SDKs or adapter modules directly. `deerflow.tracing.bootstrap` is the sole adapter-selection boundary and performs lazy imports only after configuration selects an enabled adapter.

The test separately verifies that `deerflow.tracing.runtime` imports only standard-library/typing support and that `deerflow.tracing.__init__` imports only `deerflow.tracing.runtime`. This prevents an apparently neutral core import from eagerly loading an optional adapter. Comments and documentation do not fail the AST test.

A separate packaging smoke installs/imports the base harness without Phoenix extras and exercises the no-op worker/client/task/subagent import path. The AST test protects source boundaries; the no-extra smoke protects eager and dynamic packaging behavior.

## 15. Required Test Matrix

### Delegation invariance

The effective authorization result must be identical across:

```text
tracing disabled / enabled
content capture disabled / enabled
gateway / embedded / direct / Studio
parent None / [] / restricted
child None / [] / restricted
```

For `tool_groups=["web"]`, every configured tool must come from the web group; safe built-ins may still be present according to their existing rules. Skills must be the exact parent-child intersection.

Fingerprint tests prove that equivalent reordered/duplicated policy inputs produce the same hash, distinct `subagent_type` or `requested_skills` inputs produce different delegation-decision hashes, and fingerprints never include secrets or unstable object representations. With authorization unchanged, changing any configured/MCP/ACP/deferred tool schema or a skill definition/`allowed-tools` value must change `tool_catalog_fingerprint` and invalidate every cache retaining that resolved tool set.

### Privacy

Tests cover invalid JSON, a top-level array, exact allowlist matching, nested values, reserved prefixes, caller attempts to spoof authoritative keys, content over 16,384 bytes, and callable values evaluated exactly once. They separately prove that canonical `RunnableConfig.metadata` is unchanged and that exported Phoenix metadata is filtered.

Identity tests cover safe-mode default omission, content-mode default raw values, explicit raw-mode warning, deterministic domain-separated HMAC pseudonyms, missing HMAC key startup failure, and the absence of identifiers or keys from logs. Run-ID tests prove safe export uses a DeerFlow-generated random opaque UUID, rejects caller spoofing, and is not derived from user/session/thread identity. Tag tests cover safe-mode omission, order-preserving deduplication, count/byte limits, and value-free warnings. Baggage tests prove opt-in propagation and prove it never becomes an attribute, tag, log, or metric.

### Ownership and lifecycle

Tests cover every valid provider/instrumentor ownership combination, explicit LangChain-only instrumentation, unrelated instrumentor non-interference, `shutdown_on_exit=False`, bounded flush/shutdown, and the rule that host-owned objects are never patched, rebound, shut down, or uninstrumented.

### Runtime boundary

Gateway, embedded client, subagent isolated-loop execution, sync generator early close, cancellation, exception, and normal completion use the same provider-neutral contract. Strict internal tests assert `OwnedTraceRunScope` rejects concurrent, re-entrant, or cross-owner activation. Public integration tests assert `FailOpenTraceRunScope` converts each internal tracing error to inert activation without changing business return values, exceptions, or cancellation. Tests also cover context restoration after every advancement, first-close-wins idempotency, status/outcome mapping, `start_span(context=...)` creation with no transient current-span mutation, and automatic adapter-internal graph correlation from `TraceRunSpec.run_id`. Tracing disabled uses `NoOpTraceRuntime` without conditional branches in core code.

Carrier boundary tests cover each exact byte limit, multi-byte UTF-8 values, non-string input, combined overflow, partial field rejection, value-free reason records, propagation without rejected values, and required-parent rejection after an invalid/oversized `traceparent`. Oversized baggage never reaches OpenTelemetry parsing or task-envelope propagation.

Runtime failure tests inject failures into carrier capture, span construction, privacy conversion, activation, exporter/processor paths, exact correlation, and shutdown. Business outputs and exceptions remain identical to no tracing, while stable reason-coded events/counters are emitted without sensitive values. Concurrency tests cover parallel requests, parallel subagents, cancellation racing with close, exact-registry cleanup/capacity, and shutdown racing with scope creation.

### Parentage

Standard mode verifies W3C trace continuity or explicit links without asserting the private callback tree. Every row in the parentage/ownership matrix is parameterized, including disabled tracing, disabled auto-instrumentation, host ownership, missing extras, version mismatch, and private-contract failure. Exact-mode contract tests retain exact trace and parent span-ID assertions for the strict topology. Runtime-failure tests prove `require_exact` is startup-only: affected scopes fail open, fallback attributes/counters update, and startup `active_mode="exact"` remains unchanged.

### Packaging and imports

The core tracing-import allowlist, composition-root restrictions, facade/runtime purity, no-extra imports, and standard-mode non-import of `exact_parentage` are CI gates. Parsed metadata tests are insufficient by themselves: isolated temporary environments must install and run base, `phoenix`, and `phoenix-exact-parentage` import/runtime smokes before PR 4 can merge.

## 16. PR Exit Criteria

### PR 1: Security boundary

- No global unrestricted production task tool exists.
- Every `subagent_enabled=True` production path supplies an explicit policy.
- `tool_groups` and `available_skills` are absent from tracing metadata.
- One resolver produces `ResolvedDelegation` for the executor.
- Parent policy, delegation decision, and tool catalog have distinct canonical fingerprints.
- Every cache retaining a policy-bound task tool or resolved tool set uses the corresponding fingerprints, including MCP/deferred/skill hot-reload cases.
- Tracing and capture-mode combinations produce identical authorization results.
- A metadata consumer audit records every remaining business dependency on `RunnableConfig.metadata`.

### PR 2: Side-effect boundary

- Phoenix no longer replaces canonical metadata.
- Instance-local content and metadata filtering covers every DeerFlow-owned OpenInference span path.
- Authoritative attributes are separate from filtered caller metadata.
- Identity, tags, baggage, diagnostic logging, and size limits follow the privacy policy in this ADR.
- Only LangChain is explicitly instrumented.
- No process environment variables or unrelated instrumentors are modified.
- Provider and instrumentor ownership are separate.
- Provider lifecycle uses only public APIs, including `shutdown_on_exit=False`.
- Host-owned privacy responsibility is documented and logged.
- Startup configuration errors fail before work is accepted; post-start tracing failures preserve business execution and degrade observably.

### PR 3: Core architecture boundary

- Worker, client, task tool, executor, gateway, model factory, and callback builder use only provider-neutral tracing types.
- Old Phoenix production helpers and conditional paths are removed in the same PR.
- Process-level runtime and run-local context are distinct.
- `TraceRunScope` implements the activation, outcome, ownership, idempotency, and shutdown contract in this ADR.
- Strict owned-scope misuse is observable in internal tests but cannot escape the public fail-open scope wrapper.
- External carriers enforce the 512/512/8,192/9,216-byte limits before W3C parsing or handoff.
- `build_tracing_callbacks()` has no OpenTelemetry initialization side effects.
- The AST tracing-import allowlist and composition-root gates pass.
- Removing the Phoenix adapter and selecting NoOp requires no core-code changes.

### PR 4: Compatibility and packaging boundary

- Standard mode never imports exact compatibility.
- Prefer-exact exposes a structured reason-coded degraded status for every matrix row.
- Require-exact fails during startup on an incompatible dependency contract.
- Require-exact is documented and tested as a startup capability guarantee; runtime fallbacks preserve business execution and update fallback attributes/counters without rewriting startup status.
- Exact compatibility never patches or rebinds a host-owned instrumentor.
- Base dependencies regain ordinary LangChain version ranges.
- Phoenix and exact compatibility are separate optional extras.
- Base no-extra import/run smoke passes.
- Real isolated-environment install/runtime smokes pass for base, `phoenix`, and `phoenix-exact-parentage`.

## 17. Consequences

### Positive

- Phoenix cannot widen subagent permissions or skill access.
- Privacy policy is local to the instrumented/exported attribute path.
- Existing host instrumentation is not silently taken over.
- Core execution contains only unavoidable tracing lifecycle touchpoints.
- Standard tracing can evolve independently of exact callback topology.
- Deployments that do not use Phoenix avoid its dependency and version conflicts.

### Negative

- Four staged PRs temporarily preserve some Phoenix call sites until PR 3.
- Standard mode does not promise the exact callback-derived tree currently required by the old design.
- Host-owned LangChain instrumentation cannot be covered by DeerFlow's Phoenix privacy guarantee.
- Exact mode remains a compatibility project and retains version-locked contract tests.
- Embedded/direct composition must explicitly manage process runtime ownership rather than relying on model or callback side effects.

## 18. Rollout and Rollback

1. Keep Phoenix disabled in production until PR 1 is deployed and its authorization matrix passes.
2. Deploy PR 1 as the first stage; it does not require enabling Phoenix.
3. Deploy PR 2 with Phoenix still disabled, then validate an in-memory exporter smoke and an optional real Phoenix smoke.
4. Enable standard mode in a canary environment and confirm privacy, ownership, shutdown, and trace continuity.
5. Deploy PR 3 and verify the architecture/import gates in the same release.
6. Deploy PR 4, then allow selected deployments to opt into `prefer_exact` or `require_exact`.

Rollback at every stage consists of disabling Phoenix and restarting all backend processes. PR 1's delegation-policy correction must not be rolled back once deployed; it is a business security fix independent of tracing.

## 19. Non-Goals

- Building a general observability plugin framework.
- Supporting arbitrary dynamically loaded trace providers.
- Orchestrating multiple OTLP exporters.
- Replacing LangSmith or Langfuse callback implementations.
- Guaranteeing DeerFlow privacy controls for host-owned instrumentors.
- Preserving strict parentage in standard mode.
- Adding a public tracing health endpoint without a demonstrated operational consumer.
- Refactoring unrelated tool, skill, agent, or runtime architecture.

## 20. Final Invariants

1. Production paths contain no global unrestricted task tool.
2. Delegation semantics have one resolver.
3. Tracing never changes business authorization.
4. Canonical metadata and exported metadata are separate values.
5. Authoritative trace attributes cannot be supplied or overridden by callers.
6. Process-level `TraceRuntime` and run-local carrier/scope state are separate.
7. Provider and instrumentor ownership are independent.
8. Core code imports no Phoenix/OpenInference/OpenTelemetry implementation.
9. The tracing facade imports no optional adapter eagerly.
10. Standard tracing imports no exact compatibility code.
11. Base installation neither installs nor imports Phoenix dependencies.
12. PR 3 removes old and new production tracing paths from coexisting.
13. Parent-policy, delegation-decision, and tool-catalog fingerprints have non-overlapping meanings and deterministic inputs.
14. Successful startup establishes a fail-open runtime tracing boundary; only explicit authorization, configuration, and required-parent policies may reject work.
15. `TraceRunScope.close()` and `TraceRuntime.shutdown()` are idempotent, and runtime public methods are thread-safe.
16. Safe-mode privacy covers identifiers, tags, baggage, diagnostics, and caller metadata rather than metadata alone.
17. Core tracing imports are allowlisted to `deerflow.tracing.runtime`; implementation-package deny rules are defense in depth.
18. Strict scope ownership errors remain internal; public tracing activation and close are fail-open after successful startup.
19. External carrier values are size-bounded before parsing, context storage, or task handoff.
20. `require_exact` guarantees startup activation only; runtime exact failures never become business failures and remain visible through fallback attributes and counters.

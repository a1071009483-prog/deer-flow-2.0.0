# ADR: Repair and Contain Phoenix Tracing

- **Date:** 2026-07-29
- **Revised:** 2026-07-31
- **Status:** Accepted
- **Implementation status:** Planned
- **Decision scope:** Remediation of commit `bcd4c409` (`feat(tracing): add Phoenix OpenTelemetry tracing`)
- **Replacement plans:**
  - `docs/superpowers/plans/2026-07-29-phoenix-remediation-pr1-correctness.md`
  - `docs/superpowers/plans/2026-07-29-phoenix-remediation-pr2-containment.md`

## 1. Decision

Repair only regressions and excessive coupling introduced by `bcd4c409`.
Do not use the tracing remediation to redesign delegation authorization, tool
catalogs, cache invalidation, application service containers, privacy product
policy, or a general observability plugin system.

The remediation ships in two ordered PRs:

1. **Correctness and side effects:** stop Phoenix safe mode from changing
   business metadata or subagent authorization, replace process-global privacy
   mutation with an instance-local OpenInference configuration, instrument only
   LangChain, and use public provider lifecycle APIs.
2. **Containment and deletion:** put the remaining Phoenix implementation behind
   a small neutral tracing facade, remove private exact-parentage compatibility
   and its version pins, make Phoenix dependencies optional, and delete
   implementation-coupled tests and documentation.

Strict callback-derived parent IDs are intentionally dropped. Standard W3C and
OpenTelemetry trace continuity remains required.

## 2. Review Basis

The review compares `bcd4c409` with its first parent, not with an idealized
future architecture. This distinction matters because some weaknesses exposed
by the tracing change already existed before it.

The commit added approximately:

- 1,812 production/configuration lines and removed 268;
- 6,396 test lines and removed 22;
- 1,945 documentation/OpenSpec lines.

It modified tracing concerns in gateway, worker, embedded client, model factory,
task tool, subagent executor, dependency metadata, and the public tracing
facade. The size alone is not a defect, but much of the added surface exists to
preserve private callback topology rather than the Phoenix integration's public
behavior.

The following behavior from the commit remains valuable and must be preserved:

- a DeerFlow-owned non-global Phoenix provider;
- a `deerflow.run` boundary span;
- `root`, `auto`, and `child` inbound W3C parent modes;
- optional baggage propagation;
- context restoration between sync-generator advancements;
- gateway, embedded-client, and isolated-subagent propagation;
- bounded gateway flush/shutdown;
- separation from `RunJournal`;
- existing LangSmith and Langfuse callback behavior.

## 3. Findings

### 3.1 P0: safe mode changes subagent authorization

Before `bcd4c409`, the lead-agent factory already stored `tool_groups` and
`available_skills` in `RunnableConfig.metadata`, and `task_tool` already read
those fields to restrict the subagent. The module-level task tool and this
metadata-based delegation mechanism were therefore not introduced by the
Phoenix commit.

`bcd4c409` introduced the regression in two steps:

1. `inject_trace_metadata()` starts from an empty mapping when Phoenix is
   enabled with `PHOENIX_CAPTURE_CONTENT=false`.
2. The worker snapshots caller metadata before agent construction and rebuilds
   the exact config passed to `agent.astream()` after the factory has appended
   `tool_groups` and `available_skills`.

The rebuilt mapping removes both fields. `task_tool` then interprets missing
`tool_groups` as `groups=None` and missing `available_skills` as unrestricted
inheritance. Enabling tracing can therefore widen the configured tools and
skills available to a subagent.

This is the release-blocking defect. Its direct fix is to stop tracing from
replacing business metadata and to filter only at the export boundary.

It is not necessary to introduce a new `DelegationPolicy`, a resolver, a tool
catalog, policy fingerprints, or cache fingerprints to fix this regression.
Those may be considered separately against the pre-`bcd4c409` architecture.

### 3.2 P1: privacy is implemented through business-state mutation

Safe capture currently protects OpenInference metadata by removing fields from
the `RunnableConfig` consumed by LangGraph, tools, middleware, and callbacks.
That makes an observability setting part of business execution semantics and
also changes non-Phoenix consumers such as LangSmith, Langfuse, or custom
callbacks.

The required boundary is:

```text
canonical RunnableConfig.metadata ──────────────→ LangGraph/business consumers
                              └─ copy/filter ───→ Phoenix export attributes
```

Phoenix may read canonical metadata but must not delete, replace, or overwrite
it. Existing Langfuse-owned `langfuse_*` injection retains its pre-Phoenix
behavior.

### 3.3 P1: initialization mutates unrelated process state

When content capture is disabled, the implementation writes multiple
`OPENINFERENCE_*` variables into `os.environ` for the provider lifetime. These
variables affect every OpenInference integration in the process, not only the
DeerFlow Phoenix instance.

When auto-instrumentation is enabled, the implementation enumerates every
`openinference_instrumentor` entry point, loads it, binds it to the DeerFlow
provider, records/restores private instance state, and rejects startup if any
entry point is already active. Enabling Phoenix for LangChain can therefore
take ownership of unrelated frameworks or make an otherwise valid embedded
host configuration fail.

The direct repair is instance-local configuration plus explicit
`LangChainInstrumentor` initialization. No provider registry or generalized
ownership framework is required.

### 3.4 P1: exact parentage relies on private mutable contracts

The exact-parentage path depends on:

- private LangSmith parsing functions;
- private OpenInference tracer methods, slots, maps, and locks;
- private provider processor state;
- a process-global run-ID override registry;
- dynamic replacement of a third-party tracer instance's `__class__`;
- exact pins for LangChain, LangChain Core, LangSmith, and the OpenInference
  LangChain instrumentor.

This is the main source of implementation and test volume. It also turns normal
dependency upgrades into a tracing compatibility project.

The remediation removes this path rather than wrapping it in modes, decorators,
status objects, counters, capacity settings, or another optional compatibility
package. No replacement `prefer_exact` or `require_exact` feature is introduced.

### 3.5 P1: Phoenix concerns leak into core execution paths

Core files construct `PhoenixRootContext`, activate Phoenix scopes, bind
Phoenix graph-root parents, capture Phoenix callback parents, read Phoenix
configuration, and call Phoenix shutdown functions. `build_tracing_callbacks()`
also initializes process-wide Phoenix instrumentation even though Phoenix is
not a callback provider.

The tracing package facade eagerly imports Phoenix and OpenTelemetry modules,
so a disabled deployment still needs the implementation dependencies merely to
import worker, client, task, or subagent modules.

The direct repair is a small neutral facade for the exact lifecycle operations
already required by those call sites. It is not a new runtime framework.

### 3.6 P1: lifecycle ownership uses private state and is initialized late

Initialization can be triggered incidentally by agent, model, callback, or run
construction. An enabled but invalid Phoenix configuration can therefore fail a
request instead of gateway startup.

Shutdown unregisters and assigns the provider's private `_atexit_handler`.
Embedded/direct users have no neutral public initialization/shutdown entry
point, while the gateway calls a Phoenix-specific function.

The repair must initialize at gateway startup, keep lazy first-use
initialization for the embedded client, expose neutral explicit shutdown for
non-gateway owners, pass the public `shutdown_on_exit` provider option, and use
only public `force_flush()`, `shutdown()`, `instrument()`, and `uninstrument()`
APIs.

### 3.7 P2: tests freeze mechanisms instead of the supported contract

Thousands of test lines assert private dependency versions, internal slot
names, dotted-order parsing, dynamic class replacement, registry timing, and
exact callback parent IDs. These tests make the private workaround harder to
delete without increasing confidence in the supported Phoenix behavior.

Tests retained after remediation must target metadata invariance, exported
content, W3C continuity, context restoration, explicit LangChain
instrumentation, provider ownership, and public lifecycle behavior.

## 4. Causality and Scope Rule

Every implementation task must satisfy both conditions:

1. It fixes a finding in Section 3 or removes code added by `bcd4c409` that
   caused that finding.
2. Its production changes are limited to files changed by `bcd4c409`, plus the
   smallest new tracing-facade file and package metadata needed to make the
   implementation optional.

Tests and canonical documentation may be added or updated to enforce those
changes. Archived OpenSpec change directories remain historical records and
are not rewritten.

The following are explicitly outside this remediation:

- replacing the pre-existing metadata-based delegation design;
- removing the pre-existing global task tool;
- introducing delegation request/result types or a single authorization
  resolver;
- tool, skill, policy, decision, or cache fingerprints;
- MCP, ACP, deferred-tool, or skill cache redesign;
- new identity modes, HMAC pseudonyms, or identity-key management;
- new metadata, tag, baggage, or carrier byte/count limits;
- new diagnostic rate limiters, metric registries, health endpoints, or runtime
  status snapshots;
- `ApplicationServices`, a replacement `RunContext`, Studio/direct composition
  modules, service locators, or dependency-injection frameworks;
- a provider plugin registry or multi-exporter orchestration;
- an exact-parentage compatibility extra or activation/degradation matrix;
- unrelated tool, skill, agent, model, gateway, or persistence refactoring.

If one of these is desirable, it requires its own issue and ADR independent of
Phoenix remediation.

## 5. Options Considered

### Option A: patch only the two removed metadata keys

Preserve or allowlist `tool_groups` and `available_skills` in safe mode.

This is the smallest emergency patch, but it leaves tracing responsible for
reconstructing business metadata. A future business field could be lost in the
same way, and the global instrumentation/private compatibility problems remain.

### Option B: two-PR targeted repair and containment

First restore metadata invariance and remove process-global side effects. Then
delete exact compatibility and hide the remaining provider behind a small
facade.

This option is accepted. It fixes the release blocker first, preserves a
deployable checkpoint, and removes the causes of excessive invasiveness without
building replacement frameworks.

### Option C: revert `bcd4c409`

A full revert is the lowest-maintenance result but removes Phoenix tracing and
the validated W3C/generator work. It remains an operational fallback if the two
remediation PRs cannot be completed safely.

## 6. Detailed Decisions

### 6.1 Canonical metadata is invariant

With identical business inputs, the mapping passed to LangGraph must be the
same whether Phoenix is disabled, safe capture is enabled, or full capture is
enabled. Phoenix must not mutate the mapping or nested values.

Concretely:

- remove the safe-mode empty-map branch from `inject_trace_metadata()`;
- remove the worker's post-factory metadata rebuild;
- stop injecting Phoenix correlation fields into canonical metadata;
- retain `inject_langfuse_metadata()` and its existing `setdefault` behavior;
- build Phoenix correlation/export values as separate adapter input;
- copy before JSON conversion so export filtering cannot mutate nested business
  values.

The required regression test compares the effective subagent tools and skills,
not merely the presence of the two metadata keys, across Phoenix disabled,
safe-capture, and full-capture modes.

### 6.2 Safe capture is instance-local

The Phoenix-owned LangChain instrumentor receives one explicit
`DeerFlowTraceConfig`, derived from `PhoenixTracingConfig`.

When content capture is disabled it:

- enables the existing OpenInference input/output/prompt/tool hide flags through
  constructor arguments, not environment variables;
- filters `SpanAttributes.METADATA` to exact top-level
  `PHOENIX_METADATA_ALLOWLIST` keys;
- ignores other-provider reserved metadata such as `langfuse_*`;
- returns no metadata attribute for invalid JSON or a non-object top level;
- never writes its filtered result back into `RunnableConfig`.

When content capture is enabled it preserves upstream OpenInference masking
behavior and full invocation metadata/tags as documented by `bcd4c409`.

This remediation does not redefine whether the existing server-owned
session/user/agent/model correlation values are sensitive, add pseudonymization,
or introduce new size policies. Those are separate product privacy decisions.

### 6.3 Instrument only the requested integration

Phoenix initialization explicitly creates `LangChainInstrumentor()` and no
other OpenInference entry point.

- If it is inactive, DeerFlow instruments it with the DeerFlow-owned provider
  and `DeerFlowTraceConfig`, records that it owns the instrumentation, and
  un-instruments it during shutdown or failed initialization.
- If it is already active, DeerFlow does not inspect, rebind, reject,
  un-instrument, or restore it. Phoenix manual `deerflow.run` spans remain
  available; a warning states that LangChain auto spans remain host-owned.
- Unrelated instrumentors are never loaded or inspected.

A boolean/nullable owned-instrumentor reference is sufficient. No general
ownership enum or registry is introduced.

### 6.4 Use public provider lifecycle APIs

Keep the DeerFlow-owned, non-global provider and batch export behavior. Pass
`shutdown_on_exit=False` through the public Phoenix registration API. Remove
all reads/writes of `_atexit_handler` and provider processor internals.

Gateway startup calls neutral `initialize_tracing()` before accepting work.
Gateway shutdown calls neutral `shutdown_tracing()` after in-flight work drains.
Embedded/direct users initialize on first traced run and may call the same
public shutdown function when they own process lifetime.

Configuration/initialization failure while Phoenix is explicitly enabled is
reported before a gateway run or embedded iterator begins. After successful
initialization, ordinary span creation/activation/close failures are logged and
become no-op tracing; they must not replace a business return value or
exception. The existing strict inbound-parent setting remains an intentional
exception and may reject a run before execution.

No diagnostic counter/status subsystem is added.

### 6.5 Introduce only a minimal neutral facade

The target structure is intentionally small:

```text
deerflow/tracing/
├── __init__.py      # neutral exports; no Phoenix/OpenInference/OTel imports
├── api.py           # carrier/run values and lazy lifecycle/scope dispatch
├── factory.py       # LangSmith/Langfuse callbacks only
├── metadata.py      # existing Langfuse metadata behavior
└── phoenix.py       # all Phoenix/OpenInference/OpenTelemetry implementation
```

`otel_context.py` may be folded into `api.py`/`phoenix.py` or retained with
lazy imports; no provider SDK may be imported while Phoenix is disabled.

The facade contains only the operations already required by `bcd4c409`:

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

`TraceRunHandle` exposes only `mark_complete() -> None`. The facade exports the
fixed signatures `initialize_tracing() -> None`,
`shutdown_tracing(*, timeout_millis: int = 30_000) -> None`,
`extract_trace_context_from_headers(headers: Mapping[str, object]) ->
TraceContextCarrier | None`, `serialize_trace_context(carrier:
TraceContextCarrier | None) -> dict[str, str]`,
`deserialize_trace_context(value: Mapping[str, Any] | None) ->
TraceContextCarrier | None`, `attach_trace_context_to_config(config:
dict[str, Any], carrier: TraceContextCarrier | None) -> None`,
`capture_current_trace_context() -> TraceContextCarrier | None`,
`trace_run(context: TraceRunContext) -> ContextManager[TraceRunHandle]`, and
`trace_sync_iterator(iterator: Iterator[T], context: TraceRunContext) ->
Iterator[T]`.

The facade must not grow into `TraceRuntime` protocols, service containers,
adapter registries, ownership state models, or a new application bootstrap
layer.

`trace_sync_iterator()` owns the existing per-advancement attach/detach behavior
so `DeerFlowClient.stream()` can retain its business event loop instead of
embedding provider lifecycle logic throughout it.

### 6.6 Keep standard parentage; delete exact compatibility

The supported topology is defined in public OpenTelemetry terms:

- the manual `deerflow.run` span follows `root`, `auto`, or `child` W3C parent
  policy;
- work executed while that run context is active remains in the same trace
  when the public instrumentor can propagate ambient context;
- isolated subagent execution receives a serialized W3C carrier captured from
  the current OTel context;
- missing/invalid parents follow the existing fallback/required policy;
- prior ambient context is restored on success, error, cancellation, and sync
  generator suspension.

Tests must not assert exact direct parent span IDs for LangSmith external
RunTree callbacks or require every LangChain internal wrapper to appear in a
fixed tree.

Delete:

- dependency-version contract validation;
- `_parse_dotted_order` and private OpenInference tracer imports;
- callback-span registry lookup;
- graph-root override registry and binding;
- dynamic tracer `__class__` replacement;
- exact-parentage tests and documentation;
- exact LangChain/LangSmith/OpenInference pins.

Do not replace them with new parentage modes or compatibility packages.

### 6.7 Phoenix is optional

Restore the base LangChain dependency range and remove the direct exact
`langchain-core` and `langsmith` requirements added for private compatibility.
Move Phoenix/OpenInference dependencies to one `phoenix` extra and expose that
extra from the backend workspace package.

Base worker/client/task/subagent imports must succeed without importing:

```text
phoenix
openinference
opentelemetry
```

When `PHOENIX_TRACING=true` but the extra is absent, neutral initialization
raises a concise installation error before work starts. There is one Phoenix
extra, not separate standard/exact install surfaces.

## 7. Required Test Matrix

### PR 1: correctness and side effects

- canonical metadata, including nested objects, is unchanged by Phoenix export
  conversion;
- `tool_groups` and `available_skills` reach `task_tool` in Phoenix disabled,
  safe-capture, and full-capture modes;
- effective subagent configured tools and skills are identical across those
  modes;
- safe exported metadata contains only configured allowlist keys plus existing
  server-owned root correlation attributes;
- caller metadata/tags remain available to non-Phoenix business consumers;
- no `OPENINFERENCE_*` environment variable is written or removed;
- only `LangChainInstrumentor` is initialized;
- a host-owned LangChain instrumentor and unrelated entry points are untouched;
- only DeerFlow-owned instrumentation/provider objects are cleaned up;
- shutdown uses public flush/shutdown APIs and remains bounded at the gateway.

### PR 2: containment and deletion

- core files changed by `bcd4c409` contain no Phoenix/OpenInference/OpenTelemetry
  imports or Phoenix-specific symbols;
- importing those core files with provider imports blocked succeeds while
  tracing is disabled;
- `build_tracing_callbacks()` creates LangSmith/Langfuse callbacks only;
- gateway startup catches enabled-provider configuration errors before serving;
- embedded sync iteration restores context after every advancement, early
  close, and exception;
- gateway, embedded, and isolated-subagent paths preserve W3C trace continuity;
- every `root`/`auto`/`child` and baggage case retained from the existing public
  contract passes without exact callback parent-ID assertions;
- no private LangSmith/OpenInference/provider symbols or exact version pins
  remain;
- base dependency metadata excludes Phoenix, while the single `phoenix` extra
  supplies and runs the feature.

## 8. PR Exit Criteria

### PR 1

- The P0 authorization regression has a failing-before/passing-after
  cross-mode test.
- No Phoenix code replaces or filters canonical `RunnableConfig.metadata`.
- Safe metadata/content filtering occurs only in `DeerFlowTraceConfig` and the
  manual root export builder.
- No process environment mutation or entry-point enumeration remains.
- Only explicit LangChain instrumentation is touched.
- Provider shutdown uses no private attributes.
- Delegation implementation and caching architecture are unchanged.

### PR 2

- Core call sites use only the neutral facade.
- Exact parentage code, configuration proposals, tests, pins, and documentation
  are deleted rather than relocated.
- Standard W3C continuity and context restoration pass on all supported entry
  paths.
- Model/callback construction has no Phoenix initialization side effect.
- Phoenix dependencies are available through one optional extra and are not
  imported by the disabled base path.
- No service container, runtime protocol hierarchy, plugin registry, status
  subsystem, or new privacy policy was introduced.

## 9. Rollout and Rollback

1. Keep Phoenix disabled in production until PR 1 is deployed and the
   authorization-invariance matrix passes.
2. Deploy PR 1 independently; it preserves the then-current trace topology.
3. Deploy PR 2 with release notes stating that exact callback parent IDs are no
   longer guaranteed and that Phoenix requires the `phoenix` extra.
4. Validate standard W3C continuity, safe export, and bounded shutdown in a
   canary before re-enabling Phoenix.

Disabling Phoenix and restarting backend processes is the operational rollback.
The metadata-invariance fix from PR 1 must not be rolled back.

## 10. Consequences

### Positive

- Tracing cannot change subagent authorization or other business metadata.
- Phoenix no longer mutates unrelated OpenInference integrations or process
  environment.
- Core execution paths retain only small tracing lifecycle hooks.
- Normal LangChain upgrades no longer depend on private exact-parentage
  contracts.
- Disabled deployments do not need to install or import Phoenix.
- The remediation is two reviewable PRs instead of four architecture projects.

### Negative

- Phoenix no longer guarantees the exact callback-derived tree asserted by the
  original private compatibility tests.
- A host-owned active LangChain instrumentor is not rebound to DeerFlow's
  provider, so only DeerFlow manual spans are guaranteed in that coexistence
  case.
- Embedded/direct process owners must explicitly call neutral tracing shutdown
  if they require deterministic exporter flush.

## 11. Final Invariants

1. Phoenix never removes, replaces, or overwrites business metadata.
2. Enabling Phoenix never changes effective tools or skills.
3. Safe capture filters export values, not execution values.
4. No `OPENINFERENCE_*` process environment mutation remains.
5. DeerFlow explicitly instruments only LangChain and cleans up only what it
   owns.
6. Provider lifecycle uses public APIs only.
7. Core execution imports only the neutral tracing facade.
8. Phoenix initialization is not a callback/model-factory side effect.
9. Standard W3C parent modes and generator context restoration remain supported.
10. No exact callback parent-ID guarantee or private compatibility code remains.
11. The base package neither installs nor imports Phoenix dependencies.
12. Delegation, cache, identity-policy, diagnostics, and application-service
    architecture remain unchanged by this remediation.

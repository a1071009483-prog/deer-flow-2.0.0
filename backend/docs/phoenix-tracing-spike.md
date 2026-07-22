# Phoenix Tracing Spike

## Decision

Use a DeerFlow-owned OpenInference `agent` root span around graph invocation plus Phoenix/OpenInference auto-instrumentation for child LangChain/LangGraph spans. Do not add a Phoenix LangChain callback handler.

## Defaults

- `PHOENIX_AUTO_INSTRUMENT=true`
- `PHOENIX_CAPTURE_CONTENT=false`
- `PHOENIX_METADATA_ALLOWLIST=` (empty by default)
- `PHOENIX_TRACE_PARENT_MODE=auto`
- `PHOENIX_TRACE_PARENT_REQUIRED=false`
- `PHOENIX_PROPAGATE_BAGGAGE=false`

## Metadata Allowlist Usage

`PHOENIX_METADATA_ALLOWLIST` is the deployment-controlled exception to the default safe-mode metadata boundary. It is active only when `PHOENIX_CAPTURE_CONTENT=false` and matches exact top-level keys from the root run's `RunnableConfig.metadata`.

Example configuration:

```bash
PHOENIX_CAPTURE_CONTENT=false
PHOENIX_METADATA_ALLOWLIST=request_id,tenant_id,workspace_id
```

Restart every DeerFlow backend process after changing the environment variable because tracing configuration is cached per process. For gateway runs, the corresponding request payload is:

```json
{
  "metadata": {
    "request_id": "req-20260714-001",
    "tenant_id": "tenant-acme",
    "workspace_id": "workspace-research"
  }
}
```

Parsing and export rules:

- Entries are comma-separated, whitespace-trimmed, de-duplicated in first-seen order, and matched as exact top-level keys.
- DeerFlow does not generate or authenticate caller business metadata. A trusted gateway/RBAC layer must inject or validate `request_id`, `tenant_id`, and custom keys.
- Adding a key approves its complete value for Phoenix export; it does not redact nested content or enforce a value type/length policy.
- Nested path expressions and tags are not supported by this allowlist. Caller tags remain excluded in safe mode.
- DeerFlow-authoritative correlation fields overwrite caller fields with the same name.
- Other-provider reserved keys, including `langfuse_*`, never enter the manual Phoenix root through this allowlist.
- `PHOENIX_CAPTURE_CONTENT=true` bypasses the safe-mode boundary and may export full invocation metadata and tags, so the allowlist no longer limits exported keys.

Do not allowlist prompt, messages, payload, input/output, token, authorization, cookie, or other fields that can carry user content or credentials.

## Validation Notes

- Root span shape: Live-validated on 2026-07-09 against Phoenix `17.8.1`. Phoenix project `deer-flow-spike` showed one `deerflow-spike-root` span per smoke run with `spanKind=agent`, `session.id`, `user.id`, tags `["deer-flow", "phoenix-spike"]`, and metadata `{"component": "deerflow", "spike": true}`.
- Child LLM/tool span shape: The minimal Task 1 script creates one manual `deerflow-spike-child` span under each root. Phoenix showed each child span using the same trace id as its root and `parentId` equal to the corresponding root span id. Representative LangChain/LangGraph LLM/tool auto-instrumentation remains owned by the later root wiring tasks.
- Duplicate span check: Phoenix showed exactly two spans per smoke trace: one `deerflow-spike-root` and one `deerflow-spike-child`. The script still does not add a Phoenix LangChain callback handler, so the selected wiring has no callback path competing with Phoenix/OpenInference auto-instrumentation.
- Subagent same-loop result: No real DeerFlow subagent run was executed in this Task 1 smoke. Subagent validation is still scoped to the later subagent context-propagation task; this spike records the root/session metadata shape those runs must preserve.
- Subagent isolated-loop result: No real isolated-loop subagent run was executed in this Task 1 smoke. Later subagent validation must prove either explicit parent context restoration or intentional linked-root behavior with the same session/thread metadata.
- Parent context result: Live-validated with synthetic upstream `traceparent` `00-0123456789abcdef0123456789abcdef-0123456789abcdef-01`. Phoenix showed `deerflow-spike-root` in trace `0123456789abcdef0123456789abcdef` with `parentId=0123456789abcdef`, and the child span nested under that root.

## Final Validation Notes (Tasks 1-9)

Per the SDD task reports, the spike settled on a DeerFlow-owned graph-root span with Phoenix/OpenInference auto-instrumentation and no Phoenix callback handler. The downstream documentation should treat Phoenix as external tracing only, with RunJournal/EventStore still owning internal history and token usage.

- Task 1 validated the local Phoenix smoke shape against the collector endpoint normalization to `/v1/traces`.
- Task 2 validated Phoenix config wiring and the opt-in environment shape.
- Task 3 validated Phoenix initializer idempotency.
- Task 4 validated provider-neutral OTel metadata handling, including baggage preservation when propagation is enabled.
- Task 5 validated gateway ingress context extraction and worker-side context restoration before graph execution.
- Task 6 validated the embedded client root span path and its explicit trace-context handling.
- Task 7 validated the graph-root invariant and standalone model initialization without attaching Phoenix as a callback provider.
- Task 8 validated subagent propagation for both same-loop and isolated-loop execution paths; the isolated-loop path keeps explicit carrier coverage instead of relying on implicit `ContextVar` propagation.
- Task 9 validated that RunJournal/EventStore remain the source of truth and that payload/content controls keep RunJournal-only records out of Phoenix.

## Critical Review Remediation (2026-07-13)

The whole-branch review found that OpenInference hide-input/output flags do not hide custom metadata. There were two export paths: DeerFlow passed the full caller metadata to `using_attributes(metadata=...)`, and the OpenInference LangChain tracer independently serialized `RunnableConfig.metadata`.

The fix uses a bounded, server-authoritative correlation metadata set for both paths when `PHOENIX_CAPTURE_CONTENT=false`. `PHOENIX_METADATA_ALLOWLIST` defaults to empty and permits only comma-separated, exact top-level caller keys after whitespace trimming and ordered de-duplication; the deployment example is `request_id,tenant_id`. Gateway and embedded caller metadata cannot override thread/session, effective user, assistant, effective model, environment, root run name, controlled tags, or run id. Other caller metadata and all caller tags remain excluded; subagent tags remain because DeerFlow creates them internally. Provider-reserved allowlist entries, including `langfuse_*`, are ignored by the manual Phoenix root while DeerFlow-generated Langfuse metadata remains on the auto-instrumentor path. Allowlisted values must come from a trusted gateway or RBAC validation layer. With `PHOENIX_CAPTURE_CONTENT=true`, full metadata and tags remain intentionally available.

Regression verification after the fix: `204 passed, 1 warning` for the non-hanging Phoenix/backend target and `3 passed, 59 deselected, 1 warning` for focused subagent propagation. The warning remains the existing LangGraph checkpoint serializer pending deprecation.

## Generic Graph Parent Remediation (Task 7.5, 2026-07-14)

Task 7.5 fixes the generic OpenInference parent gap identified by the real Phoenix traces. It does not special-case `ChatOpenAI`, `web_search`, or any callback type. After successful Phoenix registration, DeerFlow identifies the Phoenix provider's OpenInference LangChain tracer and changes only that tracer instance to a compatible locked-version `_start_trace` implementation. The global `OpenInferenceTracer` class and unrelated tracer instances are not modified; a failed registration leaves no compatibility state.

Parent selection follows this order:

1. Preserve a direct parent already registered by the same OpenInference tracer.
2. If the direct external RunTree parent is not registered, scan the current LangSmith `RunTree.dotted_order` from nearest to farthest and select the nearest registered business ancestor.
3. Capture that parent span's `SpanContext` once before span creation, so concurrent parent completion cannot trigger a second registry lookup and ambient fallback.
4. If no registered ancestor exists, use an explicit empty OTel context rather than inheriting the ambient DeerFlow manual root.

The deployment contract is exactly `langchain==1.2.15`, `langchain-core==1.3.3`, `langsmith==0.8.18`, and `openinference-instrumentation-langchain==0.1.67`. Startup validates all four versions, required private tracer helpers/slots, and a real two-level dotted-order parser ordering contract before Phoenix registration. Dependency upgrades require requalification instead of silently accepting private API drift.

Acceptance uses an in-memory OTel exporter with a real LangChain callback manager, LangSmith external RunTree ancestry, locked `create_agent`, actual DeerFlow model/tool middleware, and a deterministic local model/tool. Main-agent, embedded-client, copied subagent thread, and production persistent isolated-loop paths directly assert that terminal LLM spans are children of `model`, concrete tool spans are children of `tools`, and neither falls back to the manual root. The isolated-loop test retains the production loop/submission/context path; it adds only a test-owned 10 ms timer because this managed sandbox's new-thread selector did not wake from `call_soon_threadsafe` even in a standalone Python reproduction. No production executor workaround was added.

Final Task 7.5 verification: `71 passed, 1 warning`; Ruff passed; offline `uv lock --check` passed; strict OpenSpec validation and `git diff --check` passed. Independent R2 review closed all R1 findings and approved the implementation. No Phoenix or DeerFlow process was started, stopped, or restarted for this acceptance, and no live collector smoke was required for the in-memory parent-ID assertions. Deployments must restart every DeerFlow backend process before new traces use the compatibility layer; historical traces remain unchanged.

Task 7.5 intentionally does not emit a full LangSmith-style `awrap_model_call` / `awrap_tool_call` wrapper hierarchy. That default-off diagnostic capability now belongs to the independent `add-phoenix-middleware-diagnostics` OpenSpec change and must preserve the terminal-parent contract without duplicate spans.

## Run-Boundary Naming Remediation (Task 7.5.1, 2026-07-16)

The two same-name `lead_agent` spans were not duplicate provider registration. DeerFlow previously reused `root.run_name` for both its manual OTel run boundary and the LangGraph invocation, so OpenInference correctly created an automatic graph span with the same name. The manual boundary is still required for upstream parent modes, session/user attributes, and a consistent main/embedded/subagent lifecycle.

Task 7.5.1 keeps both layers and gives them distinct semantics:

```text
deerflow.run                         # manual DeerFlow/upstream boundary
└── lead_agent                       # automatic graph invocation
    ├── model -> LLM
    └── tools -> concrete tool
```

The manual boundary remains `openinference.span.kind=agent` and adds `deerflow.span.role=run_boundary`, `deerflow.agent_name`, and `deerflow.root_run_name`. The worker uses the authoritative `RunRecord.assistant_id` with the canonical `lead_agent` fallback; the embedded client and subagent executor pass their resolved identities directly. Caller metadata cannot supply this identity.

Acceptance uses a real OTel SDK provider, OpenInference tracer, and in-memory exporter through production `run_agent()`, `DeerFlowClient.stream()`, and `SubagentExecutor._aexecute()` entries. It proves the boundary alone is named `deerflow.run`, the automatic graph retains its true run name, and the graph is a direct child in the same trace. Leader verification completed with `100 passed, 1 warning`; backend Ruff and `git diff --check` passed. No Phoenix or DeerFlow process was managed, so deployments must restart backend processes before new live traces show the new name; historical traces remain unchanged.

## Parent-Mode Validity And Ambient Isolation (Task 7.5.2, 2026-07-16)

The original parent resolver treated any non-empty `traceparent` as present and attached a context only for that branch. A strict `child` run could therefore accept an invalid carrier, while `root`, missing `auto`, and non-strict `child` fallback runs could inherit whatever OTel span was ambient when `deerflow.run` started.

Task 7.5.2 resolves both conditions at the DeerFlow run boundary:

- W3C trace context is parsed from an explicit empty `Context()` and accepted only when the extracted `SpanContext.is_valid` is true. Valid unsampled parents are accepted.
- Missing and supplied-but-invalid inputs remain distinct and export `missing_parent` or `invalid_parent` fallback attributes. The carrier transport preserves supplied raw text only for this classification; it does not replace W3C validation.
- `root` and every fallback attach a context with no active span before creating `deerflow.run`, preventing ambient trace inheritance.
- Optional W3C baggage is parsed independently into that explicit context, so propagated baggage does not import ambient spans or unrelated ambient baggage.
- The context token is always detached on normal and exceptional exit, restoring the caller's prior OTel state.

Acceptance uses the real OTel SDK and `InMemorySpanExporter`, not mocked parent calculation. It covers valid, unsampled, malformed, whitespace-only, all-zero ID, forbidden-version and illegal-suffix carriers across `root`, `auto`, strict `child`, and non-strict `child`; it also verifies exact exported parent/trace IDs, fallback attributes, ambient isolation, production carrier round-trips, and exception unwind. No Phoenix or DeerFlow process restart was performed for this in-memory validation.

## Provider Ownership And Exporter Lifecycle (Task 7.6, 2026-07-17)

Phoenix registration no longer owns auto-instrumentation as one opaque call. DeerFlow calls `phoenix.otel.register()` with `set_global_tracer_provider=False`, `batch=True`, and `auto_instrument=False`, saves the returned provider, then enumerates the same `openinference_instrumentor` entry-point group and explicitly binds every instance to that provider. The manual `deerflow.run` tracer uses the saved provider directly, so a host global provider is neither replaced nor given Phoenix processors.

Before mutation, DeerFlow snapshots every discovered instrumentor. Existing active instances are foreign-owned and cause fail-fast after the new provider is created but before compatibility validation or mutation; the new provider is then closed and foreign state remains unchanged. If explicit instrumentation fails partway through, attempted instances are rolled back in reverse order, their snapshots and content-hide environment are restored, compatibility/active state is cleared, and initialization can be retried. Successful shutdown reverses all DeerFlow-owned instrumentors rather than only LangChain.

The production exporter is a `BatchSpanProcessor` configured only through `OTEL_BSP_MAX_QUEUE_SIZE`, `OTEL_BSP_SCHEDULE_DELAY`, `OTEL_BSP_EXPORT_TIMEOUT`, and `OTEL_BSP_MAX_EXPORT_BATCH_SIZE`. Gateway shutdown first drains in-flight runs, then relinquishes the provider's locked-SDK `atexit` handler before calling `force_flush` and `shutdown` on a daemon cleanup thread. The gateway waits at most its existing five-second shutdown-hook deadline, so a slow exporter cannot block the event loop or interpreter exit. Individual graph runs and embedded streams never close the provider.

Task 7.6 TDD produced focused RED results for provider-before-instrumentation ownership, multi-entry-point rollback, environment restoration, bounded gateway cleanup, and the SDK `atexit` residual. Leader acceptance completed with `110 passed, 1 warning`; scoped Ruff and `git diff --check` passed. Independent R1 found 1 Critical, 4 Important, and 1 Minor; R2 closed all six with no new findings. No Phoenix or DeerFlow process was managed, and no live collector smoke was required for these real-SDK/in-memory lifecycle assertions.

## Subagent Parentage Stabilization (2026-07-18)

Phoenix production traces `1e17242578c33de6b1724bfc5a66b8c7` and
`f38072c0a2247e57a9b682a3eb8909e3` exposed two outcomes of one parent-source
split. The manual subagent `deerflow.run` boundary used the ambient OTel context
captured before persistent-loop submission, while the automatic subagent graph
used its LangChain callback/RunTree parent. Depending on whether that callback
parent was resolvable, the boundary either became a sibling of the lead graph or
the graph bypassed the boundary and left it empty.

The supported successful shape is now exact and independent of gateway versus
embedded entry and LangSmith enabled versus disabled:

```text
tools
└── task
    └── deerflow.run
        └── subagent:<agent-name>
            ├── model → LLM
            └── tools → concrete tool
```

The isolated-loop handoff uses two narrowly scoped mechanisms:

- `task_tool` resolves the current runnable callback manager's `parent_run_id`
  against the Phoenix-owned tracer registry and serializes that registered task
  span's exact `SpanContext` into the W3C carrier. A registry miss falls back to
  ambient OTel capture without failing the business task; Phoenix-disabled runs
  use the same provider-neutral ambient path directly.
- `SubagentExecutor` assigns a fresh UUID to each automatic graph root and binds
  only that run ID to the active manual boundary. The lock-protected binding is
  consumed exactly once and cleaned on scope exit, cancellation, timeout,
  exception, reset, or shutdown. UUID isolation prevents parallel task cross-
  parentage.

The override applies only to the automatic graph root. Task 7.5's existing
direct-parent and nearest-registered-business-ancestor resolver remains
authoritative for model, tool, chain, retriever, and LLM descendants. An
intentional linked root remains a non-failing last resort only when no logical
task or ambient context can be propagated; same-trace sibling boundaries are
not a successful linked-root representation.

Regression acceptance uses real production-equivalent gateway `run_agent()` and
embedded `DeerFlowClient.stream()` entries, the persistent isolated loop, real
LangGraph callbacks, a real OTel SDK/OpenInference tracer, and an in-memory
exporter. It compares exact trace IDs and parent span IDs, covers two parallel
tasks plus completion/cancel/timeout/error cleanup, and verifies embedded caller
context restoration. These tests do not contact LangSmith or Phoenix over the
network. The historical provider change remains 52/52; that count predates and
does not claim this later exact task-boundary-graph invariant.

## Command Attempts

Local Phoenix smoke:

```bash
cd backend
NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost \
  uv run python scripts/phoenix_trace_spike.py --project deer-flow-spike --endpoint http://127.0.0.1:6006 --auto-instrument
```

Result:

```text
Collector Endpoint: http://127.0.0.1:6006/v1/traces
Phoenix project deer-flow-spike traceCount: 2 after both smoke runs.
Local-root smoke trace: 648f1ccd5712b37620926317c1e305b8
Root span: deerflow-spike-root, parentId: null, spanKind: agent
Child span: deerflow-spike-child, parentId: 24070ce937620494
```

Parent context smoke:

```bash
cd backend
NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost \
  uv run python scripts/phoenix_trace_spike.py --project deer-flow-spike --endpoint http://127.0.0.1:6006 --traceparent 00-0123456789abcdef0123456789abcdef-0123456789abcdef-01
```

Result:

```text
Collector Endpoint: http://127.0.0.1:6006/v1/traces
Parent-context smoke trace: 0123456789abcdef0123456789abcdef
Root span: deerflow-spike-root, parentId: 0123456789abcdef, spanKind: agent
Child span: deerflow-spike-child, parentId: 625803acc08d34df
```

Notes:

- `scripts/phoenix_trace_spike.py` accepts a Phoenix UI base URL such as `http://127.0.0.1:6006` and normalizes it to the OTLP traces endpoint `http://127.0.0.1:6006/v1/traces`.
- `NO_PROXY`/`no_proxy` was required in this sandboxed environment so local collector traffic did not route through the configured HTTP proxy.

## Task 11 Verification Addendum (2026-07-10)

- Step 4 of the final verification task inspected `backend/tests/test_client_live.py` and found it is a real-credentials end-to-end suite gated on `config.yaml`, not a Phoenix-only local smoke. Task 11 therefore used `backend/scripts/phoenix_trace_spike.py` as the local Phoenix smoke substitute.
- Command run against the local Phoenix collector:

```bash
cd backend
env UV_CACHE_DIR=/tmp/uv-cache \
  NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost \
  PHOENIX_TRACING=true \
  PHOENIX_COLLECTOR_ENDPOINT=http://127.0.0.1:6006 \
  PHOENIX_PROJECT_NAME=deer-flow-smoke-task11 \
  PHOENIX_TRACE_PARENT_MODE=auto \
  LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false LANGCHAIN_TRACING=false \
  uv run python scripts/phoenix_trace_spike.py --project deer-flow-smoke-task11 --endpoint http://127.0.0.1:6006
```

- Result: the script normalized the collector endpoint to `http://127.0.0.1:6006/v1/traces` and exported spans successfully when allowed to access the local collector. A sandboxed Python run without that local-network allowance produced `[Errno 1] Operation not permitted`, while `curl -I http://127.0.0.1:6006/` confirmed Phoenix `17.8.1` was listening locally.
- Phoenix GraphQL verification:

```text
Project: deer-flow-smoke-task11
traceCount: 1
Trace id: ca2ab778fb70fa117e1fe4cf31598507
Root span: deerflow-spike-root, spanKind: agent, parentId: null
Child span: deerflow-spike-child, parentId: 922e9c879317dedd
Metadata: {"component": "deerflow", "spike": true}
Session/user/tags present: session.id, user.id=spike-user, tags=["deer-flow", "phoenix-spike"]
```

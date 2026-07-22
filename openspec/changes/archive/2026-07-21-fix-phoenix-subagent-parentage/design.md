## Context

DeerFlow intentionally exports both a manual `deerflow.run` span and an automatic OpenInference graph span for each subagent run. The manual boundary owns upstream W3C context, authoritative session/user/agent attributes, lifecycle, and three-state completion status. The automatic graph span owns the LangGraph execution hierarchy and its model/tool/LLM descendants.

The two spans currently select their parents independently:

- `task_tool.py` captures the ambient OpenTelemetry context before crossing into the persistent isolated event loop. OpenInference LangChain callback spans intentionally are not attached as the ambient OTel span, so this usually captures the outer top-level `deerflow.run` rather than the logical `task` callback span.
- `PhoenixParentCompatOpenInferenceTracer._start_trace()` selects a graph parent from `parent_run_id` or a resolvable LangSmith `RunTree`; only a graph root without such a parent inherits the active manual boundary.

This creates two production-visible failures from one dual-parent-source defect.

### Dated production evidence (2026-07-18)

Trace `1e17242578c33de6b1724bfc5a66b8c7` in Phoenix project `deer-flow-embedded-demo` exported this shape:

```text
deerflow.run(7d19ee86)
├── lead-agent(705c2f4d)
│   └── tools
│       └── task(5e0c380f)
└── deerflow.run(a351a57d)
    └── subagent:general-purpose(3923e370)
        ├── model → ChatOpenAI
        ├── tools → web_search
        └── model → ChatOpenAI
```

The boundary-to-graph edge and graph descendants are present, but the boundary is a sibling of the lead graph instead of a child of `task`.

Trace `f38072c0a2247e57a9b682a3eb8909e3` in Phoenix project `deer-flow` contains 203 spans and `errorCount=0`:

```text
deerflow.run(441b4783)
├── lead_agent(a4e3...)
│   ├── tools → task(d0cc...) → subagent:general-purpose(8ab2...) → 48 descendants
│   └── tools → task(c239...) → subagent:general-purpose(29d9...) → 53 descendants
├── deerflow.run(2c2d...)
└── deerflow.run(6e60...)
```

No spans were lost. Each automatic subagent graph and all descendants bypassed its paired manual boundary, leaving two empty root-level boundaries. Whether the graph has a resolvable callback/RunTree parent explains why this trace differs from `1e172...`.

The historical `add-phoenix-tracing-provider` result remains 52/52: it accurately records the tests that existed at completion, but those tests did not assert the newly identified exact production topology invariant.

## Goals / Non-Goals

**Goals:**

- Export one deterministic successful topology: `task → deerflow.run → subagent graph → graph descendants`.
- Make the topology independent of gateway versus embedded entry, sync versus async graph invocation, LangSmith enablement, and persistent isolated-loop scheduling.
- Preserve exact callback hierarchy for ordinary graph descendants.
- Make graph-root binding exact-once and safe for parallel subagent tasks.
- Preserve Phoenix-disabled and registry-miss behavior as non-failing tracing fallbacks.
- Preserve baggage filtering, root parent modes, and boundary OK/UNSET/ERROR semantics.

**Non-Goals:**

- Removing either the manual boundary or automatic graph span.
- Making ambient OTel context globally override callback parentage.
- Changing LangSmith, Langfuse, RunJournal, or EventStore behavior.
- Upgrading tracing dependencies or changing public configuration.
- Rewriting the historical completion state of `add-phoenix-tracing-provider`.

## Decisions

### Capture the logical task span, with ambient fallback

The task handoff will read `ensure_config()["callbacks"].parent_run_id` and query only DeerFlow's Phoenix-owned compatibility tracer registry. When that run ID resolves to a valid callback span, a new provider-neutral helper serializes its `SpanContext` into W3C `traceparent`/`tracestate`, optionally including the already allowed baggage. If any lookup step is unavailable, capture falls back to the existing ambient carrier without changing the task result.

This fixes `task → boundary` at the source. Globally attaching OpenInference callback spans or globally preferring ambient context was rejected because either can leak context or break the existing nearest-business-parent contract for model/tool/chain/retriever spans.

### Bind only the exact subagent graph root to the boundary

`SubagentExecutor` will allocate a fresh UUID and pass it as the subagent graph root `run_id`. While its manual boundary is active, it registers `run_id → boundary SpanContext` in a lock-protected Phoenix registry. `_start_trace()` consumes an override only when `run.id` exactly matches. Consumption removes the entry immediately, and context-manager exit removes any unconsumed entry.

The override takes precedence over a resolvable `parent_run_id` only for that root run. All later callbacks continue through the existing direct-parent and nearest-registered-ancestor resolver. Matching by span name, agent name, thread, or “next span” was rejected because it cannot isolate parallel tasks.

### Keep boundary lifecycle authoritative

The graph-root binding scope is nested inside the active boundary. Normal graph completion exits the binding scope, then marks the boundary complete before the boundary exits. Cancellation/abort returns without marking completion, and graph exceptions propagate through the boundary, preserving OK/UNSET/ERROR status behavior. Tracing lookup or a missing boundary remains a no-op rather than a business failure.

### Verify exact IDs, not names or counts

Regression tests use a real SDK `TracerProvider`, `SimpleSpanProcessor`, `InMemorySpanExporter`, and the Phoenix-owned OpenInference tracer. Shared assertions compare exact `trace_id` and `parent.span_id` values for task, boundary, and graph. Provider-mode, entry-path, fallback, lifecycle, and parallel matrices reuse the same topology assertion.

## Risks / Trade-offs

- [The solution uses the locked OpenInference tracer's internal run registry] → Keep lookup inside the existing Phoenix compatibility layer, cover the locked dependency contract, and fall back to ambient context on lookup miss.
- [A process-global override registry could cross-wire concurrent tasks] → Require UUID keys, a lock around register/consume/cleanup, duplicate fail-fast, exact-once `pop`, and parallel isolation tests.
- [An exception before graph start could leave an override] → Always remove an unconsumed entry in the binding context manager's `finally` block and cover exception/cancellation paths.
- [A root-only override could accidentally affect descendants] → Consume the entry on the first exact root ID match and retain the existing descendant resolver unchanged.
- [Provider-disabled execution could import or depend on Phoenix state] → Select the logical Phoenix capture path only when Phoenix is enabled and retain the existing ambient helper otherwise.

## Migration Plan

1. Add failing exact-topology tests for both production branches.
2. Add logical callback-span carrier serialization and task-tool selection.
3. Add exact graph-root binding and executor wiring.
4. Run gateway, embedded, LangSmith on/off, isolated-loop, lifecycle, and parallel regression matrices.
5. Add dated corrections to canonical tracing documentation and validate both OpenSpec changes.

No data or configuration migration is required. Rollback consists of reverting this remediation's code and documentation while leaving `add-phoenix-tracing-provider` history unchanged.

## Open Questions

None. Live Phoenix smoke remains optional because the implementation must not start, stop, or restart user-owned Phoenix or DeerFlow processes.

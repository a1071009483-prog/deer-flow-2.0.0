## Why

Phoenix currently exports two inconsistent subagent trace shapes because the manual `deerflow.run` boundary and the automatic OpenInference subagent graph choose parents from different context sources. Production traces `1e17242578c33de6b1724bfc5a66b8c7` and `f38072c0a2247e57a9b682a3eb8909e3` show that this can either detach the boundary from the delegating `task` span or let the graph bypass the boundary entirely, so the completed provider change needs a focused defect correction.

## What Changes

- Define one successful Phoenix subagent topology: `task → deerflow.run → subagent graph → graph descendants`.
- Capture the isolated-loop handoff carrier from the registered Phoenix callback `task` span when available, with the existing ambient OpenTelemetry carrier as a non-failing fallback.
- Bind one exact subagent graph root run ID to its active manual boundary with a concurrency-safe, one-shot parent override.
- Preserve the existing general callback parent resolver for model, tool, chain, and retriever descendants.
- Add exact trace/span-ID regression coverage for gateway, embedded, LangSmith enabled/disabled, isolated-loop, cancellation/error, and parallel subagent paths.
- Record the two production traces as dated defect evidence without changing the historical 52/52 completion state of `add-phoenix-tracing-provider`.

## Capabilities

### New Capabilities

- `phoenix-subagent-parentage`: Defines deterministic task-to-boundary-to-graph parentage, fallback behavior, concurrency isolation, and lifecycle requirements for Phoenix subagent tracing.

### Modified Capabilities

None. The completed provider change is retained as historical context; this remediation introduces a focused capability because the repository has no promoted canonical Phoenix spec under `openspec/specs/` to modify.

## Impact

- Phoenix/OpenTelemetry context serialization and Phoenix-owned callback span lookup under `backend/packages/harness/deerflow/tracing/`.
- Task handoff capture in `backend/packages/harness/deerflow/tools/builtins/task_tool.py`.
- Subagent graph root setup in `backend/packages/harness/deerflow/subagents/executor.py`.
- Phoenix focused tests, gateway/embedded regression coverage, tracing developer documentation, and SDD evidence ledgers.
- No dependency upgrades, configuration changes, public business API changes, or replacement of RunJournal/EventStore behavior.

# Phoenix tracing port to DeerFlow v2.0.0

## Lineage

- Official clone: `git clone --branch v2.0.0 --depth 1 https://github.com/bytedance/deer-flow.git deer-flow-2.0.0`
- V2 base: `7e7f0410797693cf882594555ba414e0361d4c6f`
- Source commit: `2e3ad2d74c98552b542f0bbca32b75ab4157172d`
- V2 port commit: this commit; after checkout resolve it with `git rev-parse HEAD`
- Transfer: one `git cherry-pick -x 2e3ad2d74c98552b542f0bbca32b75ab4157172d`

## Pre-port observability audit

- Existing: LangSmith and Langfuse LangChain callbacks.
- Existing lock: langchain 1.2.15, langchain-core 1.3.3, langsmith 0.8.18, opentelemetry-api/sdk 1.41.1.
- Absent before port: Phoenix, OpenInference, and an application-owned TracerProvider.

## Compatibility decisions

- Gateway services preserve v2 Command resume, checkpoint, and run flow; only W3C carrier propagation was added.
- Gateway lifespan preserves v2 ordering; only bounded Phoenix shutdown was added; main-only OIDC shutdown was excluded.
- Subagent executor preserves v2 constructor and streaming; Phoenix task boundary, exact graph run UUID, and graph-root binding were added; main-only auth, Guardrail attribution, and stream dedup were excluded.
- Task tool preserves the v2 signature; only provider-aware carrier selection was added.
- Lead agent preserves v2 middleware composition; graph tracing uses attach_tracing=False; main-only TokenBudget was excluded.
- Pyproject keeps version 2.0.0 and v2 optional dependencies; the Phoenix dependency contract was added.
- Lockfile was regenerated from the final v2 pyproject and was not copied from main.
- V2-only test adaptations removed main-only OIDC and InputSanitizationMiddleware assumptions while retaining lifecycle ordering assertions and the real create_agent, model, tool, ToolErrorHandlingMiddleware, and OpenInference parentage path.

## Duplicate tracing audit

- Phoenix owns one non-global provider with set_global_tracer_provider=False.
- No trace.set_tracer_provider call exists.
- OpenInference LangChain instrumentation initializes once on the Phoenix-owned provider.
- Phoenix is not a LangChain callback; LangSmith/Langfuse callback placement is unchanged.
- Graph models retain attach_tracing=False and no duplicate graph root was introduced.
- RunJournal/EventStore remains isolated from external tracing.

## Verification

- uv lock --check: passed; 225 packages resolved.
- Selected pytest suite: 420 passed.
- Four selected parentage node IDs expanded to 5 parameterized cases; 5 passed.
- Ruff check: passed.
- Ruff format --check: passed; 526 files already formatted.
- OpenSpec strict validation for both specs: passed.
- git diff --check: passed for base-to-HEAD, working tree, and combined base-to-working-tree diffs.
- Live Phoenix smoke: not run; this plan does not manage user-owned processes.

## Known issues accepted for this port

- Embedded generator early-close / closable-iterator cleanup ordering was not fixed and does not represent passed behavior.
- Model-factory tracing-config cache teardown was not fixed and does not represent passed behavior.

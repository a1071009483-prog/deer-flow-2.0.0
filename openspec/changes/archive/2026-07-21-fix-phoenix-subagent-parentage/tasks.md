## 1. Defect Contract and RED Evidence

- [x] 1.1 Record both dated production trace shapes, add the shared exact task-boundary-graph assertion, and verify carrier plus graph-root regression tests fail for the expected parent span IDs before production changes.

## 2. Logical Task Context Handoff

- [x] 2.1 Add tested `SpanContext` carrier serialization, resolve the Phoenix-owned registered callback task span from the current runnable config, retain ambient fallback, and select the correct capture path in the task tool.

## 3. Exact Graph-Root Parent Binding

- [x] 3.1 Add a lock-protected exact-once root run-ID binding with duplicate/error cleanup coverage, expose only the boundary `SpanContext`, and wire a fresh root UUID through `SubagentExecutor` without changing descendant resolution or lifecycle status.

## 4. Cross-Path Regression Matrix

- [x] 4.1 Verify exact parent IDs and no empty root boundaries across LangSmith enabled/disabled, gateway async, embedded sync, persistent isolated-loop, parallel tasks, Phoenix-disabled/registry-miss fallback, cancellation, timeout, and graph exception paths.

## 5. Documentation and Final Gates

- [x] 5.1 Add the dated canonical correction and developer documentation, update SDD evidence, and pass the selected backend pytest matrix, ruff check/format, strict validation for both Phoenix OpenSpec changes, and `git diff --check` without claiming an unexecuted live smoke.

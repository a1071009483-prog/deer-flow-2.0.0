## ADDED Requirements

### Requirement: Deterministic subagent trace topology
When Phoenix tracing has a registered delegating callback task span, the system SHALL export the subagent manual run boundary as that task span's direct child and SHALL export the automatic subagent graph root as the boundary's direct child in the same trace.

#### Scenario: Registered callback task handoff
- **WHEN** a LangChain `task` callback span delegates a subagent and that callback span is registered by the Phoenix-owned tracer
- **THEN** the subagent `deerflow.run` boundary SHALL have the same trace ID as the task span
- **AND** the boundary parent span ID SHALL equal the task span ID
- **AND** the automatic subagent graph parent span ID SHALL equal the boundary span ID

#### Scenario: Graph has a resolvable callback parent
- **WHEN** the automatic subagent graph root starts with a `parent_run_id` that resolves to the delegating task span
- **THEN** the exact graph-root binding SHALL take precedence for that root only
- **AND** the graph SHALL remain a direct child of the active subagent boundary rather than bypassing it

#### Scenario: Graph has no callback parent
- **WHEN** the automatic subagent graph root starts without a resolvable callback parent
- **THEN** the graph SHALL still be a direct child of the active subagent boundary
- **AND** the resulting task-boundary-graph topology SHALL match the resolvable-parent scenario

### Requirement: Logical task context handoff
The system MUST prefer the Phoenix-owned registered callback task `SpanContext` over an unrelated ambient OpenTelemetry span when constructing the isolated-loop handoff carrier.

#### Scenario: Callback task differs from ambient span
- **WHEN** the current runnable callback manager identifies a registered task run and the ambient OTel span has a different span ID
- **THEN** the emitted `traceparent` span ID SHALL equal the registered callback task span ID
- **AND** constructing the carrier SHALL NOT attach or leak a new current OTel context

#### Scenario: Logical callback span is unavailable
- **WHEN** the callback manager, parent run ID, Phoenix-owned tracer, or registered span is unavailable
- **THEN** the system SHALL fall back to the existing ambient OTel carrier capture
- **AND** the subagent business execution SHALL continue without a tracing-specific failure

#### Scenario: Phoenix is disabled
- **WHEN** Phoenix tracing is disabled
- **THEN** task handoff SHALL use the existing provider-neutral ambient carrier path
- **AND** the system SHALL NOT require Phoenix registry state

### Requirement: Exact and isolated graph-root binding
The system SHALL bind a manual subagent boundary to one exact automatic graph root run UUID using a concurrency-safe, one-shot registration.

#### Scenario: Exact root consumes binding
- **WHEN** a graph callback starts with the registered root UUID
- **THEN** it SHALL consume the boundary parent binding exactly once
- **AND** subsequent callbacks with that UUID SHALL use the normal callback parent resolver

#### Scenario: Non-matching run starts
- **WHEN** a graph callback run ID does not match a registered root UUID
- **THEN** it SHALL use the existing callback parent compatibility resolver unchanged

#### Scenario: Duplicate registration
- **WHEN** code attempts to register the same root UUID before its current binding is consumed or cleaned up
- **THEN** the system SHALL fail fast with a Phoenix tracing error

#### Scenario: Binding scope exits before consumption
- **WHEN** cancellation, abort, or an exception exits the binding scope before the graph root consumes its registration
- **THEN** the unconsumed registration SHALL be removed

#### Scenario: Parallel subagents
- **WHEN** two or more subagent tasks register and start graph roots concurrently
- **THEN** each boundary SHALL remain a child of its own task
- **AND** each graph root SHALL remain a child of its own boundary
- **AND** no task, boundary, or graph parentage SHALL cross between registrations

### Requirement: Descendant and lifecycle preservation
The graph-root correction MUST preserve existing Phoenix descendant hierarchy, propagation controls, fallback behavior, and boundary completion status semantics.

#### Scenario: Automatic graph descendants
- **WHEN** model, tool, chain, retriever, or LLM callbacks start below the subagent graph root
- **THEN** they SHALL continue to use the nearest registered business parent
- **AND** they MUST NOT all fall back to the manual subagent boundary

#### Scenario: Baggage propagation disabled
- **WHEN** Phoenix baggage propagation is disabled
- **THEN** the task handoff carrier SHALL omit baggage while preserving valid trace context

#### Scenario: Subagent completes normally
- **WHEN** the subagent graph finishes normally
- **THEN** the manual boundary status SHALL be `OK`

#### Scenario: Subagent is cancelled or aborted
- **WHEN** the subagent graph is cancelled, times out through the existing cancellation path, or aborts without an exception
- **THEN** the manual boundary status SHALL remain `UNSET`
- **AND** no graph-root binding SHALL remain registered

#### Scenario: Subagent graph raises an exception
- **WHEN** the subagent graph raises an `Exception`
- **THEN** the manual boundary status SHALL be `ERROR`
- **AND** no graph-root binding SHALL remain registered

### Requirement: Cross-path regression evidence
The implementation SHALL verify exact subagent parent IDs across supported entry paths and relevant provider states without relying on external network access.

#### Scenario: Gateway and embedded entries
- **WHEN** deterministic subagent runs execute through gateway async and embedded sync production-equivalent entries
- **THEN** both entries SHALL satisfy the same exact task-boundary-graph parent-ID assertions
- **AND** caller-visible embedded yields SHALL restore the caller's current OTel context

#### Scenario: LangSmith enabled and disabled
- **WHEN** regression tests exercise both a resolvable callback parent and an absent callback parent
- **THEN** both states SHALL export the same task-boundary-graph topology

#### Scenario: No empty root-level subagent boundary
- **WHEN** a successfully traced subagent graph and descendants are exported
- **THEN** there SHALL be no paired root-level `deerflow.run` subagent boundary with zero children

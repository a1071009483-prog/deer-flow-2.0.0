# Phoenix Remediation PR 1: Delegation Security Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make subagent authorization independent of tracing metadata, remove the global unrestricted task tool, and bind every retained policy/tool cache to deterministic policy and catalog fingerprints.

**Architecture:** Milestone A first establishes the authorization boundary with one immutable parent policy, one resolver, one per-agent task-tool closure, and an executor that consumes the resolved decision. Milestone B then adds three distinct fingerprints and cache invalidation. Both milestones remain on one branch and one PR; Milestone A is a review/debug checkpoint, not a separately deployable change.

**Tech Stack:** Python 3.12, LangChain tools, LangGraph `ToolRuntime`, Pydantic application config, pytest, Ruff, OpenSpec.

## Global Constraints

- This PR is the first merge/deployment stage; Phoenix remains disabled in production until the complete PR exit gate passes.
- Milestone order is mandatory: authorization boundary A, then cache correctness B.
- Do not deploy or merge Milestone A alone; stale policy/catalog caches remain a security correctness risk until Milestone B passes.
- `None` means unrestricted within application configuration; an empty collection means no values are permitted.
- Authorization or strict catalog resolution failure is fail-closed and raises `DelegationPolicyError` before `SubagentExecutor` starts.
- Production code contains no module-level unrestricted `task_tool` or `SUBAGENT_TOOLS` list.
- `RunnableConfig.metadata` contains neither `tool_groups` nor `available_skills` after the lead-agent factory returns.
- Configured-tool group filtering does not remove independently governed safe built-ins.
- Fingerprints contain no `BaseTool` object, `repr()`, object address, filesystem path, credential, API key, or secret configuration value.
- Test/lint commands run from `backend/`. Git, OpenSpec, and repository architecture/search commands run from the repository root.
- Commit boundaries follow reviewable behavior: one Milestone A commit, one Milestone B commit, and one final gate/docs commit. Checkbox task boundaries do not require extra commits.
- Update `backend/README.md` and `backend/CLAUDE.md` with the changed internal contract.

## Preflight Symbol and Call-Path Inventory

Run from the repository root before editing and paste the output summary into the PR description:

```bash
git status --short
rg -n 'SUBAGENT_TOOLS|task_tool|build_task_tool|get_available_tools\(' backend/packages/harness/deerflow backend/tests
rg -n 'tool_groups|available_skills|_merge_skill_allowlists|_filter_tools' backend/packages/harness/deerflow backend/tests
rg -n '_agent_config_key|agent_cache|cached.*tool|MCP|catalog_hash' backend/packages/harness/deerflow backend/tests
```

Confirm the current production paths for gateway, embedded/direct client, Studio, lead factory, task tool, and executor before relying on line-number hints below.

---

## Milestone A: Authorization Boundary

### Task 1: Define policy values and the single fail-closed resolver

**Files:**
- Create: `backend/packages/harness/deerflow/subagents/delegation.py`
- Modify: `backend/packages/harness/deerflow/subagents/__init__.py`
- Modify: `backend/packages/harness/deerflow/tools/tools.py`
- Create: `backend/tests/test_delegation_policy.py`
- Modify: `backend/tests/test_skill_permissions.py`

**Interfaces:**
- Produces: `DelegationPolicy`, `DelegationRequest`, initial `ResolvedDelegation`, `DelegationPolicyError`.
- Produces: `resolve_delegation(*, parent_policy, request, app_config, parent_model) -> ResolvedDelegation`.
- Produces: internal immutable `ToolCatalogSnapshot` used only to load once, distinguish unknown from denied names, and filter configured-tool groups.

- [ ] **Step 1: Write failing `None`/empty/intersection tests**

Use literal expected values:

```python
@pytest.mark.parametrize(
    ("parent", "child", "expected"),
    [
        (None, None, None),
        (None, ("a",), ("a",)),
        (("a",), None, ("a",)),
        (("a",), ("a", "b"), ("a",)),
        ((), None, ()),
        ((), ("a",), ()),
        (None, (), ()),
    ],
)
def test_intersect_allowlists_preserves_unrestricted_and_empty(parent, child, expected):
    assert intersect_allowlists(parent, child) == expected
```

Add real catalog fixtures proving unknown subagent/tool/skill/group names raise `DelegationPolicyError`; known values denied by the parent are denied rather than mislabeled unknown; `tool_groups=("web",)` excludes configured non-web tools; and safe built-ins retain existing independent rules.

- [ ] **Step 2: Run tests and verify RED**

Run from `backend/`:

```bash
uv run pytest tests/test_delegation_policy.py tests/test_skill_permissions.py -q
```

Expected: collection fails because `deerflow.subagents.delegation` does not exist.

- [ ] **Step 3: Implement immutable authorization values without cache fields**

Use this Milestone A shape:

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


class DelegationPolicyError(RuntimeError):
    pass
```

Normalize unordered values with sorted duplicate removal while retaining `None` separately from empty.

- [ ] **Step 4: Refactor tool assembly into one strict catalog snapshot**

`load_available_tool_catalog()` loads configured, built-in, MCP, and ACP objects once and records source plus configured group. `get_available_tools()` projects its final tool list from the same snapshot. Resolver strict mode converts configured/MCP/ACP/skill discovery errors to `DelegationPolicyError`; it never authorizes against a partial catalog.

The resolver performs this order:

```python
all_skills = load_enabled_skills(app_config)
catalog = load_available_tool_catalog(app_config=app_config, model_name=effective_model, strict=True)
validate_parent_policy(parent_policy, catalog, all_skills)
validate_requested_names(request, catalog, all_skills)
effective_skills = intersect_allowlists(parent_policy.available_skills, request.requested_skills)
skills = select_effective_skills(all_skills, effective_skills)
tools = catalog.filter_configured_groups(parent_policy.tool_groups)
tools = apply_requested_and_disallowed_tools(tools, request)
tools = filter_tools_by_skill_allowed_tools(tools, skills)
return ResolvedDelegation(parent_policy, request, effective_skills, tuple(tools))
```

- [ ] **Step 5: Run focused tests and keep the milestone uncommitted**

Run from `backend/`:

```bash
uv run pytest tests/test_delegation_policy.py tests/test_skill_permissions.py -q
uv run ruff check packages/harness/deerflow/subagents/delegation.py packages/harness/deerflow/subagents/__init__.py packages/harness/deerflow/tools/tools.py tests/test_delegation_policy.py tests/test_skill_permissions.py
```

Expected: policy/resolver tests pass. Do not commit yet; continue Milestone A.

### Task 2: Replace the global task tool and wire trusted per-agent policy

**Files:**
- Modify: `backend/packages/harness/deerflow/tools/builtins/task_tool.py`
- Modify: `backend/packages/harness/deerflow/tools/builtins/__init__.py`
- Modify: `backend/packages/harness/deerflow/tools/tools.py`
- Modify: `backend/packages/harness/deerflow/agents/lead_agent/agent.py`
- Modify: `backend/packages/harness/deerflow/client.py`
- Modify: `backend/tests/test_task_tool_core_logic.py`
- Modify: `backend/tests/test_tool_args_schema_no_pydantic_warning.py`
- Modify: `backend/tests/test_tool_deduplication.py`
- Modify: `backend/tests/test_lead_agent_model_resolution.py`
- Modify: `backend/tests/test_client.py`

**Interfaces:**
- Produces: `build_task_tool(delegation_policy: DelegationPolicy) -> BaseTool`.
- Changes: `get_available_tools(..., delegation_policy: DelegationPolicy | None = None)` raises when `subagent_enabled=True` without policy.

- [ ] **Step 1: Write failing factory and wiring tests**

Assert two factories return distinct tools named `task`, each closure sends its own immutable policy to the resolver, and omission fails closed:

```python
def test_subagent_tool_loading_requires_explicit_policy(app_config):
    with pytest.raises(DelegationPolicyError, match="delegation_policy is required"):
        get_available_tools(subagent_enabled=True, app_config=app_config)
```

Assert lead/custom/embedded agents always pass an explicit policy and the final `RunnableConfig.metadata` lacks `tool_groups` and `available_skills` while retaining unrelated business/Langfuse fields.

- [ ] **Step 2: Run tests and verify RED**

Run from `backend/`:

```bash
uv run pytest tests/test_task_tool_core_logic.py tests/test_tool_args_schema_no_pydantic_warning.py tests/test_tool_deduplication.py tests/test_lead_agent_model_resolution.py tests/test_client.py -q
```

Expected: the factory is missing and production assembly still appends `SUBAGENT_TOOLS`.

- [ ] **Step 3: Implement the policy-bound closure**

```python
def build_task_tool(delegation_policy: DelegationPolicy) -> BaseTool:
    @tool("task", parse_docstring=True)
    async def task(runtime: Runtime, description: str, prompt: str, subagent_type: str, tool_call_id: Annotated[str, InjectedToolCallId]) -> str:
        return await _run_task(
            delegation_policy=delegation_policy,
            runtime=runtime,
            description=description,
            prompt=prompt,
            subagent_type=subagent_type,
            tool_call_id=tool_call_id,
        )

    return task
```

Move the existing body to `_run_task()`. Select the subagent config, build `DelegationRequest`, and call the blocking resolver through `await asyncio.to_thread(...)`. Never read authorization from runtime metadata. Convert `DelegationPolicyError` to the existing safe task error response without starting an executor.

- [ ] **Step 4: Remove all static production task-tool paths and wire policies**

Delete `SUBAGENT_TOOLS` and the module-level decorated `task_tool`. Append only `build_task_tool(delegation_policy)` after the explicit-policy guard. Construct immutable policies before lead/default/custom/embedded tool assembly. Remove the two former policy metadata writes.

- [ ] **Step 5: Run focused tests and keep the milestone uncommitted**

Run from `backend/`:

```bash
uv run pytest tests/test_task_tool_core_logic.py tests/test_tool_args_schema_no_pydantic_warning.py tests/test_tool_deduplication.py tests/test_lead_agent_model_resolution.py tests/test_client.py -q
uv run ruff check packages/harness/deerflow/tools packages/harness/deerflow/agents/lead_agent/agent.py packages/harness/deerflow/client.py
```

Expected: tests pass and the production tool factory is policy-bound. Do not commit yet.

### Task 3: Make executor consume the decision and pass the Milestone A matrix

**Files:**
- Modify: `backend/packages/harness/deerflow/subagents/executor.py`
- Modify: `backend/packages/harness/deerflow/tools/builtins/task_tool.py`
- Modify: `backend/tests/test_subagent_executor.py`
- Modify: `backend/tests/test_subagent_skills_config.py`
- Create: `backend/tests/test_delegation_tracing_invariance.py`
- Modify: `backend/tests/test_worker_langfuse_metadata.py`
- Modify: `backend/tests/test_gateway_phoenix_context.py`

**Interfaces:**
- Changes: `SubagentExecutor.__init__(..., resolved_delegation: ResolvedDelegation, config: SubagentConfig, ...)`.
- Guarantees: executor does not reapply parent/child allowlists or tool allow/deny rules.

- [ ] **Step 1: Write failing executor and cross-entry authorization tests**

Pass a resolved decision that differs from raw config and assert executor uses only `resolved_delegation.tools`/`effective_skills`. A mismatched subagent type raises `DelegationPolicyError`. Parameterize tracing on/off, capture on/off, gateway/embedded/direct/Studio, and parent/child unrestricted/empty/restricted combinations. Compare effective qualified tool names and skill names to the tracing-disabled literal baseline.

- [ ] **Step 2: Run tests and verify RED**

Run from `backend/`:

```bash
uv run pytest tests/test_subagent_executor.py tests/test_subagent_skills_config.py tests/test_delegation_tracing_invariance.py tests/test_worker_langfuse_metadata.py tests/test_gateway_phoenix_context.py -q
```

Expected: executor rejects the new input and still reinterprets raw config.

- [ ] **Step 3: Consume `ResolvedDelegation` without reconstruction**

```python
self.resolved_delegation = resolved_delegation
if config.name != resolved_delegation.request.subagent_type:
    raise DelegationPolicyError("Resolved delegation does not match subagent config")
self._base_tools = list(resolved_delegation.tools)
self.tools = self._base_tools
```

Filter skill messages by resolved names, remove `_filter_tools()` and executor-side allowlist merging, and pass the resolved object from `_run_task()`.

- [ ] **Step 4: Run the Milestone A checkpoint**

Run from `backend/`:

```bash
uv run pytest tests/test_delegation_policy.py tests/test_skill_permissions.py tests/test_task_tool_core_logic.py tests/test_subagent_executor.py tests/test_subagent_skills_config.py tests/test_delegation_tracing_invariance.py tests/test_worker_langfuse_metadata.py tests/test_gateway_phoenix_context.py -q
uv run ruff check packages/harness/deerflow/subagents packages/harness/deerflow/tools packages/harness/deerflow/agents/lead_agent/agent.py packages/harness/deerflow/client.py
```

From the repository root run:

```bash
! rg -n 'SUBAGENT_TOOLS|from .*task_tool import task_tool|metadata.*(tool_groups|available_skills)|(tool_groups|available_skills).*metadata' backend/packages/harness/deerflow backend/app
```

Expected: authorization matrix passes and no production global/untrusted metadata path remains.

- [ ] **Step 5: Commit Milestone A from the repository root**

```bash
git add backend/packages/harness/deerflow/subagents backend/packages/harness/deerflow/tools backend/packages/harness/deerflow/agents/lead_agent/agent.py backend/packages/harness/deerflow/client.py backend/tests/test_delegation_policy.py backend/tests/test_skill_permissions.py backend/tests/test_task_tool_core_logic.py backend/tests/test_tool_args_schema_no_pydantic_warning.py backend/tests/test_tool_deduplication.py backend/tests/test_lead_agent_model_resolution.py backend/tests/test_client.py backend/tests/test_subagent_executor.py backend/tests/test_subagent_skills_config.py backend/tests/test_delegation_tracing_invariance.py backend/tests/test_worker_langfuse_metadata.py backend/tests/test_gateway_phoenix_context.py
git commit -m "fix: isolate subagent delegation policy"
```

Milestone A is reviewable but not deployable. Diagnose any later failure as catalog/cache work unless this matrix regresses.

---

## Milestone B: Catalog and Cache Correctness

### Task 4: Add distinct deterministic fingerprints and catalog revisions

**Files:**
- Modify: `backend/packages/harness/deerflow/subagents/delegation.py`
- Create: `backend/packages/harness/deerflow/tools/catalog_fingerprint.py`
- Modify: `backend/packages/harness/deerflow/config/app_config.py`
- Modify: `backend/packages/harness/deerflow/mcp/cache.py`
- Create: `backend/tests/test_tool_catalog_fingerprint.py`
- Modify: `backend/tests/test_delegation_policy.py`

**Interfaces:**
- Extends: `ResolvedDelegation` with `parent_policy_fingerprint`, `delegation_decision_fingerprint`, `tool_catalog_fingerprint`.
- Produces: `fingerprint_parent_policy()`, `fingerprint_delegation_decision()`, `fingerprint_tool_catalog()`.
- Produces: monotonic `get_app_config_generation()` and `get_mcp_catalog_generation()`.

- [ ] **Step 1: Write failing fingerprint tests**

Prove reordered/duplicated equivalent policies hash equally; distinct `subagent_type` or `requested_skills` decisions differ; and unchanged authorization plus changed configured/MCP/ACP/deferred schema, configured group, skill content, or skill `allowed_tools` changes only the catalog fingerprint. Assert serialized canonical inputs contain no secret fixture, object address, path, or `repr()`.

- [ ] **Step 2: Run tests and verify RED**

Run from `backend/`:

```bash
uv run pytest tests/test_delegation_policy.py tests/test_tool_catalog_fingerprint.py -q
```

Expected: fingerprint functions and final fields do not exist.

- [ ] **Step 3: Implement canonical SHA-256 fingerprints**

Serialize with sorted keys, compact separators, UTF-8, and `sha256:<64 lowercase hex>`. Parent input contains policy version/groups/skills only. Decision input contains parent hash, subagent type, normalized requested/denied tools/skills, effective skills, and source-qualified effective tools. Catalog entries contain source, name, configured group, callable JSON schema, deferred mode/revision, monotonic config/MCP generation, and `{skill name, SKILL.md content digest, normalized allowed_tools}`.

Use process-local generations only as hot-reload revisions; increment when a cache accepts, replaces, or clears values. Never reset a generation to a reused number and never hash raw config/secrets.

- [ ] **Step 4: Extend resolver output after actual resolution**

Compute all three fingerprints after strict catalog and skill resolution, extend `ResolvedDelegation`, and store the exact trusted tool tuple. Fingerprints are audit/cache keys, not proof of object integrity; executor continues consuming the object directly.

- [ ] **Step 5: Run focused tests without committing**

Run from `backend/`:

```bash
uv run pytest tests/test_delegation_policy.py tests/test_tool_catalog_fingerprint.py -q
uv run ruff check packages/harness/deerflow/subagents/delegation.py packages/harness/deerflow/tools/catalog_fingerprint.py packages/harness/deerflow/config/app_config.py packages/harness/deerflow/mcp/cache.py tests/test_delegation_policy.py tests/test_tool_catalog_fingerprint.py
```

Expected: all fingerprint tests pass. Continue to cache wiring before committing.

### Task 5: Bind every retained agent/tool set to its fingerprints

**Files:**
- Modify: `backend/packages/harness/deerflow/agents/lead_agent/agent.py`
- Modify: `backend/packages/harness/deerflow/client.py`
- Modify: `backend/packages/harness/deerflow/subagents/executor.py`
- Create: `backend/tests/test_delegation_cache_invalidation.py`
- Modify: `backend/tests/test_client.py`
- Modify: `backend/tests/test_subagent_executor.py`

**Interfaces:**
- Uses: parent fingerprint for cached policy-bound task-tool closures.
- Uses: decision plus catalog fingerprints for any cache retaining a resolved child/tool set.

- [ ] **Step 1: Write failing cache invalidation tests**

Keep policy unchanged and mutate, one at a time, MCP generation/schema, configured tool group/schema, ACP catalog, deferred mode/catalog, skill body, and `allowed_tools`; assert the affected cache rebuilds exactly once. Reordered equivalent policy input must not rebuild. Assert executor exposes all three fingerprints from its trusted resolution without recomputing them.

- [ ] **Step 2: Run tests and verify RED**

Run from `backend/`:

```bash
uv run pytest tests/test_delegation_cache_invalidation.py tests/test_client.py tests/test_subagent_executor.py -q
```

Expected: current cache keys ignore policy/catalog revisions.

- [ ] **Step 3: Extend cache keys at the retaining boundary**

Include `parent_policy_fingerprint` wherever an agent retains the policy-bound task closure. Include decision and catalog hashes wherever a resolved child/tool set is retained. Load current `AppConfig`/catalog before cache comparison and reuse the resolved tool list when rebuilding; do not perform duplicate MCP/skill loads for key computation.

- [ ] **Step 4: Run Milestone B plus Milestone A regression tests**

Run from `backend/`:

```bash
uv run pytest tests/test_tool_catalog_fingerprint.py tests/test_delegation_cache_invalidation.py tests/test_client.py tests/test_subagent_executor.py tests/test_delegation_policy.py tests/test_delegation_tracing_invariance.py tests/test_task_tool_core_logic.py -q
uv run ruff check packages/harness/deerflow tests/test_tool_catalog_fingerprint.py tests/test_delegation_cache_invalidation.py
```

Expected: cache suite passes and Milestone A authorization remains unchanged.

- [ ] **Step 5: Commit Milestone B from the repository root**

```bash
git add backend/packages/harness/deerflow/subagents/delegation.py backend/packages/harness/deerflow/subagents/executor.py backend/packages/harness/deerflow/tools/catalog_fingerprint.py backend/packages/harness/deerflow/config/app_config.py backend/packages/harness/deerflow/mcp/cache.py backend/packages/harness/deerflow/agents/lead_agent/agent.py backend/packages/harness/deerflow/client.py backend/tests/test_delegation_policy.py backend/tests/test_tool_catalog_fingerprint.py backend/tests/test_delegation_cache_invalidation.py backend/tests/test_client.py backend/tests/test_subagent_executor.py
git commit -m "fix: invalidate delegation caches by policy and catalog"
```

### Task 6: Audit, document, and pass the complete PR gate

**Files:**
- Modify: `backend/README.md`
- Modify: `backend/CLAUDE.md`
- Modify: `openspec/specs/phoenix-tracing-provider/spec.md`
- Modify: `openspec/specs/phoenix-subagent-parentage/spec.md`

- [ ] **Step 1: Repeat the metadata/static-tool inventory from repository root**

```bash
! rg -n 'SUBAGENT_TOOLS|from .*task_tool import task_tool|metadata.*(tool_groups|available_skills)|(tool_groups|available_skills).*metadata' backend/packages/harness/deerflow backend/app
rg -n 'parent_policy_fingerprint|delegation_decision_fingerprint|tool_catalog_fingerprint' backend/packages/harness/deerflow backend/tests
```

Expected: first command has no production match; second shows resolver, retaining caches, executor audit fields, and tests.

- [ ] **Step 2: Update docs and canonical OpenSpec requirements**

Document the immutable policy source, one resolver, no global task tool, three fingerprint meanings, hot-reload invalidation, Milestone A/B review evidence, and the condition for removing the production `PHOENIX_TRACING=false` mitigation.

- [ ] **Step 3: Run the complete PR 1 gate from `backend/`**

```bash
uv run pytest tests/test_delegation_policy.py tests/test_tool_catalog_fingerprint.py tests/test_delegation_cache_invalidation.py tests/test_delegation_tracing_invariance.py tests/test_task_tool_core_logic.py tests/test_subagent_executor.py tests/test_subagent_skills_config.py tests/test_worker_langfuse_metadata.py tests/test_gateway_phoenix_context.py -q
uv run pytest -q
uv run ruff check
```

- [ ] **Step 4: Run repository gates from the repository root**

```bash
openspec validate --all --strict
git diff --check
```

Expected: selected tests, full backend suite, Ruff, strict OpenSpec, whitespace, static-tool, metadata-consumer, and both milestone gates pass.

- [ ] **Step 5: Commit the final gate/docs from the repository root**

```bash
git add backend/README.md backend/CLAUDE.md openspec/specs/phoenix-tracing-provider/spec.md openspec/specs/phoenix-subagent-parentage/spec.md
git commit -m "docs: define tracing-independent delegation boundary"
```

## PR 1 Exit Gate

Do not merge or deploy until all conditions hold:

- Milestone A authorization matrix passes independently before and after Milestone B.
- No global unrestricted production task tool exists.
- Every `subagent_enabled=True` production path supplies an explicit policy.
- One resolver produces the trusted `ResolvedDelegation`; executor does not reinterpret it.
- `tool_groups` and `available_skills` are absent from tracing metadata consumers.
- Parent-policy, delegation-decision, and tool-catalog fingerprints use distinct canonical inputs.
- Configured/MCP/ACP/deferred/skill changes invalidate every retaining cache.
- Full backend tests, Ruff, strict OpenSpec, and repository source gates pass.

## Rollout and Rollback

Deploy the complete PR only after both milestones pass. Keep Phoenix disabled until the deployed authorization smoke confirms equivalent results. If later tracing PRs roll back, retain PR 1 in full; neither Milestone A nor Milestone B is tracing-specific or safe to remove independently.

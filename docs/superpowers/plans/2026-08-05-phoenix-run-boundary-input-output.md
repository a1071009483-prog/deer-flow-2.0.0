# Phoenix Run Boundary Input/Output Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Use `superpowers:test-driven-development` for every behavior change and `superpowers:verification-before-completion` before the final handoff. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 当 `PHOENIX_CAPTURE_CONTENT=true` 且 DeerFlow 自己拥有 LangChain 自动埋点时，把自动 graph 根 span 已生成的标准 OpenInference 输入/输出属性镜像到 `deerflow.run`，同时保持 worker、embedded client、subagent 三个业务入口零改动。

**Architecture:** 在 DeerFlow 自有 Phoenix `TracerProvider` 上注册一个轻量、同步、线程安全的 OpenTelemetry `SpanProcessor`。它登记仍在运行的 `deerflow.run`，在其名称匹配的直接 graph 子 span 结束时，原样复制 `input.value`、`input.mime_type`、`output.value`、`output.mime_type`；不解析 LangGraph state，也不接触业务调用参数。只有 `capture_content=true + DeerFlow-owned auto-instrumentor` 才装配该处理器；manual-only 模式保持现状，根 span 可以没有输入输出。

**Tech Stack:** Python 3.12、OpenTelemetry SDK `SpanProcessor`、OpenInference semantic conventions、Phoenix OTel、pytest、pytest-asyncio、ruff。

## Global Constraints

- 历史代码锚点是 `691a5f9777a0b9f0aca58058e796f5080b337fc2`；实际 feature diff 基线是最后修改本计划文档的 commit。开始执行前必须确认该 commit 包含历史锚点、检查中间 diff，并不得覆盖用户已有改动。
- 执行时先使用 `superpowers:using-git-worktrees` 建立隔离 worktree；如果已经位于隔离 worktree，则沿用当前 worktree。
- 不修改以下业务入口文件：
  - `backend/packages/harness/deerflow/runtime/runs/worker.py`
  - `backend/packages/harness/deerflow/client.py`
  - `backend/packages/harness/deerflow/subagents/executor.py`
- 不给三个 graph 根入口增加 observer 参数、callback、hook 或额外状态传递。
- 不修改 graph 输入、graph 输出、`RunnableConfig`、消息、metadata、tags、stream chunk 或 subagent result。
- 不新增配置项。`PHOENIX_CAPTURE_CONTENT` 继续作为唯一内容开关，默认仍为 `false`。
- 不改变 `deerflow.run -> <graph root> -> descendants` 的父子拓扑、span 名称、状态、异常记录、W3C parent mode、baggage 或 provider 生命周期。
- 只使用公开的 `TracerProvider.add_span_processor()` 装配；不得增加全局 LangGraph monkeypatch、callback monkeypatch 或第三方私有 API。
- 只复制四个 OpenInference 标准属性：`input.value`、`input.mime_type`、`output.value`、`output.mime_type`。
- 属性值必须逐值原样镜像：不反序列化、不重新序列化、不截断、不摘要、不补默认值，不复制 messages、metadata、tags 或任意自定义业务字段。
- graph 子 span 缺少某个属性时，`deerflow.run` 也必须缺少该属性；不得写空字符串、`null` 或虚构 MIME type。
- 只接受同时满足以下条件的来源 span：与 boundary 同 trace、直接以 boundary 为父、span 名称等于 boundary 的 `deerflow.root_run_name`。
- 处理器的 `on_start()`、`on_end()`、`shutdown()` 和 `force_flush()` 不得阻塞，不得把异常传播给业务执行，不得在日志里输出属性值或用户内容。
- 处理器的活跃 boundary 注册表必须受锁保护，并在匹配完成、boundary 结束和 provider shutdown 时清理；并发 run 之间不得串值。
- 当 `PHOENIX_CAPTURE_CONTENT=false` 时不装配处理器，根 span 继续没有输入/输出内容。
- 当 `PHOENIX_AUTO_INSTRUMENT=false`，或 LangChain instrumentor 已由宿主拥有时，接受 manual-only 行为：Phoenix 只保证 `deerflow.run`，根 span 输入/输出可以为空。
- 不尝试让 manual-only 也具备输入/输出；实现这一点需要业务入口传值或侵入式全局拦截，明确不在本次范围内。
- 不新增第三方依赖，不改变现有 LangChain/OpenInference 精确版本约束。
- 按仓库规则，同步更新根 `README.md`、`backend/README.md` 和 `backend/CLAUDE.md`；不改写历史 OpenSpec 或既有 ADR。
- 每个任务一个聚焦 commit；不得 squash。未经用户另行授权，不 push、不创建 PR、不修改远端资源。

## Background and Accepted Trade-off

当前正常 trace 结构是：

```text
deerflow.run                         # DeerFlow 手工创建的 run boundary
└── lead_agent / embedded-agent / subagent:<name>  # OpenInference 自动 graph span
    └── model / tools / LLM / TOOL ...
```

`deerflow.run` 负责稳定的运行边界、上游 parent、session/user、agent、状态和时延；真正的 graph 输入/输出由 OpenInference LangChain 自动埋点写在其直接 graph 子 span 上。`PHOENIX_CAPTURE_CONTENT=true` 当前只是允许内容被自动埋点导出，并不会自动给手工 boundary 生成输入/输出，所以 Phoenix UI 中 boundary 的 I/O 为空是现有实现结果。

本计划采用“tracing 层镜像”而不是“业务入口显式传值”：

```text
graph child on_end
    │  已经完成 OpenInference 标准化与 capture policy
    ▼
PhoenixBoundaryIOProcessor
    │  仅校验 direct parent + expected graph name
    ▼
live deerflow.run
    └── copy input/output 四个标准属性
```

已接受的限制是：如果不存在同一 Phoenix provider 上的自动 graph span，就没有可镜像的数据源。此时称为 manual-only，`deerflow.run` 保持无 I/O，不视为缺陷。

OTel SDK 会同步调用 span processor。graph 子 span 结束时，boundary 仍处于 recording 状态，因此处理器可以在 boundary 自己结束和被 exporter 快照之前写入四个属性。Phoenix 已有的 batch/export processor 即使先注册，也只会先处理已经结束的 graph 子 span，不会提前结束或快照仍然存活的 boundary。

## Behavior Matrix

| `capture_content` | 自动埋点状态 | `deerflow.run` I/O | 预期行为 |
|---|---|---|---|
| `false` | 任意 | 无 | 安全模式不装配镜像处理器 |
| `true` | DeerFlow-owned LangChain instrumentor | 有，前提是匹配的 graph 子 span 提供该属性 | 本次新增行为 |
| `true` | `PHOENIX_AUTO_INSTRUMENT=false` | 无 | 已接受的 manual-only |
| `true` | host-owned LangChain instrumentor | 无保证 | 已接受的 manual-only，并保留现有 warning |
| `true` | graph span 缺少 output（异常/中断） | 只复制存在的 input 属性 | 不合成 output |
| `true` | 同 trace 的非直接后代或名称不匹配的直接子 span | 无复制 | 防止错误归因 |

## File Responsibility Map

- Create `backend/packages/harness/deerflow/tracing/phoenix_boundary_io.py`
  - 只负责追踪活跃 run boundary，并从已结束的匹配 graph 子 span 镜像四个标准属性。
  - 不负责读取配置、创建 span、导出 span 或管理 instrumentor。
- Create `backend/tests/test_phoenix_boundary_io.py`
  - 用真实 OpenTelemetry SDK span 验证复制条件、缺失属性、并发隔离和注册表清理。
- Modify `backend/packages/harness/deerflow/tracing/phoenix.py:13-16,243-306`
  - 在 provider 初始化成功路径中按条件装配处理器。
  - 不修改 `PhoenixRootContext` 或任何调用者接口。
- Modify `backend/tests/test_phoenix_provider_lifecycle.py:159-430`
  - 锁定处理器只在 full-capture + DeerFlow-owned auto-instrumentor 组合下安装。
  - 锁定两个 manual-only 分支不安装处理器。
- Modify `backend/tests/test_phoenix_parent_compat.py:481-550,698-725,828-875`
  - 使用真实 OpenInference tracer 和 exporter 验证 subagent exact-root、生产 worker、生产 embedded client 的 boundary I/O 与 graph root 完全一致。
- Modify `README.md:561-614`
  - 说明正常模式的 boundary I/O 镜像和 manual-only 限制。
- Modify `backend/README.md:346-421`
  - 与根 README 保持同一运行配置语义。
- Modify `backend/CLAUDE.md:528-535`
  - 记录内部处理器边界、来源校验和“业务入口不得接入 Phoenix I/O”的开发约束。
- Do not modify `backend/packages/harness/deerflow/tracing/__init__.py`
  - 处理器是内部实现，不扩大公共 API。

---

### Task 0: Confirm the execution baseline

**Files:**

- Read only: all files in the File Responsibility Map

**Interfaces:**

- Consumes: the commit that last changed this plan, whose ancestry contains `691a5f9777a0b9f0aca58058e796f5080b337fc2`.
- Produces: isolated worktree, clean starting status, and baseline test evidence.

- [ ] **Step 1: Create or enter an isolated worktree**

Invoke `superpowers:using-git-worktrees` and follow it completely. Name the branch `feat/phoenix-run-boundary-io` unless that name already exists; if it exists, stop and inspect it before choosing a different name.

- [ ] **Step 2: Verify branch, revision, and workspace ownership**

Run from the isolated repository root:

```bash
git branch --show-current
git rev-parse HEAD
git log -1 --format=%H -- docs/superpowers/plans/2026-08-05-phoenix-run-boundary-input-output.md
git status --short
git log -5 --oneline --decorate
```

Expected:

- branch is the isolated feature branch;
- HEAD equals, or is a reviewed descendant of, the commit printed for this plan document;
- history contains `691a5f9777a0b9f0aca58058e796f5080b337fc2`;
- status is empty except for explicitly supplied user files;
- any newer commits have been reviewed for overlap with the files in this plan.

- [ ] **Step 3: Re-read the exact implementation surfaces**

Run:

```bash
sed -n '1,220p' backend/packages/harness/deerflow/tracing/phoenix.py
sed -n '220,590p' backend/packages/harness/deerflow/tracing/phoenix.py
sed -n '1,220p' backend/tests/test_phoenix_provider_lifecycle.py
sed -n '470,890p' backend/tests/test_phoenix_parent_compat.py
```

Expected: `deerflow.run` is still created only in `phoenix.py`; the three business entrypoints still consume existing Phoenix root APIs; no later commit already implements boundary I/O.

- [ ] **Step 4: Run the focused baseline suite**

Run from `backend/`:

```bash
PYTHONPATH=. .venv/bin/pytest \
  tests/test_phoenix_provider_lifecycle.py \
  tests/test_phoenix_parent_compat.py \
  tests/test_phoenix_generator_scope.py \
  tests/test_phoenix_trace_config.py \
  tests/test_phoenix_root_runtime.py -q
```

Expected at plan-writing time: `88 passed, 1 warning`. A different passing count is acceptable only if reviewed newer commits added or removed tests. Any failure must be resolved as a baseline issue before feature edits begin.

- [ ] **Step 5: Confirm no baseline-only changes**

Run:

```bash
git status --short
```

Expected: unchanged from Step 2. Do not commit baseline-only work.

---

### Task 1: Implement the isolated boundary I/O span processor

**Files:**

- Create: `backend/tests/test_phoenix_boundary_io.py`
- Create: `backend/packages/harness/deerflow/tracing/phoenix_boundary_io.py`

**Interfaces:**

- Consumes:
  - SDK `SpanProcessor.on_start(span, parent_context=None) -> None`
  - SDK `SpanProcessor.on_end(span: ReadableSpan) -> None`
  - boundary name `deerflow.run`
  - boundary instrumentation scope `deerflow.tracing.phoenix`
  - boundary attribute `deerflow.root_run_name`
- Produces:
  - `PhoenixBoundaryIOProcessor(boundary_span_name: str, boundary_instrumentation_scope: str, root_run_name_attribute: str = "deerflow.root_run_name")`
  - `force_flush(timeout_millis: int = 30_000) -> bool`
  - `shutdown() -> None`
- Invariant: the processor is configuration-agnostic; `phoenix.py` decides whether to instantiate it.

- [ ] **Step 1: Write the real-SDK copying and selectivity tests**

Create `backend/tests/test_phoenix_boundary_io.py` with a real `TracerProvider`, `SimpleSpanProcessor`, and `InMemorySpanExporter`. Register the exporter first and the new processor second to match production registration order.

Use these constants and helpers:

```python
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from typing import Any

import pytest
from openinference.semconv.trace import SpanAttributes
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from deerflow.tracing.phoenix_boundary_io import PhoenixBoundaryIOProcessor

_BOUNDARY_NAME = "deerflow.run"
_BOUNDARY_SCOPE = "deerflow.tracing.phoenix"
_GRAPH_SCOPE = "openinference.instrumentation.langchain"
_ROOT_RUN_NAME = "deerflow.root_run_name"
_IO_ATTRIBUTES = (
    SpanAttributes.INPUT_VALUE,
    SpanAttributes.INPUT_MIME_TYPE,
    SpanAttributes.OUTPUT_VALUE,
    SpanAttributes.OUTPUT_MIME_TYPE,
)


@pytest.fixture
def runtime():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    processor = PhoenixBoundaryIOProcessor(
        boundary_span_name=_BOUNDARY_NAME,
        boundary_instrumentation_scope=_BOUNDARY_SCOPE,
        root_run_name_attribute=_ROOT_RUN_NAME,
    )
    provider.add_span_processor(processor)
    yield provider, exporter, processor
    provider.shutdown()


def _start_boundary(provider: TracerProvider, run_name: str):
    boundary = provider.get_tracer(_BOUNDARY_SCOPE).start_span(_BOUNDARY_NAME)
    boundary.set_attribute("deerflow.span.role", "run_boundary")
    boundary.set_attribute(_ROOT_RUN_NAME, run_name)
    return boundary


def _start_child(provider: TracerProvider, parent: Any, name: str):
    parent_context = trace.set_span_in_context(parent)
    return provider.get_tracer(_GRAPH_SCOPE).start_span(name, context=parent_context)


def _set_io(span: Any, *, input_value: str, output_value: str | None) -> None:
    span.set_attribute(SpanAttributes.INPUT_VALUE, input_value)
    span.set_attribute(SpanAttributes.INPUT_MIME_TYPE, "application/json")
    if output_value is not None:
        span.set_attribute(SpanAttributes.OUTPUT_VALUE, output_value)
        span.set_attribute(SpanAttributes.OUTPUT_MIME_TYPE, "application/json")


def _finished_by_id(exporter: InMemorySpanExporter) -> dict[int, Any]:
    return {span.context.span_id: span for span in exporter.get_finished_spans()}
```

Add these tests with the exact assertions shown:

```python
def test_copies_only_standard_io_from_matching_direct_graph_child(runtime):
    provider, exporter, processor = runtime
    boundary = _start_boundary(provider, "lead_agent")
    child = _start_child(provider, boundary, "lead_agent")
    _set_io(child, input_value='{"messages":["hello"]}', output_value='{"messages":["done"]}')
    child.set_attribute("metadata", '{"private":"do-not-copy"}')
    child.set_attribute("custom.business", "do-not-copy")

    child.end()
    boundary.end()

    spans = _finished_by_id(exporter)
    exported_boundary = spans[boundary.get_span_context().span_id]
    exported_child = spans[child.get_span_context().span_id]
    for attribute in _IO_ATTRIBUTES:
        assert exported_boundary.attributes[attribute] == exported_child.attributes[attribute]
    assert "metadata" not in exported_boundary.attributes
    assert "custom.business" not in exported_boundary.attributes
    assert processor._active_boundaries == {}


def test_ignores_wrong_name_and_non_direct_descendant(runtime):
    provider, exporter, processor = runtime
    boundary = _start_boundary(provider, "lead_agent")

    wrong_direct = _start_child(provider, boundary, "model")
    _set_io(wrong_direct, input_value='{"wrong":"direct"}', output_value='{"wrong":"direct"}')
    matching_grandchild = _start_child(provider, wrong_direct, "lead_agent")
    _set_io(matching_grandchild, input_value='{"wrong":"grandchild"}', output_value='{"wrong":"grandchild"}')
    matching_grandchild.end()
    wrong_direct.end()

    matching_direct = _start_child(provider, boundary, "lead_agent")
    _set_io(matching_direct, input_value='{"right":"input"}', output_value='{"right":"output"}')
    matching_direct.end()
    boundary.end()

    exported_boundary = _finished_by_id(exporter)[boundary.get_span_context().span_id]
    assert exported_boundary.attributes[SpanAttributes.INPUT_VALUE] == '{"right":"input"}'
    assert exported_boundary.attributes[SpanAttributes.OUTPUT_VALUE] == '{"right":"output"}'
    assert processor._active_boundaries == {}


def test_missing_output_is_not_synthesized(runtime):
    provider, exporter, processor = runtime
    boundary = _start_boundary(provider, "lead_agent")
    child = _start_child(provider, boundary, "lead_agent")
    _set_io(child, input_value='{"messages":["hello"]}', output_value=None)

    child.end()
    boundary.end()

    exported_boundary = _finished_by_id(exporter)[boundary.get_span_context().span_id]
    assert exported_boundary.attributes[SpanAttributes.INPUT_VALUE] == '{"messages":["hello"]}'
    assert exported_boundary.attributes[SpanAttributes.INPUT_MIME_TYPE] == "application/json"
    assert SpanAttributes.OUTPUT_VALUE not in exported_boundary.attributes
    assert SpanAttributes.OUTPUT_MIME_TYPE not in exported_boundary.attributes
    assert processor._active_boundaries == {}


def test_boundary_without_graph_child_stays_empty_and_is_cleaned(runtime):
    provider, exporter, processor = runtime
    boundary = _start_boundary(provider, "lead_agent")
    boundary.end()

    exported_boundary = _finished_by_id(exporter)[boundary.get_span_context().span_id]
    for attribute in _IO_ATTRIBUTES:
        assert attribute not in exported_boundary.attributes
    assert processor._active_boundaries == {}
```

- [ ] **Step 2: Write the concurrent isolation test**

Append this test. It ends two graph children concurrently and keys assertions by each boundary span ID, so a global “last input/output” implementation cannot pass.

```python
def test_concurrent_boundaries_do_not_cross_contaminate(runtime):
    provider, exporter, processor = runtime
    boundary_a = _start_boundary(provider, "graph-a")
    boundary_b = _start_boundary(provider, "graph-b")
    barrier = Barrier(2)

    def finish_graph(boundary: Any, name: str, marker: str) -> None:
        child = _start_child(provider, boundary, name)
        _set_io(
            child,
            input_value=f'{{"input":"{marker}"}}',
            output_value=f'{{"output":"{marker}"}}',
        )
        barrier.wait(timeout=5)
        child.end()

    with ThreadPoolExecutor(max_workers=2) as pool:
        future_a = pool.submit(finish_graph, boundary_a, "graph-a", "a")
        future_b = pool.submit(finish_graph, boundary_b, "graph-b", "b")
        future_a.result(timeout=10)
        future_b.result(timeout=10)

    boundary_b.end()
    boundary_a.end()

    spans = _finished_by_id(exporter)
    exported_a = spans[boundary_a.get_span_context().span_id]
    exported_b = spans[boundary_b.get_span_context().span_id]
    assert exported_a.attributes[SpanAttributes.INPUT_VALUE] == '{"input":"a"}'
    assert exported_a.attributes[SpanAttributes.OUTPUT_VALUE] == '{"output":"a"}'
    assert exported_b.attributes[SpanAttributes.INPUT_VALUE] == '{"input":"b"}'
    assert exported_b.attributes[SpanAttributes.OUTPUT_VALUE] == '{"output":"b"}'
    assert processor._active_boundaries == {}


def test_mirror_failure_is_swallowed_without_logging_content(runtime, caplog):
    _, _, processor = runtime
    parent_context = trace.SpanContext(
        trace_id=1,
        span_id=2,
        is_remote=False,
        trace_flags=trace.TraceFlags(1),
        trace_state=trace.TraceState(),
    )
    child_context = trace.SpanContext(
        trace_id=1,
        span_id=3,
        is_remote=False,
        trace_flags=trace.TraceFlags(1),
        trace_state=trace.TraceState(),
    )

    class RejectingBoundary:
        attributes = {_ROOT_RUN_NAME: "lead_agent"}

        def set_attribute(self, _key: str, value: Any) -> None:
            raise RuntimeError(f"must not leak {value}")

    with processor._lock:
        processor._active_boundaries[(1, 2)] = RejectingBoundary()
    child = SimpleNamespace(
        name="lead_agent",
        parent=parent_context,
        context=child_context,
        attributes={SpanAttributes.INPUT_VALUE: "secret-input"},
        instrumentation_scope=SimpleNamespace(name=_GRAPH_SCOPE),
    )

    with caplog.at_level(logging.WARNING, logger="deerflow.tracing.phoenix_boundary_io"):
        processor.on_end(child)

    assert "could not mirror graph attributes" in caplog.text
    assert "secret-input" not in caplog.text
    assert processor._active_boundaries == {}


def test_shutdown_clears_live_boundary_registry(runtime):
    provider, exporter, processor = runtime
    boundary = _start_boundary(provider, "lead_agent")
    assert len(processor._active_boundaries) == 1

    processor.shutdown()
    assert processor._active_boundaries == {}

    child = _start_child(provider, boundary, "lead_agent")
    _set_io(child, input_value='{"after":"shutdown"}', output_value='{"ignored":true}')
    child.end()
    boundary.end()
    exported_boundary = _finished_by_id(exporter)[boundary.get_span_context().span_id]
    for attribute in _IO_ATTRIBUTES:
        assert attribute not in exported_boundary.attributes
```

- [ ] **Step 3: Run the new tests and verify RED**

Run from `backend/`:

```bash
PYTHONPATH=. .venv/bin/pytest tests/test_phoenix_boundary_io.py -q
```

Expected: test collection fails with `ModuleNotFoundError: No module named 'deerflow.tracing.phoenix_boundary_io'`.

- [ ] **Step 4: Implement the minimal processor**

Create `backend/packages/harness/deerflow/tracing/phoenix_boundary_io.py` with this structure and behavior:

```python
from __future__ import annotations

import logging
import threading
from typing import Any

from openinference.semconv.trace import SpanAttributes
from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor

type _SpanKey = tuple[int, int]

_COPIED_ATTRIBUTES = (
    SpanAttributes.INPUT_VALUE,
    SpanAttributes.INPUT_MIME_TYPE,
    SpanAttributes.OUTPUT_VALUE,
    SpanAttributes.OUTPUT_MIME_TYPE,
)

logger = logging.getLogger(__name__)


class PhoenixBoundaryIOProcessor(SpanProcessor):
    """Mirror a direct OpenInference graph span's I/O to its live run boundary."""

    def __init__(
        self,
        *,
        boundary_span_name: str,
        boundary_instrumentation_scope: str,
        root_run_name_attribute: str = "deerflow.root_run_name",
    ) -> None:
        self._boundary_span_name = boundary_span_name
        self._boundary_instrumentation_scope = boundary_instrumentation_scope
        self._root_run_name_attribute = root_run_name_attribute
        self._lock = threading.Lock()
        self._active_boundaries: dict[_SpanKey, Span] = {}

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        del parent_context
        try:
            if not self._is_boundary(span):
                return
            span_context = span.get_span_context()
            if not span_context.is_valid:
                return
            key = (span_context.trace_id, span_context.span_id)
            with self._lock:
                self._active_boundaries[key] = span
        except Exception:
            logger.warning("Phoenix boundary I/O processor could not register a run boundary.")

    def on_end(self, span: ReadableSpan) -> None:
        try:
            if self._is_boundary(span):
                self._discard_boundary((span.context.trace_id, span.context.span_id))
                return

            parent = span.parent
            if parent is None:
                return
            boundary_key = (parent.trace_id, parent.span_id)
            with self._lock:
                boundary = self._active_boundaries.get(boundary_key)
                if boundary is None:
                    return
                expected_graph_name = boundary.attributes.get(self._root_run_name_attribute)
                if span.name != expected_graph_name:
                    return
                boundary = self._active_boundaries.pop(boundary_key)

            for attribute in _COPIED_ATTRIBUTES:
                value = span.attributes.get(attribute)
                if value is not None:
                    boundary.set_attribute(attribute, value)
        except Exception:
            logger.warning("Phoenix boundary I/O processor could not mirror graph attributes.")

    def shutdown(self) -> None:
        with self._lock:
            self._active_boundaries.clear()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        del timeout_millis
        return True

    def _is_boundary(self, span: Any) -> bool:
        instrumentation_scope = span.instrumentation_scope
        return (
            span.name == self._boundary_span_name
            and instrumentation_scope is not None
            and instrumentation_scope.name == self._boundary_instrumentation_scope
        )

    def _discard_boundary(self, key: _SpanKey) -> None:
        with self._lock:
            self._active_boundaries.pop(key, None)
```

Implementation notes that are requirements, not optional refinements:

- Perform span matching and the one-time `pop` under the same lock; this prevents two matching children from both claiming one boundary.
- Release the lock before calling `boundary.set_attribute()`; third-party SDK work must not run inside the registry critical section.
- Catch exceptions at both processor callbacks because the OTel SDK invokes them synchronously and does not protect the business path from processor exceptions.
- Error log messages must contain no exception rendering, copied value, span input, span output, metadata or user text. Use the fixed warning strings from the implementation block; do not use `logger.exception()` or `exc_info=True` here.
- The private `_active_boundaries` name is deliberately pinned by the focused tests for leak detection; do not expose it through `deerflow.tracing.__init__`.

- [ ] **Step 5: Run the unit tests and verify GREEN**

Run:

```bash
PYTHONPATH=. .venv/bin/pytest tests/test_phoenix_boundary_io.py -q
```

Expected: `7 passed`.

- [ ] **Step 6: Run ruff on the new files**

Run:

```bash
.venv/bin/ruff check \
  packages/harness/deerflow/tracing/phoenix_boundary_io.py \
  tests/test_phoenix_boundary_io.py
.venv/bin/ruff format --check \
  packages/harness/deerflow/tracing/phoenix_boundary_io.py \
  tests/test_phoenix_boundary_io.py
```

Expected: both commands pass. If formatting check reports changes, run `.venv/bin/ruff format` on exactly these two files, inspect the diff, then rerun both commands.

- [ ] **Step 7: Commit the isolated processor**

Run:

```bash
git add \
  backend/packages/harness/deerflow/tracing/phoenix_boundary_io.py \
  backend/tests/test_phoenix_boundary_io.py
git commit -m "feat(tracing): mirror graph io to run boundaries"
```

Expected: one commit containing only the new processor and its focused tests.

---

### Task 2: Install the processor only for the supported full-capture mode

**Files:**

- Modify: `backend/tests/test_phoenix_provider_lifecycle.py`
- Modify: `backend/packages/harness/deerflow/tracing/phoenix.py:13-16,243-306`

**Interfaces:**

- Consumes: `PhoenixBoundaryIOProcessor(...)` from Task 1 and existing `owned_instrumentor` ownership detection.
- Produces: exactly one processor on the Phoenix-owned provider when `phoenix_config.capture_content is True` and `owned_instrumentor is not None`.
- Does not produce: new globals, config fields, public exports, or business call-site changes.

- [ ] **Step 1: Add a processor inspection helper to the lifecycle tests**

In `backend/tests/test_phoenix_provider_lifecycle.py`, import the class next to the existing Phoenix imports and add:

```python
from deerflow.tracing.phoenix_boundary_io import PhoenixBoundaryIOProcessor


def _boundary_io_processors(provider: TracerProvider) -> list[PhoenixBoundaryIOProcessor]:
    return [
        processor
        for processor in provider._active_span_processor._span_processors
        if isinstance(processor, PhoenixBoundaryIOProcessor)
    ]
```

Private provider inspection is permitted only in tests. Production code must continue to use `add_span_processor()`.

- [ ] **Step 2: Write the owned full-capture installation test**

Add this test after `test_auto_instrument_uses_langchain_instrumentor_with_deerflow_trace_config`:

```python
def test_owned_auto_instrumentor_installs_boundary_io_processor_for_full_capture(
    monkeypatch,
    _reject_entry_point_enumeration,
):
    from deerflow.tracing import phoenix

    provider = _RecordingProvider()
    fake_instrumentor = _FakeInstrumentor()
    monkeypatch.setattr("phoenix.otel.register", lambda **_kwargs: provider)
    monkeypatch.setattr(phoenix, "_validate_openinference_langchain_parent_contract", lambda: None)
    monkeypatch.setattr(phoenix, "_install_openinference_langchain_parent_compat", lambda _provider: None)
    monkeypatch.setattr(phoenix, "_get_langchain_instrumentor", lambda: lambda: fake_instrumentor)

    phoenix.ensure_phoenix_tracing_initialized(
        _config(auto_instrument=True, capture_content=True)
    )

    processors = _boundary_io_processors(provider)
    assert len(processors) == 1
    assert fake_instrumentor.providers == [provider]
```

- [ ] **Step 3: Pin the three no-install branches**

Extend `test_auto_instrument_uses_langchain_instrumentor_with_deerflow_trace_config` with:

```python
assert _boundary_io_processors(provider) == []
```

That existing test uses `capture_content=False`, so it locks safe mode.

Extend `test_existing_host_langchain_instrumentor_is_left_unchanged` with:

```python
assert _boundary_io_processors(provider) == []
```

That locks host-owned manual-only behavior even though content capture is true.

Add this auto-instrument-disabled test:

```python
def test_manual_only_mode_does_not_install_boundary_io_processor(
    monkeypatch,
    _reject_entry_point_enumeration,
):
    from deerflow.tracing import phoenix

    provider = _RecordingProvider()
    monkeypatch.setattr("phoenix.otel.register", lambda **_kwargs: provider)

    phoenix.ensure_phoenix_tracing_initialized(
        _config(auto_instrument=False, capture_content=True)
    )

    assert _boundary_io_processors(provider) == []
```

- [ ] **Step 4: Run the lifecycle tests and verify RED**

Run:

```bash
PYTHONPATH=. .venv/bin/pytest \
  tests/test_phoenix_provider_lifecycle.py::test_owned_auto_instrumentor_installs_boundary_io_processor_for_full_capture \
  tests/test_phoenix_provider_lifecycle.py::test_auto_instrument_uses_langchain_instrumentor_with_deerflow_trace_config \
  tests/test_phoenix_provider_lifecycle.py::test_existing_host_langchain_instrumentor_is_left_unchanged \
  tests/test_phoenix_provider_lifecycle.py::test_manual_only_mode_does_not_install_boundary_io_processor -q
```

Expected: the new full-capture test fails because no `PhoenixBoundaryIOProcessor` is installed; the three no-install assertions pass.

- [ ] **Step 5: Add the public provider assembly point**

In `backend/packages/harness/deerflow/tracing/phoenix.py`, import:

```python
from deerflow.tracing.phoenix_boundary_io import PhoenixBoundaryIOProcessor
```

Inside `ensure_phoenix_tracing_initialized()`, after successful LangChain ownership and parent-compat installation, but before publishing `_phoenix_tracer_provider` and `_active_config_key`, add:

```python
if phoenix_config.capture_content and owned_instrumentor is not None:
    tracer_provider.add_span_processor(
        PhoenixBoundaryIOProcessor(
            boundary_span_name=_RUN_BOUNDARY_SPAN_NAME,
            boundary_instrumentation_scope=__name__,
            root_run_name_attribute="deerflow.root_run_name",
        )
    )
```

Keep this inside the existing initialization `try` block. If processor creation or registration fails, the current exception path must uninstrument the owned instrumentor, shut down the provider, clear Phoenix state, and raise `PhoenixTracingError` exactly as it does for other initialization failures.

Do not store the processor in a module global. The provider owns its shutdown and flush lifecycle.

- [ ] **Step 6: Run lifecycle and initialization regression tests**

Run:

```bash
PYTHONPATH=. .venv/bin/pytest \
  tests/test_phoenix_provider_lifecycle.py \
  tests/test_phoenix_root_runtime.py \
  tests/test_phoenix_trace_config.py -q
```

Expected: all pass. Existing assertions for one-time initialization, failure cleanup, retry, provider shutdown order, host ownership, config identity and no environment mutation remain unchanged.

- [ ] **Step 7: Prove the three business entry files are untouched**

Run from repository root:

```bash
phoenix_io_base=$(git log -1 --format=%H -- docs/superpowers/plans/2026-08-05-phoenix-run-boundary-input-output.md)
test -n "$phoenix_io_base"
git diff "$phoenix_io_base"...HEAD -- \
  backend/packages/harness/deerflow/runtime/runs/worker.py \
  backend/packages/harness/deerflow/client.py \
  backend/packages/harness/deerflow/subagents/executor.py
```

Expected: no output.

- [ ] **Step 8: Commit provider assembly**

Run:

```bash
git add \
  backend/packages/harness/deerflow/tracing/phoenix.py \
  backend/tests/test_phoenix_provider_lifecycle.py
git commit -m "feat(tracing): install boundary io processor for full capture"
```

Expected: one commit containing only tracing initialization and its lifecycle tests.

---

### Task 3: Lock worker, embedded, and subagent end-to-end trace behavior

**Files:**

- Modify: `backend/tests/test_phoenix_parent_compat.py`

**Interfaces:**

- Consumes: the provider-installed processor from Task 2 and existing real OpenInference exporter fixture `parent_runtime`.
- Produces: regression coverage showing the boundary copies the exact graph-root values for all three root compositions without modifying their production entrypoints.

- [ ] **Step 1: Add one equality helper for the four standard attributes**

Near `_assert_run_boundary_is_distinct_from_graph_span`, add:

```python
_BOUNDARY_IO_ATTRIBUTES = (
    "input.value",
    "input.mime_type",
    "output.value",
    "output.mime_type",
)


def _assert_boundary_io_matches_graph(boundary_span: Any, graph_span: Any) -> None:
    for attribute in _BOUNDARY_IO_ATTRIBUTES:
        if attribute in graph_span.attributes:
            assert boundary_span.attributes[attribute] == graph_span.attributes[attribute]
        else:
            assert attribute not in boundary_span.attributes
```

This deliberately compares exporter-visible values rather than reconstructing expected JSON. It proves the processor mirrors the already-standardized OpenInference representation byte-for-byte.

- [ ] **Step 2: Add the subagent exact-root assertion**

In `test_graph_root_override_wins_only_for_exact_run_id_and_is_consumed`, after `_assert_task_boundary_graph_topology(...)`, add:

```python
_assert_boundary_io_matches_graph(boundary_span, graph_span)
```

Also assert the ordinary sibling did not overwrite the boundary:

```python
assert boundary_span.attributes["input.value"] != ordinary_span.attributes["input.value"]
assert boundary_span.attributes["output.value"] != ordinary_span.attributes["output.value"]
```

The test already uses the exact UUID-bound subagent graph root and a separate ordinary child under the task span, so these assertions lock source selection without changing `SubagentExecutor`.

- [ ] **Step 3: Add the generic main/embedded/subagent topology assertion**

In `test_real_create_agent_entries_keep_distinct_run_boundary_and_graph_spans`, after `_assert_run_boundary_is_distinct_from_graph_span(...)`, resolve the two spans and compare them:

```python
spans = parent_runtime["exporter"].get_finished_spans()
boundary = next(
    span for span in spans if span.attributes.get("deerflow.span.role") == "run_boundary"
)
graph = next(span for span in spans if span.name == entry_name)
_assert_boundary_io_matches_graph(boundary, graph)
```

Keep the existing `StatusCode.UNSET` assertion. I/O mirroring must not change completion semantics.

- [ ] **Step 4: Add assertions to the real worker and embedded-client call paths**

In `test_real_exporter_accepts_production_main_and_embedded_entries`, after the existing boundary status assertion, add:

```python
spans = parent_runtime["exporter"].get_finished_spans()
graph = next(span for span in spans if span.name == graph_run_name)
_assert_boundary_io_matches_graph(boundary, graph)
```

This test already invokes `run_agent()` for `main` and `DeerFlowClient.stream()` for `embedded`; do not add observer arguments or production hooks to either call.

- [ ] **Step 5: Run the exact integration tests and verify GREEN**

Task 2 has already installed the processor, so these are characterization assertions over the completed implementation rather than a new production-code phase.

Run:

```bash
PYTHONPATH=. .venv/bin/pytest \
  tests/test_phoenix_parent_compat.py::test_graph_root_override_wins_only_for_exact_run_id_and_is_consumed \
  tests/test_phoenix_parent_compat.py::test_real_create_agent_entries_keep_distinct_run_boundary_and_graph_spans \
  tests/test_phoenix_parent_compat.py::test_real_exporter_accepts_production_main_and_embedded_entries -q
```

Expected: `6 passed` because the latter two tests are parametrized (`3 + 2`) and the subagent exact-root test contributes one.

- [ ] **Step 6: Run all Phoenix topology and generator lifecycle regressions**

Run:

```bash
PYTHONPATH=. .venv/bin/pytest \
  tests/test_phoenix_parent_compat.py \
  tests/test_phoenix_generator_scope.py \
  tests/test_phoenix_parent_modes_task_7_5_2.py -q
```

Expected: all pass. In particular:

- parent IDs and trace IDs remain unchanged;
- interleaved embedded streams remain isolated;
- aborted/cancelled boundaries keep their previous status semantics;
- no boundary survives early close or exception cleanup;
- subagent graph remains `tools -> task -> deerflow.run -> subagent graph`.

- [ ] **Step 7: Commit the integration contract**

Run:

```bash
git add backend/tests/test_phoenix_parent_compat.py
git commit -m "test(tracing): cover boundary io across graph entries"
```

Expected: a test-only commit; the three production business entry files remain untouched.

---

### Task 4: Document behavior, privacy, and manual-only limitations

**Files:**

- Modify: `README.md:561-614`
- Modify: `backend/README.md:346-421`
- Modify: `backend/CLAUDE.md:528-535`

**Interfaces:**

- Consumes: verified behavior from Tasks 1-3.
- Produces: operator-facing configuration semantics and developer-facing architecture constraints that match the code.

- [ ] **Step 1: Update the root README Phoenix section**

Immediately after the paragraph explaining the distinct `deerflow.run -> graph root` layers, add content equivalent to the following, preserving the surrounding English documentation style:

```markdown
With `PHOENIX_CAPTURE_CONTENT=true`, and only when DeerFlow owns the LangChain auto-instrumentor, the live `deerflow.run` boundary mirrors the direct graph root's OpenInference `input.value`, `input.mime_type`, `output.value`, and `output.mime_type` attributes. The values are copied as already produced by OpenInference; DeerFlow does not parse graph state or synthesize missing output. This intentionally duplicates those four attributes so the run boundary is useful in Phoenix list/detail views.

The mirror is unavailable in manual-only mode: `PHOENIX_AUTO_INSTRUMENT=false`, or a pre-existing host-owned LangChain instrumentor, leaves DeerFlow with only the manual boundary on its Phoenix provider. In those cases `deerflow.run` input/output can remain empty even when content capture is enabled. `PHOENIX_CAPTURE_CONTENT=false` never enables the mirror.

Because full capture duplicates up to four content attributes per normal run boundary, it can increase Phoenix ingest and storage volume; enable it only when both content exposure and observability cost are acceptable.
```

Keep the existing trusted-workload warning for full content capture.

- [ ] **Step 2: Mirror the same contract in backend README**

Add these exact paragraphs to `backend/README.md` after its distinct-boundary paragraph:

```markdown
With `PHOENIX_CAPTURE_CONTENT=true`, and only when DeerFlow owns the LangChain auto-instrumentor, the live `deerflow.run` boundary mirrors the direct graph root's OpenInference `input.value`, `input.mime_type`, `output.value`, and `output.mime_type` attributes. The values are copied as already produced by OpenInference; DeerFlow does not parse graph state or synthesize missing output. This intentionally duplicates those four attributes so the run boundary is useful in Phoenix list/detail views.

The mirror is unavailable in manual-only mode: `PHOENIX_AUTO_INSTRUMENT=false`, or a pre-existing host-owned LangChain instrumentor, leaves DeerFlow with only the manual boundary on its Phoenix provider. In those cases `deerflow.run` input/output can remain empty even when content capture is enabled. `PHOENIX_CAPTURE_CONTENT=false` never enables the mirror.

Because full capture duplicates up to four content attributes per normal run boundary, it can increase Phoenix ingest and storage volume; enable it only when both content exposure and observability cost are acceptable.
```

Keep the existing trusted-workload warning for full content capture.

- [ ] **Step 3: Add the developer architecture rule**

Extend the Phoenix rules in `backend/CLAUDE.md` with these bullets:

```markdown
- With full content capture and a DeerFlow-owned LangChain instrumentor, an internal provider-level span processor mirrors only `input.value`, `input.mime_type`, `output.value`, and `output.mime_type` from the matching direct automatic graph root to the live `deerflow.run` boundary.
- Boundary I/O capture must remain inside `deerflow.tracing`; do not add Phoenix observer parameters or content extraction to worker, embedded client, or subagent graph entrypoints.
- Manual-only Phoenix operation has no same-provider graph span to mirror, so empty `deerflow.run` input/output is an accepted limitation rather than a reason to inspect or mutate business graph state.
```

- [ ] **Step 4: Check documentation terms mechanically**

Run from repository root:

```bash
rg -n "input\.value|manual-only|PHOENIX_CAPTURE_CONTENT" \
  README.md backend/README.md backend/CLAUDE.md
```

Expected: all three files describe the new behavior; both READMEs mention the four attributes, the two manual-only causes and safe-mode exclusion.

- [ ] **Step 5: Commit documentation**

Run:

```bash
git add README.md backend/README.md backend/CLAUDE.md
git commit -m "docs(tracing): explain Phoenix boundary io capture"
```

Expected: documentation-only commit.

---

### Task 5: Final verification and scope audit

**Files:**

- Verify only; change files only if a test exposes a defect within the approved tracing/test/documentation scope.

**Interfaces:**

- Consumes: all prior task commits.
- Produces: evidence that behavior, compatibility, documentation, and non-invasiveness meet acceptance criteria.

- [ ] **Step 1: Run the complete focused Phoenix suite**

Run from `backend/`:

```bash
PYTHONPATH=. .venv/bin/pytest \
  tests/test_phoenix_boundary_io.py \
  tests/test_phoenix_provider_lifecycle.py \
  tests/test_phoenix_parent_compat.py \
  tests/test_phoenix_generator_scope.py \
  tests/test_phoenix_parent_modes_task_7_5_2.py \
  tests/test_phoenix_trace_config.py \
  tests/test_phoenix_root_runtime.py \
  tests/test_phoenix_safe_export.py \
  tests/test_phoenix_attribute_types.py \
  tests/test_phoenix_business_metadata_invariance.py -q
```

Expected: all pass. Preserve the exact output and test count in the final handoff.

- [ ] **Step 2: Run backend lint and formatting checks**

Run from `backend/`:

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

Expected: both pass with exit code 0.

- [ ] **Step 3: Run the complete backend suite**

Run from `backend/`:

```bash
make test
```

Expected: all backend tests pass. Existing warnings are acceptable only if they were present at baseline; no new warning may include captured content.

- [ ] **Step 4: Verify the harness/app boundary**

Run from `backend/`:

```bash
PYTHONPATH=. .venv/bin/pytest tests/test_harness_boundary.py -q
```

Expected: pass; the new processor imports no `app.*` module.

- [ ] **Step 5: Audit the final file scope**

Run from repository root:

```bash
phoenix_io_base=$(git log -1 --format=%H -- docs/superpowers/plans/2026-08-05-phoenix-run-boundary-input-output.md)
test -n "$phoenix_io_base"
git diff --name-only "$phoenix_io_base"...HEAD
git diff --stat "$phoenix_io_base"...HEAD
git status --short
```

Expected changed files are limited to:

```text
README.md
backend/CLAUDE.md
backend/README.md
backend/packages/harness/deerflow/tracing/phoenix.py
backend/packages/harness/deerflow/tracing/phoenix_boundary_io.py
backend/tests/test_phoenix_boundary_io.py
backend/tests/test_phoenix_parent_compat.py
backend/tests/test_phoenix_provider_lifecycle.py
```

Expected status: clean. Any additional path requires an explicit scope explanation and user approval before completion.

- [ ] **Step 6: Prove business-code non-invasiveness one final time**

Run:

```bash
phoenix_io_base=$(git log -1 --format=%H -- docs/superpowers/plans/2026-08-05-phoenix-run-boundary-input-output.md)
test -n "$phoenix_io_base"
git diff --exit-code "$phoenix_io_base"...HEAD -- \
  backend/packages/harness/deerflow/runtime/runs/worker.py \
  backend/packages/harness/deerflow/client.py \
  backend/packages/harness/deerflow/subagents/executor.py
```

Expected: exit code 0 and no output.

- [ ] **Step 7: Review the final commits**

Run:

```bash
phoenix_io_base=$(git log -1 --format=%H -- docs/superpowers/plans/2026-08-05-phoenix-run-boundary-input-output.md)
test -n "$phoenix_io_base"
git log --oneline "$phoenix_io_base"..HEAD
```

Expected feature commits, in order:

```text
feat(tracing): mirror graph io to run boundaries
feat(tracing): install boundary io processor for full capture
test(tracing): cover boundary io across graph entries
docs(tracing): explain Phoenix boundary io capture
```

The plan document may appear in an earlier documentation commit and is not part of the feature diff if execution begins from a descendant that already contains it.

## Acceptance Criteria

Implementation is accepted only when every item below is supported by test or diff evidence:

- [ ] Normal full-capture traces show `deerflow.run` with the same available input/output value and MIME attributes as its direct graph root.
- [ ] The values are exact copies of exporter-visible OpenInference attributes; no business-state parser or serialization path was added.
- [ ] Missing graph output produces missing boundary output, including interrupted/error cases.
- [ ] Wrong-name direct children, grandchildren, unrelated siblings and other traces cannot supply boundary I/O.
- [ ] Concurrent worker/embedded/subagent boundaries cannot cross-contaminate values.
- [ ] Active-boundary state is removed after match, boundary end and provider shutdown.
- [ ] `PHOENIX_CAPTURE_CONTENT=false` does not install the processor and does not expose boundary content.
- [ ] `PHOENIX_AUTO_INSTRUMENT=false` remains manual-only with empty boundary I/O accepted.
- [ ] A host-owned LangChain instrumentor remains untouched and does not receive the new processor; its manual-only warning remains.
- [ ] Provider initialization failure, retry, flush and shutdown behavior are unchanged.
- [ ] Boundary status, exceptions, names, timings, trace IDs and parent IDs are unchanged.
- [ ] `worker.py`, `client.py` and `subagents/executor.py` have zero diff from baseline.
- [ ] No public API or configuration field was added.
- [ ] Root README, backend README and backend developer guidance all document the same behavior matrix.
- [ ] Focused Phoenix tests, ruff, full backend tests and harness boundary test all pass.

## Operational Validation After Deployment

This section is an operator checklist, not additional implementation scope.

1. Restart every DeerFlow backend process; Phoenix provider configuration is process-start scoped.
2. In a trusted non-production workload, set:

   ```bash
   PHOENIX_ENABLED=true
   PHOENIX_AUTO_INSTRUMENT=true
   PHOENIX_CAPTURE_CONTENT=true
   ```

3. Execute one worker run, one embedded-client stream and one subagent task.
4. In Phoenix, verify each normal trace retains `deerflow.run -> <real graph name>` and the boundary displays the four attributes that exist on the graph root.
5. Repeat with `PHOENIX_CAPTURE_CONTENT=false`; verify boundary I/O is absent/redacted according to the existing safe policy.
6. Repeat with `PHOENIX_AUTO_INSTRUMENT=false`; verify only `deerflow.run` is guaranteed and empty I/O is accepted.
7. Compare ingest volume before and after full capture. Four content attributes are intentionally duplicated per normal run boundary, so storage and network use can increase.

## Rollback

Rollback requires reverting the four feature commits in reverse order. No configuration migration, database migration or business-state cleanup is required. After rollback and process restart, Phoenix returns to the current behavior: `deerflow.run` remains present but has no mirrored input/output, while automatic child spans retain their existing capture policy.

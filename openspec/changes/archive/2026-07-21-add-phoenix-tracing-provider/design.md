## 上下文

DeerFlow 目前有两条可观测性路径：

- 外部 tracing provider 通过 LangChain callbacks 接入。`build_tracing_callbacks()` 会校验已启用的 provider，并返回 LangSmith/Langfuse handlers；各个 graph 入口再把这些 handlers 追加到 root `RunnableConfig.callbacks`。
- 内部运行时可观测性由 `RunJournal` 和 `RunEventStore` 负责。这条路径捕获 run lifecycle、messages、trace events、token usage 和 gateway debug/audit APIs，并且必须保持独立，不依赖任何外部 tracing provider。

现有 placement invariant 很重要：主 agent、embedded client 和 subagent execution 都在 graph invocation root 挂载 tracing，而 graph 内部模型创建使用 `attach_tracing=False`。这样可以避免重复模型 span，也能让 provider metadata 应用到 root trace。subagent 需要额外关注，因为它会创建独立 graph run，并且可能在后台线程里的 isolated event loop 上执行。

Phoenix 与当前 provider 的差异在于，推荐接入路径是 OpenTelemetry/OpenInference instrumentation，而不是只有 LangChain callback handler。如果把 Phoenix 当成另一个普通 callback provider，会掩盖 global instrumentation、幂等性、context propagation 和 auto-instrumentation 重复 span 等生命周期问题。

## 目标 / 非目标

**目标：**

- 将 Phoenix 作为第三个外部 tracing provider 加入，同时不改变内部 RunJournal/EventStore 合约。
- 保持主 agent、embedded client 和 subagent run 的 root-level graph tracing。
- 避免 Phoenix auto-instrumentation 与手工 callback/span wiring 组合后产生重复 span。
- 让 Phoenix 初始化具备幂等性，即使 `build_tracing_callbacks()` 被多次调用也保持安全。
- 向 Phoenix 传播有用的 trace/session metadata：thread/session id、user id、assistant/subagent name、model name、environment，以及可用的 caller tags。
- 支持从上游 gateway/RBAC/自有 gateway 接收 W3C OTel context，并按配置决定 DeerFlow graph root span 是新 trace root 还是上游 span 的 child。
- 对 subagent 的 context/session 传播做聚焦验证，覆盖独立 graph run 和 isolated loop/thread 执行。
- 保持 LangSmith 和 Langfuse 的现有环境变量与 callback 路径兼容。

**非目标：**

- 替换 RunJournal、RunEventStore、token aggregation、run history APIs 或 UI-facing message/event storage。
- 构建 Phoenix 专用 event store，或把 Phoenix 作为 run history 的 source of truth。
- 为 Phoenix trace 浏览增加 provider-specific UI 功能。
- 移除 LangSmith 或 Langfuse 支持。
- 在本次变更中为所有非 LangChain 子系统加入完整 distributed tracing。

## 技术决策

### 决策：拆分 provider lifecycle 和 callback 创建

Phoenix setup 需要进程级 OpenTelemetry/OpenInference 初始化。tracing 模块应区分三类职责：

- Callback providers：LangSmith 和 Langfuse 返回 LangChain callback handlers。
- Instrumentation providers：Phoenix 初始化 OpenTelemetry/OpenInference instrumentation，可能不返回 callback。
- Root metadata/context providers：Phoenix 可能需要在 root graph invocation 外层建立 OpenInference 或 OpenTelemetry context scope；Langfuse 则使用 `RunnableConfig.metadata` 中的保留 key。

`build_tracing_callbacks()` 可以继续作为当前调用点的兼容 API，但 Phoenix 初始化应隐藏在一个幂等 provider initializer 后面。如果实现需要显式的 Phoenix session/user metadata context manager，应新增一个小的 tracing runtime helper，而不是把 callback handler 承担不了的职责塞进去。

备选方案：从 `build_tracing_callbacks()` 返回 Phoenix callback handler。默认不采用这个方案，因为它不符合 Phoenix 以 OTel/OpenInference 为主的模型，并且在启用 auto-instrumentation 时更容易产生重复 span。

### 决策：RunJournal 保持并行且 provider-independent

RunJournal 继续由 gateway run worker 挂载，并继续写入 `RunEventStore`。Phoenix 在本次变更中不能读取或写入该 store。外部 tracing 层可以接收与 RunJournal 字段重叠的 metadata，但不能替代内部 lifecycle、message、token 或 debug/audit event capture。

备选方案：直接把 RunJournal events 导出到 Phoenix。初始变更不采用这个方案，因为它会扩大范围，引入 schema drift 风险，并可能与 LangChain/OpenInference instrumentation 已捕获的事件重复。

### 决策：在现有 provider 旁边增加 Phoenix 配置

在 `TracingConfig` 下增加 `PhoenixTracingConfig`，仍然使用环境变量配置，与 LangSmith 和 Langfuse 保持一致。建议字段：

- `enabled`：`PHOENIX_TRACING`
- `collector_endpoint`：`PHOENIX_COLLECTOR_ENDPOINT`，启用后未设置时默认指向本地 Phoenix
- `api_key`：`PHOENIX_API_KEY`，本地部署可选，仅在目标 endpoint 需要认证时要求
- `project_name`：`PHOENIX_PROJECT_NAME`，默认 `deer-flow`
- `auto_instrument`：`PHOENIX_AUTO_INSTRUMENT`，默认值由 spike 结果决定
- `capture_content`：`PHOENIX_CAPTURE_CONTENT`，默认值由 spike 后选择的安全行为决定
- `metadata_allowlist`：`PHOENIX_METADATA_ALLOWLIST`，默认空；逗号分隔的精确 root `RunnableConfig.metadata` 顶层 key，去除空白并按首次出现顺序去重
- `trace_parent_mode`：`PHOENIX_TRACE_PARENT_MODE`，支持 `root`、`child`、`auto`，默认建议为 `auto`
- `trace_parent_required`：`PHOENIX_TRACE_PARENT_REQUIRED`，当 `child` 模式缺失有效上游 context 时控制 fail-fast 还是降级为 root
- `propagate_baggage`：`PHOENIX_PROPAGATE_BAGGAGE`，控制是否传播 W3C baggage

校验逻辑只应在显式启用且配置内部不一致时 fail fast。本地 Phoenix 应允许无 cloud credentials 使用。

备选方案：把 Phoenix 放进 `config.yaml`。不采用这个方案，因为当前外部 tracing provider 模型是基于环境变量配置的。

### 决策：支持上游 OTel context 和 parent mode 配置

Phoenix tracing 应支持三种链路归属模式：

- `root`：DeerFlow graph root span 永远新建 trace root。即使请求中存在 `traceparent`，也不把 DeerFlow graph 挂到上游 span 下。可以选择把上游 request id 或 trace id 作为普通 metadata 记录，便于人工关联。
- `auto`：如果入口处存在有效 `traceparent`/`tracestate`，DeerFlow graph root span 使用该 context 作为 parent；如果不存在或无效，则新建 trace root。该模式最适合作为默认值，因为它兼容无上游 tracing 的部署，也支持已有 gateway/RBAC 链路的端到端 trace。
- `child`：DeerFlow graph root span 期望作为上游 span 的 child。缺失或无效上游 context 时，行为由 `PHOENIX_TRACE_PARENT_REQUIRED` 控制：strict 模式 fail fast；非 strict 模式降级为新 root，并在 metadata 中标记 parent context 缺失。

gateway HTTP/request 入口负责提取 W3C `traceparent`、`tracestate`、`baggage`，但真正执行 graph 的 run worker 可能是后台异步路径，不能只依赖当前请求线程或 `ContextVar`。提取出的 context 应显式放入 run config/context/payload，并在 worker 执行 agent graph 前恢复为当前 OTel context。这样 Phoenix/OpenInference instrumentation 创建 DeerFlow graph root span 时，才能正确继承上游 span。

embedded client 没有 HTTP headers，因此应提供可选参数或 config 字段接收上游 trace context，例如 `trace_context`、`traceparent`/`tracestate`/`baggage`，或等价结构。未传入时按 `trace_parent_mode` 决定新建 root 或 fail/降级。

备选方案：只依赖 HTTP auto-instrumentation 自动处理上游 context。这个方案不足以覆盖后台 run worker、embedded client 和 subagent isolated loop/thread，因为这些路径会跨越请求边界或线程/事件循环边界，必须显式传递 context。

### 决策：增加 provider-neutral trace metadata helpers

当前 metadata helper 是 Langfuse-specific。应新增 provider-neutral helper，接收已有 root metadata 输入，再分发给 provider-specific injector：

- Langfuse 保留现有 `langfuse_*` reserved metadata。
- Phoenix 通过 spike 选定的机制接收稳定的 session/user/assistant/model/environment metadata：OpenInference context manager、OpenTelemetry baggage/context、`RunnableConfig.metadata`，或它们的组合。

调用点应收敛到同一个 helper，避免 gateway worker、embedded client、lead agent 和 subagent execution 之间出现漂移。

备选方案：在每个 `inject_langfuse_metadata()` 调用旁边新增独立 `inject_phoenix_metadata()` 调用。作为过渡实现可以接受，但共享 helper 更适合后续继续增加 provider。

### 决策：关闭内容采集时使用受信 correlation metadata 双重隔离

OpenInference 的 input/output hide flags 不会过滤自定义 metadata，而且 LangChain auto-instrumentor 会独立序列化 `RunnableConfig.metadata`。因此只过滤 DeerFlow 手工 root 的 `using_attributes(metadata=...)` 不足以兑现 `PHOENIX_CAPTURE_CONTENT=false`。

关闭内容采集时，系统应同时执行两层隔离：

- Phoenix root context 只接收由 DeerFlow 运行时构造的 correlation metadata，以及 `PHOENIX_METADATA_ALLOWLIST` 精确命中的 caller 顶层 key。该 Phoenix-safe 子集默认空 allowlist，当前部署示例为 `request_id,tenant_id`；它只表示可导出，不证明值可信。
- graph invocation 的 `RunnableConfig.metadata` 重建为包含相同 Phoenix-safe 子集，防止 OpenInference LangChain auto-instrumentor 从 run metadata 再次导出 caller payload 或伪造的 correlation 值。DeerFlow 权威字段后写入，因此 caller 不能覆盖 session/thread、user、assistant/subagent、model、environment、root run name、run id 或受控 tags；caller tags 不适用 allowlist。agent factory 解析 effective model 后，worker 必须从 factory 前 caller metadata 快照再次重建实际传给 `agent.astream()` 的 config，排除 factory 追加字段并同步 authoritative `model_name`。与 Langfuse 并用时，auto-instrumentor metadata 可以额外保留由 DeerFlow 构造、为 Langfuse 所需的 `langfuse_*` reserved metadata；allowlist 中的其他 provider reserved key（至少 `langfuse_*`）对 Phoenix root context 一律忽略。

开启内容采集时保持原有完整 metadata/tags 传播，表示部署方明确接受这些内容可能被外部 tracing provider 导出。

### 决策：保持 graph-root 挂载并避免模型级重复

主 agent、embedded client 和 subagent graph run 必须继续在 root invocation 挂载 provider callbacks 或进入 Phoenix tracing context。graph 内部模型创建必须继续传 `attach_tracing=False`。standalone model creation 在 `attach_tracing=True` 时仍可调用 tracing builder，但 Phoenix 初始化必须保持幂等，且不能创建重复 callback handlers。

备选方案：在每个 model instance 上挂 Phoenix instrumentation。不采用这个方案，因为它和现有 root-level trace 结构冲突，也更容易重复。

### 决策：先做 Phoenix auto-instrumentation 最小 spike

在最终确定实现形态前，先运行最小 spike，对比：

- 只启用 Phoenix/OpenInference LangChain auto-instrumentation。
- 只使用手工 LangChain callback/span wiring，前提是存在稳定的 Phoenix-compatible callback path。
- auto-instrumentation 与手工 callbacks/spans 组合。

spike 必须检查主 agent、embedded client 和 subagent run 的 trace shape。最终实现应选择最小可行 setup，保证 root traces 连贯、graph/model/tool spans 嵌套正确、metadata 正确，并且没有重复 span。

### 决策：把 subagent context propagation 作为专门验证目标

Subagent 会创建独立 graph run，并可能被调度到 isolated event loop/thread。实现不能假设 Python `ContextVar` 会天然跨 `asyncio.run_coroutine_threadsafe()` 可靠传播。spike 和测试应验证 Phoenix/OpenTelemetry context 是否能通过现有 `copy_context` 调度路径保留；如果不能，就在 subagent root run 内显式 attach parent OpenTelemetry context，或重新创建 Phoenix session/user metadata。

期望结果是：Phoenix 中能通过 session/thread/user 稳定关联 parent 与 subagent，并且 parent task dispatch 与 subagent spans 之间关系清晰。如果 isolated loop 中严格 parent-child trace propagation 不可靠，实现应有意创建一个带相同 session/thread metadata 的 linked subagent root trace，而不是意外产生孤立 spans。

### 已确认兼容性根因：LangSmith external RunTree 与 OpenInference parent registry 不对称

2026-07-14 的真实 Phoenix trace、锁定版本诊断及使用真实 `create_agent` + DeerFlow model/tool middleware 的生产等价复现确认：`create_agent()` 会把 model/tool wrapper middleware 包装为 LangSmith `traceable` RunTree。LangChain 在满足 parent tie-break 条件时，会在 wrapper 内配置嵌套 callback manager 时用 active RunTree UUID 覆盖 inherited callback parent，并只把该 external parent 注入 `LangChainTracer.run_map/order_map`。`OpenInferenceTracer` 不属于 `LangChainTracer`，其 `_spans_by_run` 没有 wrapper UUID；0.1.67 版本直接 lookup 失败后使用 ambient OTel context。当前运行进程未启用 LangSmith OTel/hybrid mode，ambient 是 DeerFlow 手工 root，所以 LLM/tool 回退到该 root；其他部署若激活 LangSmith RunTree NonRecordingSpan，registry miss 的最终 parent 可能不同，但 logical OpenInference parent gap 仍然存在。

该根因同时解释两种表现：LangSmith 能显示 `awrap_model_call` / `awrap_tool_call` RunTree，而 OpenInference auto-instrumentation 不会为这些 LangSmith-only wrappers 创建 span；嵌套终端 callback 又引用不可见 wrapper 作为 parent，进一步造成 parent hierarchy 断裂。这个机制已对 chain、LLM、tool、retriever 验证，不得按模型或具体工具逐个打补丁。Task 7.5 已采用类型无关的最近已登记业务祖先兼容规则修复基础 parent 合约，并由真实 callback/OTel 集成测试固定行为。

### 决策：基础业务 parent 与 middleware diagnostics 分离

通用 parent 关系属于 Phoenix 基础 tracing 的正确性要求，必须默认生效。实现应在 OpenInference 无法解析 direct external RunTree parent 时，使用类型无关的兼容规则找到最近的已登记业务祖先，使 LLM、tool、chain、retriever 等终端 callback span 归属到对应 `model`、`tools`、chain 或 retrieval 节点。该修复不得依赖模型名、工具名或单一 callback 类型，也不得通过删除终端 span、移除 DeerFlow 手工 root 或关闭中间节点掩盖问题。

完整复制 LangSmith 的 `awrap_model_call` / `awrap_tool_call` RunTree 不属于 OpenTelemetry 或 OpenInference 的基础合规要求。OpenInference 没有通用 middleware span kind，而且大量 wrapper span 的持续时间高度重叠；默认导出会增加 trace 噪声、存储量、重复 instrumentation 风险，以及对 LangChain factory 和 LangSmith `traceable` 内部生命周期的版本耦合。因此基础 parent 修复不要求这些 wrapper span 存在。

完整 middleware diagnostics 已迁移至独立 `add-phoenix-middleware-diagnostics` change。该能力的默认关闭配置、wrapper lifecycle、私有 API fail-fast、升级验证和专项测试只在该 change 中定义；本 change 仅要求基础 terminal-parent 合约持续成立。

### Task 7.5 实现结果：Phoenix-owned tracer instance 兼容层

Task 7.5 不修改全局 `OpenInferenceTracer` 类，也不为不同 callback 类型分别打补丁。Phoenix 注册成功后，DeerFlow 只对 Phoenix provider 所拥有的 OpenInference LangChain tracer 实例安装兼容实现；其他既有或后续创建的 OpenInference tracer 保持原状，注册失败也不会留下兼容状态。

兼容层首先保留能够直接解析的 parent。direct external RunTree parent 未登记时，它从当前 `RunTree.dotted_order` 由近到远寻找同一 tracer registry 中已登记的业务祖先，并在一次解析中保留实际 parent span、冻结其 `SpanContext`，避免并发结束 parent 导致二次 lookup 回退。若没有任何已登记祖先，则使用显式空 OTel context 创建新 root，不继承 ambient DeerFlow 手工 root。该规则对 LLM、tool、chain、retriever 一致生效。

该实现依赖 OpenInference 与 LangSmith 的私有接口，因此后端精确锁定 `langchain==1.2.15`、`langchain-core==1.3.3`、`langsmith==0.8.18` 和 `openinference-instrumentation-langchain==0.1.67`，并在 Phoenix 注册前验证版本、tracer helper/slot 和 dotted-order 解析顺序。升级任一依赖都必须重新完成兼容性验收。

验收使用真实 LangChain callback manager、LangSmith external RunTree、锁定版本 `create_agent`、DeerFlow model/tool middleware、确定性本地 model/tool 以及 in-memory OTel exporter。主 agent、embedded client、subagent copied-thread 和 production persistent isolated-loop 路径均直接断言 LLM 位于 `model` 下、具体工具位于 `tools` 下，且不会回退到手工 root。完整 `awrap_model_call` / `awrap_tool_call` middleware wrapper 树不属于本 change 的基础 provider 范围。

### 决策：稳定化 wave 是基础 provider 的合并门槛

Phoenix 必须使用独立 provider：`phoenix.otel.register(..., set_global_tracer_provider=False, batch=True, auto_instrument=False)` 只负责创建 provider，返回值由 DeerFlow 保存。DeerFlow 随后枚举 `openinference_instrumentor` entry points，将 `deerflow.run` 与所有显式 OpenInference instrumentation 绑定到同一 provider。这样 provider 在任何 instrumentor 发生失败前已经由 DeerFlow 持有，可以执行完整回滚；不得覆盖宿主 global provider，不得隐式 fan-out 到宿主 processor。

instrumentation 安装是 DeerFlow 拥有的事务：初始化前快照全部 entry-point singleton；现有任一 instrumentor 已激活时视为 foreign owner，在 compatibility validation 和 mutation 前 fail fast，并 shutdown 新建但未激活的 Phoenix provider。显式安装中途失败时，按逆序回滚全部 attempted instrumentors、恢复实例快照、content-hide 环境、兼容层和 active state，然后关闭 provider并允许重试。成功关闭时同样逆序卸载 DeerFlow 拥有的全部 instrumentors，不只处理 LangChain。

生产导出使用 `BatchSpanProcessor`，默认 `batch=True`；队列、调度、导出超时和最大批量只接受标准 `OTEL_BSP_MAX_QUEUE_SIZE`、`OTEL_BSP_SCHEDULE_DELAY`、`OTEL_BSP_EXPORT_TIMEOUT`、`OTEL_BSP_MAX_EXPORT_BATCH_SIZE`。服务受控关闭时先解除该 provider 的 OTel SDK `atexit` handler，再执行 `force_flush` 和 `shutdown`，避免超时后的 interpreter exit 再次同步进入阻塞 provider。gateway 在 run drain 后用 daemon cleanup thread 执行同步 SDK 清理，并以现有 5 秒 shutdown hook deadline 有界等待；每次 graph run 不得关闭 provider。

parent SpanContext 与 baggage 采用两个独立输入：允许仅 baggage carrier；root 和 fallback 从显式空 span context 开始，可保留显式 carrier baggage，但绝不继承 ambient span 或 ambient baggage；关闭 baggage propagation 时必须剥离 baggage。手工 root 只通过 `using_attributes(metadata=...)` 发送 metadata，不得再把 Python dict 写为 OTel span attribute；直接属性必须符合 OTel attribute 类型。

embedded sync generator 的 root span 可以覆盖整个迭代，但 current Phoenix context 只能包围底层 iterator 的单次推进。每次 `next()`、提前 `close()`、异常和交错 generator 都必须 attach/detach，禁止跨对外 yield 保持 current context。

Task 7.10 的 initializer fixture remediation 只修改 `backend/tests/test_phoenix_root_runtime.py`。它必须让真实运行时 producer 文件先执行，再以固定顺序正常收集 `test_phoenix_initializer_is_idempotent_for_same_config`、`test_phoenix_initializer_rejects_changed_active_config`、`test_phoenix_initialization_error_is_provider_specific`；RED 与 GREEN 使用完全相同的 node ID 顺序。fixture 必须逐项恢复被替换的 `sys.modules` 条目、OpenInference hide 环境变量、Phoenix initializer bookkeeping，以及 `LangChainInstrumentor` 的 tracer/instrumented state，禁止以 `-k` 或 `--deselect` 隐藏失败。

### Task 7.5.1 实现结果：运行边界与 graph invocation 命名去歧义

同名问题的根因不是 Phoenix 重复注册，而是 DeerFlow 原先把同一个 `root.run_name` 同时用于手工 OTel 运行边界和传给 LangGraph 的 graph invocation name。OpenInference 随后为真实 graph invocation 创建第二个同名 span，因此 Phoenix 中会看到两个职责不同但名称相同的 `lead_agent`。

DeerFlow 继续保留手工运行边界，因为它承载上游 parent mode、session/user attributes 和 main/embedded/subagent 的统一生命周期；仅删除该 span 会破坏现有 root 合同。手工边界现统一命名为 `deerflow.run`，自动 graph span 继续使用真实 run name。正常结构为：

```text
deerflow.run                         # DeerFlow/upstream 运行边界
└── lead_agent                       # OpenInference 自动 graph invocation
    ├── model
    │   └── ChatOpenAI
    └── tools
        └── web_search
```

手工边界保留 `openinference.span.kind=agent`，并增加直接可查询的 `deerflow.span.role=run_boundary`、`deerflow.agent_name` 和 `deerflow.root_run_name` attributes。gateway worker 在 `RunRecord.assistant_id` 缺失时使用仓库默认权威 identity `lead_agent`；embedded client 和 subagent executor 直接传递各自已解析的 assistant/subagent identity，均不从 caller metadata 反推。

验收使用真实 OTel SDK、OpenInference tracer 和 in-memory exporter，并分别经过生产 `run_agent()`、`DeerFlowClient.stream()` 与 `SubagentExecutor._aexecute()` 入口，断言手工边界独占 `deerflow.run` 名称、自动 graph 保留真实名称、两者位于同一 trace 且 graph 直接以边界为 parent。该命名修复不改变 Task 7.5 parent 兼容规则，也独立于 mandatory Task 7.6 provider ownership/exporter lifecycle。

完整 wrapper tree 仅由 `add-phoenix-middleware-diagnostics` 定义。

### Task 7.5.2 设计结果：W3C parent 有效性与 ambient 隔离

原实现把任意非空 `traceparent` 当成可用 parent，并且只在使用上游 carrier 时 attach context。结果是 strict `child` 会接受无效 carrier，而 `root`、`auto` 无 parent 和非 strict `child` fallback 会在存在 ambient OTel span 时意外成为其 child。

修复后的 parent resolver 从显式空 `Context()` 开始，分别使用 W3C baggage propagator 和 trace-context propagator 解析 carrier。只有解析后的 `SpanContext.is_valid` 为真才续接上游 trace；合法但 unsampled 的 parent 仍然有效。行为矩阵为：

| Mode | 有效 parent | 缺失 parent | 无效 parent |
|---|---|---|---|
| `root` | 忽略并新建 root | 新建 root | 忽略并新建 root |
| `auto` | 续接上游 | 新建 root，`missing_parent` | 新建 root，`invalid_parent` |
| strict `child` | 续接上游 | 创建 span 前 fail fast | 创建 span 前 fail fast |
| 非 strict `child` | 续接上游 | 新建 root，`missing_parent` | 新建 root，`invalid_parent` |

每个分支都会 attach 一个确定的 context：有效 parent 分支 attach 解析后的 parent context；新 root 分支 attach 不含 span 的空 trace context，启用 baggage 时只保留显式 carrier 中解析出的 baggage。`finally` 负责 detach，因此正常返回和异常退出都会恢复调用方原有 ambient span/baggage。carrier transport 保留“已提供但无效”的原始 `traceparent`，但不自行解析格式；W3C propagator 和 `SpanContext.is_valid` 仍是唯一有效性权威。

### Task 7.7 实施与验收结果：baggage-only ingress

carrier boundary 接受仅含 baggage 的 header/mapping；关闭 baggage propagation 时，gateway 仍保留有效 `traceparent`/`tracestate`，只剥离 baggage。parent 与 baggage 的真实 OTel 矩阵继续证明所有 root/fallback 仅使用显式 carrier baggage，不继承 ambient span 或 ambient baggage。remediation 同时修复测试级 Phoenix provider/instrumentor lifecycle 泄漏，最终六文件完整聚合在不使用 `-k` 或 `--deselect` 的情况下为 `175 passed, 1 warning`；真实跨线程测试未使用 inline 替身。

### Task 7.8 实现结果：Root metadata OTel attribute 合规性

`deerflow.run` 的 metadata 现在只有一条导出路径：`activate_phoenix_root_context()` 在创建 span 前进入 `using_attributes(metadata=...)`，由 OpenInference/Phoenix provider 负责规范编码。`_set_root_span_attributes()` 不再接收 metadata，也不再把 Python `dict` 传给 `Span.set_attribute()`。

手工 root 保留的直接属性仅包括 OpenInference span kind、session/user、字符串 tags，以及 `deerflow.span.role`、`deerflow.agent_name`、`deerflow.root_run_name`、parent mode/fallback 等字符串字段。真实 OpenTelemetry SDK 与 in-memory exporter 同时覆盖 `PHOENIX_CAPTURE_CONTENT=false/true`，证明没有 `Invalid type dict` warning，且 fake runtime 仍验证 safe/full metadata 完整进入 `using_attributes`。该修复不改变 RunnableConfig metadata allowlist、RunJournal 隔离或 auto-instrumentation 的 JSON metadata 编码路径。

### Task 7.9 实现结果：Embedded generator context scope

`DeerFlowClient.stream()` 不再跨 `yield` 持有 Phoenix context。新增 `PhoenixRootScope`（`open_phoenix_root_scope()`）：`start()` 用 `tracer.start_span()` 创建一次 `deerflow.run` 并快照 per-step `Context`（resolved parent + root span + `using_attributes` attributes）；`activate()` 只在底层 iterator 每次 `next()` 推进期间 attach/detach；`close(exc)` 幂等结束 span，`Exception` 记录 exception + ERROR status，`GeneratorExit` 普通结束（对齐 `use_span` 语义）。逐次 `next()`、提前 `close()`、迭代异常、双 stream 交错由真实 OTel SDK + in-memory exporter 锁定；gateway worker 与 subagent executor 的 async 路径继续使用 `activate_phoenix_root_context()`，行为不变。

补充（2026-07-18，wave 后三态 status 细化）：`PhoenixRootScope.close()` 的无异常路径现在显式 `set_status(Status(StatusCode.OK))`，形成三态语义——正常迭代完成（`exc is None`）为 `OK`；迭代异常（`Exception`）为 `ERROR`；caller 提前放弃流（`GeneratorExit`/其他 `BaseException`）保持 `UNSET` 且不 record exception。这是对 Task 7.9「对齐 `use_span` 语义（成功不显式标注）」决策的细化：`close(exc)` 的参数足以区分「完成」与「中断」，三态信息量严格更大，并与 OpenInference 子 span 的显式 OK 标注在 Phoenix UI 口径一致。行为锁：`backend/tests/test_phoenix_generator_scope.py` 的 `test_context_detached_between_yields`（OK）、`test_early_close_ends_span_once_and_restores_context`（UNSET）、`test_iteration_exception_records_error_and_restores_context`（ERROR）。

补充二（2026-07-18，三态扩展到 gateway/subagent 路径）：`activate_phoenix_root_context()` 现在 yield 显式确认柄 `PhoenixRunBoundary`；gateway worker（单/多模式两个调用点）与 subagent executor 在迭代循环完整结束后、with 块内调用 `mark_complete()` 设置 `StatusCode.OK`。abort `break`、cancel `return`、`CancelledError` unwind 与未绑定柄的直接使用均保持 `UNSET`；`Exception` 仍由 `start_as_current_span` 自动置 `ERROR`。`llm_error_fallback` 的业务错误状态属 RunJournal/RunRecord，不影响边界 span 的完成语义。行为锁：`test_phoenix_parent_compat.py`（worker main 完成 OK、abort UNSET、直接使用 UNSET）与 `test_subagent_executor.py`（完成 OK、cancel UNSET）。

### Task 7.10 实现结果：Cross-path 真实集成与 fixture 污染修复

`test_phoenix_root_runtime.py` 的 initializer 测试不再向 `sys.modules` 安装 non-package 假 `openinference` parent：真实 package 全程保留，fake 收敛为 per-test 的 `phoenix.otel.register` seam 与 instrumentor 交互 stub；新增 `_initializer_isolation()` 统一 snapshot/restore/assert `sys.modules`、10 个 hide env、Phoenix bookkeeping 与 `LangChainInstrumentor._tracer`/`_is_instrumented_by_opentelemetry`，两个 `stub_parent_compat=False` 测试的手动 finally 恢复收敛到同一 helper。`test_phoenix_parent_compat.py` 的 `_EmbeddedGraphAgent.stream()` 改为惰性生成器，graph span 与 Task 7.9 per-step activation 语义对齐。同序无 deselect 验收：initializer 三 node 在真实 runtime producer 后正常运行，六文件聚合 `170 passed` 全绿；cross-path 矩阵（global provider、foreign instrumentor、gateway、embedded、subagent 含 isolated-loop、batch flush/shutdown）每格均有真实入口覆盖。生产代码零改动。

### Task 7.11 实现结果：Canonical 文档与 SDD 证据校正

修正了面向当前读者的过期/高估证据措辞：`README.md` 与 `backend/README.md` 的 wrapper-tree 说明现在显式命名独立、默认关闭的 `add-phoenix-middleware-diagnostics` change；canonical review（`.superpowers/sdd/final-whole-branch-review.md`）追加 dated 证据校正 addendum，覆盖旧编号中把完整 wrapper tree 标为可选 provider Task 的表述、已被 Task 7.10 修复的 initializer fixture 失败，以及 Assessment 中已解决的“仍需修复”清单（当前唯一合并门槛为 Task 7.12）；`progress.md` 与 `handoff.md` 追加 supersession 记录。历史 review/report 原文一律保留，仅以 dated addendum 校正。验证：provider stale-binding 检查保持 exit `1`（pre-existing clean，源自 stabilization planning）；diagnostics change `tasks.md` 2.1-2.3 精确匹配；provider 对 diagnostics change 的全部引用仅为 ownership/迁移说明；strict OpenSpec validation 与 `git diff --check` 通过。`backend/CLAUDE.md` 与 `backend/docs/phoenix-tracing-spike.md` 经逐行核实无过期措辞，未改动（wave 计划 Modify 清单的 verified-no-change 偏离，已如实记录）。生产代码零改动。

## 风险 / 取舍

- 全局 OTel provider 冲突 -> 使用 lock 和幂等状态保护 Phoenix 初始化；文档说明当其他库已经配置 OpenTelemetry 时的行为。
- auto-instrumentation 与 callbacks 组合导致重复 span -> 先完成 spike，再选择最终 wiring；Phoenix 只保留一条活跃 LangChain tracing 路径。
- 上游 HTTP auto-instrumentation 与 DeerFlow graph instrumentation 产生重复或错误 parent-child 关系 -> 在 spike 中验证 HTTP/request context 提取与 LangGraph instrumentation 的组合；避免同时创建两个等价的 DeerFlow root spans。
- 异步 run worker 丢失上游 trace context -> 将提取出的 W3C context 显式写入 run config/context/payload，并在 worker 执行 graph 前恢复。
- isolated loop/thread 中 subagent context 丢失 -> 增加明确 subagent 测试，并在 subagent root run 中显式附加 metadata/context。
- gateway、embedded client 和 subagent metadata 漂移 -> 集中构造 metadata，并在所有 root invocation 路径复用。
- payload 隐私风险 -> 明确 Phoenix content capture 配置/文档，并避免直接把 RunJournal-only records 导出到 Phoenix。
- 依赖 live Phoenix 服务导致测试脆弱 -> 默认使用 monkeypatch Phoenix/OpenInference API 的单元测试，另加可选本地 Phoenix integration test。

## 迁移计划

1. 增加 Phoenix 配置与校验，默认关闭。
2. 在 tracing factory 后面加入幂等 Phoenix provider 初始化，不改变 LangSmith/Langfuse callback 行为。
3. 运行最小 spike，并在 auto-instrumentation、manual wiring 或受控 hybrid 中做选择。
4. 增加上游 OTel context 提取/序列化/恢复 helper，并接入 gateway worker 与 embedded client。
5. 增加 provider-neutral metadata/context helpers，并更新 root invocation 路径。
6. 增加配置、幂等性、root-level 挂载、parent mode、重复预防和 subagent context/session propagation 测试。
7. 文档化 Phoenix setup、环境变量、本地/云端 endpoint 示例、parent mode 行为和已知限制。

回滚很直接，因为 Phoenix 是 opt-in。关闭 `PHOENIX_TRACING` 即可恢复当前行为。如果依赖本身导致 import/runtime 问题，Phoenix imports 应保持 lazy，仅在 provider initializer 中发生，避免 disabled deployments 导入 Phoenix packages。

## 待确认问题

- 在当前代码库中，哪条 Phoenix 路径能产生最干净的 LangGraph trace shape：OpenInference auto-instrumentation、manual callback/span approach，还是受控 hybrid？
- `PHOENIX_CAPTURE_CONTENT` 默认行为应跟随 Phoenix 默认值，还是 DeerFlow 默认采用更保守的 payload 策略？
- `PHOENIX_TRACE_PARENT_MODE` 默认是否使用 `auto`，以及 `child` 模式缺失 parent 时默认是否 strict？
- subagent 运行在 isolated loop/thread 时，Phoenix 应把它表示成 parent task call 的 child spans，还是表示成共享相同 session/thread metadata 的 linked root traces？

## 2026-07-18 Subagent Parentage Canonical Correction

生产 trace `1e17242578c33de6b1724bfc5a66b8c7` 与
`f38072c0a2247e57a9b682a3eb8909e3` 是同一个 dual-parent-source 缺陷的两个分支，
不是两个互不相关的问题。跨 persistent isolated loop 时，手工 `deerflow.run` boundary
原先从 ambient OTel context 取 parent，而自动 subagent graph 从 LangChain callback/RunTree
取 parent：前者会让 boundary 成为 lead graph 的 sibling，后者会让 graph 绕过 boundary 并把
boundary 留成空的 root-level span。

成功的 canonical 拓扑固定为：

```text
tools
└── task
    └── deerflow.run                  # subagent manual run boundary
        └── subagent:<agent-name>     # automatic subagent graph root
            ├── model → LLM
            └── tools → concrete tool
```

`task_tool` 优先从当前 runnable callback manager 的 `parent_run_id` 解析 Phoenix-owned
registered task span，并把该 span 的精确 `SpanContext` 序列化为 isolated-loop handoff
carrier；registry miss 时才回退到 ambient OTel carrier。`SubagentExecutor` 为每次 automatic
graph root 分配 fresh UUID，并在 active boundary 内把该 exact run ID 一次性绑定到 boundary
`SpanContext`。绑定使用锁保护、消费时原子 `pop`、scope exit 清理，因此并行 task 不会串链。

此前记录的 linked subagent root 仍是 logical callback span 与 ambient parent 都无法传播时的
非失败降级边界，不是同一 trace 中 boundary 与 graph 成为 siblings 的默认成功形态。Phoenix
disabled 继续走 provider-neutral ambient carrier；registry miss 也不应让业务 task 失败。

本修正只覆盖 subagent handoff 和 automatic graph root。Task 7.5 对普通 model、tool、chain、
retriever 与 LLM descendants 的 direct-parent/nearest-registered-business-ancestor resolver 保持
不变。历史 `add-phoenix-tracing-provider` 的 52/52 表示当时计划和测试已完成；它不等同于后来才
加入的 exact `task → boundary → graph` production invariant 已被验证。该缺口由独立 OpenSpec
change `fix-phoenix-subagent-parentage` 记录和修复。

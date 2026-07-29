# phoenix-tracing-provider Specification

## Purpose
TBD - Define Phoenix as an opt-in external tracing provider while preserving DeerFlow's internal observability guarantees.

## Requirements

### Requirement: Phoenix provider 配置
系统 SHALL 支持将 Phoenix 作为 opt-in 外部 tracing provider，通过环境变量配置，并与 LangSmith、Langfuse 并列。

#### Scenario: Phoenix 默认关闭
- **WHEN** 没有设置 Phoenix tracing 环境开关
- **THEN** Phoenix SHALL NOT 出现在已启用 tracing providers 中
- **AND** tracing 配置或 callback 构造过程中 SHALL NOT 导入 Phoenix packages

#### Scenario: Phoenix 使用本地 collector 启用
- **WHEN** `PHOENIX_TRACING` 为 truthy，且没有配置 Phoenix API key
- **THEN** Phoenix SHALL 可配置为使用本地 collector endpoint
- **AND** 本地 Phoenix 使用场景下，校验逻辑 SHALL NOT 要求 cloud credentials

#### Scenario: Phoenix 使用远端 collector 启用
- **WHEN** `PHOENIX_TRACING` 为 truthy，且配置了需要认证的远端 collector endpoint
- **THEN** Phoenix validation SHALL 要求该 endpoint 所需的认证配置

#### Scenario: 同时启用多个外部 provider
- **WHEN** Phoenix 和一个或多个现有外部 tracing provider 都已完整配置
- **THEN** enabled tracing providers SHALL 同时包含 Phoenix 和已有已配置 provider
- **AND** LangSmith 和 Langfuse 的配置行为 SHALL 与当前环境变量保持兼容

#### Scenario: 配置 parent mode
- **WHEN** Phoenix tracing 被启用
- **THEN** 系统 SHALL 支持配置 `PHOENIX_TRACE_PARENT_MODE` 为 `root`、`child` 或 `auto`
- **AND** 系统 SHALL 支持通过 `PHOENIX_TRACE_PARENT_REQUIRED` 控制 `child` 模式缺失有效上游 context 时的 fail-fast 或降级行为
- **AND** 系统 SHALL 支持通过 `PHOENIX_PROPAGATE_BAGGAGE` 控制是否传播 W3C baggage

### Requirement: RunJournal 保持内部可观测性来源
系统 SHALL 让 Phoenix 与 RunJournal/EventStore 并行运行，并且 MUST NOT 替换内部 event storage、token aggregation、run history 或 gateway debug/audit APIs。

#### Scenario: Gateway run 中启用 Phoenix
- **WHEN** gateway-managed run 在 Phoenix tracing 启用且 RunJournal 已配置的情况下执行
- **THEN** RunJournal SHALL 继续向配置的 RunEventStore 写入 run lifecycle、message、trace、error 和 token usage events
- **AND** Phoenix SHALL 接收外部 tracing 数据，但不成为 gateway run record 的 source of truth

#### Scenario: Phoenix 配置后被关闭
- **WHEN** Phoenix tracing 被关闭
- **THEN** RunJournal/EventStore 行为 SHALL 保持不变
- **AND** gateway run events 和 token usage APIs SHALL 在没有 Phoenix 的情况下继续运行

### Requirement: Root-level graph tracing 挂载
系统 SHALL 在主 agent、embedded client 和 subagent run 的 graph invocation root 挂载或激活外部 tracing，并且 MUST 避免给 graph 内部模型实例重复挂载 provider instrumentation。

#### Scenario: Lead agent root run
- **WHEN** 为 top-level run 创建 lead agent graph
- **THEN** 已启用的外部 tracing callbacks 和 Phoenix tracing context SHALL 应用到 graph root configuration
- **AND** 该 graph 内部创建模型 SHALL 使用 `attach_tracing=False`

#### Scenario: Embedded client stream
- **WHEN** `DeerFlowClient.stream()` 创建 graph run
- **THEN** 已启用的外部 tracing callbacks 和 Phoenix tracing context SHALL 应用到 stream 的 root graph invocation
- **AND** embedded client SHALL NOT 依赖模型级 tracing callbacks 来捕获 graph 内 LLM spans

#### Scenario: Subagent graph run
- **WHEN** subagent executor 启动独立 graph run
- **THEN** 已启用的外部 tracing callbacks 和 Phoenix tracing context SHALL 应用到 subagent graph root
- **AND** subagent token collector 和 caller tags SHALL 保持挂载在同一个 root run configuration 上

#### Scenario: DeerFlow 运行边界与自动 graph span 可区分
- **WHEN** Phoenix 使用 DeerFlow 手工 span 表示上游 context、session/user attributes 和 graph invocation 的运行边界
- **THEN** 该手工边界 span SHALL 使用稳定名称 `deerflow.run`
- **AND** OpenInference 自动 graph span SHALL 保留真实 graph run name，例如 `lead_agent` 或 `subagent:<name>`
- **AND** 手工边界 span SHALL 直接包含 `deerflow.span.role=run_boundary`、权威 `deerflow.agent_name` 和 `deerflow.root_run_name` attributes
- **AND** main worker、embedded client 与 subagent executor SHALL 从服务端运行状态传入权威 agent identity，不得从 caller metadata 反推该值
- **AND** 手工边界 SHALL 保持 `openinference.span.kind=agent`，不得通过删除边界或自动 graph span 规避同名问题

#### Scenario: Standalone model creation
- **WHEN** 在 graph root 之外使用 `create_chat_model(..., attach_tracing=True)`
- **THEN** 已启用 tracing providers MAY 初始化或挂载 standalone model tracing
- **AND** Phoenix initialization MUST 保持幂等，使 standalone model tracing 不会创建重复 global instrumentation

### Requirement: Phoenix OpenTelemetry 初始化具备幂等性
系统 SHALL 以 lazy 且幂等的方式初始化 Phoenix/OpenTelemetry/OpenInference instrumentation。

#### Scenario: 重复构造 tracing callbacks
- **WHEN** 在同一进程中多次调用 `build_tracing_callbacks()` 或 tracing runtime helper
- **THEN** Phoenix instrumentation SHALL 针对当前 active configuration 最多注册一次
- **AND** 重复调用 SHALL NOT 添加重复 OpenInference/LangChain instrumentors

#### Scenario: Phoenix 初始化失败
- **WHEN** Phoenix tracing 被显式启用，但 provider 初始化失败
- **THEN** 系统 SHALL 抛出带有足够诊断上下文的 Phoenix provider-specific runtime error
- **AND** LangSmith 和 Langfuse 的错误行为 SHALL 保持不变

#### Scenario: Auto-instrumentation 重复预防
- **WHEN** Phoenix auto-instrumentation 被启用
- **THEN** 实现 MUST NOT 同时挂载会重复捕获同一 LangChain graph、model 或 tool spans 的 Phoenix manual callback/span 路径

#### Scenario: Graph 内终端 callback 的通用业务父子关系
- **WHEN** Phoenix auto-instrumentation 捕获位于 graph node 或 middleware wrapper 内的 LLM、tool、chain 或 retriever callback span
- **THEN** 每个终端 callback span SHALL 位于最近的已登记业务父节点下，包括对应的 `model`、`tools`、chain 或 retrieval 执行节点
- **AND** parent 解析 SHALL 使用通用兼容规则，不得按具体模型、工具或 callback 类型分别打补丁
- **AND** 上述终端 callback span MUST NOT 因 direct parent run/span 未登记而回退到 DeerFlow 手工 root

### Requirement: Phoenix metadata 与 session 关联
系统 SHALL 为 root graph runs 向 Phoenix 传播稳定 trace metadata，包括 session/thread id、user id、assistant 或 subagent identity、model name 和 environment。caller tags 仅可在 `PHOENIX_CAPTURE_CONTENT=true` 时传播；安全模式不得导出 caller tags，但 MAY 保留 DeerFlow 内部生成且受控的 subagent tag。

#### Scenario: Gateway lead-agent run metadata
- **WHEN** gateway run 在 Phoenix tracing 启用时启动
- **THEN** Phoenix spans 或 traces SHALL 包含从 run thread id、effective user id、assistant id、model name、environment 和 root run name 派生的 metadata

#### Scenario: Content capture enabled caller tags
- **WHEN** Phoenix tracing 启用且 `PHOENIX_CAPTURE_CONTENT=true`
- **THEN** Phoenix spans 或 traces MAY 包含可用的 caller tags

#### Scenario: Safe mode caller tags
- **WHEN** Phoenix tracing 启用且 `PHOENIX_CAPTURE_CONTENT=false`
- **THEN** Phoenix spans 或 traces SHALL NOT 包含 caller tags

#### Scenario: Embedded client metadata
- **WHEN** `DeerFlowClient.stream()` 在 Phoenix tracing 启用时启动
- **THEN** Phoenix spans 或 traces SHALL 包含生成或传入的 thread id、effective user id、assistant id、model name 和 environment

#### Scenario: Subagent metadata
- **WHEN** subagent run 在 Phoenix tracing 启用时启动
- **THEN** Phoenix spans 或 traces SHALL 包含 parent thread id、propagated user id、subagent identity、model name 和 environment
- **AND** `subagent:<name>` SHALL be treated as a DeerFlow internally generated controlled tag, not a caller tag, and MAY remain in safe mode

### Requirement: 上游 OTel context 接入与 parent mode
系统 SHALL 支持从上游 gateway/RBAC/自有 gateway 接收 W3C OTel context，并按配置决定 DeerFlow graph root span 是新 trace root 还是上游 span 的 child。

#### Scenario: Root mode 忽略上游 parent
- **WHEN** `PHOENIX_TRACE_PARENT_MODE=root` 且请求或 embedded client 输入中包含有效 `traceparent`
- **THEN** DeerFlow graph root span SHALL 新建 trace root
- **AND** DeerFlow graph root span SHALL NOT 以该上游 context 作为 parent
- **AND** 上游 request/trace 标识 MAY 作为普通 metadata 保留用于人工关联

#### Scenario: Auto mode 有上游 context
- **WHEN** `PHOENIX_TRACE_PARENT_MODE=auto` 且入口处存在有效 `traceparent` 和可选 `tracestate`/`baggage`
- **THEN** DeerFlow graph root span SHALL 使用提取出的 OTel context 作为 parent
- **AND** Phoenix 中 DeerFlow graph root span SHALL 与上游 gateway/RBAC span 处于同一 trace 链路

#### Scenario: Auto mode 无上游 context
- **WHEN** `PHOENIX_TRACE_PARENT_MODE=auto` 且入口处没有有效上游 OTel context
- **THEN** DeerFlow graph root span SHALL 新建 trace root
- **AND** 该行为 SHALL 与当前无上游链路部署兼容

#### Scenario: Child mode 缺失 parent 且 strict
- **WHEN** `PHOENIX_TRACE_PARENT_MODE=child` 且 `PHOENIX_TRACE_PARENT_REQUIRED=true`
- **AND** 入口处没有有效上游 OTel context
- **THEN** 系统 SHALL 在开始 graph run 前 fail fast
- **AND** 错误 SHALL 指出缺失上游 trace context

#### Scenario: Child mode 缺失 parent 且允许降级
- **WHEN** `PHOENIX_TRACE_PARENT_MODE=child` 且 `PHOENIX_TRACE_PARENT_REQUIRED=false`
- **AND** 入口处没有有效上游 OTel context
- **THEN** DeerFlow graph root span SHALL 降级为新建 trace root
- **AND** Phoenix metadata SHALL 标记 parent context 缺失或已降级

#### Scenario: 使用 W3C 语义验证上游 parent
- **WHEN** 任一入口提供 `traceparent` 和可选 `tracestate`
- **THEN** 系统 SHALL 从显式空 OTel context 使用 W3C trace-context propagator 解析 carrier
- **AND** 只有解析后的 `SpanContext.is_valid` 为真时才 SHALL 把该 carrier 作为上游 parent
- **AND** 合法但未采样的 W3C parent SHALL 仍被视为有效 parent
- **AND** 缺失 parent 与已提供但无效的 parent SHALL 分别记录为 `missing_parent` 与 `invalid_parent`

#### Scenario: 新 trace root 不继承 ambient span
- **WHEN** `root` mode 忽略上游 parent，或 `auto`/非 strict `child` 因缺失或无效 parent 降级
- **THEN** `deerflow.run` SHALL 从不包含 active span 的显式 OTel context 创建新 trace root
- **AND** `deerflow.run` SHALL NOT 继承调用线程或协程中的 ambient span/trace
- **AND** 启用 baggage 传播时，解析后的 W3C baggage MAY 保留，但 SHALL NOT 同时带入 ambient span 或未显式传播的 ambient baggage

#### Scenario: Parent context 激活后确定恢复
- **WHEN** `deerflow.run` 正常完成或 graph 执行抛出异常
- **THEN** 系统 SHALL detach 本次 parent/root context 并恢复调用方原有 ambient OTel context
- **AND** strict `child` 的缺失或无效 parent SHALL 在创建 `deerflow.run` 前 fail fast

#### Scenario: Gateway run worker 恢复上游 context
- **WHEN** gateway/request 入口提取到 `traceparent`、`tracestate` 或 `baggage`
- **THEN** 系统 SHALL 将该上游 OTel context 显式传递到后台 run worker 可读取的 run config、context 或 payload 中
- **AND** run worker SHALL 在执行 agent graph 前恢复该 OTel context

#### Scenario: Embedded client 提供 trace context
- **WHEN** embedded client 调用传入 `traceparent`、`tracestate`、`baggage` 或等价 trace context 结构
- **THEN** `DeerFlowClient.stream()` SHALL 按 `PHOENIX_TRACE_PARENT_MODE` 使用或忽略该 context
- **AND** 未传入 trace context 时 SHALL 按 `root`、`auto` 或 `child` 模式定义处理

### Requirement: Subagent Phoenix context propagation 已验证
系统 MUST 对 Phoenix/OpenTelemetry context 与 session 在 subagent 执行模式中的传播进行聚焦验证。

#### Scenario: Subagent same-loop execution
- **WHEN** subagent 没有跨入 isolated event loop thread 执行
- **THEN** Phoenix validation SHALL 显示 subagent graph run 具备稳定 session/user metadata
- **AND** subagent spans SHALL 通过 session/thread metadata 与 parent run 关联

#### Scenario: Subagent isolated-loop execution
- **WHEN** subagent 运行在后台线程中的 persistent isolated event loop 上
- **THEN** Phoenix validation SHALL 显示 subagent graph run 具备稳定 session/user metadata
- **AND** 实现 SHALL 要么保留 parent OpenTelemetry context，要么有意创建一个带相同 session/thread metadata 的 linked subagent root trace

#### Scenario: Subagent 接收上游链路 context
- **WHEN** top-level DeerFlow graph root span 是上游 gateway/RBAC span 的 child
- **THEN** subagent graph run SHALL 保持与该 top-level DeerFlow run 的可关联关系
- **AND** isolated loop/thread 中无法稳定保持严格 parent-child context 时，subagent SHALL 至少通过 session/thread/run metadata 与 top-level run 关联

#### Scenario: Context propagation regression
- **WHEN** isolated loop 跨线程 context propagation 无法被验证
- **THEN** subagent 的 Phoenix tracing SHALL 在 subagent root run 内使用显式 metadata/context attachment，而不是只依赖隐式 `ContextVar` propagation

### Requirement: Phoenix payload export controls
系统 SHALL 明确 Phoenix payload capture 行为，并且 MUST NOT 将 RunJournal-only event-store records 直接导出到 Phoenix。

#### Scenario: Content capture disabled
- **WHEN** Phoenix tracing 启用且 content capture 关闭
- **THEN** 导出的 Phoenix spans SHALL 按配置的 instrumentation 行为省略 prompt、completion 和 tool payload content
- **AND** 支持的非内容类 correlation metadata SHALL 仍然可用
- **AND** `PHOENIX_METADATA_ALLOWLIST` SHALL 默认为空，并将配置值解析为逗号分隔、去空白、按首次出现顺序去重的精确 caller metadata 顶层 key
- **AND** Phoenix root context SHALL 只接收 allowlist 命中的 caller metadata 和服务端权威 correlation metadata，且 SHALL NOT 接收其他 caller metadata、caller tags 或其他 provider 的 reserved metadata；allowlist 中的其他 provider reserved key（至少 `langfuse_*`）SHALL 被忽略
- **AND** 传给 OpenInference LangChain auto-instrumentation 的 root `RunnableConfig.metadata` SHALL 包含与 Phoenix root context 相同的 Phoenix-safe allowlist caller metadata 和服务端权威 correlation metadata，并且 MAY 额外包含其他已启用 provider 所需且由 DeerFlow 构造的受信 reserved metadata
- **AND** caller SHALL NOT 能通过伪造同名 metadata 覆盖 effective session/thread、user、assistant/subagent、model、environment、root run name 或 run id
- **AND** worker SHALL 在 agent factory 解析 effective model 后，从 factory 前 caller metadata 快照重建实际传给 `agent.astream()` 的 root `RunnableConfig.metadata`，使其与 Phoenix root 的 authoritative `model_name` 一致且不导出 factory 追加的非 allowlist metadata

#### Scenario: Content capture disabled allowlist example
- **WHEN** `PHOENIX_CAPTURE_CONTENT=false` 且 `PHOENIX_METADATA_ALLOWLIST=request_id,tenant_id`
- **THEN** caller 的 `request_id` 和 `tenant_id` SHALL 同时进入 Phoenix root metadata 与 OpenInference root `RunnableConfig.metadata`
- **AND** 未列入字段 SHALL 被删除
- **AND** allowlist SHALL NOT 允许 caller tags 导出

#### Scenario: Content capture enabled
- **WHEN** Phoenix tracing 启用且 content capture 开启
- **THEN** 系统 MAY 将完整 invocation metadata 和 tags 导出到 Phoenix
- **AND** 该模式 SHALL 被视为仅适用于可信 workload 的显式选择

#### Scenario: Internal event store isolation
- **WHEN** RunJournal 向 RunEventStore 写入 message 或 trace records
- **THEN** 这些 records SHALL NOT 通过 RunJournal/EventStore 路径直接转发到 Phoenix

### Requirement: Phoenix tracing 文档
系统 SHALL 文档化 Phoenix setup、configuration、validation 和 limitations。

#### Scenario: Developer 本地启用 Phoenix
- **WHEN** developer 阅读 tracing documentation
- **THEN** documentation SHALL 包含 Phoenix 环境变量、本地 collector setup 预期、dependency notes 和 verification steps

#### Scenario: Developer 评估 tracing shape
- **WHEN** developer 阅读 Phoenix tracing documentation
- **THEN** documentation SHALL 解释 root-level graph tracing invariant、auto-instrumentation spike outcome、subagent validation coverage 和 known limitations

#### Scenario: Developer 扩展 metadata allowlist
- **WHEN** developer 需要在 `PHOENIX_CAPTURE_CONTENT=false` 时导出额外业务关联字段
- **THEN** documentation SHALL 说明 `PHOENIX_METADATA_ALLOWLIST` 使用逗号分隔的精确顶层 metadata key
- **AND** documentation SHALL 提供环境变量与 gateway run metadata 示例，并说明修改后必须重启已运行的 DeerFlow backend processes
- **AND** documentation SHALL 明确 DeerFlow 不自动生成或校验 caller 业务字段、allowlist 不支持嵌套路径或 tags、开启 content capture 后 allowlist 不再构成导出边界

### Requirement: Phoenix provider ownership 与 exporter lifecycle
系统 MUST 使用 DeerFlow 独立拥有的 Phoenix `TracerProvider`，并且 MUST NOT 覆盖宿主已安装的 global tracer provider 或对宿主 processor 做隐式 fan-out。

#### Scenario: Phoenix 初始化使用独立 provider
- **WHEN** Phoenix tracing 被启用且尚未初始化
- **THEN** 系统 SHALL 调用 `phoenix.otel.register(..., set_global_tracer_provider=False)`
- **AND** 该调用 SHALL 使用 `batch=True` 与 `auto_instrument=False`，使 DeerFlow 在任何 OpenInference instrumentor mutation 前先获得 provider
- **AND** 系统 SHALL 保存返回的 Phoenix `TracerProvider`
- **AND** 系统 SHALL 枚举 `openinference_instrumentor` entry points，并将 `deerflow.run` 与全部启用的 OpenInference instrumentors 显式绑定到同一保存的 provider
- **AND** 宿主 global tracer provider SHALL 保持不变

#### Scenario: Foreign instrumentor 或失败初始化
- **WHEN** 已有 LangChain/OpenInference instrumentor 由非 Phoenix provider 拥有，或 Phoenix 初始化在 provider 创建后失败
- **THEN** 系统 SHALL 在激活 tracing 前 fail fast
- **AND** 系统 SHALL shutdown 新建但未激活的 Phoenix provider
- **AND** 系统 SHALL 逆序恢复所有 attempted instrumentors、content-capture 环境、active provider state 与兼容层
- **AND** 系统 SHALL NOT 修改 foreign instrumentor state 或留下 partial instrumentation

#### Scenario: Batch exporter 与受控关闭
- **WHEN** Phoenix tracing 在生产运行路径初始化和服务受控关闭
- **THEN** 系统 SHALL 默认使用 `batch=True` 和 `BatchSpanProcessor`
- **AND** 系统 SHALL 使用 `OTEL_BSP_MAX_QUEUE_SIZE`、`OTEL_BSP_SCHEDULE_DELAY`、`OTEL_BSP_EXPORT_TIMEOUT`、`OTEL_BSP_MAX_EXPORT_BATCH_SIZE` 配置批处理
- **AND** 受控关闭 SHALL 在 `shutdown` 前调用 `force_flush`
- **AND** 受控关闭 SHALL 在潜在阻塞清理前解除 Phoenix provider 的 OTel SDK `atexit` handler
- **AND** gateway SHALL 在 in-flight run drain 后从 daemon cleanup thread 执行 SDK 清理，并只在 gateway shutdown deadline 内等待
- **AND** 成功关闭 SHALL 逆序卸载 DeerFlow 拥有的全部 OpenInference instrumentors，并以 compare-and-restore 语义恢复 DeerFlow 设置的 content-capture 环境
- **AND** 任意单次 graph run SHALL NOT 调用 provider `shutdown`

### Requirement: Parent 与 baggage 独立隔离
系统 MUST 将 parent SpanContext 与 baggage 作为独立输入处理，且 root/fallback MUST 隔离 ambient span 与 ambient baggage。

#### Scenario: Baggage-only carrier
- **WHEN** 入口仅提供显式 W3C baggage 而未提供有效 `traceparent`
- **THEN** root 或 fallback SHALL 创建新 trace root
- **AND** 启用 baggage propagation 时 SHALL 保留该显式 carrier baggage
- **AND** root SHALL NOT 继承 ambient span 或 ambient baggage

#### Scenario: 关闭 baggage propagation
- **WHEN** `PHOENIX_PROPAGATE_BAGGAGE=false`
- **THEN** 系统 SHALL 从 Phoenix root/fallback context 剥离显式 carrier baggage
- **AND** 系统 SHALL NOT 继承 ambient baggage

### Requirement: Root metadata attribute 合规性
系统 MUST 只通过 `using_attributes(metadata=...)` 传递 Python dict metadata，并且直接写入 OTel span 的属性必须符合 OTel attribute 类型。

#### Scenario: Root span metadata 导出
- **WHEN** Phoenix root span 写入 correlation metadata
- **THEN** Python dict metadata SHALL 只传给 `using_attributes(metadata=...)`
- **AND** 系统 SHALL NOT 将该 Python dict 再写入任何 OTel span attribute
- **AND** 直接 span 属性 SHALL 仅使用 OTel SDK 接受的标量或同类型标量序列

### Requirement: Embedded generator Phoenix context scope
系统 MUST 防止 embedded sync generator 在对外 yield 期间保持 Phoenix current context。

#### Scenario: 单次推进、提前关闭与异常
- **WHEN** embedded generator 执行 `next()`、提前 `close()` 或迭代抛出异常
- **THEN** root span MAY 覆盖整个迭代生命周期
- **AND** Phoenix context attach SHALL 仅覆盖底层 iterator 的该次推进或关闭处理
- **AND** 调用方 context SHALL 在每次对外 yield、close 或异常后恢复

#### Scenario: 交错 generator
- **WHEN** 两个 embedded generator 交错执行 `next()`
- **THEN** 每个 generator SHALL 只使用自身的 Phoenix context
- **AND** 两个 generator SHALL NOT 继承、覆盖或泄漏彼此 context

### Requirement: 稳定化真实集成验收与独立审查
系统 MUST 在完成 Phoenix provider change 前以真实运行时集成测试解决已知 initializer fixture 污染，并完成新的 independent whole-branch review。

#### Scenario: Cross-path integration matrix
- **WHEN** 执行 Phoenix stabilization verification
- **THEN** 测试 SHALL 覆盖既有 global provider、既有 foreign instrumentor、gateway、embedded client、subagent isolated loop、batch flush 与受控 shutdown
- **AND** `backend/tests/test_phoenix_root_runtime.py::test_phoenix_initializer_is_idempotent_for_same_config`、`backend/tests/test_phoenix_root_runtime.py::test_phoenix_initializer_rejects_changed_active_config`、`backend/tests/test_phoenix_root_runtime.py::test_phoenix_initialization_error_is_provider_specific` SHALL 在真实 runtime producer 后以同序 RED/GREEN 正常运行并通过
- **AND** fixture cleanup SHALL 恢复被替换的 `sys.modules` 条目、OpenInference hide 环境变量、Phoenix initializer bookkeeping 和 LangChain instrumentor state
- **AND** verification SHALL NOT 将 deselect 计为通过

#### Scenario: Completion gate
- **WHEN** 计划将 Phoenix provider change 标记完成
- **THEN** 文档、canonical review、progress 与 handoff SHALL 不包含已过期或高估的验证措辞
- **AND** 完整 backend Ruff SHALL 通过并写入 verification report
- **AND** independent whole-branch review artifact SHALL 已存在，且在其生成前 Task 7.12 SHALL 保持未完成
- **AND** 新的 independent whole-branch review SHALL 报告 0 Critical、0 Important 和无未裁定测试失败

### Requirement: Tracing 不得参与子代理授权
系统 MUST 将子代理工具组与技能授权绑定到 agent 构造时的不可变业务策略，且 MUST NOT 从 tracing metadata、Phoenix content mode 或 instrumentation 状态恢复授权。

#### Scenario: Phoenix safe mode 重建 metadata
- **WHEN** Phoenix tracing 开启且 `PHOENIX_CAPTURE_CONTENT=false`，worker 重建实际传给 graph 的 metadata
- **THEN** 子代理的 effective tools 与 effective skills SHALL 与 tracing 关闭时完全一致
- **AND** metadata 中缺少或伪造 `tool_groups`、`available_skills` SHALL NOT 扩大或缩小权限

#### Scenario: Tracing 回滚
- **WHEN** Phoenix 实现或后续 tracing remediation 被回滚
- **THEN** per-agent delegation policy、单一 fail-closed resolver、catalog fingerprint 和缓存失效边界 SHALL 保留
- **AND** 系统 SHALL NOT 恢复全局 unrestricted `task` tool

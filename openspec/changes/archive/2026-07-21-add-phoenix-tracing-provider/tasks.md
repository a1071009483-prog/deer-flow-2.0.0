## 1. Phoenix 接入 Spike

- [x] 1.1 复核当前 Phoenix/OpenInference Python APIs，确定后端需要使用的精确依赖和 import paths。
- [x] 1.2 为 Phoenix/OpenInference LangChain auto-instrumentation 搭建最小 spike，并覆盖一个代表性 graph run。
- [x] 1.3 将 auto-instrumentation 与可行的手工 callback/span 路径进行对比，确认哪种方式不会重复生成 LangGraph、LLM 和 tool spans。
- [x] 1.4 在 spike 中验证 `root`、`auto`、`child` parent mode 下 DeerFlow graph root span 的 trace 归属行为。
- [x] 1.5 验证 gateway/request 入口提取的 `traceparent`、`tracestate`、`baggage` 能否在 run worker 执行 graph 前恢复为当前 OTel context。
- [x] 1.6 针对 subagent run 运行或记录同样的 spike 形态，覆盖 isolated loop/thread 执行。
- [x] 1.7 在实现注释或 tracing 文档中记录最终选择的 Phoenix wiring mode、parent mode 默认值和已知限制。

## 2. 配置与 Provider 生命周期

- [x] 2.1 在 `backend/packages/harness/deerflow/config/tracing_config.py` 中增加 `PhoenixTracingConfig`，解析 enablement、collector endpoint、API key、project name、auto-instrumentation 和 content capture 环境变量。
- [x] 2.2 将 Phoenix 纳入 `TracingConfig.enabled_providers`、`explicitly_enabled_providers`、validation 和 reset 行为，同时不改变 LangSmith/Langfuse 语义。
- [x] 2.3 为 `PhoenixTracingConfig` 增加 `trace_parent_mode`、`trace_parent_required` 和 `propagate_baggage` 配置与校验。
- [x] 2.4 增加 Phoenix provider initializer，使用 lazy imports、provider-specific errors、lock，以及针对 active configuration 的幂等注册。
- [x] 2.5 更新 `build_tracing_callbacks()` 或增加 tracing runtime helper，使 Phoenix 初始化可以运行，同时不强行把 Phoenix 塞进 callback-only 形态。
- [x] 2.6 确保 Phoenix tracing 关闭时不会导入 Phoenix/OpenInference packages。

## 3. Root Tracing 与 Metadata Wiring

- [x] 3.1 增加 provider-neutral trace metadata helpers，并保留现有 Langfuse reserved metadata 行为。
- [x] 3.2 为 Phoenix 增加 thread/session id、user id、assistant/subagent identity、model name、environment 和 caller tags 的 metadata/context 处理。
- [x] 3.3 增加 W3C OTel context helper，用于提取、序列化和恢复 `traceparent`、`tracestate`、`baggage`。
- [x] 3.4 更新 gateway/request 入口或 run 创建路径，将提取到的上游 OTel context 显式传递到 run worker。
- [x] 3.5 更新 gateway run worker，在执行 agent graph 前按 parent mode 恢复或忽略上游 OTel context。
- [x] 3.6 更新 lead agent root wiring，在 graph root 激活 Phoenix tracing，同时保持 graph 内模型创建使用 `attach_tracing=False`。
- [x] 3.7 更新 `DeerFlowClient.stream()` root wiring，支持可选传入 trace context，并在 stream root 按 parent mode 激活 Phoenix tracing。
- [x] 3.8 更新 gateway run worker metadata injection，使 gateway-managed runs 带上 Phoenix correlation metadata，同时不改变 RunJournal 挂载。
- [x] 3.9 更新 subagent root wiring，在独立 subagent graph root 激活 Phoenix tracing，并保留 `SubagentTokenCollector` callbacks 和 `subagent:<name>` tags。
- [x] 3.10 验证 standalone `create_chat_model(..., attach_tracing=True)` 可以初始化或挂载 tracing，且不会产生重复 Phoenix instrumentation。

## 4. Subagent Context Propagation

- [x] 4.1 为 same-loop subagent execution 增加 Phoenix metadata/session propagation 的聚焦测试覆盖。
- [x] 4.2 为 persistent isolated subagent event loop/thread 增加 Phoenix metadata/session propagation 的聚焦测试覆盖。
- [x] 4.3 覆盖 top-level graph 作为上游 gateway/RBAC child span 时的 subagent propagation 行为。
- [x] 4.4 如果隐式 `ContextVar` propagation 不充分，则在 subagent root run 内显式 attach 或 recreate Phoenix/OpenTelemetry context。
- [x] 4.5 验证最终 Phoenix trace relationship 要么是 parent-child，要么通过 session/thread metadata 被有意 linked，不能产生 orphan trace。

## 5. RunJournal 隔离与 Payload 控制

- [x] 5.1 增加测试，证明 Phoenix 启用时 RunJournal/EventStore run events 和 token aggregation 仍然工作。
- [x] 5.2 增加测试，证明 Phoenix 关闭时 RunJournal/EventStore 行为保持不变。
- [x] 5.3 按 spike 选定的 Phoenix/OpenInference API 实现并测试 Phoenix content capture 配置。
- [x] 5.4 确保 RunJournal/EventStore records 不会被直接转发到 Phoenix。

## 6. 文档与验证

- [x] 6.1 更新 README/backend tracing 文档，加入 Phoenix 环境变量、本地 collector setup、云端 endpoint 说明和依赖要求。
- [x] 6.2 文档化 root-level graph tracing invariant、`root`/`auto`/`child` parent mode，以及 Phoenix 与 callback-only LangSmith/Langfuse providers 的区别。
- [x] 6.3 文档化 gateway/RBAC/自有 gateway 传入 `traceparent`、`tracestate`、`baggage` 的接入方式和 embedded client trace context 参数。
- [x] 6.4 文档化 subagent validation 结果，包括 isolated loop/thread 行为、上游 context 续链行为和任何已知限制。
- [x] 6.5 运行相关后端单元测试，覆盖 tracing configuration、tracing factory/provider initialization、parent mode、root metadata wiring、RunJournal isolation 和 subagent propagation。
- [x] 6.6 在有 collector 可用时运行本地 Phoenix smoke test，并在 implementation notes 中记录验证结果。

## 7. Whole-Branch Review 修复

- [x] 7.1 修复 `PHOENIX_CAPTURE_CONTENT=false` 下任意 caller metadata 绕过 hide flags 的 Critical：同时隔离 Phoenix root attributes 与 OpenInference auto-instrumentor 可见的 `RunnableConfig.metadata`，增加关闭/开启内容采集的回归测试，并同步文档与验证记录。
- [x] 7.2 增加 `PHOENIX_METADATA_ALLOWLIST` 精确 key 白名单，使安全模式可显式导出 `request_id` 和 `tenant_id`，同时保持 DeerFlow 受信字段优先、caller tags 隔离和双导出路径一致，并同步测试与文档。
- [x] 7.3 补充 `PHOENIX_METADATA_ALLOWLIST` 使用文档，覆盖新增字段、gateway run metadata、重启要求、顶层 key/安全边界和 content capture 差异。
- [x] 7.4 完成通用根因诊断：验证 LangSmith `traceable` external RunTree 只桥接到 `LangChainTracer`、未登记到 OpenInference span registry，并以 chain/LLM/tool/retriever 及真实 Phoenix trace 证明 ambient root fallback。
- [x] 7.5 必须修复通用 parent 关系：采用类型无关的最小兼容方案，使 LLM、tool、chain、retriever 等终端 callback span 回到最近的已登记业务父节点，不得回退到 DeerFlow 手工 root；先增加锁定版本真实 OTel/OpenInference 失败集成测试，再验证主 agent、embedded client 与 subagent（包括 isolated loop/thread）路径。
- [x] 7.5.1 修复 DeerFlow 手工运行边界与自动 graph span 同名问题：手工边界统一命名为 `deerflow.run`，保留自动 graph 的真实 run name，并通过直接 OTel attributes 记录边界角色、权威 agent identity 和 graph root run name；使用真实 exporter 验证 main worker、embedded client 与 subagent executor 三条生产入口。
- [x] 7.5.2 修复上游 parent mode 有效性与 ambient 隔离：使用 W3C propagator 和 `SpanContext.is_valid` 校验 parent，区分 missing/invalid fallback，并确保 `root`、`auto` fallback 与非 strict `child` fallback 从显式空 trace context 创建新 root；使用真实 OTel SDK 验证 parent id、trace id、context 恢复和生产 carrier round-trip。
- [x] 7.6 完成 provider ownership/coexistence 与 exporter lifecycle：保存 `phoenix.otel.register(..., set_global_tracer_provider=False)` 返回的 Phoenix provider；`deerflow.run` 和 OpenInference auto-instrumentor 使用同一 provider；global provider 不被替换；foreign instrumentor fail-fast、失败初始化清理、默认 `batch=True`、标准 `OTEL_BSP_*` 参数与受控 `force_flush`/`shutdown` 生命周期均有真实 SDK 验收。
- [x] 7.7 完成 baggage-only ingress 与 parent x baggage 真实 OTel 矩阵：允许 baggage-only carrier；root/fallback 只保留显式 carrier baggage，绝不继承 ambient span 或 ambient baggage；关闭 baggage propagation 时剥离 baggage。最终无过滤六文件验收为 `175 passed, 1 warning`，并完成 Phoenix 测试 lifecycle 隔离与 canonical remediation review 记账。
- [x] 7.8 删除 root span 上重复且非法的 Python dict metadata attribute，仅保留 `using_attributes(metadata=...)` 与符合 OTel attribute 类型的直接属性；以真实 SDK 证明没有 attribute type warning。
- [x] 7.9 修复 embedded generator context scope：root span 生命周期可覆盖迭代，但 context attach 仅包围底层 iterator 的每次推进；覆盖逐次 `next()`、提前 `close()`、异常和两个 generator 交错。
- [x] 7.10 补齐 cross-path 真实集成测试，并仅在 `backend/tests/test_phoenix_root_runtime.py` 修复 `test_phoenix_initializer_is_idempotent_for_same_config`、`test_phoenix_initializer_rejects_changed_active_config`、`test_phoenix_initialization_error_is_provider_specific` 的 fake-module fixture 污染；同序 RED/GREEN 必须在真实 runtime producer 后正常运行三个 node ID，并恢复 `sys.modules`、OpenInference hide env、Phoenix initializer bookkeeping 与 LangChain instrumentor state；同时验收已有 global provider、已有 foreign instrumentor、gateway/embedded/subagent isolated-loop、batch flush/shutdown，禁止以 deselect 计为通过。
- [x] 7.11 修正文档、canonical review、`.superpowers/sdd` progress/handoff 中过期或高估的证据措辞，并记录 middleware diagnostics 已迁移至 `add-phoenix-middleware-diagnostics`。
- [x] 7.12 执行完整 Phoenix backend pytest acceptance、全 backend Ruff、strict OpenSpec、diff verification 和新的 independent whole-branch review；review artifact 存在前不得勾选，只有 report evidence gate 通过、0 Critical、0 Important 且无未裁定测试失败时才能标记完成。

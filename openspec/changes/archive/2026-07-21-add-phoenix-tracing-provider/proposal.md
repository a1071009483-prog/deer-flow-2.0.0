## 背景与动机

DeerFlow 目前通过 LangSmith 和 Langfuse callback handler 支持外部 tracing，同时由内部 RunJournal/EventStore 负责运行历史、事件流、token 用量和 gateway/UI 可观测性。对于已经标准化使用 Arize Phoenix 的团队，需要一种外部 tracing 接入方式，能够与现有内部运行审计链路并行，而不是替换 DeerFlow 的运行时事件存储。

Phoenix 的主要接入方式是 OpenTelemetry/OpenInference，而不是单纯的 LangChain callback。因此本次变更需要谨慎引入，不能简单复制现有 callback-only provider 形态。它必须保持 graph invocation root 级别挂载 tracing 的现有原则，避免模型级重复 span，并验证主 agent、embedded client 和 subagent 执行路径中的 context/session 传播。

## 变更内容

- 将 Phoenix 添加为 LangSmith、Langfuse 之外的第三个外部 tracing provider。
- 保留 RunJournal/EventStore 作为内部 run event、message history、token aggregation 和 debug/audit API 的来源。
- 增加 Phoenix 专属配置、依赖和 provider 初始化逻辑，以支持 OpenTelemetry/OpenInference setup。
- 保持现有 tracing placement invariant：主 agent、embedded client、subagent graph run 都在 graph invocation root 级别应用 tracing；graph 内部创建模型时继续避免重复 tracing callbacks。
- 支持接收上游 OTel trace context，使 DeerFlow graph root span 可以配置为新 trace root，也可以作为上游 gateway/RBAC span 的 child。
- 增加 Phoenix parent mode 配置，支持 `root`、`child`、`auto` 三种链路归属模式，并覆盖缺失上游 context 时的降级或 fail-fast 行为。
- 在最终确定实现形态前，先做最小 spike，验证 Phoenix auto-instrumentation 与手工 callback/span wiring 是否会产生重复或割裂 trace。
- 单独验证 subagent，因为 subagent 有独立 graph run，并且可能运行在 isolated event loop/thread 中。
- 文档化导出到 Phoenix 的 prompt、tool input/output、metadata、error 等 payload 的隐私与控制策略。
- 在基础 provider change 中完成阻断合并的稳定化 wave：provider ownership、baggage/parent 隔离、OTel attribute 合规性、generator context scope、真实集成验收与新的独立 whole-branch review。
- 将完整 middleware wrapper 诊断迁移到独立的 `add-phoenix-middleware-diagnostics` change；它默认关闭，不属于本 change 的合并门槛。

## 能力范围

### 新增能力
- `phoenix-tracing-provider`：将 Phoenix 配置并运行成外部 tracing provider，与 DeerFlow 内部 RunJournal/EventStore 可观测性并行。

### 修改能力

无。本仓库当前没有 tracing 或 observability 相关的现有 OpenSpec capability。

## 影响范围

- 后端 tracing 配置与 provider factory：
  - `backend/packages/harness/deerflow/config/tracing_config.py`
  - `backend/packages/harness/deerflow/tracing/factory.py`
  - `backend/packages/harness/deerflow/tracing/metadata.py`
- 在 root 级别挂载 tracing 的 graph invocation 路径：
  - `backend/packages/harness/deerflow/agents/lead_agent/agent.py`
  - `backend/packages/harness/deerflow/client.py`
  - `backend/packages/harness/deerflow/runtime/runs/worker.py`
  - `backend/packages/harness/deerflow/subagents/executor.py`
- 上游 trace context 接入路径：
  - gateway HTTP/request 入口需要提取 `traceparent`、`tracestate`、`baggage`。
  - 后台 run worker 需要从 run config/context/payload 恢复上游 OTel context。
  - embedded client 需要可选传入 trace context，以支持嵌入式场景续接外部链路。
- 必须继续避免 graph 内重复 tracing 的模型创建逻辑：
  - `backend/packages/harness/deerflow/models/factory.py`
- 依赖与文档：
  - 在 backend harness package 中加入 Phoenix/OpenTelemetry/OpenInference Python 依赖。
  - 更新 README/backend README 的 tracing setup 文档。
- 测试：
  - 覆盖 provider 配置和幂等初始化的单元测试。
  - 覆盖主 agent、embedded client、subagent tracing 挂载与 context 传播的集成或聚焦回归测试。

# DeerFlow Backend

DeerFlow is a LangGraph-based AI super agent with sandbox execution, persistent memory, and extensible tool integration. The backend enables AI agents to execute code, browse the web, manage files, delegate tasks to subagents, and retain context across conversations - all in isolated, per-thread environments.

---

## Architecture

```
                        ┌──────────────────────────────────────┐
                        │          Nginx (Port 2026)           │
                        │      Unified reverse proxy           │
                        └───────┬──────────────────┬───────────┘
                                │
            /api/langgraph/*    │    /api/* (other)
            rewritten to /api/* │
                                ▼
               ┌────────────────────────────────────────┐
               │        Gateway API (8001)              │
               │        FastAPI REST + agent runtime    │
               │                                        │
               │ Models, MCP, Skills, Memory, Uploads,  │
               │ Artifacts, Threads, Runs, Streaming    │
               │                                        │
               │ ┌────────────────────────────────────┐ │
               │ │ Lead Agent                         │ │
               │ │ Middleware Chain, Tools, Subagents │ │
               │ └────────────────────────────────────┘ │
               └────────────────────────────────────────┘
```

**Request Routing** (via Nginx):
- `/api/langgraph/*` → Gateway LangGraph-compatible API - agent interactions, threads, streaming
- `/api/*` (other) → Gateway API - models, MCP, skills, memory, artifacts, uploads, thread-local cleanup
- `/` (non-API) → Frontend - Next.js web interface

---

## Core Components

### Lead Agent

The single LangGraph agent (`lead_agent`) is the runtime entry point, created via `make_lead_agent(config)`. It combines:

- **Dynamic model selection** with thinking and vision support
- **Middleware chain** for cross-cutting concerns (9 middlewares)
- **Tool system** with sandbox, MCP, community, and built-in tools
- **Subagent delegation** for parallel task execution
- **System prompt** with skills injection, memory context, and working directory guidance

### Middleware Chain

Middlewares execute in strict order, each handling a specific concern:

| # | Middleware | Purpose |
|---|-----------|---------|
| 1 | **ThreadDataMiddleware** | Creates per-thread isolated directories (workspace, uploads, outputs) |
| 2 | **UploadsMiddleware** | Injects newly uploaded files into conversation context |
| 3 | **SandboxMiddleware** | Acquires sandbox environment for code execution |
| 4 | **SummarizationMiddleware** | Reduces context when approaching token limits (optional) |
| 5 | **TodoListMiddleware** | Tracks multi-step tasks in plan mode (optional) |
| 6 | **TitleMiddleware** | Auto-generates conversation titles after first exchange |
| 7 | **MemoryMiddleware** | Queues conversations for async memory extraction |
| 8 | **ViewImageMiddleware** | Injects image data for vision-capable models (conditional) |
| 9 | **ClarificationMiddleware** | Intercepts clarification requests and interrupts execution (must be last) |

### Sandbox System

Per-thread isolated execution with virtual path translation:

- **Abstract interface**: `execute_command`, `read_file`, `write_file`, `list_dir`
- **Providers**: `LocalSandboxProvider` (filesystem) and `AioSandboxProvider` (Docker, in community/). Async runtime paths use async sandbox lifecycle hooks so startup, readiness polling, and release do not block the event loop. `AioSandboxProvider` validates active-cache and warm-pool containers during acquire/reuse, dropping definitively dead entries so a thread can provision a fresh sandbox after an unexpected container exit while keeping `get()` as an in-memory lookup. Backend health-check failures are treated as unknown, not dead, and a container that cannot be verified during discovery is simply not adopted (acquire falls through to create instead of failing).
- **Virtual paths**: `/mnt/user-data/{workspace,uploads,outputs}` → thread-specific physical directories
- **Skills path**: `/mnt/skills` → `deer-flow/skills/` directory
- **Skills loading**: Recursively discovers nested `SKILL.md` files under `skills/{public,custom}` and preserves nested container paths
- **File-write safety**: `str_replace` serializes read-modify-write per `(sandbox.id, path)` so isolated sandboxes keep concurrency even when virtual paths match
- **Tools**: `bash`, `ls`, `read_file`, `write_file`, `str_replace` (`write_file` overwrites by default and exposes `append` for end-of-file writes; `bash` is disabled by default when using `LocalSandboxProvider`; use `AioSandboxProvider` for isolated shell access)

### Subagent System

Async task delegation with concurrent execution:

- **Built-in agents**: `general-purpose` (full toolset) and `bash` (command specialist, exposed only when shell access is available)
- **Concurrency**: Max 3 subagents per turn, 15-minute timeout
- **Execution**: Background thread pools with status tracking and SSE events
- **Flow**: Agent calls `task()` tool → executor runs subagent in background → polls for completion → returns result

Delegation authorization is bound when each agent is built. Every agent gets a distinct `task` tool closure carrying an immutable parent policy; there is no process-global unrestricted task tool, and `RunnableConfig.metadata` is not an authorization source. A single fail-closed resolver validates the parent policy and child request, resolves the exact tool/skill set, and passes that immutable decision to `SubagentExecutor`. `None` means unrestricted while an empty collection means deny all. Unknown names and incomplete configured/MCP/ACP/skill discovery deny delegation before execution.

Agent caches use separate SHA-256 fingerprints for the normalized parent policy, the resolved delegation decision, and the actual tool/skill catalog. Tool schemas, configured groups, deferred-tool mode, AppConfig/MCP generations, skill content digests, and skill `allowed_tools` therefore invalidate retained tool sets without placing raw skill content, config secrets, paths, or object representations in cache keys.

These rules are invariant across gateway, embedded, direct, and Studio entry paths and across Phoenix content modes. Phoenix safe-mode metadata reconstruction can no longer remove or expand delegation policy. Keep the production `PHOENIX_TRACING=false` mitigation until this complete security PR is deployed and the tracing-on/off authorization smoke passes; the delegation fix must remain deployed if later tracing work is rolled back.

### Memory System

LLM-powered persistent context retention across conversations:

- **Automatic extraction**: Analyzes conversations for user context, facts, and preferences
- **Structured storage**: User context (work, personal, top-of-mind), history, and confidence-scored facts
- **Debounced updates**: Batches updates to minimize LLM calls (configurable wait time)
- **System prompt injection**: Top facts + context injected into agent prompts
- **Storage**: JSON file with mtime-based cache invalidation

### Tool Ecosystem

| Category | Tools |
|----------|-------|
| **Sandbox** | `bash`, `ls`, `read_file`, `write_file`, `str_replace` |
| **Built-in** | `present_files`, `ask_clarification`, `view_image`, `task` (subagent) |
| **Community** | Tavily (web search), Jina AI (web fetch), Firecrawl (scraping), DuckDuckGo (image search) |
| **MCP** | Any Model Context Protocol server (stdio, SSE, HTTP transports) |
| **Skills** | Domain-specific workflows injected via system prompt |

### Gateway API

FastAPI application providing REST endpoints for frontend integration:

| Route | Purpose |
|-------|---------|
| `GET /api/models` | List available LLM models |
| `GET/PUT /api/mcp/config` | Manage MCP server configurations |
| `POST /api/mcp/cache/reset` | Reset cached MCP tools so they reload on next use |
| `GET/PUT /api/skills` | List and manage skills |
| `POST /api/skills/install` | Install skill from `.skill` archive |
| `GET /api/memory` | Retrieve memory data |
| `POST /api/memory/reload` | Force memory reload |
| `GET /api/memory/config` | Memory configuration |
| `GET /api/memory/status` | Combined config + data |
| `POST /api/threads/{id}/uploads` | Upload files (auto-converts PDF/PPT/Excel/Word to Markdown, rejects directory paths, auto-renames duplicate filenames in one request) |
| `GET /api/threads/{id}/uploads/list` | List uploaded files |
| `DELETE /api/threads/{id}` | Delete DeerFlow-managed local thread data after LangGraph thread deletion; unexpected failures are logged server-side and return a generic 500 detail |
| `GET /api/threads/{id}/artifacts/{path}` | Serve generated artifacts |

### IM Channels

The IM bridge supports Feishu, Slack, and Telegram. Slack and Telegram still use the final `runs.wait()` response path, while Feishu now streams through `runs.stream(["messages-tuple", "values"])` and updates a single in-thread card in place.

For Feishu card updates, DeerFlow stores the running card's `message_id` per inbound message and patches that same card until the run finishes, preserving the existing `OK` / `DONE` reaction flow.

---

## Quick Start

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager
- API keys for your chosen LLM provider

### Installation

```bash
cd deer-flow

# Copy configuration files
cp config.example.yaml config.yaml

# Install backend dependencies
cd backend
make install
```

### Configuration

Edit `config.yaml` in the project root:

```yaml
models:
  - name: gpt-4o
    display_name: GPT-4o
    use: langchain_openai:ChatOpenAI
    model: gpt-4o
    api_key: $OPENAI_API_KEY
    supports_thinking: false
    supports_vision: true

  - name: gpt-5-responses
    display_name: GPT-5 (Responses API)
    use: langchain_openai:ChatOpenAI
    model: gpt-5
    api_key: $OPENAI_API_KEY
    use_responses_api: true
    output_version: responses/v1
    supports_vision: true
```

Set your API keys:

```bash
export OPENAI_API_KEY="your-api-key-here"
```

### Running

**Full Application** (from project root):

```bash
make dev  # Starts Gateway + Frontend + Nginx
```

Access at: http://localhost:2026

**Backend Only** (from backend directory):

```bash
# Gateway API + embedded agent runtime
make dev
```

Direct access: Gateway at http://localhost:8001

---

## Project Structure

```
backend/
├── src/
│   ├── agents/                  # Agent system
│   │   ├── lead_agent/         # Main agent (factory, prompts)
│   │   ├── middlewares/        # 9 middleware components
│   │   ├── memory/             # Memory extraction & storage
│   │   └── thread_state.py    # ThreadState schema
│   ├── gateway/                # FastAPI Gateway API
│   │   ├── app.py             # Application setup
│   │   └── routers/           # 6 route modules
│   ├── sandbox/                # Sandbox execution
│   │   ├── local/             # Local filesystem provider
│   │   ├── sandbox.py         # Abstract interface
│   │   ├── tools.py           # bash, ls, read/write/str_replace
│   │   └── middleware.py      # Sandbox lifecycle
│   ├── subagents/              # Subagent delegation
│   │   ├── builtins/          # general-purpose, bash agents
│   │   ├── executor.py        # Background execution engine
│   │   └── registry.py        # Agent registry
│   ├── tools/builtins/         # Built-in tools
│   ├── mcp/                    # MCP protocol integration
│   ├── models/                 # Model factory
│   ├── skills/                 # Skill discovery & loading
│   ├── config/                 # Configuration system
│   ├── community/              # Community tools & providers
│   ├── reflection/             # Dynamic module loading
│   └── utils/                  # Utilities
├── docs/                       # Documentation
├── tests/                      # Test suite
├── langgraph.json              # LangGraph graph registry for tooling/Studio compatibility
├── pyproject.toml              # Python dependencies
├── Makefile                    # Development commands
└── Dockerfile                  # Container build
```

`langgraph.json` is not the default service entrypoint.  The scripts and Docker
deployments run the Gateway embedded runtime; the file is kept for LangGraph
tooling, Studio, or direct LangGraph Server compatibility.

---

## Configuration

### Main Configuration (`config.yaml`)

Place in project root. Config values starting with `$` resolve as environment variables.

Key sections:
- `models` - LLM configurations with class paths, API keys, thinking/vision flags
- `tools` - Tool definitions with module paths and groups
- `tool_groups` - Logical tool groupings
- `sandbox` - Execution environment provider
- `skills` - Skills directory paths
- `title` - Auto-title generation settings
- `summarization` - Context summarization settings
- `subagents` - Subagent system (enabled/disabled)
- `memory` - Memory system settings (enabled, storage, debounce, facts limits)

Provider note:
- `models[*].use` references provider classes by module path (for example `langchain_openai:ChatOpenAI`).
- If a provider module is missing, DeerFlow now returns an actionable error with install guidance (for example `uv add langchain-google-genai`).

### Extensions Configuration (`extensions_config.json`)

MCP servers and skill states in a single file:

```json
{
  "mcpServers": {
    "github": {
      "enabled": true,
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {"GITHUB_TOKEN": "$GITHUB_TOKEN"}
    },
    "secure-http": {
      "enabled": true,
      "type": "http",
      "url": "https://api.example.com/mcp",
      "oauth": {
        "enabled": true,
        "token_url": "https://auth.example.com/oauth/token",
        "grant_type": "client_credentials",
        "client_id": "$MCP_OAUTH_CLIENT_ID",
        "client_secret": "$MCP_OAUTH_CLIENT_SECRET"
      }
    }
  },
  "skills": {
    "pdf-processing": {"enabled": true}
  }
}
```

### Environment Variables

- `DEER_FLOW_CONFIG_PATH` - Override config.yaml location
- `DEER_FLOW_EXTENSIONS_CONFIG_PATH` - Override extensions_config.json location
- Model API keys: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `DEEPSEEK_API_KEY`, etc.
- Tool API keys: `TAVILY_API_KEY`, `GITHUB_TOKEN`, etc.

### LangSmith Tracing

DeerFlow has built-in [LangSmith](https://smith.langchain.com) integration for observability. When enabled, all LLM calls, agent runs, tool executions, and middleware processing are traced and visible in the LangSmith dashboard.

**Setup:**

1. Sign up at [smith.langchain.com](https://smith.langchain.com) and create a project.
2. Add the following to your `.env` file in the project root:

```bash
LANGSMITH_TRACING=true
LANGSMITH_ENDPOINT=https://api.smith.langchain.com
LANGSMITH_API_KEY=lsv2_pt_xxxxxxxxxxxxxxxx
LANGSMITH_PROJECT=xxx
```

**Legacy variables:** The `LANGCHAIN_TRACING_V2`, `LANGCHAIN_API_KEY`, `LANGCHAIN_PROJECT`, and `LANGCHAIN_ENDPOINT` variables are also supported for backward compatibility. `LANGSMITH_*` variables take precedence when both are set.

### Langfuse Tracing

DeerFlow also supports [Langfuse](https://langfuse.com) observability for LangChain-compatible runs.

Add the following to your `.env` file:

```bash
LANGFUSE_TRACING=true
LANGFUSE_PUBLIC_KEY=pk-lf-xxxxxxxxxxxxxxxx
LANGFUSE_SECRET_KEY=sk-lf-xxxxxxxxxxxxxxxx
LANGFUSE_BASE_URL=https://cloud.langfuse.com
```

If you are using a self-hosted Langfuse deployment, set `LANGFUSE_BASE_URL` to your Langfuse host.

### Phoenix Tracing

Phoenix is external tracing only. RunJournal/EventStore remains the internal source of truth for run history and token usage. LangSmith/Langfuse callbacks remain supported. Phoenix uses OpenTelemetry/OpenInference initialization plus a DeerFlow graph-root span around each graph invocation, and it is not attached as a callback provider at model creation inside graph runs.

Add the following to your `.env` file:

```bash
PHOENIX_TRACING=true
PHOENIX_COLLECTOR_ENDPOINT=http://localhost:6006
PHOENIX_PROJECT_NAME=deer-flow
PHOENIX_AUTO_INSTRUMENT=true
PHOENIX_CAPTURE_CONTENT=false
PHOENIX_METADATA_ALLOWLIST=request_id,tenant_id
PHOENIX_TRACE_PARENT_MODE=auto
PHOENIX_TRACE_PARENT_REQUIRED=false
PHOENIX_PROPAGATE_BAGGAGE=false
```

For local Phoenix, `http://localhost:6006` is accepted and DeerFlow normalizes it to `/v1/traces`. For cloud or remote Phoenix, or any authenticated collector, set `PHOENIX_COLLECTOR_ENDPOINT` to that service's OTLP traces endpoint or accepted base URL and set `PHOENIX_API_KEY` when the collector requires auth.

Phoenix support in the backend comes from the harness dependencies already installed from this repo, including `arize-phoenix-otel` and `openinference-instrumentation-langchain`. Parent compatibility uses a qualified private API set, so the harness exactly pins `langchain==1.2.15`, `langchain-core==1.3.3`, `langsmith==0.8.18`, and `openinference-instrumentation-langchain==0.1.67`. Any upgrade to this set requires a new tracing qualification.

Phoenix runs on a DeerFlow-owned `TracerProvider`, separate from the host global OTel provider. Registration uses `set_global_tracer_provider=false`, `batch=true`, and `auto_instrument=false`; DeerFlow first saves the returned provider and then transactionally binds every discovered `openinference_instrumentor` entry point plus `deerflow.run` to it. Existing instrumentors are treated as foreign ownership and remain untouched. A failed transition rolls back all attempted instrumentors, compatibility state, and DeerFlow-installed content-hide environment before closing the new provider. Standard `OTEL_BSP_MAX_QUEUE_SIZE`, `OTEL_BSP_SCHEDULE_DELAY`, `OTEL_BSP_EXPORT_TIMEOUT`, and `OTEL_BSP_MAX_EXPORT_BATCH_SIZE` variables configure batching.

Gateway shutdown drains in-flight runs before Phoenix cleanup. It relinquishes the provider's OTel SDK `atexit` hook, then executes `force_flush` followed by `shutdown` on a daemon thread with the gateway's five-second wait deadline. This keeps exporter latency off the event loop and prevents a timed-out cleanup from blocking interpreter exit. Per-run graph and embedded stream scopes end spans and restore context but never shut down the process provider.

After Phoenix registration, DeerFlow modifies only the Phoenix-owned OpenInference LangChain tracer instance. If a LangSmith `traceable` middleware parent is absent from that tracer's registry, terminal LLM, tool, chain, and retriever spans resolve to the nearest registered business ancestor. In the normal trace this places LLM calls below `model` and concrete tools below `tools`, without ambient fallback to the manual DeerFlow root. This base mode intentionally does not reproduce the full `awrap_model_call` / `awrap_tool_call` middleware wrapper tree; the full wrapper tree is owned by the independent, default-off `add-phoenix-middleware-diagnostics` OpenSpec change. Restart all DeerFlow backend processes after deploying this tracing change. Existing Phoenix traces remain unchanged.

The DeerFlow-owned run boundary and the auto-instrumented graph invocation are both retained but no longer share a name. Phoenix displays the boundary as `deerflow.run` and its automatic child with the real graph run name, for example `lead_agent` or `subagent:general-purpose`. The boundary remains an OpenInference `agent` span and carries directly queryable `deerflow.span.role=run_boundary`, `deerflow.agent_name`, and `deerflow.root_run_name` attributes. This separates boundary and graph latency/count aggregation while preserving upstream parent modes and session/user attributes.

When `PHOENIX_CAPTURE_CONTENT=false`, DeerFlow rebuilds metadata for both the Phoenix root context and the OpenInference LangChain auto-instrumentor. `PHOENIX_METADATA_ALLOWLIST` defaults to empty and accepts comma-separated, exact top-level caller metadata keys, with whitespace removed and duplicate keys ignored in first-seen order. The example exports only `request_id` and `tenant_id`; all other caller metadata and every caller tag remain excluded. Provider-reserved allowlist entries, including `langfuse_*`, are ignored by the manual Phoenix root; DeerFlow-generated reserved keys remain available only to the auto-instrumentor path that needs them. DeerFlow's session/thread, user, assistant/subagent, model, environment, root run name, run id, and controlled tags have final precedence. Treat allowlisted values as untrusted unless a gateway or RBAC layer generates or validates them. When `PHOENIX_CAPTURE_CONTENT=true`, full invocation metadata and tags can be exported; only enable it for trusted workloads.

#### Using the metadata allowlist

Add exact root `RunnableConfig.metadata` keys as a comma-separated list. Empty entries are ignored, surrounding whitespace is trimmed, and duplicate keys keep their first occurrence:

```bash
PHOENIX_CAPTURE_CONTENT=false
PHOENIX_METADATA_ALLOWLIST=request_id,tenant_id,workspace_id,region
```

Restart all DeerFlow backend processes after changing the value. `TracingConfig` is cached in-process, so editing `.env` alone does not update already-running workers.

For gateway runs, put the values in the top-level `metadata` object of the run request:

```json
{
  "metadata": {
    "request_id": "req-20260714-001",
    "tenant_id": "tenant-acme",
    "workspace_id": "workspace-research",
    "region": "cn-north"
  }
}
```

Operational constraints:

- Matching is by exact top-level key. `context.request_id`, nested objects addressed as paths, and tags are not matched.
- DeerFlow does not automatically create `request_id`, `tenant_id`, or custom business metadata. Inject and validate them in a trusted gateway/RBAC layer.
- The allowlist controls which keys can be exported; it does not validate authenticity, redact values, or limit value length. Do not add prompt, messages, payload, input/output, token, authorization, cookie, or other content/credential fields.
- Caller values cannot override DeerFlow-authoritative session/thread, user, assistant/subagent, effective model, environment, root run name, run id, or controlled subagent tags.
- Other-provider reserved namespaces such as `langfuse_*` remain excluded from the manual Phoenix root even if listed.
- The allowlist is enforced only when `PHOENIX_CAPTURE_CONTENT=false`. With content capture enabled, full invocation metadata and tags may be exported.

Phoenix supports these parent modes for upstream OTel context:

| Mode | Behavior |
|---|---|
| `root` | Ignore any upstream parent and start a new DeerFlow trace root |
| `auto` | Use a valid upstream `traceparent` when present; otherwise start a new root |
| `child` | Require a valid upstream parent; missing or invalid parent fails fast or falls back based on `PHOENIX_TRACE_PARENT_REQUIRED` |

Accepted incoming context fields: `traceparent`, `tracestate`, `baggage`.

Parent presence and validity are separate. DeerFlow preserves a supplied carrier through gateway/embedded serialization, parses it from an explicit empty context with the W3C propagator, and accepts it only when the extracted `SpanContext.is_valid` is true; a valid unsampled parent remains valid. Missing and supplied-but-invalid parents are marked `missing_parent` and `invalid_parent`. `root` mode and every fallback attach a context without an active span before creating `deerflow.run`, so they cannot accidentally inherit an ambient gateway, worker, or caller span. With `PHOENIX_PROPAGATE_BAGGAGE=true`, only W3C baggage parsed from the supplied carrier is retained. Normal and exceptional exits detach the DeerFlow context and restore the caller's prior OTel context.

Gateway, RBAC, and custom gateway ingress should extract those fields, store them in run config/context/payload, and restore them before graph execution. The embedded Python client accepts the same upstream carrier through its `trace_context` parameter.

### Dual Provider Behavior

If both LangSmith and Langfuse are enabled, DeerFlow initializes and attaches both callbacks so the same run data is reported to both systems.

If a provider is explicitly enabled but required credentials are missing, or the provider callback cannot be initialized, DeerFlow raises an error when tracing is initialized during model creation instead of silently disabling tracing.

**Docker:** In `docker-compose.yaml`, tracing is disabled by default (`LANGSMITH_TRACING=false`). Set `LANGSMITH_TRACING=true` and/or `LANGFUSE_TRACING=true` in your `.env`, together with the required credentials, to enable tracing in containerized deployments.

---

## Development

### Commands

```bash
make install    # Install dependencies
make dev        # Run Gateway API + embedded agent runtime (port 8001)
make gateway    # Run Gateway API without reload (port 8001)
make lint       # Run linter (ruff)
make format     # Format code (ruff)
make detect-blocking-io  # Inventory blocking IO that may block the backend event loop
make migrate-rev MSG="..."  # Autogenerate a new alembic revision against the live ORM models
```

### Schema Migrations

DeerFlow's application tables (`runs`, `threads_meta`, `feedback`, `users`,
`run_events`, and the `channel_*` tables) are owned by alembic. The Gateway
runs `alembic upgrade head` automatically on startup via
`bootstrap_schema(engine, backend=...)`, so operators do not run `alembic`
manually in production. Bootstrap is concurrency-safe (Postgres advisory lock
across processes; per-engine `asyncio.Lock` inside one SQLite process) and
idempotent against pre-existing schemas (empty / legacy / versioned).

When you add or change an ORM model, ship the change as a new revision under
`packages/harness/deerflow/persistence/migrations/versions/`:

```bash
make migrate-rev MSG="add foo column to runs"
```

The target invokes `scripts/_autogen_revision.py`, which builds a fresh temp
SQLite at `head` and diffs the live models against it — so a clean checkout
does not need a pre-existing `./data/deerflow.db`. Review the generated file
and switch raw `op.add_column` / `op.drop_column` calls to the idempotent
helpers in `migrations/_helpers.py` before committing. There is no
`make migrate` / `make migrate-stamp` target on purpose — Gateway startup is
the only execution path, which keeps operational mistakes off the table. See
`backend/CLAUDE.md` (Schema Migrations) for the full design.

### Code Style

- **Linter/Formatter**: `ruff`
- **Line length**: 240 characters
- **Python**: 3.12+ with type hints
- **Quotes**: Double quotes
- **Indentation**: 4 spaces

### Testing

```bash
uv run pytest
```

`make detect-blocking-io` statically scans backend business code for blocking
IO that may run on the backend event loop and is not test-coverage-bound. It
prints a concise summary for human review and writes complete JSON findings to
`.deer-flow/blocking-io-findings.json` at the repository root (regardless of
whether the target is invoked from the repo root or from `backend/`). JSON
findings include both broad IO category and review-oriented fields such as
`priority`, `location`, `blocking_call`, `event_loop_exposure`, `reason`, and
`code`. `priority` is a deterministic review ordering from the operation type,
not proof of a bug. Bare-name same-file calls are resolved by function name,
so duplicate helper names in one file can conservatively over-report async
reachability.

---

## Technology Stack

- **LangGraph** (1.0.6+) - Agent framework and multi-agent orchestration
- **LangChain** (1.2.3+) - LLM abstractions and tool system
- **FastAPI** (0.115.0+) - Gateway REST API
- **langchain-mcp-adapters** - Model Context Protocol support
- **agent-sandbox** - Sandboxed code execution
- **markitdown** - Multi-format document conversion
- **tavily-python** / **firecrawl-py** - Web search and scraping

---

## Documentation

- [Configuration Guide](docs/CONFIGURATION.md)
- [Architecture Details](docs/ARCHITECTURE.md)
- [API Reference](docs/API.md)
- [File Upload](docs/FILE_UPLOAD.md)
- [Path Examples](docs/PATH_EXAMPLES.md)
- [Context Summarization](docs/summarization.md)
- [Plan Mode](docs/plan_mode_usage.md)
- [Setup Guide](docs/SETUP.md)

---

## License

See the [LICENSE](../LICENSE) file in the project root.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution guidelines.

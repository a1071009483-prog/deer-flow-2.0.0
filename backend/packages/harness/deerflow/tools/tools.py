import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from langchain.tools import BaseTool

from deerflow.config import get_app_config
from deerflow.config.app_config import AppConfig
from deerflow.reflection import resolve_variable
from deerflow.sandbox.security import is_host_bash_allowed
from deerflow.tools.builtins import ask_clarification_tool, present_file_tool, view_image_tool
from deerflow.tools.mcp_metadata import tag_mcp_tool
from deerflow.tools.sync import make_sync_tool_wrapper

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from deerflow.subagents.delegation import DelegationPolicy

BUILTIN_TOOLS = [
    present_file_tool,
    ask_clarification_tool,
]

ToolSource = Literal["configured", "builtin", "mcp", "acp"]


class ToolCatalogLoadError(RuntimeError):
    """Raised when strict tool discovery cannot produce a complete catalog."""


@dataclass(frozen=True, slots=True)
class ToolCatalogEntry:
    tool: BaseTool
    source: ToolSource
    configured_group: str | None


@dataclass(frozen=True, slots=True)
class ToolCatalogSnapshot:
    """One immutable load of the tools visible to an agent build."""

    entries: tuple[ToolCatalogEntry, ...]
    known_tool_names: frozenset[str]
    known_groups: frozenset[str]

    def project(self, groups: tuple[str, ...] | list[str] | None = None) -> list[BaseTool]:
        allowed_groups = set(groups) if groups is not None else None
        projected = [
            entry.tool
            for entry in self.entries
            if entry.source != "configured" or allowed_groups is None or entry.configured_group in allowed_groups
        ]
        return _deduplicate_tools(projected)


@dataclass(frozen=True, slots=True)
class ParentToolSet:
    """One resolved parent tool set and the hashes required by agent caches."""

    tools: tuple[BaseTool, ...]
    parent_policy_fingerprint: str
    tool_catalog_fingerprint: str


def _is_host_bash_tool(tool: object) -> bool:
    """Return True if the tool config represents a host-bash execution surface."""
    group = getattr(tool, "group", None)
    use = getattr(tool, "use", None)
    if group == "bash":
        return True
    if use == "deerflow.sandbox.tools:bash_tool":
        return True
    return False


def _ensure_sync_invocable_tool(tool: BaseTool) -> BaseTool:
    """Attach a sync wrapper to async-only tools used by sync agent callers."""
    if getattr(tool, "func", None) is None and getattr(tool, "coroutine", None) is not None:
        tool.func = make_sync_tool_wrapper(tool.coroutine, tool.name)
    return tool


def _deduplicate_tools(tools: list[BaseTool]) -> list[BaseTool]:
    seen_names: set[str] = set()
    unique_tools: list[BaseTool] = []
    for candidate in tools:
        tool = _ensure_sync_invocable_tool(candidate)
        if tool.name not in seen_names:
            unique_tools.append(tool)
            seen_names.add(tool.name)
        else:
            logger.warning(
                "Duplicate tool name %r detected and skipped — check your config.yaml and MCP server registrations (issue #1803).",
                tool.name,
            )
    return unique_tools


def load_available_tool_catalog(
    *,
    include_mcp: bool = True,
    model_name: str | None = None,
    app_config: AppConfig | None = None,
    strict: bool = False,
) -> ToolCatalogSnapshot:
    """Load configured and independently governed tools exactly once.

    ``strict=True`` is used by delegation authorization. Discovery failures
    raise instead of returning a partial catalog, so authorization fails
    closed. Tool-group projection is deliberately deferred to
    :meth:`ToolCatalogSnapshot.project`; groups apply only to configured tools,
    not to built-ins, MCP, or ACP integrations.
    """
    config = app_config or get_app_config()
    known_names = {tool.name for tool in config.tools}
    known_names.add("task")  # Reserved delegation surface, never delegated.
    known_groups = {group.name for group in config.tool_groups}
    known_groups.update(tool.group for tool in config.tools)
    entries: list[ToolCatalogEntry] = []

    host_bash_allowed = is_host_bash_allowed(config)
    for tool_config in config.tools:
        # Preserve the existing security boundary and avoid importing a host
        # execution surface that cannot be exposed in this process.
        if not host_bash_allowed and _is_host_bash_tool(tool_config):
            continue
        try:
            loaded = resolve_variable(tool_config.use, BaseTool)
        except Exception as exc:
            if strict:
                raise ToolCatalogLoadError(f"Failed to resolve configured tool {tool_config.name!r}") from exc
            # Configured-tool failures historically abort agent construction;
            # non-strict applies only to optional MCP/ACP discovery.
            raise

        known_names.add(loaded.name)
        if tool_config.name != loaded.name:
            logger.warning(
                "Tool name mismatch: config name %r does not match tool .name %r (use: %s). The tool's own .name will be used for binding.",
                tool_config.name,
                loaded.name,
                tool_config.use,
            )
        entries.append(
            ToolCatalogEntry(
                tool=_ensure_sync_invocable_tool(loaded),
                source="configured",
                configured_group=tool_config.group,
            )
        )

    builtin_tools = BUILTIN_TOOLS.copy()
    skill_evolution_config = getattr(config, "skill_evolution", None)
    if getattr(skill_evolution_config, "enabled", False):
        from deerflow.tools.skill_manage_tool import skill_manage_tool

        builtin_tools.append(skill_manage_tool)

    if model_name is None and config.models:
        model_name = config.models[0].name
    model_config = config.get_model_config(model_name) if model_name else None
    if model_config is not None and model_config.supports_vision:
        builtin_tools.append(view_image_tool)
        logger.info("Including view_image_tool for model %r (supports_vision=True)", model_name)

    for builtin in builtin_tools:
        known_names.add(builtin.name)
        entries.append(ToolCatalogEntry(tool=builtin, source="builtin", configured_group=None))

    mcp_tools: list[BaseTool] = []
    if include_mcp:
        try:
            from deerflow.config.extensions_config import ExtensionsConfig

            extensions_config = ExtensionsConfig.from_file()
            if extensions_config.get_enabled_mcp_servers():
                from deerflow.mcp.cache import get_cached_mcp_tools

                mcp_tools = list(get_cached_mcp_tools(strict=True) if strict else get_cached_mcp_tools())
                for mcp_tool in mcp_tools:
                    tag_mcp_tool(mcp_tool)
        except ImportError as exc:
            if strict:
                raise ToolCatalogLoadError("MCP tool support is unavailable") from exc
            logger.warning("MCP module not available. Install 'langchain-mcp-adapters' package to enable MCP tools.")
        except Exception as exc:
            if strict:
                raise ToolCatalogLoadError("Failed to load MCP tool catalog") from exc
            logger.error("Failed to get cached MCP tools", exc_info=True)

    for mcp_tool in mcp_tools:
        known_names.add(mcp_tool.name)
        entries.append(ToolCatalogEntry(tool=mcp_tool, source="mcp", configured_group=None))

    acp_tools: list[BaseTool] = []
    try:
        if app_config is None:
            from deerflow.config.acp_config import get_acp_agents

            acp_agents = get_acp_agents()
        else:
            acp_agents = getattr(config, "acp_agents", {}) or {}
        if acp_agents:
            from deerflow.tools.builtins.invoke_acp_agent_tool import build_invoke_acp_agent_tool

            acp_tools.append(build_invoke_acp_agent_tool(acp_agents))
    except Exception as exc:
        if strict:
            raise ToolCatalogLoadError("Failed to load ACP tool catalog") from exc
        logger.warning("Failed to load ACP tool", exc_info=True)

    for acp_tool in acp_tools:
        known_names.add(acp_tool.name)
        entries.append(ToolCatalogEntry(tool=acp_tool, source="acp", configured_group=None))

    logger.info(
        "Tool catalog loaded: configured=%d, builtins=%d, MCP=%d, ACP=%d",
        sum(entry.source == "configured" for entry in entries),
        len(builtin_tools),
        len(mcp_tools),
        len(acp_tools),
    )
    return ToolCatalogSnapshot(
        entries=tuple(entries),
        known_tool_names=frozenset(known_names),
        known_groups=frozenset(known_groups),
    )


def get_available_tools(
    groups: list[str] | tuple[str, ...] | None = None,
    include_mcp: bool = True,
    model_name: str | None = None,
    subagent_enabled: bool = False,
    *,
    app_config: AppConfig | None = None,
    delegation_policy: "DelegationPolicy | None" = None,
) -> list[BaseTool]:
    """Project available tools and optionally append a policy-bound task tool.

    Args:
        groups: Optional list of tool groups to filter by.
        include_mcp: Whether to include tools from MCP servers (default: True).
        model_name: Optional model name to determine if vision tools should be included.
        subagent_enabled: Whether to include the task delegation tool.
        delegation_policy: Trusted parent policy required when delegation is
            enabled. There is intentionally no unrestricted default.

    Returns:
        List of available tools.
    """
    catalog = load_available_tool_catalog(
        include_mcp=include_mcp,
        model_name=model_name,
        app_config=app_config,
        strict=False,
    )
    tools = catalog.project(groups)
    if subagent_enabled:
        if delegation_policy is None:
            from deerflow.subagents.delegation import DelegationPolicyError

            raise DelegationPolicyError("delegation_policy is required when subagent_enabled=True")
        from deerflow.tools.builtins.task_tool import build_task_tool

        tools.append(build_task_tool(delegation_policy))
        logger.info("Including subagent tools (task)")
    return _deduplicate_tools(tools)


def load_parent_tool_set(
    *,
    model_name: str | None,
    subagent_enabled: bool,
    app_config: AppConfig,
    delegation_policy: "DelegationPolicy",
) -> ParentToolSet:
    """Load and fingerprint the exact tool catalog retained by a parent agent."""
    from deerflow.agents.lead_agent.prompt import get_enabled_skills_for_config
    from deerflow.config.app_config import get_app_config_generation
    from deerflow.mcp.cache import get_mcp_catalog_generation
    from deerflow.tools.catalog_fingerprint import (
        fingerprint_parent_policy,
        fingerprint_tool_catalog,
    )

    catalog = load_available_tool_catalog(
        include_mcp=True,
        model_name=model_name,
        app_config=app_config,
        strict=False,
    )
    tools = catalog.project(delegation_policy.tool_groups)
    if subagent_enabled:
        from deerflow.tools.builtins.task_tool import build_task_tool

        tools.append(build_task_tool(delegation_policy))
    tools = _deduplicate_tools(tools)
    skills = tuple(get_enabled_skills_for_config(app_config))
    return ParentToolSet(
        tools=tuple(tools),
        parent_policy_fingerprint=fingerprint_parent_policy(delegation_policy),
        tool_catalog_fingerprint=fingerprint_tool_catalog(
            catalog=catalog,
            skills=skills,
            app_config_generation=get_app_config_generation(),
            mcp_catalog_generation=get_mcp_catalog_generation(),
            deferred_enabled=app_config.tool_search.enabled,
        ),
    )

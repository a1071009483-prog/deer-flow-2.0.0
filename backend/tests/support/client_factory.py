"""Formal construction helper for ``DeerFlowClient`` test doubles.

Some tests need a client without loading a real ``config.yaml``, so they
bypass ``__init__`` via ``object.__new__(DeerFlowClient)``. That bypass is
only safe when every private attribute the streaming / agent-ensure paths
touch is initialized explicitly. This helper is the single place tracking
that contract: when ``client.py`` grows a new lazily-used attribute, add it
here instead of hand-patching individual tests, so no test can produce a
"constructed" client whose ``_app_config`` (or any other field) is undefined.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from deerflow.client import DeerFlowClient


def make_client_stub(**overrides: Any) -> DeerFlowClient:
    """Build a ``DeerFlowClient`` with all private fields initialized.

    Keyword overrides replace individual private attributes (pass the
    attribute name without the leading underscore, e.g.
    ``make_client_stub(agent_name="embedded")`` sets ``client._agent_name``).
    """
    defaults: dict[str, Any] = {
        "agent": None,
        "agent_config_key": None,
        "agent_name": None,
        "available_skills": None,
        "middlewares": [],
        "checkpointer": None,
        "app_config": SimpleNamespace(tool_search=SimpleNamespace(enabled=False)),
        "model_name": None,
        "thinking_enabled": True,
        "plan_mode": False,
        "subagent_enabled": False,
        "environment": None,
    }
    unknown = set(overrides) - set(defaults)
    if unknown:
        raise TypeError(f"Unknown DeerFlowClient stub attribute(s): {', '.join(sorted(unknown))}")
    defaults.update(overrides)

    client = object.__new__(DeerFlowClient)
    for name, value in defaults.items():
        setattr(client, f"_{name}", value)
    return client

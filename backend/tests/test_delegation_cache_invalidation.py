"""Caches retaining policy-bound tools must include policy/catalog revisions."""

from unittest.mock import MagicMock, patch

import pytest
from support.client_factory import make_client_stub

from deerflow.client import DeerFlowClient
from deerflow.tools.tools import ParentToolSet


def _client() -> DeerFlowClient:
    return make_client_stub()


def _tool_set(*, policy="sha256:policy", catalog="sha256:catalog") -> ParentToolSet:
    return ParentToolSet(
        tools=(),
        parent_policy_fingerprint=policy,
        tool_catalog_fingerprint=catalog,
    )


@pytest.mark.parametrize(
    "catalog_change",
    [
        "configured-schema",
        "configured-group",
        "mcp-generation",
        "mcp-schema",
        "acp-catalog",
        "deferred-mode",
        "skill-content",
        "skill-allowed-tools",
    ],
)
def test_embedded_agent_cache_rebuilds_once_for_catalog_change(monkeypatch, catalog_change):
    client = _client()
    before = _tool_set()
    after = _tool_set(catalog=f"sha256:{catalog_change}")
    monkeypatch.setattr(client, "_get_tools", MagicMock(side_effect=[before, before, after, after]))
    created = []

    def fake_create_agent(**_kwargs):
        agent = MagicMock()
        created.append(agent)
        return agent

    config = client._get_runnable_config("thread-1")
    with (
        patch("deerflow.client.create_agent", side_effect=fake_create_agent),
        patch("deerflow.client.create_chat_model"),
        patch("deerflow.client.build_middlewares", return_value=[]),
        patch("deerflow.client.apply_prompt_template", return_value="prompt"),
        patch("deerflow.runtime.checkpointer.get_checkpointer", return_value=None),
    ):
        for _ in range(4):
            client._ensure_agent(config)

    assert len(created) == 2
    assert client._agent is created[-1]


def test_embedded_agent_cache_uses_normalized_parent_policy_hash(monkeypatch):
    client = _client()
    equivalent_first = _tool_set(policy="sha256:canonical")
    equivalent_second = _tool_set(policy="sha256:canonical")
    monkeypatch.setattr(client, "_get_tools", MagicMock(side_effect=[equivalent_first, equivalent_second]))
    create_agent = MagicMock(side_effect=[MagicMock(), MagicMock()])
    config = client._get_runnable_config("thread-1")

    with (
        patch("deerflow.client.create_agent", create_agent),
        patch("deerflow.client.create_chat_model"),
        patch("deerflow.client.build_middlewares", return_value=[]),
        patch("deerflow.client.apply_prompt_template", return_value="prompt"),
        patch("deerflow.runtime.checkpointer.get_checkpointer", return_value=None),
    ):
        client._ensure_agent(config)
        client._ensure_agent(config)

    assert create_agent.call_count == 1

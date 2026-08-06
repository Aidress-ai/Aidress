"""Convenience toolkit bundling the Aidress tools."""

from __future__ import annotations

import os
from typing import Any

from langchain_core.tools import BaseTool, BaseToolkit
from pydantic import Field

from langchain_aidress.tools import (
    DEFAULT_BASE_URL,
    AidressCallAgentTool,
    AidressClaimBearerKeyTool,
    AidressGetAgentTool,
    AidressImportAgentTool,
    AidressListRegistryTool,
    AidressMatchAgentsTool,
    AidressRegisterAgentTool,
    AidressReviewTransactionTool,
    AidressRotateAgentKeyTool,
    AidressUpdateAgentTool,
    AidressVerifyAgentTool,
)


class AidressToolkit(BaseToolkit):
    """All Aidress tools, sharing one configuration.

    By default only the tools that work without credentials are returned, so the
    toolkit is useful straight out of the box::

        from langchain_aidress import AidressToolkit

        tools = AidressToolkit().get_tools()

    Pass an ``agent_key`` to unlock the tools that act as a registered agent —
    calling, reviewing, and updating::

        tools = AidressToolkit(agent_key="aidress-agent-sk-...").get_tools()

    An agent key comes from redeeming the ``claim_link`` that registration or
    rotation returns; both of those steps are in the keyless set, so an agent can
    bootstrap its own credentials with this toolkit alone. Set ``include_all=True``
    to return every tool regardless of which credentials are configured.
    """

    base_url: str = Field(
        default_factory=lambda: os.environ.get("AIDRESS_BASE_URL", DEFAULT_BASE_URL)
    )
    agent_key: str | None = Field(
        default_factory=lambda: os.environ.get("AIDRESS_AGENT_KEY")
    )
    timeout: float = 30.0
    retry_budget: float = 10.0
    include_all: bool = False

    def _config(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "agent_key": self.agent_key,
            "timeout": self.timeout,
            "retry_budget": self.retry_budget,
        }

    def get_tools(self) -> list[BaseTool]:
        """Return the tools available for the configured credentials."""
        config = self._config()

        # Discovery, verification, registration, and the key lifecycle need no
        # credentials — rotation is authorised by the contact_email already on the
        # agent's record, and claiming is authorised by possession of the token.
        tools: list[BaseTool] = [
            AidressVerifyAgentTool(**config),
            AidressMatchAgentsTool(**config),
            AidressGetAgentTool(**config),
            AidressListRegistryTool(**config),
            AidressImportAgentTool(**config),
            AidressRegisterAgentTool(**config),
            AidressRotateAgentKeyTool(**config),
            AidressClaimBearerKeyTool(**config),
        ]

        # Acting as a registered agent requires that agent's key.
        if self.include_all or self.agent_key:
            tools.append(AidressCallAgentTool(**config))
            tools.append(AidressReviewTransactionTool(**config))
            tools.append(AidressUpdateAgentTool(**config))

        return tools

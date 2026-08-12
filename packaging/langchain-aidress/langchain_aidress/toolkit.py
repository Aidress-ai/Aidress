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
    AidressGenerateKeypairTool,
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

    An agent key can be bootstrapped with this toolkit alone, by either of two
    routes — both entirely inside the keyless set:

    * **Signature** — ``aidress_generate_keypair`` → ``aidress_register_agent``
      with the returned ``public_key`` → ``aidress_rotate_agent_key``, which
      returns the key inline. No email, no human. This is the route for an agent
      with nobody to read an inbox.
    * **Claim link** — ``aidress_register_agent`` with a ``contact_email`` →
      ``aidress_claim_bearer_key`` with the link that comes back.

    Set ``include_all=True`` to return every tool regardless of which credentials
    are configured.
    """

    base_url: str = Field(
        default_factory=lambda: os.environ.get("AIDRESS_BASE_URL", DEFAULT_BASE_URL)
    )
    agent_key: str | None = Field(
        default_factory=lambda: os.environ.get("AIDRESS_AGENT_KEY")
    )
    keypair_path: str | None = Field(
        default_factory=lambda: os.environ.get("AIDRESS_KEYPAIR_PATH")
    )
    timeout: float = 30.0
    retry_budget: float = 10.0
    include_all: bool = False

    def _config(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "agent_key": self.agent_key,
            "keypair_path": self.keypair_path,
            "timeout": self.timeout,
            "retry_budget": self.retry_budget,
        }

    def get_tools(self) -> list[BaseTool]:
        """Return the tools available for the configured credentials."""
        config = self._config()

        # Discovery, verification, registration, and the key lifecycle need no
        # bearer key. Keypair generation is purely local; rotation is authorised by
        # an Ed25519 signature or by the contact_email already on the agent's record,
        # and claiming is authorised by possession of the token. So this whole set is
        # reachable by an agent that has no credentials yet.
        tools: list[BaseTool] = [
            AidressVerifyAgentTool(**config),
            AidressMatchAgentsTool(**config),
            AidressGetAgentTool(**config),
            AidressListRegistryTool(**config),
            AidressImportAgentTool(**config),
            AidressRegisterAgentTool(**config),
            AidressGenerateKeypairTool(**config),
            AidressRotateAgentKeyTool(**config),
            AidressClaimBearerKeyTool(**config),
        ]

        # Acting as a registered agent requires that agent's key.
        if self.include_all or self.agent_key:
            tools.append(AidressCallAgentTool(**config))
            tools.append(AidressReviewTransactionTool(**config))
            tools.append(AidressUpdateAgentTool(**config))

        return tools

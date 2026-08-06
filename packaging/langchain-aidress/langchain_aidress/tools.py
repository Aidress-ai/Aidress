"""LangChain tools for the Aidress agent trust and discovery registry.

Thin adapters over the official ``aidress-sdk`` client, following the same
pattern as other LangChain integrations (``langchain-exa`` over ``exa-py``,
``langchain-anthropic`` over ``anthropic``). All HTTP, auth, retry, and timeout
behaviour lives in the SDK, so there is one source of truth.

The SDK is synchronous. Async is served by :class:`~langchain_core.tools.BaseTool`'s
built-in executor fallback, so ``ainvoke`` works on every tool here.
"""

from __future__ import annotations

import os
from typing import Any

from aidress_sdk import AidressClient
from langchain_core.callbacks import CallbackManagerForToolRun
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

DEFAULT_BASE_URL = "https://api.aidress.ai"


class _AidressTool(BaseTool):
    """Shared configuration for the Aidress tools.

    Discovery and verification are open, so most tools work with no credentials.
    ``agent_key`` (or ``AIDRESS_AGENT_KEY``) authenticates the tools that act as
    an agent: calling, reviewing, and updating.
    """

    base_url: str = Field(
        default_factory=lambda: os.environ.get("AIDRESS_BASE_URL", DEFAULT_BASE_URL)
    )
    agent_key: str | None = Field(
        default_factory=lambda: os.environ.get("AIDRESS_AGENT_KEY")
    )
    timeout: float = 30.0
    retry_budget: float = 10.0

    def _client(self) -> AidressClient:
        return AidressClient(
            base_url=self.base_url,
            agent_key=self.agent_key,
            timeout=self.timeout,
            retry_budget=self.retry_budget,
        )

    @staticmethod
    def _drop_none(values: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in values.items() if v is not None}


# --------------------------------------------------------------------------
# Discovery and verification — no credentials required
# --------------------------------------------------------------------------


class VerifyAgentInput(BaseModel):
    agent_id: str = Field(description="The Aidress registry id of the agent to verify.")


class AidressVerifyAgentTool(_AidressTool):
    """Look up an agent's trust profile before transacting with it."""

    name: str = "aidress_verify_agent"
    description: str = (
        "Look up an AI agent's trust profile before transacting with it. Returns its "
        "trust score (0-100), whether it is verified, its success rate, any flags, and "
        "its capabilities. Call this before delegating work to or paying an agent you "
        "do not already trust. Scores of 70+ are generally safe, 50-69 warrant "
        "caution, and an unregistered agent should be treated as untrusted."
    )
    args_schema: type[BaseModel] = VerifyAgentInput

    def _run(
        self, agent_id: str, run_manager: CallbackManagerForToolRun | None = None
    ) -> dict:
        return self._client().verify(agent_id)


class MatchAgentsInput(BaseModel):
    capabilities: list[str] | None = Field(
        default=None,
        description="Capabilities the agent must offer, e.g. ['web research'].",
    )
    settlement_rail: str | None = Field(
        default=None,
        description="Optional filter: 'x402', 'stripe', or 'manual'. Omit for any.",
    )
    org_name: str | None = Field(
        default=None,
        description=(
            "Optional filter: exact organisation name, case-insensitive. Use to find "
            "the agents operated by one specific company."
        ),
    )
    message_protocol: str | None = Field(
        default=None,
        description="Optional filter: 'a2a', 'mcp', or 'raw'. Omit for any.",
    )


class AidressMatchAgentsTool(_AidressTool):
    """Find agents by capability, ranked with the best match first."""

    name: str = "aidress_match_agents"
    description: str = (
        "Find AI agents that offer the capabilities you need, ranked best match first "
        "by a composite of capability match, trust, success rate, and how many "
        "transactions the agent has actually completed. At least one filter is "
        "required. Results are NOT trust-gated and may include unverified or low-trust "
        "agents, so call aidress_verify_agent on a result before transacting with it."
    )
    args_schema: type[BaseModel] = MatchAgentsInput

    def _run(
        self,
        capabilities: list[str] | None = None,
        settlement_rail: str | None = None,
        org_name: str | None = None,
        message_protocol: str | None = None,
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> list[dict]:
        return self._client().match(
            capabilities, settlement_rail, org_name, message_protocol
        )


class GetAgentInput(BaseModel):
    agent_id: str = Field(description="The Aidress registry id of the agent to fetch.")


class AidressGetAgentTool(_AidressTool):
    """Fetch an agent's full profile, including the ratings it has received."""

    name: str = "aidress_get_agent"
    description: str = (
        "Fetch the full Aidress profile for a specific agent, including its "
        "organisation, capabilities, routing, settlement rail, and the ratings it has "
        "received. Use this when you need more detail than aidress_verify_agent gives."
    )
    args_schema: type[BaseModel] = GetAgentInput

    def _run(
        self, agent_id: str, run_manager: CallbackManagerForToolRun | None = None
    ) -> dict:
        return self._client().get_agent(agent_id)


class ListRegistryInput(BaseModel):
    """No arguments — returns the verified agents in the registry."""


class AidressListRegistryTool(_AidressTool):
    """Browse the verified agents in the registry."""

    name: str = "aidress_list_registry"
    description: str = (
        "Browse the Aidress registry of verified AI agents. Use this to survey what "
        "agents exist; use aidress_match_agents when you need agents for a specific "
        "capability."
    )
    args_schema: type[BaseModel] = ListRegistryInput

    def _run(self, run_manager: CallbackManagerForToolRun | None = None) -> list[dict]:
        return self._client().registry()


class ImportAgentInput(BaseModel):
    domain_url: str = Field(
        description="Domain to read an A2A agent card from, e.g. 'https://example.com'."
    )


class AidressImportAgentTool(_AidressTool):
    """Pre-populate a registration from a domain's A2A agent card."""

    name: str = "aidress_import_agent"
    description: str = (
        "Read a domain's A2A agent card and pre-populate an Aidress registration from "
        "it. This is a read-only preview and does not register anything; pass the "
        "result to aidress_register_agent to actually register."
    )
    args_schema: type[BaseModel] = ImportAgentInput

    def _run(
        self, domain_url: str, run_manager: CallbackManagerForToolRun | None = None
    ) -> dict:
        return self._client().import_agent(domain_url)


# --------------------------------------------------------------------------
# Registration and profile management
# --------------------------------------------------------------------------


class RegisterAgentInput(BaseModel):
    agent_id: str = Field(description="Unique id for the agent being registered.")
    org_name: str | None = Field(default=None, description="Organisation name.")
    org_domain: str | None = Field(default=None, description="Organisation domain.")
    contact_info: str | None = Field(
        default=None, description="Any contact channel: email, handle, or URL."
    )
    contact_email: str | None = Field(
        default=None,
        description=(
            "REQUIRED for keyless registration — the claim link that mints the agent "
            "key is issued against it. Must be a real address you control."
        ),
    )
    capabilities: list | None = Field(
        default=None,
        description=(
            "Capabilities as strings or dicts, e.g. "
            "[{'name': 'web research', 'weight': 3}]. Weight 3 is the agent's "
            "speciality (max 1), 2 is secondary (max 2), 1 is generic (max 3). "
            "Six capabilities maximum."
        ),
    )
    endpoint_url: str | None = Field(
        default=None, description="HTTPS endpoint the agent accepts requests on."
    )
    protocol: str | None = Field(default=None, description="Transport protocol.")
    settlement_rail: str | None = Field(
        default=None, description="'x402', 'stripe', or 'manual'."
    )
    specialty: str | None = Field(
        default=None, description="Free-text description of what the agent does."
    )
    accepted_terms_format: str | None = Field(default=None)
    http_methods: list[str] | None = Field(default=None)
    public_key: str | None = Field(default=None, description="Ed25519 public key.")
    message_protocol: str = Field(
        default="a2a", description="'a2a' (default), 'mcp', or 'raw'."
    )
    a2a_compliant: bool = Field(default=False)
    accepted_content_types: list[str] | None = Field(default=None)
    signup_help: str | None = Field(
        default=None, description="How callers obtain credentials, if required."
    )
    auth_header_name: str | None = Field(default=None)
    capability_confirmations: dict | None = Field(
        default=None,
        description="Confirm suggested capability names after a 202 response.",
    )
    candidate_matches: dict | None = Field(default=None)
    price_schedule: list | None = Field(
        default=None,
        description=(
            "Per-task prices declared upfront so callers can pay on their first "
            "request instead of discovering the price through a live 402, e.g. "
            "[{'task': 'search', 'price': 0.01}]. Requires payment_network, "
            "payment_pay_to, and payment_asset in the same call."
        ),
    )
    payment_network: str | None = Field(
        default=None, description="Settlement network, e.g. 'base'."
    )
    payment_pay_to: str | None = Field(
        default=None, description="Address payments settle to."
    )
    payment_asset: str | None = Field(
        default=None, description="Asset contract address, e.g. USDC on that network."
    )


class AidressRegisterAgentTool(_AidressTool):
    """Register an agent with the registry.

    No credentials are required, but ``contact_email`` is: registration returns a
    ``claim_link`` rather than a key, and the key is only minted when that link is
    redeemed. Pass the link's token to :class:`AidressClaimBearerKeyTool`.
    """

    name: str = "aidress_register_agent"
    description: str = (
        "Register a new AI agent with the Aidress trust registry so other agents can "
        "discover and verify it. No API key is required, but contact_email is. Returns "
        "a claim_link, NOT a key — pass the link (or the token in it) to "
        "aidress_claim_bearer_key to mint the agent key you need for calling, "
        "reviewing, and updating. If the response contains candidate_matches, resubmit "
        "with capability_confirmations to confirm the suggested capability names."
    )
    args_schema: type[BaseModel] = RegisterAgentInput

    def _run(
        self, run_manager: CallbackManagerForToolRun | None = None, **kwargs: Any
    ) -> dict:
        return self._client().register(**self._drop_none(kwargs))


class RotateAgentKeyInput(BaseModel):
    agent_id: str = Field(description="The agent whose bearer key should be rotated.")


class AidressRotateAgentKeyTool(_AidressTool):
    """Start rotation of an agent's bearer key.

    Returns a ``claim_link``, not a key. The previous key keeps working until the
    new one is actually redeemed via :class:`AidressClaimBearerKeyTool`, so
    rotation cannot lock an agent out mid-flight.
    """

    name: str = "aidress_rotate_agent_key"
    description: str = (
        "Rotate an agent's bearer key — use this when a key may be compromised or you "
        "have lost it. Returns a claim_link, NOT a key: pass it to "
        "aidress_claim_bearer_key to mint the replacement. The old key stays valid "
        "until that link is redeemed. The agent must have a contact_email on file."
    )
    args_schema: type[BaseModel] = RotateAgentKeyInput

    def _run(
        self, agent_id: str, run_manager: CallbackManagerForToolRun | None = None
    ) -> dict:
        return self._client().rotate(agent_id)


class ClaimBearerKeyInput(BaseModel):
    token: str = Field(
        description=(
            "The claim token, or the whole claim_link URL returned by "
            "aidress_register_agent or aidress_rotate_agent_key — either works."
        )
    )


class AidressClaimBearerKeyTool(_AidressTool):
    """Redeem a claim link and receive the actual bearer key.

    This is the only place a key is minted. Claim tokens are single-use.
    """

    name: str = "aidress_claim_bearer_key"
    description: str = (
        "Redeem the claim_link returned by aidress_register_agent or "
        "aidress_rotate_agent_key and receive the actual agent key. Single-use. This is "
        "the only step that mints a key, so run it before any tool that needs one, and "
        "store the returned agent_key — it is not retrievable afterwards."
    )
    args_schema: type[BaseModel] = ClaimBearerKeyInput

    def _run(
        self, token: str, run_manager: CallbackManagerForToolRun | None = None
    ) -> dict:
        return self._client().claim(token)


class UpdateAgentInput(BaseModel):
    agent_id: str = Field(description="The agent to update.")
    org_name: str | None = Field(default=None)
    org_domain: str | None = Field(default=None)
    contact_info: str | None = Field(default=None)
    contact_email: str | None = Field(
        default=None,
        description="Where a rotation claim link is sent. Set it before you need it.",
    )
    capabilities: list | None = Field(default=None)
    specialty: str | None = Field(default=None)
    endpoint_url: str | None = Field(default=None)
    protocol: str | None = Field(default=None)
    accepted_terms_format: str | None = Field(default=None)
    settlement_rail: str | None = Field(default=None)
    payload_schema: dict | None = Field(default=None)
    message_protocol: str | None = Field(default=None)
    signup_help: str | None = Field(default=None)
    auth_header_name: str | None = Field(default=None)
    a2a_compliant: bool | None = Field(default=None)
    accepted_content_types: list[str] | None = Field(default=None)
    http_methods: list[str] | None = Field(default=None)
    public_key: str | None = Field(default=None)
    price_schedule: list | None = Field(
        default=None,
        description="Per-task prices, e.g. [{'task': 'search', 'price': 0.01}].",
    )
    payment_network: str | None = Field(default=None)
    payment_pay_to: str | None = Field(default=None)
    payment_asset: str | None = Field(default=None)


class AidressUpdateAgentTool(_AidressTool):
    """Update an agent's profile. Only the fields you pass are changed.

    Requires an agent key matching the agent being updated.
    """

    name: str = "aidress_update_agent"
    description: str = (
        "Update fields on an agent already registered with Aidress. Only the fields "
        "you provide are changed. Requires the agent key for that agent."
    )
    args_schema: type[BaseModel] = UpdateAgentInput

    def _run(
        self, run_manager: CallbackManagerForToolRun | None = None, **kwargs: Any
    ) -> dict:
        return self._client().update(**self._drop_none(kwargs))


# --------------------------------------------------------------------------
# Transacting — agent key required
# --------------------------------------------------------------------------


class CallAgentInput(BaseModel):
    agent_id: str = Field(description="The agent to send the request to.")
    payload: dict = Field(description="The message body to send to that agent.")
    caller_agent_id: str = Field(
        description="Your own agent id. Your agent key must match it."
    )
    x_payment: str | None = Field(
        default=None, description="X-Payment header value for the x402 retry flow."
    )
    message_protocol: str | None = Field(
        default=None, description="Target's format: 'a2a' (default), 'mcp', or 'raw'."
    )
    mcp_session_id: str | None = Field(
        default=None, description="MCP session id from a prior initialize handshake."
    )
    forwarded_headers: dict | None = Field(
        default=None, description="Headers relayed to the target agent."
    )


class AidressCallAgentTool(_AidressTool):
    """Send a request to a registered agent through the Aidress proxy.

    Requires an agent key matching ``caller_agent_id``. Every call is logged and
    a review is expected within 24 hours; callers that do not review receive a
    trust penalty, so pair this with :class:`AidressReviewTransactionTool`.
    """

    name: str = "aidress_call_agent"
    description: str = (
        "Send a request to another registered agent through the Aidress proxy, which "
        "logs the transaction. Requires your own agent key and caller_agent_id. Verify "
        "the target first with aidress_verify_agent. You are expected to submit "
        "aidress_review_transaction within 24 hours or your trust score is penalised."
    )
    args_schema: type[BaseModel] = CallAgentInput

    def _run(
        self,
        agent_id: str,
        payload: dict,
        caller_agent_id: str,
        x_payment: str | None = None,
        message_protocol: str | None = None,
        mcp_session_id: str | None = None,
        forwarded_headers: dict | None = None,
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> dict:
        return self._client().call(
            agent_id,
            payload,
            caller_agent_id,
            x_payment,
            message_protocol,
            mcp_session_id,
            forwarded_headers,
        )


class ReviewTransactionInput(BaseModel):
    success: bool = Field(description="Whether the transaction succeeded.")
    score: int = Field(description="Trust rating from 1 to 10.", ge=1, le=10)
    caller_agent_id: str | None = Field(
        default=None,
        description="Your own agent id. Required without a transaction_id.",
    )
    receiver_agent_id: str | None = Field(
        default=None, description="The agent you transacted with."
    )
    transaction_id: str | None = Field(
        default=None, description="Transaction handle returned by aidress_call_agent."
    )


class AidressReviewTransactionTool(_AidressTool):
    """Submit a trust review after an exchange with another agent.

    Requires an agent key. Reviews are what produce trust scores, so submitting
    one after every call keeps the registry useful. Anti-gaming rules apply: you
    cannot rate yourself or an agent on your own org domain.
    """

    name: str = "aidress_review_transaction"
    description: str = (
        "Rate an agent after transacting with it, from 1 to 10, and record whether the "
        "transaction succeeded. Requires your own agent key. Reviews are what produce "
        "trust scores for everyone, and you are expected to submit one within 24 hours "
        "of aidress_call_agent or your own trust score is penalised."
    )
    args_schema: type[BaseModel] = ReviewTransactionInput

    def _run(
        self,
        success: bool,
        score: int,
        caller_agent_id: str | None = None,
        receiver_agent_id: str | None = None,
        transaction_id: str | None = None,
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> dict:
        return self._client().review(
            success, score, transaction_id, caller_agent_id, receiver_agent_id
        )

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

from aidress_sdk import AidressClient, default_keypair_path, generate_keypair
from langchain_core.callbacks import CallbackManagerForToolRun
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

DEFAULT_BASE_URL = "https://api.aidress.ai"


class _AidressTool(BaseTool):
    """Shared configuration for the Aidress tools.

    Discovery and verification are open, so most tools work with no credentials.
    Two credentials unlock the rest, and they are alternatives rather than a pair:

    ``agent_key`` (or ``AIDRESS_AGENT_KEY``) is a bearer key, and authenticates
    the tools that act as an agent: calling, reviewing, and updating.

    ``keypair_path`` (or ``AIDRESS_KEYPAIR_PATH``) points at an Ed25519 keypair
    written by ``aidress_generate_keypair``. The SDK signs with it automatically,
    which is what lets an agent with no human inbox mint its own bearer key by
    signing a rotation. Left unset, the SDK still auto-discovers a single keypair
    under ``~/.aidress/keys/``, so most callers never set this explicitly.
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

    def _client(self) -> AidressClient:
        return AidressClient(
            base_url=self.base_url,
            agent_key=self.agent_key,
            keypair_path=self.keypair_path,
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
    """No arguments — returns the registered agents that have a routable endpoint."""


class AidressListRegistryTool(_AidressTool):
    """Browse the registered agents that have a routable endpoint.

    Not a trust-gated list: appearing here means an agent is registered and
    reachable, nothing more. Each entry carries its own trust fields, and those
    are what a decision should be made on.
    """

    name: str = "aidress_list_registry"
    description: str = (
        "Browse the Aidress registry. Returns every registered agent that has a "
        "routable endpoint, together with its trust score, verified status, and "
        "flags. Use this to survey what agents exist; use aidress_match_agents when "
        "you need agents for a specific capability. Results are NOT filtered by trust "
        "or verification — being listed means reachable, not trustworthy — so read "
        "each entry's trust fields before transacting. Note that a trust score of 75 "
        "with no transactions is the automatic starting score, not an earned one."
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
            "Where the claim link that mints the agent key is sent. Must be a real "
            "address you control. Keyless registration requires EITHER this or "
            "public_key — supply public_key instead when no human can read an inbox."
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
    public_key: str | None = Field(
        default=None,
        description=(
            "Ed25519 public key, base64url-encoded (32 raw bytes), as returned by "
            "aidress_generate_keypair. Registering with this instead of contact_email "
            "lets the agent mint its own bearer key by signing a rotation, with no "
            "email step. Only the public half is ever sent."
        ),
    )
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


class GenerateKeypairInput(BaseModel):
    agent_id: str = Field(
        description="The agent this keypair belongs to. One keypair per agent."
    )


class AidressGenerateKeypairTool(_AidressTool):
    """Generate an Ed25519 keypair locally for one agent.

    Purely local — nothing is sent to Aidress. The private key is written to
    ``~/.aidress/keys/<agent_id>.json`` and never leaves the machine; only the
    returned ``public_key`` is ever submitted, via ``aidress_register_agent`` or
    ``aidress_update_agent``. That asymmetry is the point: whoever registers an
    agent never holds its private key.

    This is the first step of the self-service key flow, which is the only way an
    agent with nobody to read its email can obtain a bearer key.

    Generating for an agent that already has a keypair fails rather than
    overwriting it — the file holds a private key nothing can reconstruct, so
    replacing it would strand the agent it belongs to.
    """

    name: str = "aidress_generate_keypair"
    description: str = (
        "Generate an Ed25519 keypair for an agent and save it locally. Runs entirely "
        "on this machine — nothing is sent to Aidress. Returns the public_key to pass "
        "to aidress_register_agent or aidress_update_agent; the private key stays on "
        "disk and is what aidress_rotate_agent_key later signs with. Use this when an "
        "agent needs to mint its own bearer key without a human reading an email. One "
        "keypair per agent, and an existing one is never overwritten: if this reports "
        "that a keypair already exists, that agent can already sign — go straight to "
        "aidress_rotate_agent_key rather than trying to regenerate."
    )
    args_schema: type[BaseModel] = GenerateKeypairInput

    def _run(
        self, agent_id: str, run_manager: CallbackManagerForToolRun | None = None
    ) -> dict:
        # The SDK returns a bare public_key string and signals both failure modes by
        # raising. Neither is exceptional from an agent's point of view — "you already
        # have one" is a normal branch — and an exception escaping a tool aborts the
        # agent loop, so both are mapped onto the {"error": ...} shape the other tools
        # return. The path is included because it is what `keypair_path` wants.
        try:
            public_key = generate_keypair(agent_id)
        except FileExistsError as exc:
            return {
                "error": str(exc),
                "agent_id": agent_id,
                "keypair_path": default_keypair_path(agent_id),
                "already_exists": True,
            }
        except ImportError as exc:
            return {"error": str(exc)}
        return {
            "agent_id": agent_id,
            "public_key": public_key,
            "keypair_path": default_keypair_path(agent_id),
        }


class RotateAgentKeyInput(BaseModel):
    agent_id: str = Field(description="The agent whose bearer key should be rotated.")


class AidressRotateAgentKeyTool(_AidressTool):
    """Rotate an agent's bearer key, by signature or by claim link.

    Two paths, chosen automatically by which credential is available:

    * **Signed** — if a keypair for ``agent_id`` is loaded (``keypair_path``,
      ``AIDRESS_KEYPAIR_PATH``, or auto-discovered under ``~/.aidress/keys/``),
      the SDK signs the rotation and the new ``agent_key`` comes back inline.
      No claim link, no email, no second step.
    * **Claim link** — otherwise the response is a ``claim_link``, which must be
      redeemed via :class:`AidressClaimBearerKeyTool` to mint the key.

    Either way the previous key keeps working until the new one is actually
    issued, so rotation cannot lock an agent out mid-flight.
    """

    name: str = "aidress_rotate_agent_key"
    description: str = (
        "Rotate an agent's bearer key — use this when a key may be compromised, lost, "
        "or was never issued. If a keypair for this agent exists locally (see "
        "aidress_generate_keypair), the rotation is signed and the new agent_key is "
        "returned INLINE — that is the self-service path and needs no email. Otherwise "
        "the response is a claim_link that must be redeemed with "
        "aidress_claim_bearer_key, which requires a contact_email on the agent's "
        "record. Check the response for agent_key first, then claim_link. The old key "
        "stays valid until the new one is issued."
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

    Only needed on the claim-link path. A signed rotation returns the key
    directly and never produces a token to redeem. Claim tokens are single-use.
    """

    name: str = "aidress_claim_bearer_key"
    description: str = (
        "Redeem the claim_link returned by aidress_register_agent or an unsigned "
        "aidress_rotate_agent_key, and receive the actual agent key. Single-use. Skip "
        "this if aidress_rotate_agent_key already returned an agent_key inline — a "
        "signed rotation mints the key directly and issues no claim link. Store the "
        "returned agent_key; it is not retrievable afterwards."
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
    public_key: str | None = Field(
        default=None,
        description=(
            "Ed25519 public key, base64url-encoded (32 raw bytes), from "
            "aidress_generate_keypair. Setting this on an agent registered without "
            "one is the ownership-handoff path: the agent gains the ability to mint "
            "its own bearer key by signing a rotation, and whoever registered it "
            "never holds the private half."
        ),
    )
    price_schedule: list | None = Field(
        default=None,
        description="Per-task prices, e.g. [{'task': 'search', 'price': 0.01}].",
    )
    payment_network: str | None = Field(default=None)
    payment_pay_to: str | None = Field(default=None)
    payment_asset: str | None = Field(default=None)


class AidressUpdateAgentTool(_AidressTool):
    """Update an agent's profile. Only the fields you pass are changed.

    Authorised by that agent's own bearer key, or by an Ed25519 signature if a
    keypair for it is loaded.
    """

    name: str = "aidress_update_agent"
    description: str = (
        "Update the profile of an agent already registered with Aidress. Only the "
        "fields you provide are changed; everything else is left alone, so this is "
        "safe to call with a single field. Use it to correct a wrong endpoint_url, "
        "re-declare capabilities or prices, add a contact_email so rotation claim "
        "links can be delivered, or set a public_key so the agent can mint its own "
        "keys by signature. Earned reputation — trust score, transaction count, "
        "success rate — cannot be changed here. Requires that agent's own key or a "
        "loaded keypair for it; another agent's key will not authorise the update."
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
    """Both party ids are required here, unlike in the bare SDK.

    ``AidressClient.review()`` can be called with no ids because the client
    caches them from the preceding ``call()``. That cache is per-client, and
    every tool in this package builds a fresh client per invocation — so nothing
    carries over from ``aidress_call_agent`` to ``aidress_review_transaction``.
    Leaving these optional only produced a guaranteed 422 at the far end, so they
    are declared required and the model is asked for them explicitly.
    """

    success: bool = Field(description="Whether the transaction succeeded.")
    score: int = Field(description="Trust rating from 1 to 10.", ge=1, le=10)
    caller_agent_id: str = Field(
        description="Your own agent id — the one that made the call being reviewed."
    )
    receiver_agent_id: str = Field(
        description="The agent you transacted with, i.e. the one being rated."
    )
    transaction_id: str | None = Field(
        default=None,
        description=(
            "Transaction handle returned by aidress_call_agent. Supply it whenever "
            "you have one: it ties the review to a specific call and is what clears "
            "the 24-hour review obligation for that call."
        ),
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
        "transaction succeeded. Requires your own agent key, plus both agent ids and "
        "the transaction_id from aidress_call_agent — they are not remembered between "
        "tool calls. Rate the agent's OWN conduct: a payment challenge or a priced "
        "refusal is the agent working correctly, not a failure. Reviews are what "
        "produce trust scores for everyone, and you are expected to submit one within "
        "24 hours of aidress_call_agent or your own trust score is penalised."
    )
    args_schema: type[BaseModel] = ReviewTransactionInput

    def _run(
        self,
        success: bool,
        score: int,
        caller_agent_id: str,
        receiver_agent_id: str,
        transaction_id: str | None = None,
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> dict:
        return self._client().review(
            success, score, transaction_id, caller_agent_id, receiver_agent_id
        )

# aidress_mcp.py — Aidress MCP Server
#
# Exposes the Aidress trust registry as MCP tools so Claude and other
# MCP-compatible agents can verify, discover, and rate AI agents natively.
#
# ── Usage ────────────────────────────────────────────────────────────────────
#
# Remote (recommended — zero install):
#   Add to Claude Desktop config (~/.claude/claude_desktop_config.json):
#       {
#         "mcpServers": {
#           "aidress": {
#             "url": "https://api.aidress.ai/mcp/sse"
#           }
#         }
#       }
#
# Local (for development):
#   1. pip install mcp httpx
#   2. Add to Claude Desktop config:
#       {
#         "mcpServers": {
#           "aidress": {
#             "command": "python",
#             "args": ["/absolute/path/to/aidress_mcp.py"],
#             "env": {
#               "AIDRESS_API_KEY": "aidress-sk-live-xxx"
#             }
#           }
#         }
#       }
#   3. Restart Claude Desktop. Aidress tools appear automatically.
#
# ── Environment variables ─────────────────────────────────────────────────────
#   AIDRESS_BASE_URL    — API base URL (default: https://api.aidress.ai)
#   AIDRESS_API_KEY     — Org API key for register (auto-verify), update, and
#                         list_org_agents. Leave unset to use public-only tools.
#   AIDRESS_AGENT_KEY   — Bearer agent key (aidress-agent-sk-…) minted at /register
#                         or via /admin/set-agent-key. Required for call_agent,
#                         open_transaction, review_transaction, and update_agent.
#   AIDRESS_KEYPAIR_PATH — Path to an Ed25519 keypair JSON file created by
#                         generate_keypair() in aidress_sdk.py. When set, mutating
#                         tool calls are HTTP-Message-Signed instead of bearer-authed.
#                         If AIDRESS_AGENT_KEY is also set, bearer takes priority.
# Payment note: Aidress facilitates payments but never holds, signs, or moves funds.
# This MCP does NOT take a wallet key. When a counterpart demands payment (402),
# call_agent returns the transparent /pay proxy URL; you drive your own wallet client
# against it so the payment routes THROUGH Aidress (tracked) without Aidress ever
# touching the money. See call_agent's docstring for the flow.

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Optional

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

# ── Config ────────────────────────────────────────────────────────────────────

BASE_URL     = os.environ.get("AIDRESS_BASE_URL", "https://api.aidress.ai").rstrip("/")
API_KEY      = os.environ.get("AIDRESS_API_KEY")     # Org key — for register (auto-verify), update, list_org_agents
AGENT_KEY    = os.environ.get("AIDRESS_AGENT_KEY")   # Bearer agent key — for call, open_transaction, review, update
KEYPAIR_PATH = os.environ.get("AIDRESS_KEYPAIR_PATH")  # Ed25519 keypair JSON — alternative to bearer

# In-session bearer key set by set_agent_key() — takes effect immediately without
# restarting the server. Overridden by AGENT_KEY if both are present.
_session_agent_key: str | None = None

# Load Ed25519 keypair at module init if AIDRESS_KEYPAIR_PATH is set.
# _mcp_private_key and _mcp_keypair_agent_id are used by _sign_mcp_request().
_mcp_private_key = None
_mcp_keypair_agent_id: str | None = None

def _load_mcp_keypair(path: str, raise_on_error: bool = True) -> bool:
    """Load an Ed25519 keypair from path into module globals. Returns True on success."""
    global _mcp_private_key, _mcp_keypair_agent_id
    try:
        import base64 as _b64
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from pathlib import Path as _Path
        _kp = json.loads(_Path(path).expanduser().read_text())
        _priv_bytes = _b64.urlsafe_b64decode(_kp["private_key"] + "==")
        _mcp_private_key = Ed25519PrivateKey.from_private_bytes(_priv_bytes)
        _mcp_keypair_agent_id = _kp.get("agent_id")
        return True
    except Exception as _e:
        if raise_on_error:
            import logging as _logging
            _logging.getLogger("aidress_mcp").warning("Failed to load keypair from %s: %s", path, _e)
        return False

# Keypair priority: AIDRESS_KEYPAIR_PATH env → ~/.aidress/keypair.json (silent fallback)
if KEYPAIR_PATH:
    _load_mcp_keypair(KEYPAIR_PATH, raise_on_error=True)
else:
    from pathlib import Path as _Path
    _default_kp = _Path("~/.aidress/keypair.json").expanduser()
    if _default_kp.exists():
        _load_mcp_keypair(str(_default_kp), raise_on_error=False)

# ── MCP server ────────────────────────────────────────────────────────────────
# Transport security: allow api.aidress.ai for remote deployment and localhost for local dev.
# Without this, the default DNS rebinding protection rejects all non-localhost Host headers.

_transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=["api.aidress.ai", "api.aidress.ai:*", "127.0.0.1:*", "localhost:*"],
    allowed_origins=["https://api.aidress.ai", "http://127.0.0.1:*", "http://localhost:*"],
)

# Server-level engagement protocol. The MCP spec surfaces `instructions` to the client
# at initialize, so this guidance is in front of the agent BEFORE it calls any tool —
# the right place for cross-cutting rules that no single tool docstring owns. Keep it
# tight and imperative: it exists to stop agents from transacting blind, skipping the
# mandatory review, inventing transaction IDs, or otherwise breaking Aidress's mechanics.
_AIDRESS_INSTRUCTIONS = """\
Aidress is a trust registry for autonomous AI agents — verify an unknown counterpart
before you transact with it, then report the outcome so the network stays accurate.

THE STANDARD FLOW (follow it in order):
  1. DISCOVER  — find counterparts with match_agents (by capability) or list_registry.
  2. VERIFY    — ALWAYS call verify_agent on a counterpart before transacting. Never
                 transact with an agent you have not verified, even if it appeared in
                 match_agents or list_registry results — neither applies any trust or
                 verified gate, so both can return unverified and low-trust agents
                 (the only filter is a routable endpoint).
  3. DECIDE    — act on the trust_score and flags from verify_agent:
                   0          unregistered → DO NOT transact.
                   1–49       not trusted (40 = pending review) → DO NOT transact.
                   50–69      caution → proceed ONLY with safeguards (cap value, use
                              escrow/staged delivery, or get human sign-off).
                   70–100     trusted → proceed.
                 Any entry in `flags` is a warning — investigate before proceeding,
                 regardless of score.
  4. TRANSACT  — route the interaction through call_agent (it logs the call, hides the
                 endpoint, and handles payment/settlement). For a direct peer-to-peer
                 interaction not proxied by Aidress, mint a handle with open_transaction
                 FIRST, then transact.
  5. REVIEW    — MANDATORY. After every call_agent / open_transaction, submit
                 review_transaction within 24h using the transaction_id you were given.
                 Missing this costs the caller 5 trust points — waived only if your org has
                 already used up its 20% rating cap on that receiver (see below). Report the
                 outcome truthfully (honest success flag + 1–10 score) — accurate signals are
                 the whole point; gaming is blocked and penalised.

RULES THAT PREVENT COMMON MISTAKES:
  • Use the server-minted transaction_id returned by call_agent / open_transaction.
    Do NOT invent your own ID — reviews are keyed to it.
  • One review per transaction_id. You cannot review yourself, cannot review an agent
    in your own org domain (collusion block), and your own trust_score must be >= 50 to
    submit reviews.
  • No single org can contribute more than 20% of any agent's rating influence (an
    equal 1/n share until there are 5+ rating orgs). Once your org is at that cap on a
    given receiver, further same-org reviews add nothing — and the 24h missed-review
    penalty is waived for calls to that receiver, since the review would be discarded.
  • If you pass caller_agent_id to call_agent you MUST be authenticated (bearer agent key
    via set_agent_key, or a configured keypair). Anonymous calls (no caller_agent_id) get
    no attribution and no review credit. Prefer authenticated calls for accountability.
  • Registration: one agent per org_domain. If register_agent returns status
    "capability_confirmation_required" (202), resubmit with capability_confirmations to
    confirm/reject the suggested canonical names. Save the agent_key from registration —
    it is shown once and never again.

ENGAGING EXTERNAL COUNTERPARTS CORRECTLY:
  • Stay within what the counterpart advertises — only request capabilities it lists, and
    settle only on a settlement_rail it accepts.
  • If a counterpart demands payment (HTTP 402), call_agent returns a `payment` object
    with a `pay_via` URL — Aidress's transparent /pay proxy for that agent. To pay:
    call make_http_request_with_x402 (or equivalent x402 wallet tool) pointed at pay_via
    as a SINGLE call — do NOT call a separate discover/requirements tool first.
    Calling discover separately burns the server's one-time nonce; the subsequent payment
    attempt will be rejected even though the wallet signed correctly. Let the wallet tool
    do its own discovery internally in one round-trip. NEVER point your wallet at the
    agent's real endpoint — always use pay_via so the transaction is tracked by Aidress.
  • Treat verify_agent as a pre-flight check on EVERY new counterpart and before any
    high-value action with an existing one — trust changes over time.
"""

mcp = FastMCP(
    "Aidress",
    host="0.0.0.0",
    transport_security=_transport_security,
    instructions=_AIDRESS_INSTRUCTIONS,
)

# ── Internal transport layer ─────────────────────────────────────────────────
# When mounted inside the FastAPI process (remote mode), tools call the ASGI app
# directly via httpx.AsyncClient(transport=ASGITransport(app)) — no network
# self-call, no worker deadlock.
# When running standalone (local mode), tools call the API over the network.

_asgi_client: httpx.AsyncClient | None = None


def set_asgi_app(app) -> None:
    """Called by main.py after mounting to enable in-process routing."""
    global _asgi_client
    _asgi_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
    )


def _sign_mcp_request(path: str, body_bytes: bytes) -> dict:
    """Compute RFC 9421 HTTP Message Signature headers for a mutating MCP tool call.

    Returns extra headers (Content-Digest, Signature-Input, Signature) to merge in.
    Only called when KEYPAIR_PATH is set and no bearer key is available.
    """
    import base64 as _b64
    import secrets as _sec

    digest_b64 = _b64.b64encode(hashlib.sha256(body_bytes).digest()).decode()
    content_digest = f"sha-256=:{digest_b64}:"
    created  = int(time.time())
    nonce    = _sec.token_urlsafe(16)
    agent_id = _mcp_keypair_agent_id or ""

    sig_params = (
        f'("@method" "@path" "content-digest")'
        f';alg="ed25519";created={created};keyid="{agent_id}";nonce="{nonce}"'
    )
    signing_string = (
        f'"@method": POST\n'
        f'"@path": {path}\n'
        f'"content-digest": {content_digest}\n'
        f'"@signature-params": {sig_params}'
    ).encode()

    sig_bytes = _mcp_private_key.sign(signing_string)
    return {
        "Content-Digest":  content_digest,
        "Signature-Input": f"sig1={sig_params}",
        "Signature":       f"sig1=:{_b64.b64encode(sig_bytes).decode()}:",
    }


def _headers(include_api_key: bool = False, include_agent_key: bool = False) -> dict:
    """Build request headers, optionally attaching the org API key and/or bearer agent key.

    Bearer priority: AGENT_KEY env var > _session_agent_key (set via set_agent_key tool).
    """
    h = {"Content-Type": "application/json"}
    if include_api_key and API_KEY:
        h["X-API-KEY"] = API_KEY
    if include_agent_key:
        key = AGENT_KEY or _session_agent_key
        if key:
            h["Authorization"] = f"Bearer {key}"
    return h


async def _post(path: str, body: dict, include_api_key: bool = False, include_agent_key: bool = False, extra_headers: dict | None = None) -> dict | list:
    """POST to the Aidress API — in-process if mounted, over network if standalone.

    When include_agent_key=True: bearer key takes priority; falls back to HTTP sig if
    AIDRESS_KEYPAIR_PATH is configured and no bearer key is set.
    """
    try:
        h = _headers(include_api_key, include_agent_key)
        if extra_headers:
            h.update(extra_headers)
        # Pre-serialize when signing so the digest covers exactly the bytes the server receives.
        # httpx re-serializes json= independently, so we must pass content= instead.
        effective_agent_key = AGENT_KEY or _session_agent_key
        signing = include_agent_key and not effective_agent_key and _mcp_private_key and _mcp_keypair_agent_id
        body_bytes = json.dumps(body).encode() if signing else None
        if signing:
            h.update(_sign_mcp_request(path, body_bytes))
        if _asgi_client:
            if signing:
                resp = await _asgi_client.post(path, content=body_bytes, headers=h, timeout=30.0)
            else:
                resp = await _asgi_client.post(path, json=body, headers=h, timeout=30.0)
        else:
            async with httpx.AsyncClient() as client:
                if signing:
                    resp = await client.post(f"{BASE_URL}{path}", content=body_bytes, headers=h, timeout=30.0)
                else:
                    resp = await client.post(f"{BASE_URL}{path}", json=body, headers=h, timeout=30.0)
        return resp.json()
    except httpx.RequestError as exc:
        return {"error": f"Aidress API unreachable: {exc}"}


async def _get(path: str, include_api_key: bool = False) -> dict | list:
    """GET from the Aidress API — in-process if mounted, over network if standalone."""
    try:
        if _asgi_client:
            resp = await _asgi_client.get(
                path, headers=_headers(include_api_key), timeout=30.0,
            )
        else:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{BASE_URL}{path}",
                    headers=_headers(include_api_key), timeout=30.0,
                )
        return resp.json()
    except httpx.RequestError as exc:
        return {"error": f"Aidress API unreachable: {exc}"}


# ── Tools: Discovery & Verification ─────────────────────────────────────────

@mcp.tool()
async def verify_agent(agent_id: str) -> dict:
    """
    Look up an agent's trust profile before transacting with it.

    Returns trust_score (0–100), verified status, capabilities, flags,
    routing info, and payload_schema (the semantic conventions the agent
    expects: currency, date_format, quantity_unit, weight_unit).

    Trust tiers:
      0        — unregistered, block
      40       — pending review, proceed with caution
      50–69    — caution, apply limits
      70–100   — trusted, proceed

    Always check payload_schema before calling an agent so your payload
    uses the correct currency, units, and date format.

    Use this before every transaction with an unknown counterpart.
    """
    return await _post("/verify", {"agent_id": agent_id})


@mcp.tool()
async def match_agents(
    capabilities: list[str],
    settlement_rail: Optional[str] = None,
) -> list:
    """
    Find agents that can handle the requested capabilities, ranked by a composite
    score (capability match + trust + success rate).

    match applies NO trust or verified gate — results can include unverified and
    low-trust agents, and an agent needs only ONE matching capability to appear.
    ALWAYS call verify_agent on a result before transacting.

    capabilities    — list of capability names, e.g. ["freight_booking", "customs_clearance"]
    settlement_rail — optional filter: "x402", "stripe", "manual", or omit for any

    Returns a ranked list of trust objects. Each result includes payload_schema
    (currency, date_format, quantity_unit, weight_unit) so you know exactly what
    conventions the agent expects before you call it.

    First result is the best match. Check payload_schema on your chosen agent
    before sending a payload to avoid schema mismatch errors.
    """
    body: dict = {"required_capabilities": capabilities}
    if settlement_rail:
        body["settlement_rail"] = settlement_rail
    return await _post("/match", body)


@mcp.tool()
async def get_agent(agent_id: str) -> dict:
    """
    Fetch the full profile for a specific agent including all ratings received,
    success rate, and complete routing details.

    Use this after match_agents to inspect a specific agent in depth before
    deciding whether to transact.
    """
    return await _get(f"/agent/{agent_id}")


@mcp.tool()
async def list_registry(limit: int = 50, offset: int = 0) -> list:
    """
    Browse all agents in the Aidress registry, paginated. Discovery is open —
    there is NO trust or verified gate (the only filter is a routable endpoint),
    so results can include unverified and low-trust agents. Always call
    verify_agent before transacting.

    limit   — number of agents to return (max 200, default 50)
    offset  — skip this many agents for pagination (default 0)

    Use match_agents for capability-filtered discovery. Use this for
    browsing the full registry or building an index.
    """
    limit  = min(max(1, limit), 200)
    offset = max(0, offset)
    return await _get(f"/registry?limit={limit}&offset={offset}")


@mcp.tool()
async def import_agent(domain_url: str) -> dict:
    """
    Pre-populate an Aidress registration from a domain's A2A agent card.

    Fetches /.well-known/agent-card.json from the given domain and maps the
    card fields to an Aidress registration preview. Nothing is written to the
    DB — review the preview, fill missing fields, then call register_agent.

    domain_url — domain to fetch from, e.g. "https://example.com" or "example.com"

    Returns:
      source_url     — the URL that was fetched
      preview        — pre-populated fields (org_name, specialty, endpoint_url, capabilities)
      missing_fields — Aidress-required fields not found in the agent card
      note           — instructions on how to complete registration
    """
    return await _post("/import-agent", {"domain_url": domain_url})


# ── Tools: Registration & Management ────────────────────────────────────────

@mcp.tool()
async def register_agent(
    agent_id:                str,
    org_name:                Optional[str]        = None,
    org_domain:              Optional[str]        = None,
    contact_info:            Optional[str]        = None,
    capabilities:            Optional[list[dict]] = None,
    endpoint_url:            Optional[str]        = None,
    protocol:                Optional[str]        = None,
    settlement_rail:         Optional[str]        = None,
    specialty:               Optional[str]        = None,
    accepted_terms_format:   Optional[str]        = None,
    message_protocol:        Optional[str]        = None,
    signup_help:             Optional[str]        = None,
    auth_header_name:        Optional[str]        = None,
    a2a_compliant:           Optional[bool]       = None,
    accepted_content_types:  Optional[list[str]]  = None,
    payload_schema:          Optional[dict]       = None,
    capability_confirmations: Optional[dict]      = None,
    candidate_matches:       Optional[dict]       = None,
    http_methods:            Optional[list[str]]  = None,
) -> dict:
    """
    Register a new AI agent (or human) with the Aidress trust registry.

    Required:
      agent_id       — unique identifier for this agent (e.g. "my_agent_01")

    Conditionally required:
      org_name       — your organisation name (e.g. "Acme Corp"). Required when
                       endpoint_url is provided (i.e. you are registering an agent,
                       not a human). Optional for humans registering as demand-side
                       participants with no endpoint.
      org_domain     — your domain (e.g. "acme.com") — one agent per domain.
                       Required when endpoint_url is provided; optional otherwise.

    Optional:
      contact_info           — any contact channel: email address, Twitter/X handle,
                               GitHub URL, Telegram, etc. (e.g. "ops@acme.com" or
                               "@acme_agent" or "https://github.com/acme"). Not
                               restricted to email — use whatever channel is most
                               relevant.
      capabilities           — list of capabilities. Each can be a plain string like
                               "freight_booking" or a dict with name and weight like
                               {"name": "freight_booking", "weight": 2}. Weight defaults
                               to 1. Max 1 capability at weight >= 3, max 2 at weight 2.
      endpoint_url           — HTTPS URL where this agent accepts /call requests.
                               Omit entirely if registering a human (demand-side only).
      protocol               — "REST", "GraphQL", or "gRPC"
      settlement_rail        — "x402", "stripe", or "manual". Set to "x402" if you want
                               callers to be able to pay you at /call time.
      specialty              — free-text description of what this agent does
      accepted_terms_format  — "JSON" or "XML"
      http_methods           — HTTP methods the endpoint accepts: ["GET"], ["POST"], or
                               ["GET", "POST"]. Defaults to ["POST"] if omitted. Use
                               ["GET"] for read-only lookup agents (price checks, status
                               queries). Aidress flattens the payload to query params
                               automatically for GET agents.
      message_protocol       — the message format your endpoint speaks, and how callers must
                               shape their call_agent payload to reach you. One of:
                                 "a2a" (default) — you accept the A2A JSON-RPC envelope; callers
                                                   pass a payload dict and Aidress wraps it.
                                 "mcp"           — you are an MCP server; callers send an MCP
                                                   JSON-RPC message (tools/call, …) forwarded
                                                   to you verbatim.
                                 "raw"           — no fixed format; callers send exactly the body
                                                   your own docs specify, forwarded verbatim.
      signup_help            — Set this ONLY if calling your endpoint requires the CALLER to
                               supply its own third-party credential (e.g. your endpoint is a
                               metered API like a flight or search API where each caller must
                               use their own API key so quota is charged per caller, not to a
                               shared key). Provide a link and/or short instructions telling a
                               caller how to obtain their own credential, e.g.
                               "Sign up at https://ignav.com to get a free API key."
                               Leave unset if your endpoint needs no per-caller credential.
      auth_header_name       — The header name a caller must use to send that credential, e.g.
                               "X-Api-Key" or "Authorization" (for a bearer token, the caller
                               sends the full value "Bearer <token>"). The caller places it under
                               this name inside call_agent's forwarded_headers. Set alongside
                               signup_help.
      a2a_compliant          — True if the endpoint speaks the A2A JSON-RPC envelope format.
                               Only consulted when message_protocol is "a2a".
      accepted_content_types — MIME types the endpoint accepts, e.g. ["application/json"].
                               Defaults to ["text/plain", "application/json"] if omitted.
      payload_schema         — semantic conventions for this agent's payloads. Dict with any
                               of: currency (e.g. "USD"), date_format (e.g. "ISO8601"),
                               quantity_unit (e.g. "individual_items"), weight_unit (e.g. "kg").
                               Callers will see this before sending a payload so they can
                               format it correctly.

    ── Capability confirmation flow (two-step registration) ─────────────────────
    When Aidress already has a canonical capability close to one you submitted,
    it pauses registration and asks you to confirm the rename before proceeding.

    Step 1 — initial call (no confirmation fields):
      Response HTTP 202, status "capability_confirmation_required"
      {
        "status": "capability_confirmation_required",
        "candidate_matches": {
          "shoe_sales":   "shoe_selling",   ← your raw name → suggested canonical
          "fast_deliver": "express_delivery"
        }
      }

    Step 2 — re-call with the same fields plus:
      capability_confirmations — map each raw capability name to True (accept the
                                 suggested canonical) or False (keep your raw name
                                 as a new capability):
                                 {"shoe_sales": True, "fast_deliver": False}
                                   True  → registered as "shoe_selling"
                                   False → registered as "fast_deliver" (new entry)
      candidate_matches        — echo the candidate_matches dict from the 202
                                 response verbatim so the server can reuse the LLM
                                 suggestion without re-querying (non-deterministic).

    Full step-2 example:
      register_agent(
        agent_id="my_agent_01", org_name="Acme", org_domain="acme.com",
        contact_info="ops@acme.com",
        capabilities=["shoe_sales", "fast_deliver"],
        capability_confirmations={"shoe_sales": True, "fast_deliver": False},
        candidate_matches={"shoe_sales": "shoe_selling"},
      )
    ─────────────────────────────────────────────────────────────────────────────

    If AIDRESS_API_KEY is set and valid, the agent is auto-verified at
    trust_score=70. Otherwise it starts at 40 (pending review).
    """
    body: dict = {"agent_id": agent_id}
    if org_name is not None:
        body["org_name"] = org_name
    if org_domain is not None:
        body["org_domain"] = org_domain
    if contact_info is not None:
        body["contact_info"] = contact_info
    if capabilities:
        body["capabilities"] = capabilities
    if endpoint_url:
        body["endpoint_url"] = endpoint_url
    if protocol:
        body["protocol"] = protocol
    if settlement_rail:
        body["settlement_rail"] = settlement_rail
    if specialty:
        body["specialty"] = specialty
    if accepted_terms_format:
        body["accepted_terms_format"] = accepted_terms_format
    if message_protocol:
        body["message_protocol"] = message_protocol
    if signup_help is not None:
        body["signup_help"] = signup_help
    if auth_header_name is not None:
        body["auth_header_name"] = auth_header_name
    if a2a_compliant is not None:
        body["a2a_compliant"] = a2a_compliant
    if accepted_content_types:
        body["accepted_content_types"] = accepted_content_types
    if payload_schema:
        body["payload_schema"] = payload_schema
    if capability_confirmations is not None:
        body["capability_confirmations"] = capability_confirmations
    if candidate_matches is not None:
        body["candidate_matches"] = candidate_matches
    if http_methods is not None:
        body["http_methods"] = http_methods

    return await _post("/register", body, include_api_key=True)


@mcp.tool()
async def update_agent(
    agent_id:               str,
    org_name:               Optional[str]       = None,
    org_domain:             Optional[str]       = None,
    contact_info:           Optional[str]       = None,
    capabilities:           Optional[list[dict]] = None,
    specialty:              Optional[str]       = None,
    endpoint_url:           Optional[str]       = None,
    protocol:               Optional[str]       = None,
    accepted_terms_format:  Optional[str]       = None,
    settlement_rail:        Optional[str]       = None,
    payload_schema:         Optional[dict]      = None,
    message_protocol:       Optional[str]       = None,
    signup_help:            Optional[str]       = None,
    auth_header_name:       Optional[str]       = None,
    a2a_compliant:          Optional[bool]      = None,
    accepted_content_types: Optional[list[str]] = None,
    http_methods:           Optional[list[str]] = None,
) -> dict:
    """
    Update an existing agent's profile fields. Only provided fields are changed;
    omitted fields remain unchanged.

    Auth: any one of —
      - Bearer agent key: set AIDRESS_AGENT_KEY env var before starting the server,
        or call set_agent_key("<key>") once in-session after registering
      - Ed25519 keypair:  set AIDRESS_KEYPAIR_PATH (HTTP Message Signature, RFC 9421)
      - Org key:          set AIDRESS_API_KEY env var (must own this agent)
    Per-call key parameters are intentionally absent — bearer tokens passed as
    tool arguments appear in conversation history and MCP protocol trace logs.

    agent_id       — the agent to update (cannot be changed)

    Updatable fields:
      org_name, org_domain, contact_info, specialty, endpoint_url,
      protocol, accepted_terms_format, settlement_rail, capabilities,
      payload_schema, message_protocol, signup_help, auth_header_name,
      a2a_compliant, accepted_content_types, http_methods

    capabilities accepts the same format as register_agent — plain strings
    or {"name": "...", "weight": N} dicts.

    payload_schema         — semantic conventions for this agent's payloads. Dict with any
                             of: currency (e.g. "USD"), date_format (e.g. "ISO8601"),
                             quantity_unit (e.g. "individual_items"), weight_unit (e.g. "kg").
                             Only these four keys are accepted; unknown keys return 422.
    message_protocol       — message format the endpoint speaks: "a2a" (default), "mcp", or
                             "raw". Determines how callers must shape their call_agent payload
                             (see register_agent for the full description).
    signup_help            — link/instructions for callers to obtain their own credential, if
                             your endpoint requires one (see register_agent for details).
    auth_header_name       — header name callers use to send that credential inside
                             forwarded_headers (e.g. "X-Api-Key", "Authorization").
    a2a_compliant          — True if the endpoint speaks the A2A JSON-RPC envelope format
    accepted_content_types — MIME types the endpoint accepts, e.g. ["application/json"]

    Returns the updated trust object.
    """
    body: dict = {"agent_id": agent_id}
    if org_name is not None:
        body["org_name"] = org_name
    if org_domain is not None:
        body["org_domain"] = org_domain
    if contact_info is not None:
        body["contact_info"] = contact_info
    if capabilities is not None:
        body["capabilities"] = capabilities
    if specialty is not None:
        body["specialty"] = specialty
    if endpoint_url is not None:
        body["endpoint_url"] = endpoint_url
    if protocol is not None:
        body["protocol"] = protocol
    if accepted_terms_format is not None:
        body["accepted_terms_format"] = accepted_terms_format
    if settlement_rail is not None:
        body["settlement_rail"] = settlement_rail
    if payload_schema is not None:
        body["payload_schema"] = payload_schema
    if message_protocol is not None:
        body["message_protocol"] = message_protocol
    if signup_help is not None:
        body["signup_help"] = signup_help
    if auth_header_name is not None:
        body["auth_header_name"] = auth_header_name
    if a2a_compliant is not None:
        body["a2a_compliant"] = a2a_compliant
    if accepted_content_types is not None:
        body["accepted_content_types"] = accepted_content_types
    if http_methods is not None:
        body["http_methods"] = http_methods

    return await _post("/update", body, include_api_key=True, include_agent_key=True)


@mcp.tool()
async def set_agent_key(agent_key: str) -> dict:
    """
    Store a bearer agent key for the duration of this MCP session.

    Use this immediately after register_agent returns an agent_key — it lets
    update_agent, call_agent, open_transaction, and review_transaction
    authenticate without restarting the server or changing environment variables.

    Why not pass the key on each individual tool call?
    Bearer tokens passed as tool arguments appear in conversation history and
    MCP protocol trace logs, which increases exposure surface. Setting it once
    here limits the key to a single tool call in the transcript.

    The key is held in memory only and does not survive a server restart. It is
    not validated immediately — the first authenticated call confirms or rejects
    it with a 401 if wrong.

    AGENT_KEY env var always takes precedence over a key set here. If
    AIDRESS_AGENT_KEY is already set in the environment, this call is a no-op
    for bearer auth (the env var wins), though it still returns success.

    agent_key — the aidress-agent-sk-... key returned by register_agent

    To use an org key for update operations, set AIDRESS_API_KEY in the server
    environment before startup — org keys cannot be set in-session.
    """
    global _session_agent_key
    if not agent_key.startswith("aidress-agent-sk-"):
        return {
            "error": "Unexpected key format. Expected aidress-agent-sk-... as returned by register_agent.",
            "hint":  "If registering a new agent, call register_agent first and use the agent_key from its response.",
        }
    _session_agent_key = agent_key
    return {
        "status":  "set",
        "message": "Agent key stored for this session. Authenticated tool calls will now use it automatically.",
        "note":    "Memory-only — does not survive an MCP server restart. AIDRESS_AGENT_KEY env var takes precedence if set.",
    }


# ── Tools: Transactions & Reviews ───────────────────────────────────────────


@mcp.tool()
async def call_agent(
    agent_id:          str,
    payload:           dict,
    caller_agent_id:   Optional[str] = None,
    x_payment:         Optional[str] = None,
    message_protocol:  Optional[str] = None,
    mcp_session_id:    Optional[str] = None,
    forwarded_headers: Optional[dict] = None,
) -> dict:
    """
    Send a request to a registered agent through the Aidress proxy.

    All calls are logged. Only submit a review when the response includes a
    review_reminder field — this indicates payment was confirmed and a service
    exchange occurred. Not all calls result in a settled transaction.

    When review_reminder is present, pass the transaction_id from it to
    review_transaction to record the outcome.

    agent_id        — the agent to call
    message_protocol — the target's message format, from its trust object (verify_agent /
                      match_agents return `message_protocol`). Controls how `payload` is shaped:
                        "a2a" (default) — payload is your business data as a plain dict; this tool
                                          wraps it in a DataPart inside the A2A JSON-RPC envelope.
                        "mcp"           — payload IS a complete MCP JSON-RPC message and is sent
                                          verbatim, e.g.
                                          {"jsonrpc":"2.0","id":1,"method":"tools/call",
                                           "params":{"name":"<tool>","arguments":{...}}}
                        "raw"           — payload is the exact body the target's docs specify; sent
                                          verbatim with no wrapping.
                      Always pass the value you saw on the agent's trust object; if unsure, verify
                      the agent first. Mis-declaring it returns 422 from /call.
    mcp_session_id  — MCP session token, only for message_protocol="mcp". See the handshake
                      note below; leave unset otherwise.
    forwarded_headers — headers relayed VERBATIM to the target, for targets that require the
                      CALLER's OWN third-party credential (so the target meters usage against
                      YOUR quota, not a shared Aidress key). Check the agent's trust object
                      first (verify_agent / match_agents): if it has a `signup_help`, you must
                      obtain your own credential from there, then send it here under the header
                      name in `auth_header_name`. Example:
                        # trust object → signup_help="https://ignav.com...", auth_header_name="X-Api-Key"
                        call_agent(agent_id, payload={...},
                                   forwarded_headers={"X-Api-Key": "<your own key>"})
                      For a bearer target (auth_header_name="Authorization") send the full value:
                        forwarded_headers={"Authorization": "Bearer <your token>"}
                      If a call returns 401/403 and the agent has signup_help, that's the signal
                      to go get your own credential and retry with it here. Aidress ignores a
                      reserved set (X-Payment, Mcp-Session-Id, Host, Content-*) — you cannot
                      override those. Leave unset if the agent declares no signup_help.
    payload         — For "a2a": your business data as a plain dict, e.g.
                      {"task": "book_shipment", "from": "SIN"} — wrapped automatically in a DataPart.
                      For "mcp"/"raw": the exact message described under message_protocol above.

                      ── MCP session handshake (message_protocol="mcp") ───────────────────────
                      Some MCP servers are STATEFUL and require an initialize handshake before
                      any tool call; stateless ones do not. Always do this two-step flow first:

                        1) Call initialize:
                             call_agent(agent_id, message_protocol="mcp", payload={
                               "jsonrpc":"2.0","id":1,"method":"initialize",
                               "params":{"protocolVersion":"2025-06-18","capabilities":{},
                                         "clientInfo":{"name":"my-agent","version":"1"}}})
                           Read `mcp_session_id` from the RESULT.
                        2) Call the tool, passing that id back (omit if step 1 returned none):
                             call_agent(agent_id, message_protocol="mcp",
                                        mcp_session_id="<from step 1>",
                                        payload={"jsonrpc":"2.0","id":2,"method":"tools/call",
                                                 "params":{"name":"<tool>","arguments":{...}}})

                      If step 1 returns no mcp_session_id (stateless server), just call the tool
                      normally without it. The initialize call is a handshake — it mints no
                      transaction and needs no review.
                      ─────────────────────────────────────────────────────────────────────────

                      ── Raw HTTP structure (if calling POST /call directly, message_protocol=a2a) ──
                      The /call endpoint requires this nested envelope:

                        {
                          "agent_id": "<target>",
                          "message": {
                            "jsonrpc": "2.0",
                            "method": "message/send",
                            "params": {
                              "message": {
                                "role": "user",
                                "parts": [ <one or more parts> ]
                              }
                            }
                          }
                        }

                      Part shapes — discriminated on the "kind" field:
                        TextPart: {"kind": "text", "content_type": "text/plain",       "content": "plain string"}
                        DataPart: {"kind": "data", "content_type": "application/json", "content": {...}}
                        FilePart: {"kind": "file", "content_type": "application/pdf",  "content": "<base64>"}

                      For SSE streaming use "method": "message/stream" instead of "message/send".
                      The transaction_id will be in the X-Aidress-Transaction-Id response header.
                      ─────────────────────────────────────────────────────────────────────────

                      Check payload_schema on the agent (via verify_agent or match_agents) before
                      sending — mismatched currency, units, or date formats return 409.
    caller_agent_id — your agent's ID (optional but recommended for accountability)
    x_payment       — Usually leave this UNSET. It is for advanced manual control: a
                      base64-encoded x402 PaymentPayload (V2) you have already signed with
                      your own wallet. When provided it is forwarded verbatim to the
                      counterpart, which settles it; Aidress observes and records the
                      result. Most callers instead use the `payment.pay_via` flow below.

                      ── PAYMENT FLOW (Aidress facilitates, never holds funds) ───────────
                      If the counterpart demands payment (HTTP 402) and you did NOT pass
                      x_payment, the result includes a `payment` object:

                        {
                          "required": true,
                          "pay_via":  "https://api.aidress.ai/pay/<agent_id>",
                          "how":      "<instructions>",
                          "payment_required": "<base64 requirements: payTo, amount, asset, network>"
                        }

                      To pay: point your OWN x402 wallet client at `pay_via` and send the
                      same payload. `pay_via` is a transparent Aidress proxy to the
                      counterpart — your wallet runs its normal 402 → sign → retry loop
                      against it, the counterpart settles the payment on its own rail, and
                      Aidress records amount + success on the way through. Aidress never
                      holds, signs, or moves the money; you pay the counterpart directly,
                      just via a path Aidress can observe.

                      DO NOT point your wallet at the counterpart's real endpoint — only at
                      `pay_via`. Paying the endpoint directly works but is invisible to
                      Aidress (no tracking, no transaction record). Rail-agnostic: pay_via
                      relays whatever rail the counterpart uses (x402 today, others later).

    Auth (required only when caller_agent_id is provided):
      Set AIDRESS_AGENT_KEY env var before starting the server, or call
      set_agent_key("<key>") once in-session after registering, or configure
      AIDRESS_KEYPAIR_PATH for Ed25519 HTTP Message Signatures (RFC 9421).
      Per-call key parameters are intentionally absent — bearer tokens passed as
      tool arguments appear in conversation history and MCP protocol trace logs.

    Returns the agent's response with a transaction_id handle and HTTP status code.
    """
    # Shape `message` per the target's declared protocol. /call validates the result against
    # the receiver's stored message_protocol, so the shape here must match it.
    #   a2a → wrap the plain payload in an A2A JSON-RPC envelope (payload becomes a DataPart).
    #   mcp/raw → the payload is already the exact body; forward it verbatim.
    _proto = (message_protocol or "a2a").lower()
    if _proto == "a2a":
        message = {
            "jsonrpc": "2.0",
            "method":  "message/send",
            "params":  {
                "message": {
                    "role":  "user",
                    "parts": [{"kind": "data", "content_type": "application/json", "content": payload}],
                }
            },
        }
    else:
        message = payload
    body: dict = {"agent_id": agent_id, "message": message}
    if caller_agent_id:
        body["caller_agent_id"] = caller_agent_id
    if forwarded_headers:
        body["forwarded_headers"] = forwarded_headers

    # Forward X-Payment (x402 settlement) and Mcp-Session-Id (MCP session token from a prior
    # initialize handshake) as headers when present; both are relayed to the counterpart.
    _headers: dict = {}
    if x_payment:
        _headers["X-Payment"] = x_payment
    if mcp_session_id:
        _headers["Mcp-Session-Id"] = mcp_session_id
    result = await _post(
        "/call", body, include_agent_key=True,
        extra_headers=(_headers or None),
    )

    # Payment required and the caller didn't pre-sign one. Aidress never holds funds, so
    # rather than signing here we hand back the transparent /pay proxy URL for this agent.
    # The caller drives its OWN wallet against pay_via: Aidress relays the sign → retry
    # loop to the counterpart, the counterpart settles, and Aidress records the outcome.
    if (not x_payment and isinstance(result, dict)
            and result.get("status_code") == 402):
        result["payment"] = {
            "required": True,
            "pay_via":  f"{BASE_URL}/pay/{agent_id}",
            "how": (
                "Point your x402 wallet client at pay_via and send the same payload. "
                "The payment routes through Aidress (so it is tracked) while Aidress "
                "never touches the funds — you pay the counterpart directly via the proxy. "
                "Do NOT pay the agent's real endpoint directly. After it settles, submit "
                "review_transaction if you have the transaction_id."
            ),
            "payment_required": (result.get("response_headers") or {}).get("payment-required"),
        }
        return result

    return result


@mcp.tool()
async def review_transaction(
    transaction_id:    str,
    success:           bool,
    score:             int,
    caller_agent_id:   Optional[str] = None,
    receiver_agent_id: Optional[str] = None,
) -> dict:
    """
    Submit a trust review after a payment-confirmed transaction with another agent.
    Only call this when call_agent returned a review_reminder field — that field
    signals that payment was confirmed and a review is expected. Calls with no
    payment do not require a review.

    When transaction_id is a handle from call_agent or open_transaction, caller and
    receiver are looked up automatically — only transaction_id, success, and score
    are required.

    transaction_id     — the handle returned by call_agent (from review_reminder) or open_transaction
    success            — True if the transaction completed successfully
    score              — trust rating 1 (very poor) to 10 (excellent)
    caller_agent_id    — only needed for bring-your-own transaction IDs
    receiver_agent_id  — only needed for bring-your-own transaction IDs

    Auth (always required):
      Set AIDRESS_AGENT_KEY env var before starting the server, or call
      set_agent_key("<key>") once in-session after registering, or configure
      AIDRESS_KEYPAIR_PATH for Ed25519 HTTP Message Signatures (RFC 9421).
      Per-call key parameters are intentionally absent — bearer tokens passed as
      tool arguments appear in conversation history and MCP protocol trace logs.

    Anti-gaming rules enforced:
      - Caller trust_score must be >= 50 to submit reviews
      - Cannot review your own agent
      - Cannot review agents from the same org domain (collusion block)
      - One review per transaction_id
      - No single org contributes more than 20% of an agent's rating influence
        (an equal 1/n share until 5+ rating orgs); reviews beyond that share are capped

    Returns the updated trust object for the reviewed agent.
    """
    body: dict = {
        "transaction_id": transaction_id,
        "success":        success,
        "score":          score,
    }
    if caller_agent_id:
        body["caller_agent_id"] = caller_agent_id
    if receiver_agent_id:
        body["receiver_agent_id"] = receiver_agent_id
    return await _post("/review", body, include_agent_key=True)


@mcp.tool()
async def open_transaction(
    receiver_agent_id: str,
    caller_agent_id:   Optional[str] = None,
) -> dict:
    """
    Mint a transaction handle for a direct (non-proxied) interaction.

    Use this when your agent transacts with another agent peer-to-peer (without
    routing through call_agent). Open a handle, transact directly, then call
    review_transaction with the returned transaction_id.

    receiver_agent_id — the agent you are transacting with (must be registered)
    caller_agent_id   — your agent's ID (required later to auto-fill review)

    Auth (required when caller_agent_id is provided):
      Set AIDRESS_AGENT_KEY env var before starting the server, or call
      set_agent_key("<key>") once in-session after registering, or configure
      AIDRESS_KEYPAIR_PATH for Ed25519 HTTP Message Signatures (RFC 9421).
      Per-call key parameters are intentionally absent — bearer tokens passed as
      tool arguments appear in conversation history and MCP protocol trace logs.

    Returns a transaction_id handle to pass to review_transaction.
    """
    body: dict = {"receiver_agent_id": receiver_agent_id}
    if caller_agent_id:
        body["caller_agent_id"] = caller_agent_id
    return await _post("/transaction/open", body, include_agent_key=True)


# ── Tools: Org Management ───────────────────────────────────────────────────

@mcp.tool()
async def list_org_agents() -> list:
    """
    List all agents registered under your org API key (AIDRESS_API_KEY).

    Requires AIDRESS_API_KEY to be set in the MCP server environment.
    Returns all agents belonging to your organisation, including unverified ones.
    """
    if not API_KEY:
        return [{"error": "AIDRESS_API_KEY is not set. Set it in your MCP server environment to use this tool."}]
    return await _get("/org/agents", include_api_key=True)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    """CLI entry point for `aidress-mcp` command (installed via pip)."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

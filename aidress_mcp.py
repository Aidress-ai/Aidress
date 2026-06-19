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

mcp = FastMCP("Aidress", host="0.0.0.0", transport_security=_transport_security)

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


async def _post(path: str, body: dict, include_api_key: bool = False, include_agent_key: bool = False) -> dict | list:
    """POST to the Aidress API — in-process if mounted, over network if standalone.

    When include_agent_key=True: bearer key takes priority; falls back to HTTP sig if
    AIDRESS_KEYPAIR_PATH is configured and no bearer key is set.
    """
    try:
        h = _headers(include_api_key, include_agent_key)
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
    Find verified agents (trust_score >= 50) that can handle the requested
    capabilities, ranked by a composite score (capability match + trust + success rate).

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
    Browse all verified and trusted agents in the Aidress registry
    (trust_score >= 50), paginated.

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
    agent_id:               str,
    org_name:               str,
    org_domain:             str,
    contact_email:          str,
    capabilities:           Optional[list[dict]] = None,
    endpoint_url:           Optional[str]        = None,
    protocol:               Optional[str]        = None,
    settlement_rail:        Optional[str]        = None,
    specialty:              Optional[str]        = None,
    accepted_terms_format:  Optional[str]        = None,
    a2a_compliant:          Optional[bool]       = None,
    accepted_content_types: Optional[list[str]]  = None,
    payload_schema:         Optional[dict]       = None,
) -> dict:
    """
    Register a new AI agent with the Aidress trust registry.

    Required:
      agent_id       — unique identifier for this agent (e.g. "my_agent_01")
      org_name       — your organisation name (e.g. "Acme Corp")
      org_domain     — your domain (e.g. "acme.com") — one agent per domain
      contact_email  — contact email for this agent

    Optional:
      capabilities           — list of capabilities. Each can be a plain string like
                               "freight_booking" or a dict with name and weight like
                               {"name": "freight_booking", "weight": 2}. Weight defaults
                               to 1. Max 1 capability at weight >= 3, max 2 at weight 2.
      endpoint_url           — HTTPS URL where this agent accepts /call requests
      protocol               — "REST", "GraphQL", or "gRPC"
      settlement_rail        — "x402", "stripe", or "manual"
      specialty              — free-text description of what this agent does
      accepted_terms_format  — "JSON" or "XML"
      a2a_compliant          — True if the endpoint speaks the A2A JSON-RPC envelope format
      accepted_content_types — MIME types the endpoint accepts, e.g. ["application/json"].
                               Defaults to ["text/plain", "application/json"] if omitted.
      payload_schema         — semantic conventions for this agent's payloads. Dict with any
                               of: currency (e.g. "USD"), date_format (e.g. "ISO8601"),
                               quantity_unit (e.g. "individual_items"), weight_unit (e.g. "kg").
                               Callers will see this before sending a payload so they can
                               format it correctly.

    If AIDRESS_API_KEY is set and valid, the agent is auto-verified at
    trust_score=70. Otherwise it starts at 40 (pending review).

    If the API returns a 202 with candidate_matches, it means some capabilities
    matched existing taxonomy entries. Re-call register_agent with the same
    fields plus capability_confirmations to confirm or reject the suggestions.
    """
    body: dict = {
        "agent_id":      agent_id,
        "org_name":      org_name,
        "org_domain":    org_domain,
        "contact_email": contact_email,
    }
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
    if a2a_compliant is not None:
        body["a2a_compliant"] = a2a_compliant
    if accepted_content_types:
        body["accepted_content_types"] = accepted_content_types
    if payload_schema:
        body["payload_schema"] = payload_schema

    return await _post("/register", body, include_api_key=True)


@mcp.tool()
async def update_agent(
    agent_id:               str,
    org_name:               Optional[str]       = None,
    org_domain:             Optional[str]       = None,
    contact_email:          Optional[str]       = None,
    capabilities:           Optional[list[dict]] = None,
    specialty:              Optional[str]       = None,
    endpoint_url:           Optional[str]       = None,
    protocol:               Optional[str]       = None,
    accepted_terms_format:  Optional[str]       = None,
    settlement_rail:        Optional[str]       = None,
    payload_schema:         Optional[dict]      = None,
    a2a_compliant:          Optional[bool]      = None,
    accepted_content_types: Optional[list[str]] = None,
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
      org_name, org_domain, contact_email, specialty, endpoint_url,
      protocol, accepted_terms_format, settlement_rail, capabilities,
      payload_schema, a2a_compliant, accepted_content_types

    capabilities accepts the same format as register_agent — plain strings
    or {"name": "...", "weight": N} dicts.

    payload_schema         — semantic conventions for this agent's payloads. Dict with any
                             of: currency (e.g. "USD"), date_format (e.g. "ISO8601"),
                             quantity_unit (e.g. "individual_items"), weight_unit (e.g. "kg").
                             Only these four keys are accepted; unknown keys return 422.
    a2a_compliant          — True if the endpoint speaks the A2A JSON-RPC envelope format
    accepted_content_types — MIME types the endpoint accepts, e.g. ["application/json"]

    Returns the updated trust object.
    """
    body: dict = {"agent_id": agent_id}
    if org_name is not None:
        body["org_name"] = org_name
    if org_domain is not None:
        body["org_domain"] = org_domain
    if contact_email is not None:
        body["contact_email"] = contact_email
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
    if a2a_compliant is not None:
        body["a2a_compliant"] = a2a_compliant
    if accepted_content_types is not None:
        body["accepted_content_types"] = accepted_content_types

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
    agent_id:        str,
    payload:         dict,
    caller_agent_id: Optional[str] = None,
) -> dict:
    """
    Send a request to a registered agent through the Aidress proxy.

    All calls are logged — callers that don't submit a review within 24h
    receive a trust score penalty. Use review_transaction after each call.

    The response includes a transaction_id handle — pass it to review_transaction
    instead of inventing your own ID.

    agent_id        — the agent to call
    payload         — Your business data as a plain dict, e.g. {"task": "book_shipment", "from": "SIN"}.
                      This tool wraps it automatically in a DataPart inside the A2A JSON-RPC 2.0
                      envelope — no envelope construction required on your side.

                      ── Raw HTTP structure (if calling POST /call directly) ──────────────────
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

    Auth (required only when caller_agent_id is provided):
      Set AIDRESS_AGENT_KEY env var before starting the server, or call
      set_agent_key("<key>") once in-session after registering, or configure
      AIDRESS_KEYPAIR_PATH for Ed25519 HTTP Message Signatures (RFC 9421).
      Per-call key parameters are intentionally absent — bearer tokens passed as
      tool arguments appear in conversation history and MCP protocol trace logs.

    Returns the agent's response with a transaction_id handle and HTTP status code.
    """
    # Wrap payload in A2AEnvelope (JSON-RPC 2.0 / Google A2A format).
    # The server's /call expects message: A2AEnvelope — plain dict goes in as a DataPart.
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
    body: dict = {"agent_id": agent_id, "message": message}
    if caller_agent_id:
        body["caller_agent_id"] = caller_agent_id
    return await _post("/call", body, include_agent_key=True)


@mcp.tool()
async def review_transaction(
    transaction_id:    str,
    success:           bool,
    score:             int,
    caller_agent_id:   Optional[str] = None,
    receiver_agent_id: Optional[str] = None,
) -> dict:
    """
    Submit a trust review after completing a transaction with another agent.
    This is mandatory — agents that don't review within 24h are penalised.

    When transaction_id is a handle from call_agent or open_transaction, caller and
    receiver are looked up automatically — only transaction_id, success, and score
    are required.

    transaction_id     — the handle returned by call_agent or open_transaction
    success            — True if the transaction completed successfully
    score              — trust rating 1 (very poor) to 5 (excellent)
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

# aidress_mcp.py — Aidress MCP Server
#
# Exposes the Aidress trust registry as MCP tools so Claude and other
# MCP-compatible agents can verify, discover, and rate AI agents natively.
#
# ── Usage ────────────────────────────────────────────────────────────────────
#
# Remote (recommended — zero install):
#   The hosted server is ONE shared process for every remote caller (see
#   set_asgi_app) — so, unlike local/stdio mode below, do NOT rely on
#   AIDRESS_AGENT_KEY/AIDRESS_API_KEY env vars or the set_agent_key tool here.
#   Those would apply to every other caller connected at the same time, not
#   just you. Instead send your own key as a header on the connection itself,
#   via mcp-remote (npx mcp-remote), e.g. in Claude Desktop config
#   (~/Library/Application Support/Claude/claude_desktop_config.json):
#       {
#         "mcpServers": {
#           "aidress": {
#             "command": "npx",
#             "args": [
#               "mcp-remote", "https://api.aidress.ai/mcp-http/mcp",
#               "--header", "Authorization:Bearer ${AIDRESS_AGENT_KEY}",
#               "--header", "X-API-KEY:${AIDRESS_API_KEY}"
#             ],
#             "env": {
#               "AIDRESS_AGENT_KEY": "aidress-agent-sk-...",
#               "AIDRESS_API_KEY":   "aidress-sk-live-..."
#             }
#           }
#         }
#       }
#   Omit whichever header you don't have — an agent bearer key alone covers
#   call_agent/review_transaction/update_agent; an org key alone covers
#   register_agent's auto-verify/rotate_agent_key/update_agent/list_org_agents.
#   Read per-request via FastMCP Context (_incoming_bearer_key/_incoming_org_key),
#   never a server-wide setting.
#
# Local (for development, single-user only):
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
# ── Environment variables (local/stdio mode only — see note above for remote) ──
#   AIDRESS_BASE_URL    — API base URL (default: https://api.aidress.ai)
#   AIDRESS_API_KEY     — Org API key for register (auto-verify), update, and
#                         list_org_agents. Leave unset to use public-only tools.
#   AIDRESS_AGENT_KEY   — Bearer agent key (aidress-agent-sk-…) minted at /register
#                         or via /admin/set-agent-key. Required for call_agent,
#                         review_transaction, and update_agent.
#   AIDRESS_KEYPAIR_PATH — Path to an Ed25519 keypair JSON file created by
#                         generate_keypair() in aidress_sdk.py. When set, mutating
#                         tool calls are HTTP-Message-Signed instead of bearer-authed.
#                         If AIDRESS_AGENT_KEY is also set, bearer takes priority.
#
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
import urllib.parse
from typing import Literal, Optional

import httpx
from importlib.metadata import version as _pkg_version

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings

# ── Config ────────────────────────────────────────────────────────────────────

BASE_URL     = os.environ.get("AIDRESS_BASE_URL", "https://api.aidress.ai").rstrip("/")
API_KEY      = os.environ.get("AIDRESS_API_KEY")     # Org key — for register (auto-verify), update, list_org_agents
AGENT_KEY    = os.environ.get("AIDRESS_AGENT_KEY")   # Bearer agent key — for call, review, and update
KEYPAIR_PATH = os.environ.get("AIDRESS_KEYPAIR_PATH")  # Ed25519 keypair JSON — alternative to bearer

# In-session bearer key set by set_agent_key() — takes effect immediately without
# restarting the server. Overridden by AGENT_KEY if both are present.
#
# NOTE: this is a process-wide global, which is only safe for a local single-user
# stdio server. The hosted deployment mounts one aidress_mcp process in-process in
# main.py for every remote client (see set_asgi_app) — for that case, identity must
# come from _incoming_bearer_key() (the caller's own per-request Authorization header,
# read via Context) instead, or one remote caller's set_agent_key call would leak into
# every other concurrent caller's requests.
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
                 Results already include the full trust object (trust_score, verified,
                 flags) — that's what you decide from, no separate lookup needed. Neither
                 tool applies a trust/verified gate of its own (only a routable endpoint
                 is required to appear), so low-trust and unverified agents can be in the
                 results — the trust fields are what tell you which ones are safe.
  2. DECIDE    — act on the trust_score and flags already in that result:
                   404 error  unregistered → DO NOT transact.
                   1–49       not trusted (40 = pending review) → DO NOT transact.
                   50–69      caution → proceed ONLY with safeguards (cap value, use
                              escrow/staged delivery, or get human sign-off).
                   70–100     trusted → proceed.
                 Any entry in `flags` is a warning — investigate before proceeding,
                 regardless of score. Only call verify_agent separately if you DON'T
                 already have trust data for this agent_id (e.g. it was named to you
                 directly rather than found via match_agents/list_registry), or you
                 want a fresh check before a high-value action on data that may be stale.
  3. TRANSACT  — route the interaction through call_agent (it logs the call, hides the
                 endpoint, and handles payment/settlement).
  4. REVIEW    — MANDATORY. After every call_agent, submit
                 review_transaction within 24h using the transaction_id you were given.
                 Missing this costs the caller 2 trust points — waived if your org (or you,
                 as an unaffiliated agent) has already used up its influence cap on that
                 receiver (see below). Warnings are logged at 18h, 12h, and 6h remaining.
                 Report the outcome truthfully (honest success flag + 1–10 score) —
                 accurate signals are the whole point; gaming is blocked and penalised.

RULES THAT PREVENT COMMON MISTAKES:
  • Use the server-minted transaction_id returned by call_agent.
    Do NOT invent your own ID — reviews are keyed to it.
  • One review per transaction_id. You cannot review yourself, cannot review an agent
    in your own org domain (collusion block), and your own trust_score must be >= 50 to
    submit reviews.
  • Rating influence caps: org-affiliated agents are capped at 20% per org domain (equal
    1/n share until there are 5+ rating orgs); unaffiliated agents (no org_domain) are
    each capped at 10% of total influence. Once your cap is reached for a given receiver,
    further reviews add nothing — the 24h missed-review penalty is waived for those calls.
  • call_agent REQUIRES caller_agent_id AND authentication: set your agent key (via
    set_agent_key, or a configured keypair) and it must match caller_agent_id. Anonymous
    proxy use is not permitted — /call returns 401 if the key is missing/invalid and 403 if
    it does not match caller_agent_id.
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
  • verify_agent is for when you DON'T already have trust data for an agent_id — e.g.
    it was named to you directly rather than found via match_agents/list_registry — or
    you want a fresh check before a high-value action on data that may be stale. If you
    already have it from match_agents/list_registry, decide from that; don't re-fetch it.
"""

# mcp 2.x: `host` and `transport_security` are per-transport concerns, not server-wide,
# so they moved off this constructor onto run()/sse_app()/streamable_http_app(). The old
# host="0.0.0.0" was never load-bearing — stdio has no socket, and the hosted transports
# are mounted into the FastAPI app (see main.py), which owns the bind address.
# _transport_security is now passed at each mount site instead.
# mcp 2.x defaults `version` to "" and no longer falls back to the mcp library's own
# version the way 1.x did, so serverInfo.version would arrive empty at clients. Report
# the aidress-mcp version instead — more useful to a caller than the transport library's.
#
# Two resolution paths because this file runs in two very different places:
#   - pip-installed (`aidress-mcp` on PyPI): importlib.metadata is authoritative, and can
#     legitimately differ from the constant below if someone installed an older wheel.
#   - source checkout (the hosted server on Render): no distribution is installed, so
#     metadata lookup raises and the constant is the only real answer. Returning "0.0.0"
#     here would mean the hosted endpoint reports no version to every client.
# release.sh keeps _FALLBACK_VERSION in step with pyproject.toml, same as _CLI_VERSION.
_FALLBACK_VERSION = "0.4.0"
try:
    _AIDRESS_MCP_VERSION = _pkg_version("aidress-mcp")
except Exception:
    _AIDRESS_MCP_VERSION = _FALLBACK_VERSION

mcp = MCPServer(
    "Aidress",
    version=_AIDRESS_MCP_VERSION,
    instructions=_AIDRESS_INSTRUCTIONS,
)

# ── Internal transport layer ─────────────────────────────────────────────────
# When mounted inside the FastAPI process (remote mode), tools call the ASGI app
# directly via httpx.AsyncClient(transport=ASGITransport(app)) — no network
# self-call, no worker deadlock.
# When running standalone (local mode), tools call the API over the network.

_asgi_client: httpx.AsyncClient | None = None
_standalone_client: httpx.AsyncClient | None = None


def set_asgi_app(app) -> None:
    """Called by main.py after mounting to enable in-process routing."""
    global _asgi_client
    _asgi_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
    )


def _get_standalone_client() -> httpx.AsyncClient:
    """Lazily create and reuse one httpx.AsyncClient for standalone/local (stdio) mode,
    so a real network deployment doesn't pay a fresh TCP+TLS handshake to BASE_URL on
    every single tool call. Only used when _asgi_client is unset (this file running
    standalone, not mounted inside main.py) — the hosted path already reuses
    _asgi_client for the identical reason. Left open for the life of the process;
    a local stdio server has no request-scoped lifecycle to tie a shutdown to.
    """
    global _standalone_client
    if _standalone_client is None:
        _standalone_client = httpx.AsyncClient()
    return _standalone_client


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


def _incoming_bearer_key(ctx: Optional[Context]) -> Optional[str]:
    """Read the CALLER's own Authorization: Bearer aidress-agent-sk-... header off the
    current MCP request, when one exists.

    Only populated on HTTP transports (streamable-http, SSE) — FastMCP attaches the
    raw Starlette Request to ctx.request_context.request per call. stdio (local,
    single-user) has no HTTP request, so this always returns None there and callers
    fall back to AGENT_KEY / _session_agent_key as before.

    This is what actually makes mcp-remote's `--header Authorization:Bearer ...`
    do something: previously nothing on the server read that header at all, so every
    remote caller shared the same server-wide identity (AGENT_KEY env var or whichever
    key someone last passed to set_agent_key).
    """
    if ctx is None:
        return None
    try:
        request = ctx.request_context.request
    except Exception:
        return None
    if request is None:
        return None
    auth = request.headers.get("authorization")
    if not auth or not auth.startswith("Bearer aidress-agent-sk-"):
        return None
    return auth.removeprefix("Bearer ")


def _incoming_org_key(ctx: Optional[Context]) -> Optional[str]:
    """Read the CALLER's own X-API-KEY header off the current MCP request, when one
    exists. Same idea and same shared-process problem as _incoming_bearer_key, but for
    the org key path (register_agent's auto-verify, rotate_agent_key, update_agent's
    org-key ownership check, list_org_agents): previously only the server-wide
    AIDRESS_API_KEY env var was ever consulted, so a caller's own org key sent on their
    MCP connection was silently ignored — and since that env var isn't set on the
    hosted server, those tools simply failed for everyone rather than using the
    caller's own key.
    """
    if ctx is None:
        return None
    try:
        request = ctx.request_context.request
    except Exception:
        return None
    if request is None:
        return None
    key = request.headers.get("x-api-key")
    if not key or not key.startswith("aidress-sk-"):
        return None
    return key


def _headers(include_api_key: bool = False, include_agent_key: bool = False, agent_key_override: Optional[str] = None, api_key_override: Optional[str] = None) -> dict:
    """Build request headers, optionally attaching the org API key and/or bearer agent key.

    Org key priority:    api_key_override (this request's own X-API-KEY header, see
                          _incoming_org_key) > API_KEY env var.
    Bearer key priority: agent_key_override (this request's own Authorization header, see
                          _incoming_bearer_key) > AGENT_KEY env var > _session_agent_key
                          (set via set_agent_key).
    """
    h = {"Content-Type": "application/json"}
    if include_api_key:
        key = api_key_override or API_KEY
        if key:
            h["X-API-KEY"] = key
    if include_agent_key:
        key = agent_key_override or AGENT_KEY or _session_agent_key
        if key:
            h["Authorization"] = f"Bearer {key}"
    return h


async def _post(path: str, body: dict, include_api_key: bool = False, include_agent_key: bool = False, extra_headers: dict | None = None, agent_key_override: Optional[str] = None, api_key_override: Optional[str] = None) -> dict | list:
    """POST to the Aidress API — in-process if mounted, over network if standalone.

    When include_agent_key=True: bearer key takes priority; falls back to HTTP sig if
    AIDRESS_KEYPAIR_PATH is configured and no bearer key is set.
    """
    try:
        h = _headers(include_api_key, include_agent_key, agent_key_override, api_key_override)
        if extra_headers:
            h.update(extra_headers)
        # Pre-serialize when signing so the digest covers exactly the bytes the server receives.
        # httpx re-serializes json= independently, so we must pass content= instead.
        effective_agent_key = agent_key_override or AGENT_KEY or _session_agent_key
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
            client = _get_standalone_client()
            if signing:
                resp = await client.post(f"{BASE_URL}{path}", content=body_bytes, headers=h, timeout=30.0)
            else:
                resp = await client.post(f"{BASE_URL}{path}", json=body, headers=h, timeout=30.0)
        return resp.json()
    except httpx.RequestError as exc:
        return {"error": f"Aidress API unreachable: {exc}"}


async def _get(path: str, include_api_key: bool = False, api_key_override: Optional[str] = None) -> dict | list:
    """GET from the Aidress API — in-process if mounted, over network if standalone."""
    try:
        if _asgi_client:
            resp = await _asgi_client.get(
                path, headers=_headers(include_api_key, api_key_override=api_key_override), timeout=30.0,
            )
        else:
            client = _get_standalone_client()
            resp = await client.get(
                f"{BASE_URL}{path}",
                headers=_headers(include_api_key, api_key_override=api_key_override), timeout=30.0,
            )
        return resp.json()
    except httpx.RequestError as exc:
        return {"error": f"Aidress API unreachable: {exc}"}


# ── Enum aliases ─────────────────────────────────────────────────────────────
# Literal types (not just Optional[str]) so the JSON schema sent to the model
# enumerates legal values and rejects a near-miss client-side, before any network call.
Protocol        = Literal["REST", "GraphQL", "gRPC"]
SettlementRail  = Literal["x402", "stripe", "manual"]
TermsFormat     = Literal["JSON", "XML"]
MessageProtocol = Literal["a2a", "mcp", "raw"]
HttpMethod      = Literal["GET", "POST"]


# ── Tools: Discovery & Verification ─────────────────────────────────────────

@mcp.tool()
async def verify_agent(agent_id: str) -> dict:
    """
    Look up an agent's trust profile by agent_id.

    NOT required after match_agents/list_registry — both already return this same
    trust object (trust_score, verified, flags, routing, payload_schema) for every
    result, so decide directly from there instead of re-fetching it here. Use this
    tool when you have an agent_id from somewhere else (named directly by a user or
    counterpart, not from match_agents/list_registry), or want a fresh read before a
    high-value action on data that might be stale.

    Returns trust_score (0–100), verified status, capabilities, flags,
    routing info, and payload_schema (the semantic conventions the agent
    expects: currency, date_format, quantity_unit, weight_unit).
    Returns a 404 error if the agent_id is not in the registry — treat this
    as "do not transact" (same as score 0).

    Trust tiers:
      1–49     — not trusted (40 = pending review) → do not transact
      50–69    — caution → proceed only with safeguards
      70–100   — trusted → proceed

    Always check payload_schema before calling an agent so your payload
    uses the correct currency, units, and date format.
    """
    return await _post("/verify", {"agent_id": agent_id})


@mcp.tool()
async def match_agents(
    capabilities: Optional[list[str]] = None,
    settlement_rail: Optional[SettlementRail] = None,
    org_name: Optional[str] = None,
    message_protocol: Optional[MessageProtocol] = None,
) -> list:
    """
    Find agents matching any combination of capability, settlement rail, org, or message
    protocol, ranked by a composite score (capability match + trust + success rate).

    match applies NO trust or verified gate — results can include unverified and
    low-trust agents, and an agent needs only ONE matching capability to appear. Each
    result already includes the full trust object (trust_score, verified, flags) —
    decide directly from that. No need to call verify_agent on a result too; it returns
    the same data. Use verify_agent only for an agent_id you don't have match/registry
    data for, or to force a fresh check before a high-value action.

    All four filters are optional, but at least one must be given. Agents must match
    every filter present in the call.
    capabilities     — list of capability names, e.g. ["freight_booking", "customs_clearance"]
    settlement_rail  — "x402", "stripe", "manual", or omit for any
    org_name         — exact match, case-insensitive
    message_protocol — "a2a", "mcp", or "raw" — restrict to agents whose endpoint speaks this format

    Returns a ranked list of trust objects. Each result includes payload_schema
    (currency, date_format, quantity_unit, weight_unit) so you know exactly what
    conventions the agent expects before you call it.

    If capabilities is omitted, capability match contributes nothing to ranking — results
    are ordered by trust/success-rate/transaction-count instead. First result is the best
    match. Check payload_schema on your chosen agent before sending a payload to avoid
    schema mismatch errors.
    """
    body: dict = {}
    if capabilities:
        body["required_capabilities"] = capabilities
    if settlement_rail:
        body["settlement_rail"] = settlement_rail
    if org_name:
        body["org_name"] = org_name
    if message_protocol:
        body["message_protocol"] = message_protocol
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
async def protocol_reference(
    topic: Literal[
        "mcp_handshake",
        "register_capability_confirmation",
        "register_advanced_fields",
        "call_agent_advanced_fields",
        "update_agent_advanced_fields",
    ]
) -> dict:
    """
    Look up the worked example for an edge-case protocol flow, on demand.

    Call this the FIRST time you actually hit the situation — not proactively
    every session. Keeps other tools' docstrings short by moving rarely-needed
    detail here instead of repeating it on every call.

    topic:
      "mcp_handshake" — you're about to call_agent a target whose message_protocol
                        is "mcp". Returns the two-step initialize -> tools/call
                        flow, including how to read and pass back mcp_session_id.
      "register_capability_confirmation" — register_agent just returned HTTP 202,
                        status "capability_confirmation_required". Returns the
                        two-step confirm/reject flow to complete registration.
      "register_advanced_fields" — you need one of register_agent's less-common
                        fields (signup_help, auth_header_name, a2a_compliant,
                        accepted_content_types, payload_schema, accepted_terms_format,
                        clone_from_agent_id).
      "call_agent_advanced_fields" — you need call_agent's `method` override (forcing
                        which HTTP method Aidress uses against a plain endpoint).
      "update_agent_advanced_fields" — you need update_agent's `pull_from_agent_id`
                        (sandbox-only: refresh a draft from its paired live agent).
    """
    reference = {
        "register_capability_confirmation": {
            "when": (
                "register_agent's response was HTTP 202 with status "
                "\"capability_confirmation_required\" — Aidress found an existing "
                "canonical capability close to one you submitted and paused "
                "registration to confirm the rename before proceeding."
            ),
            "step_1_initial_response": {
                "status": "capability_confirmation_required",
                "candidate_matches": {
                    "shoe_sales": "shoe_selling",
                    "fast_deliver": "express_delivery",
                    "_comment": "your raw name -> suggested canonical name",
                },
            },
            "step_2_recall": {
                "call": "register_agent(... same fields as before ..., capability_confirmations=<map below>, candidate_matches=<echoed from step 1>)",
                "capability_confirmations": {
                    "shoe_sales": True,
                    "fast_deliver": False,
                    "_comment": "True = accept suggested canonical name, False = keep your raw name as a new capability",
                },
                "candidate_matches": (
                    "Echo the candidate_matches dict from the 202 response verbatim "
                    "so the server can reuse the LLM suggestion without re-querying "
                    "(the LLM call is non-deterministic)."
                ),
            },
        },
        "register_advanced_fields": {
            "signup_help": (
                "Set ONLY if calling your endpoint requires the CALLER to supply its own "
                "third-party credential (e.g. your endpoint is a metered API like a flight "
                "or search API where each caller must use their own API key so quota is "
                "charged per caller, not to a shared key). Provide a link and/or short "
                "instructions telling a caller how to obtain their own credential, e.g. "
                "\"Sign up at https://ignav.com to get a free API key.\" Leave unset if your "
                "endpoint needs no per-caller credential."
            ),
            "auth_header_name": (
                "The header name a caller must use to send that credential, e.g. "
                "\"X-Api-Key\" or \"Authorization\" (for a bearer token, the caller sends "
                "the full value \"Bearer <token>\"). The caller places it under this name "
                "inside call_agent's forwarded_headers. Set alongside signup_help."
            ),
            "a2a_compliant": (
                "True if the endpoint speaks the A2A JSON-RPC envelope format. Only "
                "consulted when message_protocol is \"a2a\"."
            ),
            "accepted_content_types": (
                "MIME types the endpoint accepts, e.g. [\"application/json\"]. Defaults to "
                "[\"text/plain\", \"application/json\"] if omitted."
            ),
            "accepted_terms_format": "\"JSON\" or \"XML\" — the format your terms/pricing are published in, if any.",
            "payload_schema": (
                "Semantic conventions for this agent's payloads. Dict with any of: "
                "currency (e.g. \"USD\"), date_format (e.g. \"ISO8601\"), quantity_unit "
                "(e.g. \"individual_items\"), weight_unit (e.g. \"kg\"). Callers will see "
                "this before sending a payload so they can format it correctly."
            ),
            "clone_from_agent_id": (
                "SANDBOX ONLY (requires the org's sandbox_api_key). Pre-fills this new "
                "agent's config fields (capabilities, specialty, endpoint_url, protocol, "
                "settlement_rail, signup_help, auth_header_name, payload_schema, "
                "http_methods, org_domain, contact_info) from an existing LIVE "
                "(non-sandbox) agent your org owns, and permanently pairs the two — that "
                "pairing is required by preview_sandbox_match and promote_sandbox_agent, "
                "and cannot be established any other way. Never copies earned stats "
                "(trust_score, transaction_count, success_rate, verified, flags). Any "
                "other field also present in this call overrides the cloned value. 403 if "
                "clone_from_agent_id isn't a live agent your org's production api_key "
                "owns, or if this call isn't using a sandbox_api_key."
            ),
        },
        "call_agent_advanced_fields": {
            "method": (
                "Overrides which HTTP method AIDRESS itself uses for the outbound request "
                "to the target. NOT a header, and never relayed to the target (unrelated to "
                "forwarded_headers). Leave unset to keep the current auto-detection (the "
                "agent's registered http_methods[0]). Only affects plain endpoints — "
                "A2A-compliant and message_protocol=\"mcp\"/\"raw\" targets always receive "
                "POST regardless of this field."
            ),
        },
        "update_agent_advanced_fields": {
            "pull_from_agent_id": (
                "SANDBOX ONLY (requires the org's sandbox_api_key). Overwrites agent_id's "
                "config fields with its confirmed-paired live agent's CURRENT values — use "
                "this to refresh a sandbox draft after the live original has changed since "
                "you cloned it (see register_agent's clone_from_agent_id, which is the only "
                "way that pairing gets established). Only succeeds if pull_from_agent_id is "
                "already agent_id's confirmed pair (403 otherwise, even for an agent_id your "
                "org owns but isn't paired with). Any other field also present in this call "
                "overrides the pulled value. Earned stats are never touched either direction."
            ),
        },
        "mcp_handshake": {
            "when": (
                "Only for call_agent targets whose message_protocol is \"mcp\". Some "
                "MCP servers are stateful and require an initialize handshake before "
                "any tool call; stateless ones do not — try step 1 first either way."
            ),
            "step_1_initialize": {
                "call": "call_agent(agent_id, message_protocol=\"mcp\", payload=<payload below>)",
                "payload": {
                    "jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "my-agent", "version": "1"},
                    },
                },
                "then": (
                    "Read mcp_session_id from the RESULT. If it's absent, the server "
                    "is stateless — go to step 2 without it."
                ),
            },
            "step_2_tool_call": {
                "call": (
                    "call_agent(agent_id, message_protocol=\"mcp\", "
                    "mcp_session_id=\"<from step 1, if any>\", payload=<payload below>)"
                ),
                "payload": {
                    "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "<tool>", "arguments": {}},
                },
            },
            "note": "Step 1 is a handshake — it mints no transaction and needs no review.",
        },
    }
    return reference[topic]


@mcp.tool()
async def list_registry(limit: int = 50, offset: int = 0) -> list:
    """
    Browse all agents in the Aidress registry, paginated. Discovery is open —
    there is NO trust or verified gate (the only filter is a routable endpoint),
    so results can include unverified and low-trust agents. Each result already
    includes trust_score/verified/flags — decide from that directly; no need to
    call verify_agent on a result too (see verify_agent's docstring for when it's
    actually needed).

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
    org_name:                Optional[str]              = None,
    org_domain:              Optional[str]              = None,
    contact_info:            Optional[str]              = None,
    contact_email:           Optional[str]              = None,
    capabilities:            Optional[list[dict]]       = None,
    endpoint_url:            Optional[str]              = None,
    protocol:                Optional[Protocol]         = None,
    settlement_rail:         Optional[SettlementRail]   = None,
    specialty:               Optional[str]              = None,
    accepted_terms_format:   Optional[TermsFormat]      = None,
    message_protocol:        Optional[MessageProtocol]  = None,
    signup_help:             Optional[str]              = None,
    auth_header_name:        Optional[str]              = None,
    a2a_compliant:           Optional[bool]             = None,
    accepted_content_types:  Optional[list[str]]        = None,
    payload_schema:          Optional[dict]             = None,
    capability_confirmations: Optional[dict]            = None,
    candidate_matches:       Optional[dict]             = None,
    http_methods:            Optional[list[HttpMethod]] = None,
    clone_from_agent_id:     Optional[str]              = None,
    price_schedule:          Optional[list[dict]]       = None,
    payment_network:         Optional[str]              = None,
    payment_pay_to:          Optional[str]              = None,
    payment_asset:           Optional[str]              = None,
    ctx:                     Optional[Context]          = None,
) -> dict:
    """
    Register a new AI agent (or human) with the Aidress trust registry.

    Required:
      agent_id      — unique identifier for this agent (e.g. "my_agent_01")

    Conditionally required (when endpoint_url is set, i.e. registering an agent
    rather than a human demand-side participant):
      org_name      — your organisation name. One agent per org_domain.
      org_domain    — your domain (e.g. "acme.com").
      contact_email — required UNLESS an org key is supplied (X-API-KEY header on
                      this connection, or AIDRESS_API_KEY locally) — that also
                      auto-verifies the agent at trust_score=70 instead of 40
                      (pending review). TEMPORARY: agent_key is never returned
                      directly — you always get a claim_link back; pass its token
                      to claim_bearer_key to mint and receive the real key.

    Common optional fields:
      contact_info     — any contact channel: email, X/Twitter handle, GitHub URL,
                         Telegram, etc.
      capabilities     — list of strings or {"name", "weight"} dicts. weight 3
                         (USP, max 1), weight 2 (secondary, max 2), weight 1
                         (generic, max 3). Max 6 capabilities total.
      endpoint_url     — HTTPS URL accepting /call requests. Omit for a human.
      protocol         — "REST", "GraphQL", or "gRPC".
      settlement_rail  — "x402" (lets callers pay you at /call time), "stripe",
                         or "manual".
      specialty        — free-text description of what this agent does.
      message_protocol — how call_agent must shape payloads to reach you:
                         "a2a" (default) — Aidress wraps your payload in the A2A
                         JSON-RPC envelope. "mcp" — you're an MCP server; the
                         caller's MCP JSON-RPC message is forwarded verbatim.
                         "raw" — no fixed format; forwarded exactly as sent.
      http_methods     — defaults to ["POST"]; use ["GET"] for read-only lookup
                         agents (Aidress flattens the payload to query params).
      price_schedule   — self-declared per-task pricing, e.g.
                         [{"task": "search", "price": 0.01}, {"task": "deep_research",
                         "price": 0.4}]. Surfaced to callers via verify_agent/
                         match_agents (routing.price_schedule + routing.pay_via) so
                         they can pay you on their FIRST call instead of discovering
                         your price through a live 402 — fewer round-trips, faster
                         business for you. Requires payment_network/payment_pay_to/
                         payment_asset in this SAME call. Real 402 quotes are checked
                         against this schedule in the background; a mismatch gets
                         flagged for manual review.
      payment_network  — CAIP-2 network your price_schedule pays out on, e.g. "eip155:8453".
      payment_pay_to   — your receiving wallet address.
      payment_asset    — asset contract address you accept (e.g. USDC's contract).

    Less common fields — call protocol_reference("register_advanced_fields") if
    you need one of: signup_help, auth_header_name, a2a_compliant,
    accepted_content_types, payload_schema, accepted_terms_format ("JSON" or
    "XML"), clone_from_agent_id (sandbox cloning).

    If the response is HTTP 202 with status "capability_confirmation_required",
    call protocol_reference("register_capability_confirmation") for the two-step
    confirm/reject flow needed to complete registration.
    """
    body: dict = {"agent_id": agent_id}
    if org_name is not None:
        body["org_name"] = org_name
    if org_domain is not None:
        body["org_domain"] = org_domain
    if contact_info is not None:
        body["contact_info"] = contact_info
    if contact_email is not None:
        body["contact_email"] = contact_email
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
    if clone_from_agent_id is not None:
        body["clone_from_agent_id"] = clone_from_agent_id
    if price_schedule is not None:
        body["price_schedule"] = price_schedule
    if payment_network is not None:
        body["payment_network"] = payment_network
    if payment_pay_to is not None:
        body["payment_pay_to"] = payment_pay_to
    if payment_asset is not None:
        body["payment_asset"] = payment_asset

    return await _post(
        "/register", body, include_api_key=True,
        api_key_override=_incoming_org_key(ctx),
    )


@mcp.tool()
async def rotate_agent_key(agent_id: str, ctx: Optional[Context] = None) -> dict:
    """
    Request rotation of an agent's bearer key — the previous key stops working the moment
    the new one is actually claimed (see claim_bearer_key).

    Auth: an org key that owns this agent skips a check that this agent has a
    contact_email on file (that check is otherwise required, 400 if missing). On the
    hosted remote connector, send your org's X-API-KEY header on the MCP connection
    itself; locally, set AIDRESS_API_KEY in the server environment.

    TEMPORARY (short-term server-side change): agent_key is currently NEVER returned
    directly here, even with an org key — the response instead has a claim_link (and
    agent_key: None) regardless of credentials. Pass the token from that link to
    claim_bearer_key to actually mint and receive the key.

    agent_id — the agent whose bearer key to rotate.

    Returns an error (400) if the agent has no contact_email on file and no org key was
    used, (404) if agent_id doesn't exist, or (429) if a claim link was requested too
    recently for this agent.
    """
    return await _post(
        "/rotate", {"agent_id": agent_id}, include_api_key=True,
        api_key_override=_incoming_org_key(ctx),
    )


@mcp.tool()
async def claim_bearer_key(token: str) -> dict:
    """
    Redeem a claim-token link's token (from register_agent's or rotate_agent_key's
    claim_link field) and receive the actual bearer key. This is the GET /rotate?token=...
    step — the only place a key is currently minted (see the TEMPORARY notes on
    register_agent/rotate_agent_key).

    token — everything after "token=" in the claim_link URL, or the whole URL (either
            works; the query string is parsed out if present).

    Returns an error (400) if the token is invalid or already used. Does NOT auto-store
    the returned key for this session — call set_agent_key with it afterward if you want
    subsequent update_agent/call_agent/review_transaction calls to authenticate with it.
    """
    if "token=" in token:
        token = token.rsplit("token=", 1)[-1]
    return await _get(f"/rotate?token={urllib.parse.quote(token, safe='')}")


@mcp.tool()
async def update_agent(
    agent_id:               str,
    org_name:               Optional[str]              = None,
    org_domain:             Optional[str]              = None,
    contact_info:           Optional[str]              = None,
    contact_email:          Optional[str]              = None,
    capabilities:           Optional[list[dict]]       = None,
    specialty:              Optional[str]              = None,
    endpoint_url:           Optional[str]              = None,
    protocol:               Optional[Protocol]         = None,
    accepted_terms_format:  Optional[TermsFormat]      = None,
    settlement_rail:        Optional[SettlementRail]   = None,
    payload_schema:         Optional[dict]             = None,
    message_protocol:       Optional[MessageProtocol]  = None,
    signup_help:            Optional[str]              = None,
    auth_header_name:       Optional[str]              = None,
    a2a_compliant:          Optional[bool]             = None,
    accepted_content_types: Optional[list[str]]        = None,
    http_methods:           Optional[list[HttpMethod]] = None,
    pull_from_agent_id:     Optional[str]              = None,
    price_schedule:         Optional[list[dict]]       = None,
    payment_network:        Optional[str]              = None,
    payment_pay_to:         Optional[str]              = None,
    payment_asset:          Optional[str]              = None,
    ctx:                    Optional[Context]          = None,
) -> dict:
    """
    Update an existing agent's profile fields. Only provided fields are changed;
    omitted fields remain unchanged.

    Auth: any one of —
      - Bearer agent key: on the hosted remote connector, send your own
        Authorization: Bearer <agent_key> header on the MCP connection (this is
        what it authenticates with automatically). Locally: set AIDRESS_AGENT_KEY
        env var before starting the server, or call set_agent_key("<key>") once
        in-session after registering.
      - Ed25519 keypair:  set AIDRESS_KEYPAIR_PATH (HTTP Message Signature, RFC 9421)
      - Org key:          must own this agent. On the hosted remote connector, send
        your org's X-API-KEY header on the MCP connection itself; locally, set
        AIDRESS_API_KEY in the server environment.
    Per-call key parameters are intentionally absent — bearer tokens passed as
    tool arguments appear in conversation history and MCP protocol trace logs.

    agent_id       — the agent to update (cannot be changed)

    contact_email — where rotate_agent_key's claim-token link is sent when this
                   agent's key is rotated without an org/admin credential.

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
                             your endpoint requires one (see
                             protocol_reference("register_advanced_fields") for details).
    auth_header_name       — header name callers use to send that credential inside
                             forwarded_headers (e.g. "X-Api-Key", "Authorization").
    a2a_compliant          — True if the endpoint speaks the A2A JSON-RPC envelope format
    accepted_content_types — MIME types the endpoint accepts, e.g. ["application/json"]

    pull_from_agent_id — SANDBOX ONLY; refreshes a sandbox draft from its paired live
                        agent's current values. See
                        protocol_reference("update_agent_advanced_fields").

    price_schedule, payment_network, payment_pay_to, payment_asset — see register_agent;
    same fields, same rule (all three payment_* fields required together whenever
    price_schedule is set in this call).

    Returns the updated trust object.
    """
    body: dict = {"agent_id": agent_id}
    if org_name is not None:
        body["org_name"] = org_name
    if org_domain is not None:
        body["org_domain"] = org_domain
    if contact_info is not None:
        body["contact_info"] = contact_info
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
    if pull_from_agent_id is not None:
        body["pull_from_agent_id"] = pull_from_agent_id
    if price_schedule is not None:
        body["price_schedule"] = price_schedule
    if payment_network is not None:
        body["payment_network"] = payment_network
    if payment_pay_to is not None:
        body["payment_pay_to"] = payment_pay_to
    if payment_asset is not None:
        body["payment_asset"] = payment_asset

    return await _post(
        "/update", body, include_api_key=True, include_agent_key=True,
        agent_key_override=_incoming_bearer_key(ctx),
        api_key_override=_incoming_org_key(ctx),
    )


@mcp.tool()
async def preview_sandbox_match(
    sandbox_agent_id: str,
    required_capabilities: list[str],
    settlement_rail: Optional[SettlementRail] = None,
    ctx: Optional[Context] = None,
) -> dict:
    """
    Preview exactly where a sandbox agent's tested config would rank against REAL, live
    competition — before you actually promote it. Requires the org's sandbox_api_key (on
    the hosted remote connector, send it as your MCP connection's X-API-KEY header;
    locally, set AIDRESS_API_KEY in the server environment).

    sandbox_agent_id — must already have a confirmed live counterpart (see
                       register_agent's clone_from_agent_id) — 403 otherwise.
    required_capabilities — same capability-matching semantics as match_agents.
    settlement_rail — optional filter on the real competitor set: "x402", "stripe",
                      "manual", or omit for any.

    What gets compared: the sandbox agent's own config (capabilities, specialty,
    endpoint, etc. — exactly what promote_sandbox_agent would copy), but its
    trust_score/transaction_count/success_rate/verified are drawn from the LIVE
    counterpart's CURRENT values instead (promotion never changes those). Real
    competitors are pulled from production (verified=true, trust_score>=50); the live
    counterpart itself is excluded from that competitor list (post-promotion it IS this
    draft, not a separate agent). Nothing here is written anywhere — the draft's ranking
    entry exists only for the duration of this call.

    Returns results (ranked list, draft included at its earned position),
    draft_agent_id, a short factual explanation of the ranking gap (or null if the LLM
    call failed — never blocks results), and a disclaimer about where the draft's stats
    came from.
    """
    body = {"sandbox_agent_id": sandbox_agent_id, "required_capabilities": required_capabilities}
    if settlement_rail:
        body["settlement_rail"] = settlement_rail
    return await _post(
        "/sandbox/preview_match", body, include_api_key=True,
        api_key_override=_incoming_org_key(ctx),
    )


@mcp.tool()
async def promote_sandbox_agent(
    sandbox_agent_id: str,
    real_agent_id: str,
    ctx: Optional[Context] = None,
) -> dict:
    """
    Push a sandbox agent's tested config onto its paired live agent — the way a
    sandbox-tested change actually goes live. Requires the org's sandbox_api_key (on the
    hosted remote connector, send it as your MCP connection's X-API-KEY header; locally,
    set AIDRESS_API_KEY in the server environment).

    sandbox_agent_id and real_agent_id must already be each other's CONFIRMED paired
    agent (established only by a prior register_agent clone_from_agent_id call) — any
    unrelated pair, even two agents your own org owns, is rejected with 403.

    What moves: capabilities, specialty, endpoint_url, protocol, settlement_rail,
    org_domain, signup_help, auth_header_name, payload_schema, http_methods.
    What never moves: trust_score, transaction_count, success_rate, verified, flags,
    org_name, org_id — the live agent's earned identity and reputation are untouched.
    Every promotion is logged (fields copied, when, which org) for audit purposes.

    Consider calling preview_sandbox_match first to see how this config would actually
    rank before committing to it.

    Returns sandbox_agent_id, real_agent_id, fields_copied (list of field names actually
    written), and promoted_at.
    """
    return await _post(
        "/sandbox/promote",
        {"sandbox_agent_id": sandbox_agent_id, "real_agent_id": real_agent_id},
        include_api_key=True,
        api_key_override=_incoming_org_key(ctx),
    )


@mcp.tool()
async def set_agent_key(agent_key: str) -> dict:
    """
    Store a bearer agent key for the duration of this MCP session.

    Use this immediately after register_agent returns an agent_key — it lets
    update_agent, call_agent, and review_transaction
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

    On the hosted remote connector (api.aidress.ai), this key is stored in a
    process-wide slot shared by every remote caller currently connected — avoid
    this tool there. Instead send your own Authorization: Bearer <agent_key>
    header on the MCP connection itself; update_agent/call_agent/review_transaction
    read that per-request and it always wins over anything set here. This tool
    remains correct for a local single-user stdio server, where there is exactly
    one caller.

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


def _build_v1_network_caip2_map() -> dict[str, str]:
    """Legacy network name → CAIP-2 (e.g. "base-sepolia" → "eip155:84532"). Pulled from
    the x402 SDK's own tables so it can't drift; hardcoded subset is the fallback."""
    mapping: dict[str, str] = {
        "base": "eip155:8453", "base-sepolia": "eip155:84532",
        "polygon": "eip155:137", "polygon-amoy": "eip155:80002",
        "avalanche": "eip155:43114", "avalanche-fuji": "eip155:43113",
    }
    try:
        from x402.mechanisms.evm.v1.constants import V1_NETWORK_CHAIN_IDS
        mapping.update({name: f"eip155:{chain_id}" for name, chain_id in V1_NETWORK_CHAIN_IDS.items()})
    except Exception:
        pass
    try:
        from x402.mechanisms.svm.constants import V1_TO_V2_NETWORK_MAP
        mapping.update(V1_TO_V2_NETWORK_MAP)
    except Exception:
        pass
    return mapping


_V1_NETWORK_TO_CAIP2 = _build_v1_network_caip2_map()


def _normalize_payment_requirement_item(item: dict) -> dict:
    """One `accepts[]` entry, v1 → v2: maxAmountRequired→amount, legacy network→CAIP-2,
    drop the v1 per-item string `resource` (v2 has it once at the body's top level)."""
    if not isinstance(item, dict):
        return item
    out = dict(item)
    out.pop("resource", None)
    if "amount" not in out and "maxAmountRequired" in out:
        out["amount"] = out.pop("maxAmountRequired")
    network = out.get("network")
    if isinstance(network, str) and ":" not in network:
        out["network"] = _V1_NETWORK_TO_CAIP2.get(network, network)
    return out


def _normalize_payment_required_body(body):
    """Reshape a passthrough 402 body to v2 regardless of what the receiver actually sent.
    Body only — never touches the payment-required header, since a wallet signs off that
    header verbatim and rewriting it would break verification on the receiver's side."""
    if not isinstance(body, dict) or not isinstance(body.get("accepts"), list):
        return body
    out = dict(body)
    out["accepts"] = [_normalize_payment_requirement_item(a) for a in body["accepts"]]
    if not isinstance(out.get("resource"), dict):
        v1_item = next(
            (a for a in body["accepts"] if isinstance(a, dict) and isinstance(a.get("resource"), str)),
            None,
        )
        if v1_item is not None:
            out["resource"] = {
                "url": v1_item["resource"],
                **({"description": v1_item["description"]} if v1_item.get("description") else {}),
                **({"mimeType": v1_item["mimeType"]} if v1_item.get("mimeType") else {}),
            }
    out["x402Version"] = 2
    return out


@mcp.tool()
async def call_agent(
    agent_id:          str,
    payload:           dict,
    caller_agent_id:   str,
    x_payment:         Optional[str] = None,
    message_protocol:  Optional[MessageProtocol] = None,
    mcp_session_id:    Optional[str] = None,
    forwarded_headers: Optional[dict] = None,
    method:            Optional[HttpMethod] = None,
    ctx:               Optional[Context] = None,
) -> dict:
    """
    Send a request to a registered agent through the Aidress proxy.

    All calls are logged. Submit review_transaction within 24h — check
    review_reminder in the response; skip only if it says "no review needed".

    agent_id        — the agent to call.
    message_protocol — the target's format, from verify_agent/match_agents'
                      `message_protocol` field:
                        "a2a" (default) — payload is a plain business-data dict; this tool
                                          wraps it in a DataPart automatically.
                        "mcp"           — payload IS a complete MCP JSON-RPC message, sent
                                          verbatim. Stateful targets need an initialize
                                          handshake first — call
                                          protocol_reference("mcp_handshake") before your
                                          first attempt on a new target.
                        "raw"           — payload is the exact body the target's own docs
                                          specify, sent verbatim.
                      Always use the value from the agent's trust object — mis-declaring it
                      returns 422.
    mcp_session_id  — session token from a prior initialize call. Only for
                      message_protocol="mcp"; see protocol_reference("mcp_handshake").
    forwarded_headers — headers relayed VERBATIM to the target, only when its trust object
                      has a `signup_help` (it needs the CALLER's own third-party credential,
                      under the header named in `auth_header_name`). A 401/403 from an agent
                      with signup_help is the signal to get your own credential and retry
                      with it here. Reserved headers (X-Payment, Mcp-Session-Id, Host,
                      Content-*) are ignored.
    method          — rarely needed; overrides the outbound HTTP method Aidress uses
                      against the target. See protocol_reference("call_agent_advanced_fields").
    payload         — business data (message_protocol="a2a") or the exact protocol message
                      (message_protocol="mcp"/"raw"). Check payload_schema on the agent first
                      — mismatched currency/units/date format returns 409.
    caller_agent_id — REQUIRED: your agent's ID. Must match your set agent key or /call
                      rejects the request (401 missing/invalid key, 403 mismatch). No
                      anonymous calls.
    x_payment       — Leave UNSET in normal use — only for a pre-signed x402 PaymentPayload
                      (V2) if you're driving your own wallet manually. On a 402 without
                      x_payment, the result carries a `payment.pay_via` proxy URL instead —
                      see the server's payment-flow instructions (shown at session start)
                      for how to use it.
                      SKIP THE 402 ENTIRELY: if verify_agent/match_agents already returned
                      this agent's `routing.price_schedule` + `routing.pay_via`, sign a
                      PaymentPayload yourself for the matching task's declared price and
                      pass it here as x_payment on your FIRST call — no discovery round-trip.

    Auth (REQUIRED): on the hosted remote connector, your own Authorization: Bearer
    <agent_key> header on the MCP connection is used automatically. Locally: set
    AIDRESS_AGENT_KEY env var, call set_agent_key(...) once in-session, or configure
    AIDRESS_KEYPAIR_PATH. Per-call key parameters are intentionally absent — bearer
    tokens as tool arguments would appear in conversation history and trace logs.

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
    body: dict = {"agent_id": agent_id, "message": message, "caller_agent_id": caller_agent_id}
    if forwarded_headers:
        body["forwarded_headers"] = forwarded_headers
    if method:
        body["method"] = method

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
        agent_key_override=_incoming_bearer_key(ctx),
    )

    # Normalize the passthrough 402 body to v2 shape, whether or not x_payment was set.
    if isinstance(result, dict) and result.get("status_code") == 402:
        result["body"] = _normalize_payment_required_body(result.get("body"))

    # Payment required and the caller didn't pre-sign one. Aidress never holds funds, so
    # rather than signing here we hand back the transparent /pay proxy URL for this agent.
    # The caller drives its OWN wallet against pay_via: Aidress relays the sign → retry
    # loop to the counterpart, the counterpart settles, and Aidress records the outcome.
    if (not x_payment and isinstance(result, dict)
            and result.get("status_code") == 402):
        result["payment"] = {
            "required": True,
            # call_ref ties this payment back to THIS authenticated /call so the
            # settlement inherits the caller_agent_id that /call already verified —
            # without it, /pay has no trusted caller identity and the exchange can't
            # be reviewed. Attribution comes from the authenticated call, not a
            # spoofable query param, so it stays framing-safe.
            "pay_via":  f"{BASE_URL}/pay/{agent_id}?call_ref={result.get('transaction_id')}",
            # Full nonce/single-call/never-point-at-real-endpoint rules are already in the
            # server instructions (sent once at session start) — restate only the pointer
            # plus the one thing that's new here (the review_transaction reminder), instead
            # of repeating the whole explanation on every 402 in the conversation.
            "how": (
                "See the x402 payment flow in your session instructions — call your wallet "
                "tool once, pointed at pay_via, in a single call. After it settles, submit "
                "review_transaction if you have the transaction_id."
            ),
            "payment_required": (result.get("response_headers") or {}).get("payment-required"),
        }
        return result

    return result


@mcp.tool()
async def review_transaction(
    caller_agent_id:   str,
    receiver_agent_id: str,
    success:           bool,
    score:             int,
    ctx:               Optional[Context] = None,
) -> dict:
    """
    Submit a trust review after a confirmed exchange with another agent.

    The system automatically finds the most recent unreviewed executed exchange
    between the two agents — no transaction_id needed. Reviews without a real
    prior /call exchange are rejected.

    caller_agent_id   — the agent submitting the review (must match your bearer key)
    receiver_agent_id — the agent being reviewed
    success           — True if the transaction completed successfully
    score             — trust rating 1 (very poor) to 10 (excellent)

    Auth (always required): on the hosted remote connector, your own
      Authorization: Bearer <agent_key> header on the MCP connection is used
      automatically. Locally: set AIDRESS_AGENT_KEY env var before starting the
      server, or call set_agent_key("<key>") once in-session after registering,
      or configure AIDRESS_KEYPAIR_PATH for Ed25519 HTTP Message Signatures
      (RFC 9421).

    Anti-gaming rules enforced:
      - Caller trust_score must be >= 50
      - Cannot review your own agent
      - Cannot review agents from the same org domain (collusion block)
      - One review per executed exchange
      - No single org contributes more than 20% of an agent's rating influence; unaffiliated agents (no org_domain) are each capped at 10%

    Returns the updated trust object for the reviewed agent.
    """
    body: dict = {
        "caller_agent_id":   caller_agent_id,
        "receiver_agent_id": receiver_agent_id,
        "success":           success,
        "score":             score,
    }
    return await _post(
        "/review", body, include_agent_key=True,
        agent_key_override=_incoming_bearer_key(ctx),
    )



# ── Tools: Org Management ───────────────────────────────────────────────────

@mcp.tool()
async def list_org_agents(ctx: Optional[Context] = None) -> list:
    """
    List all agents registered under your org API key.

    On the hosted remote connector, send your org's X-API-KEY header on the MCP
    connection itself. Locally, set AIDRESS_API_KEY in the server environment.
    Returns all agents belonging to your organisation, including unverified ones.
    """
    org_key = _incoming_org_key(ctx)
    if not org_key and not API_KEY:
        return [{"error": "No org API key found. Send your org's X-API-KEY header on your MCP connection (or set AIDRESS_API_KEY in a local server's environment)."}]
    return await _get("/org/agents", include_api_key=True, api_key_override=org_key)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    """CLI entry point for `aidress-mcp` command (installed via pip)."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

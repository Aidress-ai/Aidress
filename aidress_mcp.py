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
#   AIDRESS_BASE_URL  — API base URL (default: https://api.aidress.ai)
#   AIDRESS_API_KEY   — Org API key for register (auto-verify), update, and
#                       list_org_agents. Leave unset to use public-only tools.

from __future__ import annotations

import os
from typing import Optional

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

# ── Config ────────────────────────────────────────────────────────────────────

BASE_URL = os.environ.get("AIDRESS_BASE_URL", "https://api.aidress.ai").rstrip("/")
API_KEY  = os.environ.get("AIDRESS_API_KEY")   # None if not provided

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


def _headers(include_api_key: bool = False) -> dict:
    """Build request headers, optionally attaching the org API key."""
    h = {"Content-Type": "application/json"}
    if include_api_key and API_KEY:
        h["X-API-KEY"] = API_KEY
    return h


async def _post(path: str, body: dict, include_api_key: bool = False) -> dict | list:
    """POST to the Aidress API — in-process if mounted, over network if standalone."""
    try:
        if _asgi_client:
            resp = await _asgi_client.post(
                path, json=body, headers=_headers(include_api_key), timeout=30.0,
            )
        else:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{BASE_URL}{path}", json=body,
                    headers=_headers(include_api_key), timeout=30.0,
                )
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

    Returns trust_score (0–100), verified status, capabilities, flags, and
    routing info. Trust tiers:
      0        — unregistered, block
      40       — pending review, proceed with caution
      50–69    — caution, apply limits
      70–100   — trusted, proceed

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

    Returns a ranked list of trust objects. First result is the best match.
    Use verify_agent on your chosen agent before initiating a transaction.
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
    agent_id:              str,
    org_name:              str,
    org_domain:            str,
    contact_email:         str,
    capabilities:          Optional[list[dict]] = None,
    endpoint_url:          Optional[str]        = None,
    protocol:              Optional[str]        = None,
    settlement_rail:       Optional[str]        = None,
    specialty:             Optional[str]        = None,
    accepted_terms_format: Optional[str]        = None,
) -> dict:
    """
    Register a new AI agent with the Aidress trust registry.

    Required:
      agent_id       — unique identifier for this agent (e.g. "my_agent_01")
      org_name       — your organisation name (e.g. "Acme Corp")
      org_domain     — your domain (e.g. "acme.com") — one agent per domain
      contact_email  — contact email for this agent

    Optional:
      capabilities   — list of capabilities. Each can be a plain string like
                        "freight_booking" or a dict with name and weight like
                        {"name": "freight_booking", "weight": 2}. Weight defaults
                        to 1. Max 1 capability at weight >= 3, max 2 at weight 2.
      endpoint_url   — HTTPS URL where this agent accepts /call requests
      protocol       — "REST", "GraphQL", or "gRPC"
      settlement_rail — "x402", "stripe", or "manual"
      specialty      — free-text description of what this agent does
      accepted_terms_format — "JSON" or "XML"

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

    return await _post("/register", body, include_api_key=True)


@mcp.tool()
async def update_agent(
    agent_id:              str,
    org_name:              Optional[str]  = None,
    org_domain:            Optional[str]  = None,
    contact_email:         Optional[str]  = None,
    capabilities:          Optional[list[dict]] = None,
    specialty:             Optional[str]  = None,
    endpoint_url:          Optional[str]  = None,
    protocol:              Optional[str]  = None,
    accepted_terms_format: Optional[str]  = None,
    settlement_rail:       Optional[str]  = None,
) -> dict:
    """
    Update an existing agent's profile fields. Only provided fields are changed;
    omitted fields remain unchanged.

    Requires AIDRESS_API_KEY — the key must belong to the org that owns the agent.

    agent_id       — the agent to update (cannot be changed)

    Updatable fields:
      org_name, org_domain, contact_email, specialty, endpoint_url,
      protocol, accepted_terms_format, settlement_rail, capabilities

    capabilities accepts the same format as register_agent — plain strings
    or {"name": "...", "weight": N} dicts.

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

    return await _post("/update", body, include_api_key=True)


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

    agent_id        — the agent to call
    payload         — JSON body to forward to the agent (max 64 KB)
    caller_agent_id — your agent's ID (optional but recommended for accountability)

    Returns the agent's response wrapped in a CallResponse with the HTTP status code.
    """
    body: dict = {"agent_id": agent_id, "payload": payload}
    if caller_agent_id:
        body["caller_agent_id"] = caller_agent_id
    return await _post("/call", body)


@mcp.tool()
async def review_transaction(
    transaction_id:    str,
    caller_agent_id:   str,
    receiver_agent_id: str,
    success:           bool,
    score:             int,
) -> dict:
    """
    Submit a trust review after completing a transaction with another agent.
    This is mandatory — agents that don't review within 24h are penalised.

    transaction_id     — unique ID for this transaction (you generate this)
    caller_agent_id    — your agent's ID (the one that initiated the transaction)
    receiver_agent_id  — the agent you transacted with
    success            — True if the transaction completed successfully
    score              — trust rating 1 (very poor) to 5 (excellent)

    Anti-gaming rules enforced:
      - Your trust_score must be >= 50 to submit reviews
      - Cannot review your own agent
      - Cannot review agents from the same org domain (collusion block)
      - One review per transaction_id

    Returns the updated trust object for the reviewed agent.
    """
    return await _post("/review", {
        "transaction_id":    transaction_id,
        "caller_agent_id":   caller_agent_id,
        "receiver_agent_id": receiver_agent_id,
        "success":           success,
        "score":             score,
    })


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

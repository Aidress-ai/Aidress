from __future__ import annotations

# aidress_sdk.py — Lightweight Aidress client SDK
#
# Drop this single file into any Python project to add Aidress trust verification.
# The simplest possible integration is two lines:
#
#   from aidress_sdk import verify
#   trust = verify("agent_id_here")
#
# Auth options (for mutating calls: /review, /call, /update):
#
#   Bearer key (Phase 1) — returned at /register; pass agent_key= to AidressClient
#   or call set_agent_key() for pre-existing agents:
#
#     from aidress_sdk import set_agent_key, call, review
#     set_agent_key("aidress-agent-sk-...")
#
#   HTTP Message Signatures (Phase 2) — Ed25519 keypair; more secure, no bearer token needed:
#
#     from aidress_sdk import generate_keypair, AidressClient
#     pub = generate_keypair("my_agent_01", "~/.aidress/keypair.json")
#     # Register pub key via /register public_key= or /update public_key=
#     client = AidressClient(keypair_path="~/.aidress/keypair.json")
#
# No external dependencies beyond Python stdlib; cryptography is optional (Phase 2 only).
#
# Prefer the terminal? The same functionality is exposed as the `aidress` CLI
# (see aidress_cli.py), installed alongside this SDK by the `aidress` pip package:
#
#     aidress verify agent_freightbot_01
#     aidress match freight_booking customs_clearance --rail x402
#     aidress --key aidress-agent-sk-... call agent_freightbot_01 '{"action":"book"}'
#
# HOW TO USE AIDRESS CORRECTLY: read ENGAGEMENT_PROTOCOL below (or print it with
# `python -c "import aidress_sdk; print(aidress_sdk.ENGAGEMENT_PROTOCOL)"`). It is
# the same discover → verify → decide → transact → review flow the MCP server
# surfaces to agents, kept deliberately in lockstep so the SDK, CLI, and MCP give
# identical guidance. If you change one, change the others.

import hashlib
import os
import urllib.request
import urllib.error
import json
import time

# The error object returned whenever Aidress is unreachable or returns an
# unexpected response — safe defaults so callers can always read trust_score.
_UNREACHABLE = {"error": "Aidress unreachable", "verified": False, "trust_score": 0}

# Cross-cutting engagement protocol — the SDK-facing twin of the MCP server's
# `instructions` block (aidress_mcp.py::_AIDRESS_INSTRUCTIONS). Same substance
# (the standard flow, trust tiers, anti-gaming rules, and the 402/payment flow),
# worded for SDK method names instead of MCP tool names. Exposed as a public
# constant so an agent building on the SDK can read the same guidance an MCP
# agent gets at initialize. Keep this in lockstep with the MCP instructions —
# any change to one is a change to both, or the two integration paths drift.
ENGAGEMENT_PROTOCOL = """\
Aidress is a trust registry for autonomous AI agents — verify an unknown counterpart
before you transact with it, then report the outcome so the network stays accurate.

THE STANDARD FLOW (follow it in order):
  1. DISCOVER  — find counterparts with match(capabilities) or registry().
  2. VERIFY    — ALWAYS call verify(agent_id) on a counterpart before transacting.
                 Never transact with an agent you have not verified, even if it
                 appeared in match() or registry() results — neither applies any
                 trust or verified gate, so both can return unverified and low-trust
                 agents (the only filter is a routable endpoint).
  3. DECIDE    — act on the trust_score and flags from verify():
                   0          unregistered → DO NOT transact.
                   1–49       not trusted (40 = pending review) → DO NOT transact.
                   50–69      caution → proceed ONLY with safeguards (cap value, use
                              escrow/staged delivery, or get human sign-off).
                   70–100     trusted → proceed.
                 Any entry in `flags` is a warning — investigate before proceeding,
                 regardless of score.
  4. TRANSACT  — route the interaction through call(agent_id, payload) (it logs the
                 call, hides the endpoint, and handles payment/settlement).
  5. REVIEW    — MANDATORY. After every call(), submit
                 review(success, score) within 24h using the transaction_id you were
                 given (auto-filled from the cached handle after call()).
                 Missing this costs the caller 2 trust points — waived if your org (or you,
                 as an unaffiliated agent) has already used up its influence cap on that
                 receiver (see below). Warnings are logged at 18h, 12h, and 6h remaining.
                 Report the outcome truthfully (honest success flag + 1–10 score) —
                 accurate signals are the whole point; gaming is blocked and penalised.

RULES THAT PREVENT COMMON MISTAKES:
  • Use the server-minted transaction_id returned by call().
    Do NOT invent your own ID — reviews are keyed to it.
  • One review per transaction_id. You cannot review yourself, cannot review an agent
    in your own org domain (collusion block), and your own trust_score must be >= 50 to
    submit reviews.
  • Rating influence caps: org-affiliated agents are capped at 20% per org domain (equal
    1/n share until there are 5+ rating orgs); unaffiliated agents (no org_domain) are
    each capped at 10% of total influence. Once your cap is reached for a given receiver,
    further reviews add nothing — the 24h missed-review penalty is waived for those calls.
  • call() REQUIRES caller_agent_id AND authentication: you must set your agent key (bearer
    via agent_key=/set_agent_key(), or a configured keypair) and it must match caller_agent_id.
    Anonymous proxy use is not permitted — /call returns 401 if the key is missing/invalid and
    403 if it does not match caller_agent_id.
  • Registration: one agent per org_domain. If register() returns status
    "capability_confirmation_required" (202), resubmit with capability_confirmations to
    confirm/reject the suggested canonical names. Save the agent_key from registration —
    it is shown once and never again.

ENGAGING EXTERNAL COUNTERPARTS CORRECTLY:
  • Stay within what the counterpart advertises — only request capabilities it lists, and
    settle only on a settlement_rail it accepts.
  • If a counterpart demands payment, call() returns status_code 402 with decoded terms in
    response_headers["payment-required"]. Aidress facilitates payment but never holds,
    signs, or moves funds — and you MUST route payment through Aidress so it is tracked:
      – Wallet-driven (recommended): point your OWN x402 wallet client at the transparent
        proxy {base_url}/pay/{agent_id} and send the same payload; it runs its normal
        402 → sign → retry loop while the counterpart settles on its own rail.
      – Manual: build an EIP-3009 X-Payment proof yourself and retry the SAME call() with
        x_payment=<proof>, which Aidress forwards to the counterpart verbatim.
    NEVER point your wallet at the counterpart's real endpoint — paying it directly works
    but is invisible to Aidress (no tracking, no transaction record).
  • Treat verify() as a pre-flight check on EVERY new counterpart and before any
    high-value action with an existing one — trust changes over time.
"""


def _parse_body(raw_bytes: bytes, status_code: int) -> dict:
    """
    Safely decode an HTTP response body to a dict.
    Falls back to a plain error dict if the body is empty or non-JSON
    (e.g. an HTML gateway error page from a hosting proxy).
    """
    raw = raw_bytes.decode("utf-8", errors="replace").strip()
    if not raw:
        return {"detail": f"HTTP {status_code} (empty body)"}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"detail": f"HTTP {status_code} — non-JSON response from server"}


# ── AidressClient ─────────────────────────────────────────────────────────────

class AidressClient:
    """
    A thin wrapper around the Aidress REST API.

    Create one instance per agent and reuse it across calls:
        client = AidressClient()                          # uses live API
        client = AidressClient("http://localhost:8000")   # points at local server
    """

    def __init__(
        self,
        base_url: str = "https://api.aidress.ai",
        agent_key: str | None = None,
        keypair_path: str | None = None,
    ):
        # Strip trailing slash so callers don't need to worry about formatting
        self.base_url = base_url.rstrip("/")
        # In-session handle cache — stores the most recent server-minted transaction_id
        # so review() can be called with no arguments after call()
        self._last_handle: str | None = None
        # Transport diagnostics — set to the failure reason (e.g. an SSL cert error)
        # when the most recent request could not reach the server, else None. Lets
        # callers distinguish "server said empty" from "never reached the server",
        # which list-returning methods (match/registry) otherwise both flatten to [].
        self.last_error: str | None = None
        # Ed25519 keypair fields — populated by _load_keypair
        self._private_key = None
        self._keypair_agent_id: str | None = None
        self._public_key_b64: str | None = None

        # Auth priority chain (highest → lowest):
        #   1. Explicit agent_key param
        #   2. AIDRESS_AGENT_KEY env var
        # Bearer takes priority over signatures — if both are available, bearer is used.
        self._agent_key: str | None = agent_key or os.environ.get("AIDRESS_AGENT_KEY")

        # Keypair priority chain:
        #   1. Explicit keypair_path param  → raise on failure (caller asked for it)
        #   2. AIDRESS_KEYPAIR_PATH env var → raise on failure (caller configured it)
        #   3. ~/.aidress/keypair.json      → silent skip if absent or cryptography missing
        resolved_path: str | None = keypair_path or os.environ.get("AIDRESS_KEYPAIR_PATH")
        if resolved_path:
            self._load_keypair(resolved_path)  # explicit — let errors surface
        else:
            from pathlib import Path
            default = Path("~/.aidress/keypair.json").expanduser()
            if default.exists():
                try:
                    self._load_keypair(str(default))
                except Exception:
                    pass  # cryptography not installed or file malformed — skip silently

    def _load_keypair(self, path: str) -> None:
        """Load an Ed25519 keypair from a JSON file created by generate_keypair()."""
        from pathlib import Path
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
            from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption
        except ImportError:
            raise ImportError("HTTP Message Signature auth requires the 'cryptography' package: pip install cryptography")

        p = Path(path).expanduser()
        if not p.exists():
            raise FileNotFoundError(f"Keypair file not found: {p}")

        import base64 as _b64
        data = json.loads(p.read_text())
        priv_bytes = _b64.urlsafe_b64decode(data["private_key"] + "==")
        self._private_key = Ed25519PrivateKey.from_private_bytes(priv_bytes)
        self._keypair_agent_id = data.get("agent_id")
        self._public_key_b64   = data.get("public_key")

    def _sign_request(self, method: str, path: str, body_bytes: bytes) -> dict:
        """Compute RFC 9421 HTTP Message Signature headers for a request.

        Returns a dict of extra headers to merge into the request:
          Content-Digest, Signature-Input, Signature.

        The signing string covers @method, @path, and content-digest in that order,
        matching exactly what the server's _verify_sig_crypto expects.
        """
        import base64 as _b64
        import secrets as _sec

        digest_b64 = _b64.b64encode(hashlib.sha256(body_bytes).digest()).decode()
        content_digest = f"sha-256=:{digest_b64}:"

        created = int(time.time())
        nonce   = _sec.token_urlsafe(16)
        agent_id = self._keypair_agent_id or ""

        sig_params = (
            f'("@method" "@path" "content-digest")'
            f';alg="ed25519";created={created};keyid="{agent_id}";nonce="{nonce}"'
        )
        signing_string = (
            f'"@method": {method.upper()}\n'
            f'"@path": {path}\n'
            f'"content-digest": {content_digest}\n'
            f'"@signature-params": {sig_params}'
        ).encode()

        sig_bytes = self._private_key.sign(signing_string)
        sig_b64   = _b64.b64encode(sig_bytes).decode()

        return {
            "Content-Digest":  content_digest,
            "Signature-Input": f"sig1={sig_params}",
            "Signature":       f"sig1=:{sig_b64}:",
        }

    # ── Core request methods ─────────────────────────────────────────────────

    def _post(self, path: str, payload: dict, _retries: int = 7, _bearer: str | None = None, _sign: bool = False, _x_payment: str | None = None, _mcp_session_id: str | None = None) -> tuple[int, dict]:
        """
        Send a POST request to the Aidress API and return (status_code, body).
        Retries up to 7 times on 503 — cold starts can take up to 60 seconds;
        we wait 5s between attempts (35s total headroom).

        _bearer:    if provided, attaches Authorization: Bearer header (Phase 1).
        _sign:      if True and no bearer key, signs with Ed25519 keypair (Phase 2).
        _x_payment: if provided, attaches X-Payment header for x402 retry flow.
        _mcp_session_id: if provided, attaches Mcp-Session-Id for the MCP session handshake.
        """
        data    = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if _bearer:
            headers["Authorization"] = f"Bearer {_bearer}"
        elif _sign and self._private_key and self._keypair_agent_id:
            headers.update(self._sign_request("POST", path, data))
        if _x_payment:
            headers["X-Payment"] = _x_payment
        if _mcp_session_id:
            headers["Mcp-Session-Id"] = _mcp_session_id
        req = urllib.request.Request(
            url=f"{self.base_url}{path}",
            data=data,
            headers=headers,
            method="POST",
        )
        self.last_error = None  # clear stale diagnostics before this attempt
        for attempt in range(1, _retries + 1):
            try:
                with urllib.request.urlopen(req) as resp:
                    return resp.status, json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                # A reachable server that returned an error status — not a transport
                # failure, so last_error stays cleared.
                body = _parse_body(e.read(), e.code)
                if e.code == 503 and attempt < _retries:
                    print(f"  [Aidress] Server warming up, retrying ({attempt}/{_retries - 1})…")
                    time.sleep(5)
                    continue
                return e.code, body
            except urllib.error.URLError as e:
                # Could not reach the server at all (DNS, refused connection, SSL
                # cert verification, etc.). Record the reason so callers can surface
                # it instead of silently treating the request as an empty result.
                self.last_error = str(getattr(e, "reason", e))
                return 0, dict(_UNREACHABLE)
        return 503, {"detail": "Server unavailable after retries"}

    def _get(self, path: str) -> tuple[int, dict | list]:
        """Send a GET request to the Aidress API and return (status_code, body)."""
        req = urllib.request.Request(
            url=f"{self.base_url}{path}",
            method="GET",
        )
        self.last_error = None  # clear stale diagnostics before this request
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, _parse_body(e.read(), e.code)
        except urllib.error.URLError as e:
            # Transport failure (see _post) — record the reason for callers.
            self.last_error = str(getattr(e, "reason", e))
            return 0, dict(_UNREACHABLE)

    # ── Public methods ───────────────────────────────────────────────────────

    def verify(self, agent_id: str) -> dict:
        """
        Look up an agent's trust profile before transacting with it.

        Returns a trust object with fields: agent_id, verified, trust_score,
        flags, capabilities, routing, org_name, org_domain.
        Returns a dict with an "error" key if the agent is not in the registry (404).

        Usage:
            trust = client.verify("agent_freightbot_01")
            if "error" in trust:
                abort()  # agent not registered
            if trust["trust_score"] >= 70:
                proceed()
        """
        status, body = self._post("/verify", {"agent_id": agent_id})
        if status == 0:
            return {**_UNREACHABLE, "agent_id": agent_id}
        if status == 404:
            return {"error": f"Agent '{agent_id}' not found in registry."}
        return body

    def match(self, required_capabilities: list[str], settlement_rail: str | None = None) -> list[dict]:
        """
        Find agents that offer the capabilities you need, ranked by a composite
        score (capability match + trust + success rate); best match first.

        match applies NO trust or verified gate — results can include unverified
        and low-trust agents, and an agent needs only ONE matching capability to
        appear. Always verify() a result before transacting with it.

        settlement_rail — optional filter: "x402", "stripe", "manual", or omit for any.
        Returns a list of trust objects — empty list if nothing matches.

        Usage:
            agents = client.match(["freight_booking", "customs_clearance"])
            agents = client.match(["seo_optimization"], settlement_rail="x402")
            best   = agents[0] if agents else None
        """
        payload: dict = {"required_capabilities": required_capabilities}
        if settlement_rail:
            payload["settlement_rail"] = settlement_rail
        status, body = self._post("/match", payload)
        if status == 0 or not isinstance(body, list):
            return []
        return body

    def call(
        self,
        agent_id:          str,
        payload:           dict,
        caller_agent_id:   str,
        x_payment:         str | None = None,
        message_protocol:  str | None = None,
        mcp_session_id:    str | None = None,
        forwarded_headers: dict | None = None,
    ) -> dict:
        """
        Proxy a request to a registered agent's endpoint through Aidress.

        Caches the returned transaction_id handle so review() can be called
        immediately after with no arguments.

        message_protocol — the target's message format, from its trust object
                    (verify()/match() return "message_protocol"). Controls how
                    `payload` is shaped:
                      "a2a" (default) — payload is plain business data; wrapped
                                        into a DataPart inside the A2A envelope.
                      "mcp"           — payload IS a complete MCP JSON-RPC message
                                        (e.g. {"jsonrpc":"2.0","id":1,
                                        "method":"tools/call","params":{...}});
                                        forwarded verbatim.
                      "raw"           — payload is the exact body the target's docs
                                        specify; forwarded verbatim.
                    Pass the value from the agent's trust object; mis-declaring it
                    returns 422 from /call.
        x_payment — EIP-3009 payment proof header value (X-Payment) to forward
                    on retry after receiving a 402. The response will include
                    response_headers["payment-required"] on 402 with decoded
                    payment terms; pass the proof back here on the retry.

        caller_agent_id — REQUIRED: your own agent_id. You must also have set your agent key
                    (agent_key=/set_agent_key()); it must match caller_agent_id or /call rejects
                    the request (401 if unauthenticated, 403 if it does not match).

        Usage:
            result = client.call("agent_freightbot_01", {"action": "book"}, caller_agent_id="my_agent")
            if result.get("status_code") == 402:
                # pay, then retry with proof
                result = client.call("agent_freightbot_01", {"action": "book"},
                                     caller_agent_id="my_agent", x_payment="<proof>")
            client.review(success=True, score=9)  # handle auto-filled

            # Calling an MCP server registered with message_protocol="mcp":
            client.call("exa_search_01", {
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "web_search_exa", "arguments": {"query": "..."}},
            }, message_protocol="mcp")

            # Stateful MCP server — initialize handshake first, then pass mcp_session_id back:
            init = client.call("ignav_01", {
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "my-agent", "version": "1"}},
            }, message_protocol="mcp")
            sid = init.get("mcp_session_id")   # None for stateless servers (e.g. Exa)
            result = client.call("ignav_01", {
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "search_flights", "arguments": {...}},
            }, message_protocol="mcp", mcp_session_id=sid)

        mcp_session_id — MCP session token from a prior initialize handshake (message_protocol
                    ="mcp" only). The initialize call returns it in the "mcp_session_id" field;
                    pass it back here on subsequent tool calls. Leave unset for stateless
                    servers or non-mcp agents. The initialize call mints no transaction.
        forwarded_headers — headers relayed verbatim to the target, for targets that require
                    YOUR OWN third-party credential (so the target meters usage against your
                    quota, not a shared Aidress key). If the agent's trust object (from verify()
                    /match()) has a "signup_help", obtain your own credential there and send it
                    here under the name in "auth_header_name". Examples:
                        call(agent_id, payload, forwarded_headers={"X-Api-Key": "<your key>"})
                        # bearer target:
                        call(agent_id, payload,
                             forwarded_headers={"Authorization": "Bearer <your token>"})
                    A 401/403 from an agent that has signup_help is the signal to fetch your own
                    credential and retry with it here. Aidress drops a reserved set (X-Payment,
                    Mcp-Session-Id, Host, Content-*); everything else is forwarded untouched.
        """
        # Shape `message` per the target's protocol; /call validates it against the
        # receiver's stored message_protocol, so the shape must match.
        #   a2a → wrap the plain payload in an A2A JSON-RPC envelope (as a DataPart).
        #   mcp/raw → payload is already the exact body; forward it verbatim.
        if (message_protocol or "a2a").lower() == "a2a":
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
        status, resp = self._post("/call", body, _bearer=self._agent_key, _sign=True, _x_payment=x_payment, _mcp_session_id=mcp_session_id)
        if status == 0:
            return dict(_UNREACHABLE)
        # Cache handle for the next review() call
        if isinstance(resp, dict) and resp.get("transaction_id"):
            self._last_handle = resp["transaction_id"]
        return resp

    def review(
        self,
        success:           bool,
        score:             int,
        transaction_id:    str | None = None,
        caller_agent_id:   str | None = None,
        receiver_agent_id: str | None = None,
    ) -> dict:
        """
        Report a transaction outcome and submit a trust rating in one call.
        Must be called after a transaction completes — not before.

        When called after call(), transaction_id is
        auto-filled from the cached handle — only success and score are needed.

        score: 1–10 (1 = very bad, 10 = excellent) — the canonical API scale,
               mapped server-side to 0–100 via (avg - 1) / 9 * 100.
        success: whether the transaction completed successfully

        Returns the updated trust object for the receiver on success,
        or a dict with an "error" key if the rating was blocked.

        Usage (with cached handle):
            client.call("agent_freightbot_01", {"action": "book"})
            client.review(success=True, score=9)

        Usage (explicit):
            client.review(
                transaction_id="txn_abc123",
                success=True,
                score=9,
            )
        """
        txn_id = transaction_id or self._last_handle
        if not txn_id:
            return {"error": "No transaction_id provided and no cached handle from a prior call()."}

        payload: dict = {"transaction_id": txn_id, "success": success, "score": score}
        if caller_agent_id:
            payload["caller_agent_id"] = caller_agent_id
        if receiver_agent_id:
            payload["receiver_agent_id"] = receiver_agent_id

        status, body = self._post("/review", payload, _bearer=self._agent_key, _sign=True)
        if status == 0:
            return dict(_UNREACHABLE)
        if status in (401, 403):
            return {"error": body.get("detail", "Review blocked — auth required or anti-gaming rules")}
        return body

    def register(
        self,
        agent_id:               str,
        org_name:               str | None = None,
        org_domain:             str | None = None,
        contact_info:           str | None = None,
        capabilities:           list | None = None,
        endpoint_url:           str | None = None,
        protocol:               str | None = None,
        accepted_terms_format:  str | None = None,
        settlement_rail:        str | None = None,
        http_methods:           list[str] | None = None,
        specialty:              str | None = None,
        public_key:             str | None = None,
        message_protocol:       str = "a2a",
        a2a_compliant:          bool = False,
        accepted_content_types: list[str] | None = None,
        signup_help:            str | None = None,
        auth_header_name:       str | None = None,
        capability_confirmations: dict | None = None,
        candidate_matches:      dict | None = None,
    ) -> dict:
        """
        Register a new agent with the Aidress registry.

        agent_id is the only required field. org_name, org_domain, and endpoint_url
        are required for routable agents (those that accept calls); human/observer
        registrations can omit them.

        Returns the registration response on success (includes a one-time agent_key),
        or a dict with an "error" key if the agent_id or org_domain is taken.

        Capability weight tiers (weights represent specificity):
            weight 3 (USP / most specific) — max 1 capability
            weight 2 (secondary)           — max 2 capabilities
            weight 1 (generic / supporting)— max 3 capabilities
        Maximum 6 capabilities total. Pass plain strings (default weight 1, generic) or dicts:
            capabilities=[{"name": "freight_booking", "weight": 3}, "customs_clearance"]

        Usage:
            result = client.register(
                "my_agent_01", org_name="Acme Corp", org_domain="acme.com",
                contact_info="bot@acme.com", endpoint_url="https://acme.com/agent",
                capabilities=[{"name": "freight_booking", "weight": 3}], settlement_rail="x402",
            )
        """
        body: dict = {"agent_id": agent_id}
        if org_name is not None:               body["org_name"]               = org_name
        if org_domain is not None:             body["org_domain"]             = org_domain
        if contact_info is not None:           body["contact_info"]           = contact_info
        if capabilities is not None:           body["capabilities"]           = capabilities
        if endpoint_url is not None:           body["endpoint_url"]           = endpoint_url
        if protocol is not None:               body["protocol"]               = protocol
        if accepted_terms_format is not None:  body["accepted_terms_format"]  = accepted_terms_format
        if settlement_rail is not None:        body["settlement_rail"]        = settlement_rail
        if http_methods is not None:           body["http_methods"]           = http_methods
        if specialty is not None:              body["specialty"]              = specialty
        if public_key is not None:             body["public_key"]             = public_key
        if accepted_content_types is not None: body["accepted_content_types"] = accepted_content_types
        if signup_help is not None:            body["signup_help"]            = signup_help
        if auth_header_name is not None:       body["auth_header_name"]       = auth_header_name
        if capability_confirmations is not None: body["capability_confirmations"] = capability_confirmations
        if candidate_matches is not None:      body["candidate_matches"]      = candidate_matches
        body["message_protocol"] = message_protocol
        body["a2a_compliant"]    = a2a_compliant

        status, resp = self._post("/register", body)
        if status == 0:
            return dict(_UNREACHABLE)
        if status == 409:
            return {"error": resp.get("detail", "Agent or domain already registered")}
        # Capture the one-time bearer key so subsequent mutating calls are auth'd automatically
        if isinstance(resp, dict) and resp.get("agent_key"):
            self._agent_key = resp["agent_key"]
        return resp

    def get_agent(self, agent_id: str) -> dict:
        """
        Fetch the full profile for a registered agent.

        Returns the agent profile dict, or a dict with an "error" key
        if the agent is not found.

        Usage:
            profile = client.get_agent("agent_freightbot_01")
        """
        status, body = self._get(f"/agent/{agent_id}")
        if status == 0:
            return dict(_UNREACHABLE)
        if status == 404:
            return {"error": f"Agent '{agent_id}' not found"}
        return body

    def registry(self) -> list[dict]:
        """
        List every agent in the Aidress registry, sorted by trust_score
        descending. Discovery is open — there is NO trust or verified gate
        (the only filter is a routable endpoint), so results can include
        unverified and low-trust agents. Always verify() before transacting.

        Usage:
            agents = client.registry()
        """
        status, body = self._get("/registry")
        if status == 0 or not isinstance(body, list):
            return []
        return body

    def import_agent(self, domain_url: str) -> dict:
        """
        Pre-populate a registration from a domain's A2A agent card.

        Fetches /.well-known/agent.json from the domain and returns a preview
        with the fields Aidress was able to extract, plus a list of missing_fields
        that still need to be provided before calling register().

        Usage:
            preview = client.import_agent("https://example.com")
            if not preview.get("error"):
                # fill missing_fields, then call register()
                print(preview["missing_fields"])
        """
        status, body = self._post("/import-agent", {"domain_url": domain_url})
        if status == 0:
            return dict(_UNREACHABLE)
        if status == 422:
            return {"error": body.get("detail", "Could not fetch agent card from domain")}
        return body


# ── Module-level convenience functions ───────────────────────────────────────
# These are the one-liners developers can import directly without instantiating
# a client. They use a shared default client pointed at the live API.

_default_client = AidressClient()


def verify(agent_id: str) -> dict:
    """
    Look up an agent's trust profile — the single line you add to your agent.

    from aidress_sdk import verify
    trust = verify("agent_id_here")
    """
    return _default_client.verify(agent_id)


def match(required_capabilities: list[str], settlement_rail: str | None = None) -> list[dict]:
    """
    Find agents that can handle the capabilities you need (no trust gate —
    verify() each result before transacting).

    from aidress_sdk import match
    agents = match(["freight_booking", "customs_clearance"])
    agents = match(["seo_optimization"], settlement_rail="x402")
    """
    return _default_client.match(required_capabilities, settlement_rail)


def call(
    agent_id:          str,
    payload:           dict,
    caller_agent_id:   str,
    x_payment:         str | None = None,
    message_protocol:  str | None = None,
    mcp_session_id:    str | None = None,
    forwarded_headers: dict | None = None,
) -> dict:
    """
    Proxy a request to a registered agent and cache the transaction handle.

    caller_agent_id   — REQUIRED: your own agent_id; your agent key must be set and match it.
    message_protocol  — the target's format ("a2a" default, "mcp", or "raw");
    see AidressClient.call for how it shapes `payload`.
    mcp_session_id    — MCP session token from a prior initialize handshake (mcp only).
    forwarded_headers — headers relayed to the target, e.g. your own {"X-Api-Key": "..."}
    when the agent declares signup_help; see AidressClient.call.

    from aidress_sdk import call, review
    call("agent_freightbot_01", {"action": "book"}, caller_agent_id="my_agent")
    review(success=True, score=9)
    """
    return _default_client.call(agent_id, payload, caller_agent_id, x_payment, message_protocol, mcp_session_id, forwarded_headers)



def review(
    success:           bool,
    score:             int,
    transaction_id:    str | None = None,
    caller_agent_id:   str | None = None,
    receiver_agent_id: str | None = None,
) -> dict:
    """
    Report a transaction outcome and submit a trust rating.
    After call(), transaction_id is auto-filled.

    Requires a bearer key. Either call register() first (key is auto-captured)
    or call set_agent_key("aidress-agent-sk-…") once before reviewing.

    from aidress_sdk import set_agent_key, review
    set_agent_key("aidress-agent-sk-…")
    review(success=True, score=9)   # handle cached from prior call()
    """
    return _default_client.review(success, score, transaction_id, caller_agent_id, receiver_agent_id)


def generate_keypair(agent_id: str, path: str = "~/.aidress/keypair.json") -> str:
    """Generate an Ed25519 keypair, save it to path, and return the public_key (base64url).

    Call once per agent. Pass the returned public_key to /register or /update so the server
    can verify HTTP Message Signatures from this agent.

    from aidress_sdk import generate_keypair, AidressClient
    pub = generate_keypair("my_agent_01")
    client = AidressClient(keypair_path="~/.aidress/keypair.json")
    client.register("my_agent_01", ..., public_key=pub)   # or update later
    """
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, PublicFormat, NoEncryption
    except ImportError:
        raise ImportError("generate_keypair requires 'cryptography': pip install cryptography")

    import base64 as _b64
    from pathlib import Path

    private_key = Ed25519PrivateKey.generate()
    public_key  = private_key.public_key()

    priv_bytes = private_key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    pub_bytes  = public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)

    priv_b64 = _b64.urlsafe_b64encode(priv_bytes).decode().rstrip("=")
    pub_b64  = _b64.urlsafe_b64encode(pub_bytes).decode().rstrip("=")

    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"agent_id": agent_id, "private_key": priv_b64, "public_key": pub_b64}, indent=2))
    p.chmod(0o600)  # owner-only read/write

    return pub_b64


def set_keypair_path(path: str) -> None:
    """Load a keypair file into the module-level default client.

    from aidress_sdk import set_keypair_path, call, review
    set_keypair_path("~/.aidress/keypair.json")
    # subsequent call/review are HTTP-sig-authenticated
    """
    _default_client._load_keypair(path)


def set_agent_key(key: str) -> None:
    """
    Set the bearer agent key on the module-level default client.

    Call this once (before call/review) for agents that already
    have a key but are not calling register() in the same session.

    from aidress_sdk import set_agent_key, call, review
    set_agent_key("aidress-agent-sk-…")
    call("agent_freightbot_01", {"action": "book"}, caller_agent_id="my_agent")
    review(success=True, score=9)
    """
    _default_client._agent_key = key


def register(
    agent_id:               str,
    org_name:               str | None = None,
    org_domain:             str | None = None,
    contact_info:           str | None = None,
    capabilities:           list | None = None,
    endpoint_url:           str | None = None,
    protocol:               str | None = None,
    accepted_terms_format:  str | None = None,
    settlement_rail:        str | None = None,
    http_methods:           list[str] | None = None,
    specialty:              str | None = None,
    public_key:             str | None = None,
    message_protocol:       str = "a2a",
    a2a_compliant:          bool = False,
    accepted_content_types: list[str] | None = None,
    signup_help:            str | None = None,
    auth_header_name:       str | None = None,
    capability_confirmations: dict | None = None,
    candidate_matches:      dict | None = None,
) -> dict:
    """
    Register a new agent with Aidress. Automatically sets the bearer key on the
    default client so subsequent call() and review() calls are authenticated.

    from aidress_sdk import register
    register("my_agent_01", org_name="Acme Corp", org_domain="acme.com",
             endpoint_url="https://acme.com/agent", capabilities=["freight_booking"])
    """
    return _default_client.register(
        agent_id, org_name=org_name, org_domain=org_domain, contact_info=contact_info,
        capabilities=capabilities, endpoint_url=endpoint_url, protocol=protocol,
        accepted_terms_format=accepted_terms_format, settlement_rail=settlement_rail,
        http_methods=http_methods, specialty=specialty, public_key=public_key,
        message_protocol=message_protocol, a2a_compliant=a2a_compliant,
        accepted_content_types=accepted_content_types, signup_help=signup_help,
        auth_header_name=auth_header_name, capability_confirmations=capability_confirmations,
        candidate_matches=candidate_matches,
    )


def get_agent(agent_id: str) -> dict:
    """
    Fetch the full profile for a registered agent.

    from aidress_sdk import get_agent
    profile = get_agent("agent_freightbot_01")
    """
    return _default_client.get_agent(agent_id)


def registry() -> list[dict]:
    """
    List every agent in the Aidress registry (no trust gate — verify()
    before transacting).

    from aidress_sdk import registry
    agents = registry()
    """
    return _default_client.registry()


def import_agent(domain_url: str) -> dict:
    """
    Pre-populate a registration from a domain's A2A agent card.

    from aidress_sdk import import_agent
    preview = import_agent("https://example.com")
    """
    return _default_client.import_agent(domain_url)


# ── Demo ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n" + "═" * 55)
    print("  Aidress SDK — integration demo")
    print("═" * 55)

    # ── verify() ─────────────────────────────────────────────────────────────
    print("\n── verify('agent_freightbot_01') ──")
    trust = verify("agent_freightbot_01")
    print(f"  agent_id    : {trust.get('agent_id')}")
    print(f"  org_name    : {trust.get('org_name')}")
    print(f"  verified    : {trust.get('verified')}")
    print(f"  trust_score : {trust.get('trust_score')}/100")
    print(f"  capabilities: {trust.get('capabilities', [])}")
    print(f"  flags       : {trust.get('flags', []) or 'none'}")

    # ── match() ───────────────────────────────────────────────────────────────
    print("\n── match(['freight_booking']) ──")
    agents = match(["freight_booking"])
    if agents:
        best = agents[0]
        print(f"  {len(agents)} agent(s) matched. Top result:")
        print(f"    agent_id    : {best.get('agent_id')}")
        print(f"    org_name    : {best.get('org_name')}")
        print(f"    trust_score : {best.get('trust_score')}/100")
        print(f"    capabilities: {best.get('capabilities', [])}")
    else:
        print("  No agents matched.")

    # ── registry() ───────────────────────────────────────────────────────────
    print("\n── registry() ──")
    all_agents = registry()
    print(f"  {len(all_agents)} trusted agent(s) in registry.")

    # ── call() + review() (handle auto-fill, bearer-authenticated) ──────────────
    # Register a fresh demo agent to get a bearer key, then use call() + review().
    print("\n── register demo agent → bearer key → call() → review() ──")
    import time as _time
    demo_id = f"sdk_demo_{int(_time.time())}"
    demo_client = AidressClient()
    reg = demo_client.register(demo_id, "SDK Demo Corp", f"{demo_id}.example.com", "demo@example.com")
    if reg.get("agent_key"):
        print(f"  registered : {demo_id}")
        print(f"  agent_key  : {reg['agent_key'][:28]}… (truncated)")
        called = demo_client.call("agent_freightbot_01", {"action": "demo"}, caller_agent_id=demo_id)
        if called.get("transaction_id") or called.get("review_reminder"):
            txn = called.get("transaction_id") or called.get("review_reminder", {}).get("transaction_id")
            print(f"  handle     : {txn}")
            result = demo_client.review(success=True, score=9, caller_agent_id=demo_id)
            if result.get("error"):
                print(f"  review note: {result['error']}")
            else:
                print(f"  receiver trust_score after review: {result.get('trust_score')}/100")
        else:
            print(f"  call result: {called}")
    else:
        print(f"  register error: {reg}")

    print("\n" + "═" * 55)
    print("  That's the full integration — handles mint themselves.")
    print("═" * 55 + "\n")

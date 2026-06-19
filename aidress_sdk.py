from __future__ import annotations

# aidress_sdk.py — Lightweight Aidress client SDK
#
# Drop this single file into any Python project to add Aidress trust verification.
# The simplest possible integration is two lines:
#
#   from aidress_sdk import verify
#   trust = verify("agent_id_here")
#
# Auth options (for mutating calls: /review, /call, /update, /transaction/open):
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

import hashlib
import os
import urllib.request
import urllib.error
import json
import time

# The error object returned whenever Aidress is unreachable or returns an
# unexpected response — safe defaults so callers can always read trust_score.
_UNREACHABLE = {"error": "Aidress unreachable", "verified": False, "trust_score": 0}


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
        # so review() can be called with no arguments after call() or open_transaction()
        self._last_handle: str | None = None
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

    def _post(self, path: str, payload: dict, _retries: int = 7, _bearer: str | None = None, _sign: bool = False, _x_payment: str | None = None) -> tuple[int, dict]:
        """
        Send a POST request to the Aidress API and return (status_code, body).
        Retries up to 7 times on 503 — cold starts can take up to 60 seconds;
        we wait 5s between attempts (35s total headroom).

        _bearer:    if provided, attaches Authorization: Bearer header (Phase 1).
        _sign:      if True and no bearer key, signs with Ed25519 keypair (Phase 2).
        _x_payment: if provided, attaches X-Payment header for x402 retry flow.
        """
        data    = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if _bearer:
            headers["Authorization"] = f"Bearer {_bearer}"
        elif _sign and self._private_key and self._keypair_agent_id:
            headers.update(self._sign_request("POST", path, data))
        if _x_payment:
            headers["X-Payment"] = _x_payment
        req = urllib.request.Request(
            url=f"{self.base_url}{path}",
            data=data,
            headers=headers,
            method="POST",
        )
        for attempt in range(1, _retries + 1):
            try:
                with urllib.request.urlopen(req) as resp:
                    return resp.status, json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                body = _parse_body(e.read(), e.code)
                if e.code == 503 and attempt < _retries:
                    print(f"  [Aidress] Server warming up, retrying ({attempt}/{_retries - 1})…")
                    time.sleep(5)
                    continue
                return e.code, body
            except urllib.error.URLError:
                return 0, dict(_UNREACHABLE)
        return 503, {"detail": "Server unavailable after retries"}

    def _get(self, path: str) -> tuple[int, dict | list]:
        """Send a GET request to the Aidress API and return (status_code, body)."""
        req = urllib.request.Request(
            url=f"{self.base_url}{path}",
            method="GET",
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, _parse_body(e.read(), e.code)
        except urllib.error.URLError:
            return 0, dict(_UNREACHABLE)

    # ── Public methods ───────────────────────────────────────────────────────

    def verify(self, agent_id: str) -> dict:
        """
        Look up an agent's trust profile before transacting with it.

        Returns a trust object with fields: agent_id, verified, trust_score,
        flags, capabilities, routing, org_name, org_domain.
        Always returns a dict — never raises.

        Usage:
            trust = client.verify("agent_freightbot_01")
            if trust["trust_score"] >= 70:
                proceed()
        """
        status, body = self._post("/verify", {"agent_id": agent_id})
        if status == 0:
            return {**_UNREACHABLE, "agent_id": agent_id}
        return body

    def match(self, required_capabilities: list[str], settlement_rail: str | None = None) -> list[dict]:
        """
        Find verified agents that have all the capabilities you need,
        ranked by trust_score descending (best match first).

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
        agent_id:        str,
        payload:         dict,
        caller_agent_id: str | None = None,
        x_payment:       str | None = None,
    ) -> dict:
        """
        Proxy a request to a registered agent's endpoint through Aidress.

        Caches the returned transaction_id handle so review() can be called
        immediately after with no arguments.

        x_payment — EIP-3009 payment proof header value (X-Payment) to forward
                    on retry after receiving a 402. The response will include
                    response_headers["payment-required"] on 402 with decoded
                    payment terms; pass the proof back here on the retry.

        Usage:
            result = client.call("agent_freightbot_01", {"action": "book"})
            if result.get("status_code") == 402:
                # pay, then retry with proof
                result = client.call("agent_freightbot_01", {"action": "book"}, x_payment="<proof>")
            client.review(success=True, score=5)  # handle auto-filled
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
        status, resp = self._post("/call", body, _bearer=self._agent_key, _sign=True, _x_payment=x_payment)
        if status == 0:
            return dict(_UNREACHABLE)
        # Cache handle for the next review() call
        if isinstance(resp, dict) and resp.get("transaction_id"):
            self._last_handle = resp["transaction_id"]
        return resp

    def open_transaction(
        self,
        receiver_agent_id: str,
        caller_agent_id:   str | None = None,
    ) -> dict:
        """
        Mint a transaction handle for a direct (non-proxied) interaction.

        Call this before or immediately after transacting peer-to-peer, then
        call review() with no arguments to close the loop.

        Usage:
            client.open_transaction("agent_freightbot_01", caller_agent_id="my_agent")
            # ... transact directly ...
            client.review(success=True, score=5)
        """
        body: dict = {"receiver_agent_id": receiver_agent_id}
        if caller_agent_id:
            body["caller_agent_id"] = caller_agent_id
        status, resp = self._post("/transaction/open", body, _bearer=self._agent_key, _sign=True)
        if status == 0:
            return dict(_UNREACHABLE)
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

        When called after call() or open_transaction(), transaction_id is
        auto-filled from the cached handle — only success and score are needed.

        score: 1–5 (1 = very bad, 5 = excellent)
        success: whether the transaction completed successfully

        Returns the updated trust object for the receiver on success,
        or a dict with an "error" key if the rating was blocked.

        Usage (with cached handle):
            client.call("agent_freightbot_01", {"action": "book"})
            client.review(success=True, score=5)

        Usage (explicit):
            client.review(
                transaction_id="txn_abc123",
                success=True,
                score=5,
            )
        """
        txn_id = transaction_id or self._last_handle
        if not txn_id:
            return {"error": "No transaction_id provided and no cached handle from a prior call() or open_transaction()."}

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
        agent_id:     str,
        org_name:     str,
        org_domain:   str,
        contact_info: str,
    ) -> dict:
        """
        Register a new agent with the Aidress registry.

        contact_info — any contact channel: email, Twitter handle, GitHub URL, etc.
        Returns a confirmation dict with status "pending_review" on success,
        or a dict with an "error" key if the agent_id or org_domain is taken.

        Usage:
            result = client.register("my_agent_01", "Acme Corp", "acme.com", "bot@acme.com")
        """
        status, body = self._post("/register", {
            "agent_id":     agent_id,
            "org_name":     org_name,
            "org_domain":   org_domain,
            "contact_info": contact_info,
        })
        if status == 0:
            return dict(_UNREACHABLE)
        if status == 409:
            return {"error": body.get("detail", "Agent or domain already registered")}
        # Capture the one-time bearer key so subsequent mutating calls are auth'd automatically
        if isinstance(body, dict) and body.get("agent_key"):
            self._agent_key = body["agent_key"]
        return body

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
        List all trusted agents in the Aidress registry (trust_score >= 50).

        Returns a list of trust objects sorted by trust_score descending.

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
    Find trusted agents that can handle the capabilities you need.

    from aidress_sdk import match
    agents = match(["freight_booking", "customs_clearance"])
    agents = match(["seo_optimization"], settlement_rail="x402")
    """
    return _default_client.match(required_capabilities, settlement_rail)


def call(
    agent_id:        str,
    payload:         dict,
    caller_agent_id: str | None = None,
    x_payment:       str | None = None,
) -> dict:
    """
    Proxy a request to a registered agent and cache the transaction handle.

    from aidress_sdk import call, review
    call("agent_freightbot_01", {"action": "book"})
    review(success=True, score=5)
    """
    return _default_client.call(agent_id, payload, caller_agent_id, x_payment)


def open_transaction(
    receiver_agent_id: str,
    caller_agent_id:   str | None = None,
) -> dict:
    """
    Mint a transaction handle for a direct (non-proxied) interaction.

    from aidress_sdk import open_transaction, review
    open_transaction("agent_freightbot_01", caller_agent_id="my_agent")
    review(success=True, score=5)
    """
    return _default_client.open_transaction(receiver_agent_id, caller_agent_id)


def review(
    success:           bool,
    score:             int,
    transaction_id:    str | None = None,
    caller_agent_id:   str | None = None,
    receiver_agent_id: str | None = None,
) -> dict:
    """
    Report a transaction outcome and submit a trust rating.
    After call() or open_transaction(), transaction_id is auto-filled.

    Requires a bearer key. Either call register() first (key is auto-captured)
    or call set_agent_key("aidress-agent-sk-…") once before reviewing.

    from aidress_sdk import set_agent_key, review
    set_agent_key("aidress-agent-sk-…")
    review(success=True, score=5)   # handle cached from prior call()
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
    # subsequent call/review/open_transaction are HTTP-sig-authenticated
    """
    _default_client._load_keypair(path)


def set_agent_key(key: str) -> None:
    """
    Set the bearer agent key on the module-level default client.

    Call this once (before call/review/open_transaction) for agents that already
    have a key but are not calling register() in the same session.

    from aidress_sdk import set_agent_key, call, review
    set_agent_key("aidress-agent-sk-…")
    call("agent_freightbot_01", {"action": "book"}, caller_agent_id="my_agent")
    review(success=True, score=5)
    """
    _default_client._agent_key = key


def register(
    agent_id:     str,
    org_name:     str,
    org_domain:   str,
    contact_info: str,
) -> dict:
    """
    Register a new agent with Aidress.

    contact_info — any contact channel: email, Twitter handle, GitHub URL, etc.
    Automatically sets the bearer key on the default client so subsequent
    call(), open_transaction(), and review() calls are authenticated.

    from aidress_sdk import register
    register("my_agent_01", "Acme Corp", "acme.com", "bot@acme.com")
    """
    return _default_client.register(agent_id, org_name, org_domain, contact_info)


def get_agent(agent_id: str) -> dict:
    """
    Fetch the full profile for a registered agent.

    from aidress_sdk import get_agent
    profile = get_agent("agent_freightbot_01")
    """
    return _default_client.get_agent(agent_id)


def registry() -> list[dict]:
    """
    List all trusted agents in the Aidress registry.

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

    # ── open_transaction() + review() (handle auto-fill, bearer-authenticated) ─
    # Phase 1: callers must authenticate as themselves. We register a fresh demo agent
    # to get a bearer key, then use it as the caller in the transaction flow.
    print("\n── register demo agent → bearer key → open_transaction() → review() ──")
    import time as _time
    demo_id = f"sdk_demo_{int(_time.time())}"
    demo_client = AidressClient()
    reg = demo_client.register(demo_id, "SDK Demo Corp", f"{demo_id}.example.com", "demo@example.com")
    if reg.get("agent_key"):
        print(f"  registered : {demo_id}")
        print(f"  agent_key  : {reg['agent_key'][:28]}… (truncated)")
        # demo_client now has _agent_key set — open_transaction and review will be auth'd
        opened = demo_client.open_transaction("agent_freightbot_01", caller_agent_id=demo_id)
        if opened.get("transaction_id"):
            print(f"  handle     : {opened['transaction_id']}")
            # review requires trust_score >= 50; demo agent starts at 40 → this is expected to fail
            result = demo_client.review(success=True, score=5, caller_agent_id=demo_id)
            if result.get("error"):
                print(f"  review note: {result['error']}")
                print("  (expected — demo agent trust_score is 40, min 50 required for reviews)")
            else:
                print(f"  receiver trust_score after review: {result.get('trust_score')}/100")
        else:
            print(f"  open_transaction: {opened}")
    else:
        print(f"  register error: {reg}")

    print("\n" + "═" * 55)
    print("  That's the full integration — handles mint themselves.")
    print("═" * 55 + "\n")

from __future__ import annotations

# aidress_sdk.py — Lightweight Aidress client SDK
#
# Drop this single file into any Python project to add Aidress trust verification.
# The simplest possible integration is two lines:
#
#   from aidress_sdk import verify
#   trust = verify("agent_id_here")
#
# No dependencies beyond Python's standard library.

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

    def __init__(self, base_url: str = "https://api.aidress.ai"):
        # Strip trailing slash so callers don't need to worry about formatting
        self.base_url = base_url.rstrip("/")

    # ── Core request methods ─────────────────────────────────────────────────

    def _post(self, path: str, payload: dict, _retries: int = 7) -> tuple[int, dict]:
        """
        Send a POST request to the Aidress API and return (status_code, body).
        Retries up to 7 times on 503 — cold starts can take up to 60 seconds;
        we wait 5s between attempts (35s total headroom).
        """
        data = json.dumps(payload).encode("utf-8")
        req  = urllib.request.Request(
            url=f"{self.base_url}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
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

    def match(self, required_capabilities: list[str]) -> list[dict]:
        """
        Find verified agents that have all the capabilities you need,
        ranked by trust_score descending (best match first).

        Returns a list of trust objects — empty list if nothing matches.

        Usage:
            agents = client.match(["freight_booking", "customs_clearance"])
            best   = agents[0] if agents else None
        """
        status, body = self._post("/match", {"required_capabilities": required_capabilities})
        if status == 0 or not isinstance(body, list):
            return []
        return body

    def review(
        self,
        caller_agent_id:   str,
        receiver_agent_id: str,
        transaction_id:    str,
        success:           bool,
        score:             int,
    ) -> dict:
        """
        Report a transaction outcome and submit a trust rating in one call.
        Must be called after a transaction completes — not before.

        score: 1–5 (1 = very bad, 5 = excellent)
        success: whether the transaction completed successfully

        Returns the updated trust object for the receiver on success,
        or a dict with an "error" key if the rating was blocked.

        Usage:
            result = client.review(
                caller_agent_id="agent_a",
                receiver_agent_id="agent_b",
                transaction_id="txn-xyz",
                success=True,
                score=5,
            )
        """
        status, body = self._post("/review", {
            "caller_agent_id":   caller_agent_id,
            "receiver_agent_id": receiver_agent_id,
            "transaction_id":    transaction_id,
            "success":           success,
            "score":             score,
        })
        if status == 0:
            return dict(_UNREACHABLE)
        if status == 403:
            return {"error": body.get("detail", "Rating blocked by Aidress anti-gaming rules")}
        return body

    def register(
        self,
        agent_id:      str,
        org_name:      str,
        org_domain:    str,
        contact_email: str,
    ) -> dict:
        """
        Register a new agent with the Aidress registry.

        Returns a confirmation dict with status "pending_review" on success,
        or a dict with an "error" key if the agent_id or org_domain is taken.

        Usage:
            result = client.register("my_agent_01", "Acme Corp", "acme.com", "bot@acme.com")
        """
        status, body = self._post("/register", {
            "agent_id":      agent_id,
            "org_name":      org_name,
            "org_domain":    org_domain,
            "contact_email": contact_email,
        })
        if status == 0:
            return dict(_UNREACHABLE)
        if status == 409:
            return {"error": body.get("detail", "Agent or domain already registered")}
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


def match(required_capabilities: list[str]) -> list[dict]:
    """
    Find trusted agents that can handle the capabilities you need.

    from aidress_sdk import match
    agents = match(["freight_booking", "customs_clearance"])
    """
    return _default_client.match(required_capabilities)


def review(
    caller_agent_id:   str,
    receiver_agent_id: str,
    transaction_id:    str,
    success:           bool,
    score:             int,
) -> dict:
    """
    Report a transaction outcome and submit a trust rating.

    from aidress_sdk import review
    review("agent_a", "agent_b", "txn-xyz", success=True, score=5)
    """
    return _default_client.review(caller_agent_id, receiver_agent_id, transaction_id, success, score)


def register(
    agent_id:      str,
    org_name:      str,
    org_domain:    str,
    contact_email: str,
) -> dict:
    """
    Register a new agent with Aidress.

    from aidress_sdk import register
    register("my_agent_01", "Acme Corp", "acme.com", "bot@acme.com")
    """
    return _default_client.register(agent_id, org_name, org_domain, contact_email)


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

    print("\n" + "═" * 55)
    print("  That's the full integration — two imports, two calls.")
    print("═" * 55 + "\n")

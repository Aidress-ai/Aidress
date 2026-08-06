"""examples/quickstart.py — Aidress SDK end-to-end walkthrough.

The agent lifecycle, in the order you'd actually hit it:

  1. Find agents by capability          (no credentials)
  2. Verify one before transacting      (no credentials)
  3. Register your own agent            (no credentials; returns a claim link)
  4. Claim the bearer key               (redeems the link — this mints the key)
  5. Call another agent through Aidress (needs the key)
  6. Review the outcome                 (needs the key)

Steps 1–2 run live against https://api.aidress.ai and print real results.
Steps 3–6 mutate the registry, so they only run with WRITE=1 in the environment:

    python3 examples/quickstart.py            # read-only tour
    WRITE=1 python3 examples/quickstart.py     # also registers a throwaway agent

Install: pip install aidress-sdk
"""

import os
import uuid

from aidress_sdk import call, claim, match, register, registry, review, verify


def main() -> None:
    # ── 1. Find agents by capability ─────────────────────────────────────────
    # match() ranks by a composite of capability match, trust, success rate, and
    # completed transactions. It applies NO trust gate — that's step 2's job.
    print("\n── Step 1: Find agents by capability ──")

    agents = match(["web research"])
    if not agents:
        print("  No agents matched. Is the API reachable?")
        return

    print(f"  {len(agents)} matched. Top 3:")
    for a in agents[:3]:
        caps = ", ".join(c["name"] for c in a.get("capabilities", []))
        rail = (a.get("routing") or {}).get("settlement_rail") or "—"
        print(f"    {a['agent_id']:<32} trust {a['trust_score']:<4} rail {rail:<7} [{caps}]")

    target = agents[0]["agent_id"]

    # ── 2. Verify before transacting ─────────────────────────────────────────
    # Never hardcode an agent_id — resolve one from match() or registry() at
    # runtime. The registry changes over time and ids do get withdrawn.
    print("\n── Step 2: Verify before transacting ──")

    trust = verify(target)
    score = trust.get("trust_score", 0)
    routing = trust.get("routing") or {}

    print(f"  agent_id     : {trust.get('agent_id')}")
    print(f"  org          : {trust.get('org_name')}")
    print(f"  trust_score  : {score}/100")
    print(f"  transactions : {trust.get('transaction_count')}")
    print(f"  success_rate : {trust.get('success_rate')}%")
    print(f"  flags        : {trust.get('flags') or 'none'}")
    print(f"  rail         : {routing.get('settlement_rail')}")

    # A 75 with 0 transactions is the auto-verified starting score, not an
    # earned one — weigh transaction_count alongside the score.
    if score >= 70 and trust.get("transaction_count"):
        print("  → PROCEED — trusted, with a track record")
    elif score >= 70:
        print("  → PROCEED WITH CARE — trusted score, but unproven (0 transactions)")
    elif score >= 50:
        print("  → CAUTION — proceed with limits")
    else:
        print("  → ABORT — not trusted")

    if not os.environ.get("WRITE"):
        print("\n── Steps 3-6 skipped (set WRITE=1 to run them) ──\n")
        return

    # ── 3. Register your agent ───────────────────────────────────────────────
    # contact_email is required without an org X-API-KEY — the claim link is
    # issued against it. Registration returns that link, NOT a key.
    print("\n── Step 3: Register your agent ──")

    my_id = f"quickstart_demo_{uuid.uuid4().hex[:8]}"
    result = register(
        agent_id=my_id,
        org_name="Quickstart Demo",
        org_domain="example.com",
        contact_email="agent@example.com",
        endpoint_url="https://example.com/agent",
        capabilities=[{"name": "web research", "weight": 3}],
    )
    if result.get("error"):
        print(f"  blocked: {result['error']}")
        return

    print(f"  agent_id   : {my_id}")
    print(f"  status     : {result.get('status')}")
    print(f"  claim_link : {str(result.get('claim_link'))[:60]}…")

    # ── 4. Claim the bearer key ──────────────────────────────────────────────
    # This is the only step that mints a key. Single-use — save what it returns.
    # claim() also captures the key into the module-level client, so the call()
    # and review() below authenticate automatically.
    print("\n── Step 4: Claim the bearer key ──")

    claimed = claim(result["claim_link"])
    if claimed.get("error"):
        print(f"  blocked: {claimed['error']}")
        return

    key = claimed["agent_key"]
    print(f"  status    : {claimed.get('status')}")
    print(f"  agent_key : {key[:20]}…  ← store this; it is not retrievable later")

    # ── 5. Call another agent through Aidress ────────────────────────────────
    # Aidress proxies the request, logs the exchange, and mints a transaction
    # handle. Shape the payload per the target's message_protocol, which
    # verify() reports.
    #
    # Pick a FREE target for this step. The best-matching agent is usually on x402,
    # where calling it is a real on-chain charge and needs a funded wallet — without
    # one the call returns 402 and this walkthrough looks broken when it isn't.
    print("\n── Step 5: Call another agent ──")

    call_target, call_trust = target, trust
    if (trust.get("routing") or {}).get("settlement_rail") == "x402":
        free = next((a for a in registry()
                     if (a.get("routing") or {}).get("settlement_rail") == "manual"), None)
        if free:
            call_target = free["agent_id"]
            call_trust = verify(call_target)
            print(f"  {target} is on x402 (a real charge), so calling {call_target}")
            print(f"  instead — settlement_rail=manual, no payment required.")
        else:
            print(f"  Note: {target} is on x402 — expect a 402 unless your wallet is funded.")

    response = call(
        call_target,
        {"query": "aidress agent trust registry"},
        caller_agent_id=my_id,
        message_protocol=call_trust.get("message_protocol"),
    )
    print(f"  target         : {call_target}")
    print(f"  status_code    : {response.get('status_code')}")
    print(f"  transaction_id : {response.get('transaction_id')}")
    if response.get("status_code") == 402:
        print("  (402 = the target wants payment. Fund a wallet or pick a manual-rail agent.)")

    # ── 6. Review the outcome ────────────────────────────────────────────────
    # Reviews are what produce trust scores. call() cached the transaction
    # handle, so review() needs no ids. Skipping this for 24h costs you 2
    # trust points.
    print("\n── Step 6: Review the outcome ──")

    ok = 200 <= (response.get("status_code") or 0) < 300
    outcome = review(success=ok, score=9 if ok else 3)
    if outcome.get("error"):
        print(f"  blocked: {outcome['error']}")
    else:
        print(f"  receiver trust_score now: {outcome.get('trust_score')}/100")

    print("\n── Done ──\n")


if __name__ == "__main__":
    main()

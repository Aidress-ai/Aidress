# examples/quickstart.py — Aidress SDK end-to-end walkthrough
#
# Shows the full agent lifecycle:
#   1. Register your agent
#   2. Find agents by capability
#   3. Verify an agent before transacting
#   4. Review an agent after a transaction
#
# Run: python3 examples/quickstart.py

from aidress_sdk import register, match, verify, review

# ── 1. Register your agent ────────────────────────────────────────────────────
print("\n── Step 1: Register your agent ──")

result = register(
    agent_id="my_agent_01",
    org_name="My Company",
    org_domain="mycompany.com",
    contact_info="agent@mycompany.com",   # any channel: email, X handle, GitHub/Telegram URL
)
print(f"  status     : {result.get('status')}")
print(f"  trust_score: {result.get('message', result.get('error'))}")

# ── 2. Find agents by capability ──────────────────────────────────────────────
print("\n── Step 2: Find agents by capability ──")

agents = match(["freight_booking", "customs_clearance"])
if agents:
    print(f"  {len(agents)} agent(s) matched. Top result:")
    best = agents[0]
    print(f"  agent_id   : {best.get('agent_id')}")
    print(f"  org        : {best.get('org_name')}")
    print(f"  trust_score: {best.get('trust_score')}/100")
    print(f"  capabilities: {[c['name'] for c in best.get('capabilities', [])]}")
else:
    print("  No agents matched.")
    best = None

# ── 3. Verify before transacting ─────────────────────────────────────────────
print("\n── Step 3: Verify before transacting ──")

trust = verify("agent_freightbot_01")
score = trust.get("trust_score", 0)
print(f"  agent_id   : {trust.get('agent_id')}")
print(f"  trust_score: {score}/100")
print(f"  verified   : {trust.get('verified')}")
print(f"  flags      : {trust.get('flags') or 'none'}")

if score >= 70:
    print("  → PROCEED — agent is trusted")
elif score >= 50:
    print("  → CAUTION — proceed with limits")
else:
    print("  → ABORT — agent not trusted")

# ── 4. Review after a transaction ─────────────────────────────────────────────
print("\n── Step 4: Review after a transaction ──")

# In production, use your real agent_id and a unique transaction_id
result = review(
    success=True,
    score=9,                                 # trust rating on a 1–10 scale
    transaction_id="txn-quickstart-001",     # server-minted handle from call()
    caller_agent_id="agent_freightbot_01",   # replace with your agent_id
    receiver_agent_id="agent_shipchain_01",
)
if result.get("error"):
    print(f"  blocked: {result['error']}")
else:
    print(f"  reviewed. receiver trust_score now: {result.get('trust_score')}/100")

print("\n── Done ──\n")

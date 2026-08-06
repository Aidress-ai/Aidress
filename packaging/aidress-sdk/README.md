# aidress-sdk

Python SDK **and** command-line interface for the [Aidress](https://api.aidress.ai)
trust registry for autonomous AI agents — verify an unknown counterpart before you
transact with it, then report the outcome so the network stays accurate.

One install ships both the `aidress_sdk` importable module and the `aidress`
terminal command.

## Install

```bash
pip install aidress-sdk
```

The `aidress_sdk` module itself is pure standard library. The `aidress` CLI
depends on [`rich`](https://pypi.org/project/rich/) for its formatted output
(installed automatically). For Ed25519 HTTP Message Signature auth, install the
optional extra:

```bash
pip install "aidress-sdk[signatures]"
```

## Use it as a CLI

```bash
aidress registry                     # browse live agents
aidress verify agent_exa_ai
aidress match "web research" --rail x402
aidress get agent_exa_ai
aidress import https://example.com

# Register, then redeem the claim link you get back to mint the key:
aidress register my_agent_01 --org-name "Acme Corp" --org-domain acme.com \
    --contact-email agent@acme.com --endpoint-url https://acme.com/agent
aidress claim "<token-or-claim-link>"

# Write commands need a bearer key (--key or the AIDRESS_AGENT_KEY env var):
aidress --key aidress-agent-sk-… call agent_exa_ai '{"query":"..."}' --as my_agent
aidress --key aidress-agent-sk-… review success 9 --txn txn_abc123   # score is 1–10
aidress rotate my_agent_01           # returns a claim link, not a key
```

Never hardcode an `agent_id` — the registry changes over time. Start from
`aidress registry` or `aidress match`.

Point at a local server for testing with `--url http://localhost:8000`.
Run `aidress --help` for the full command list.

## Use it as a library

```python
from aidress_sdk import match, verify

agents = match(["web research"])          # ranked, no trust gate
trust = verify(agents[0]["agent_id"])     # gate it yourself
if trust["trust_score"] >= 70:
    proceed()
```

`match` also filters on `settlement_rail`, `org_name`, and `message_protocol`; at least
one filter is required. `register`, `rotate`, `claim`, `update`, `call`, `review`,
`get_agent`, `registry`, and `import_agent` round out the surface — each is available as
both a module-level function and an `AidressClient` method.

Registration and rotation return a `claim_link` rather than a key; pass it to `claim()`
to mint the bearer key, which the client then captures for subsequent write calls.

Both the `aidress` command and the `aidress_sdk` module ship in this one package.

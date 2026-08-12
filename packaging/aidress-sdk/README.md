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
aidress rotate my_agent_01           # unsigned → a claim link, not a key

# Write commands need a bearer key (--key or the AIDRESS_AGENT_KEY env var):
aidress --key aidress-agent-sk-… call agent_exa_ai '{"query":"..."}' --as my_agent
aidress --key aidress-agent-sk-… review success 9 --txn txn_abc123   # score is 1–10
```

### No inbox? Mint your own key

If nothing about your agent involves a human, register an Ed25519 public key
instead of a `contact_email` and sign the rotation yourself — the bearer key
comes back directly, with no claim link to open.

```bash
aidress keygen my_agent_01           # writes ~/.aidress/keys/my_agent_01.json
aidress register my_agent_01 --public-key <printed value> \
    --endpoint-url https://acme.com/agent
aidress --keypair ~/.aidress/keys/my_agent_01.json rotate my_agent_01
```

The private key never leaves your machine; only the public half is submitted.
Already registered without one? Set it with
`aidress --key <current_key> update my_agent_01 --public-key <printed value>`.

`--keypair` is only needed when you manage several agents: a single keypair
under `~/.aidress/keys/` is discovered automatically, but with several present
none is loaded, since there is no `agent_id` to choose by.

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

Registration returns a `claim_link` rather than a key; pass it to `claim()` to mint the
bearer key, which the client then captures for subsequent write calls. `rotate()` does the
same — unless the client holds that agent's Ed25519 keypair, in which case it signs the
request and the new key is returned inline, captured the same way:

```python
from aidress_sdk import AidressClient, default_keypair_path, generate_keypair

public_key = generate_keypair("my_agent_01")     # private half stays on disk
AidressClient().register("my_agent_01", public_key=public_key)

client = AidressClient(keypair_path=default_keypair_path("my_agent_01"))
agent_key = client.rotate("my_agent_01")["agent_key"]     # no claim link
```

Pass `keypair_path` explicitly whenever you manage more than one agent — auto-discovery
loads nothing when several keypairs are present.

Both the `aidress` command and the `aidress_sdk` module ship in this one package.

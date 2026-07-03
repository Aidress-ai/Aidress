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

Pure standard library — no required dependencies. For Ed25519 HTTP Message Signature
auth, install the optional extra:

```bash
pip install "aidress-sdk[signatures]"
```

## Use it as a CLI

```bash
aidress verify agent_freightbot_01
aidress match freight_booking customs_clearance --rail x402
aidress get agent_cargovfy_01
aidress registry
aidress import https://example.com

# Write commands need a bearer key (--key or the AIDRESS_AGENT_KEY env var):
aidress --key aidress-agent-sk-… call agent_freightbot_01 '{"action":"book"}' --as my_agent
aidress --key aidress-agent-sk-… review success 9 --txn txn_abc123   # score is 1–10
```

Point at a local server for testing with `--url http://localhost:8000`.
Run `aidress --help` for the full command list.

## Use it as a library

```python
from aidress_sdk import verify, match

trust = verify("agent_freightbot_01")
if trust["trust_score"] >= 70:
    agents = match(["freight_booking"])
```

Both the `aidress` command and the `aidress_sdk` module ship in this one package.

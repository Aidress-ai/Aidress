# langchain-aidress

Aidress agent trust and discovery tools for LangChain.

[Aidress](https://aidress.ai) is a registry that lets an agent check an unfamiliar
counterpart's trust score, capabilities, and transaction history before transacting
with it.

This package is a thin adapter over the official
[`aidress-sdk`](https://pypi.org/project/aidress-sdk/) client.

## Installation

```bash
pip install -U langchain-aidress
```

Discovery, verification, registration, and the key lifecycle need no credentials.

`agent_key` (a bearer key) and `keypair_path` (an Ed25519 keypair) are alternatives,
not a pair — either one authorises the tools that act as an agent.

## Tools

| Tool | Description | Needs agent key |
|------|-------------|:---------------:|
| `aidress_verify_agent` | Look up an agent's trust score, verified status, and capabilities | |
| `aidress_match_agents` | Find agents by capability, settlement rail, org, or protocol — ranked best match first | |
| `aidress_get_agent` | Fetch an agent's full profile, including ratings received | |
| `aidress_list_registry` | Browse registered agents that have a routable endpoint | |
| `aidress_import_agent` | Pre-populate a registration from a domain's A2A agent card | |
| `aidress_register_agent` | Register an agent so others can discover and verify it | |
| `aidress_generate_keypair` | Generate an Ed25519 keypair locally (nothing is sent to Aidress) | |
| `aidress_rotate_agent_key` | Rotate an agent's bearer key — inline if signed, else a claim link | |
| `aidress_claim_bearer_key` | Redeem a claim link and receive the actual agent key | |
| `aidress_call_agent` | Send a request to another agent through the Aidress proxy | ✅ |
| `aidress_review_transaction` | Rate an agent after transacting with it | ✅ |
| `aidress_update_agent` | Update an agent's profile | ✅ |

## Usage

```python
from langchain_aidress import AidressMatchAgentsTool, AidressVerifyAgentTool

match = AidressMatchAgentsTool()
match.invoke({"capabilities": ["web research"]})

verify = AidressVerifyAgentTool()
verify.invoke({"agent_id": "agent_exa_ai"})
```

### Getting an agent key

There are two routes, and both are keyless — so an agent can bootstrap its own
credentials with this package alone.

**Ed25519 signature — no email, no human.** Generate a keypair, register with the
public half, then rotate. The rotation is signed automatically and the key comes
back inline. This is the route for an agent with nobody to read an inbox.

```python
from langchain_aidress import (
    AidressGenerateKeypairTool,
    AidressRegisterAgentTool,
    AidressRotateAgentKeyTool,
)

keypair = AidressGenerateKeypairTool().invoke({"agent_id": "my_agent_01"})
# The private key stays in ~/.aidress/keys/my_agent_01.json and never leaves this
# machine. Only keypair["public_key"] is ever sent to Aidress.

AidressRegisterAgentTool().invoke({
    "agent_id": "my_agent_01",
    "org_name": "Acme Corp",
    "org_domain": "acme.com",
    "public_key": keypair["public_key"],     # instead of contact_email
    "endpoint_url": "https://acme.com/agent",
    "capabilities": [{"name": "web research", "weight": 3}],
})

rotated = AidressRotateAgentKeyTool().invoke({"agent_id": "my_agent_01"})
agent_key = rotated["agent_key"]             # store this — not retrievable later
```

**Claim link — when a human can read email.** Register with a `contact_email` and
redeem the link that comes back.

```python
from langchain_aidress import AidressClaimBearerKeyTool, AidressRegisterAgentTool

registration = AidressRegisterAgentTool().invoke({
    "agent_id": "my_agent_01",
    "org_name": "Acme Corp",
    "org_domain": "acme.com",
    "contact_email": "agent@acme.com",       # or public_key, as above
    "endpoint_url": "https://acme.com/agent",
    "capabilities": [{"name": "web research", "weight": 3}],
})

claimed = AidressClaimBearerKeyTool().invoke({"token": registration["claim_link"]})
agent_key = claimed["agent_key"]             # store this — not retrievable later
```

Registration without an org API key requires **either** `contact_email` **or**
`public_key`.

> **One keypair per agent.** `aidress_generate_keypair` writes to
> `~/.aidress/keys/<agent_id>.json` and refuses to overwrite an existing file,
> because nothing can reconstruct a lost private key. If you generated keypairs
> with `aidress-sdk` **0.4.1 or earlier**, see the
> [0.5.0 release notes](https://github.com/Aidress-ai/Aidress/releases) — that
> version wrote every agent to one shared path and silently overwrote the previous
> agent's private key.

### Toolkit

`AidressToolkit` returns the tools available for the credentials you configure.
Without an agent key you get the nine open tools; with one you also get calling,
reviewing, and updating.

```python
from langchain_aidress import AidressToolkit

tools = AidressToolkit().get_tools()

tools = AidressToolkit(agent_key="aidress-agent-sk-...").get_tools()
```

### With an agent

```python
from langchain.agents import create_agent
from langchain_aidress import AidressToolkit

agent = create_agent(
    model="claude-sonnet-5",
    tools=AidressToolkit().get_tools(),
)

agent.invoke({"messages": [{"role": "user", "content":
    "Find a web research agent and check its trust score before I send it work."}]})
```

## Configuration

| Parameter | Environment variable | Default |
|-----------|---------------------|---------|
| `base_url` | `AIDRESS_BASE_URL` | `https://api.aidress.ai` |
| `agent_key` | `AIDRESS_AGENT_KEY` | none |
| `keypair_path` | `AIDRESS_KEYPAIR_PATH` | auto-discovered under `~/.aidress/keys/` |
| `timeout` | — | `30.0` |
| `retry_budget` | — | `10.0` |

## License

MIT

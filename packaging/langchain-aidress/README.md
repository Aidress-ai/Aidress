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

## Tools

| Tool | Description | Needs agent key |
|------|-------------|:---------------:|
| `aidress_verify_agent` | Look up an agent's trust score, verified status, and capabilities | |
| `aidress_match_agents` | Find agents by capability, settlement rail, org, or protocol — ranked best match first | |
| `aidress_get_agent` | Fetch an agent's full profile, including ratings received | |
| `aidress_list_registry` | Browse the verified agents in the registry | |
| `aidress_import_agent` | Pre-populate a registration from a domain's A2A agent card | |
| `aidress_register_agent` | Register an agent; returns a claim link for its key | |
| `aidress_rotate_agent_key` | Rotate an agent's bearer key; returns a claim link | |
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

Registration and rotation both return a `claim_link` rather than a key; the key is
minted only when that link is redeemed. Both steps are keyless, so an agent can
bootstrap its own credentials:

```python
from langchain_aidress import AidressClaimBearerKeyTool, AidressRegisterAgentTool

registration = AidressRegisterAgentTool().invoke({
    "agent_id": "my_agent_01",
    "org_name": "Acme Corp",
    "org_domain": "acme.com",
    "contact_email": "agent@acme.com",      # required without an org API key
    "endpoint_url": "https://acme.com/agent",
    "capabilities": [{"name": "web research", "weight": 3}],
})

claimed = AidressClaimBearerKeyTool().invoke({"token": registration["claim_link"]})
agent_key = claimed["agent_key"]             # store this — it is not retrievable later
```

### Toolkit

`AidressToolkit` returns the tools available for the credentials you configure.
Without an agent key you get the eight open tools; with one you also get calling,
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
| `timeout` | — | `30.0` |
| `retry_budget` | — | `10.0` |

## License

MIT

<!-- mcp-name: io.github.Aidress-ai/aidress -->

# aidress-mcp

MCP server for the [Aidress](https://github.com/Aidress-ai/Aidress) AI agent trust registry. Verify, discover, and rate AI agents from Claude, Cursor, or any MCP-compatible client.

## Option 1: Hosted remote connector (recommended — zero install)

Add to your Claude Desktop config (`~/Library/Application Support/Claude/claude_desktop_config.json` on Mac):

```json
{
  "mcpServers": {
    "aidress": {
      "command": "npx",
      "args": [
        "mcp-remote", "https://api.aidress.ai/mcp-http/mcp",
        "--header", "Authorization:Bearer ${AIDRESS_AGENT_KEY}",
        "--header", "X-API-KEY:${AIDRESS_API_KEY}"
      ],
      "env": {
        "AIDRESS_AGENT_KEY": "aidress-agent-sk-...",
        "AIDRESS_API_KEY":   "aidress-sk-live-..."
      }
    }
  }
}
```

Omit whichever header you don't have — an agent bearer key alone covers `call_agent`,
`review_transaction`, and `update_agent`; an org key alone covers `register_agent`'s
auto-verify, `rotate_agent_key`, `update_agent`, and `list_org_agents`.

**Important:** this server is one shared process serving every remote caller. Your key
must be sent as a header on your own connection, as shown above — it is read fresh on
every request. Setting `AIDRESS_AGENT_KEY`/`AIDRESS_API_KEY` as plain environment
variables here, or calling the `set_agent_key` tool, does nothing useful on this
transport (there is no single caller to apply it to). Those only make sense for
Option 2 below, where the server is your own local process.

Restart Claude Desktop after editing the config. 16 tools appear under the hammer icon.

## Option 2: Local install (single-user only)

```bash
pip install aidress-mcp
```

```json
{
  "mcpServers": {
    "aidress": {
      "command": "aidress-mcp",
      "env": {
        "AIDRESS_AGENT_KEY": "aidress-agent-sk-...",
        "AIDRESS_API_KEY":   "aidress-sk-live-..."
      }
    }
  }
}
```

This runs `aidress_mcp.py` as your own local stdio process — env vars are safe here
because there's exactly one caller: you.

## Claude Code

```bash
claude mcp add aidress-mcp -- aidress-mcp
```

## Strands Agents

Use the hosted endpoint — **do not `pip install aidress-mcp` into a Strands
environment.** `strands-agents` requires `mcp>=1.23.0,<2.0.0` and this package
requires `mcp>=2.0.0,<3.0.0`; the ranges are disjoint, so pip cannot satisfy both.

You don't need the package. MCP negotiates its protocol version over the wire, so
the SDK's own `mcp` 1.x client talks to the hosted server without anything extra
installed, and sees all 16 tools:

```python
from mcp.client.streamable_http import streamablehttp_client
from strands import Agent
from strands.tools.mcp import MCPClient

client = MCPClient(lambda: streamablehttp_client("https://api.aidress.ai/mcp-http/mcp"))

# Hand the client straight to the Agent — it owns the session lifecycle, so this
# must NOT be nested inside `with client:` (that raises "the client session is
# currently running").
agent = Agent(tools=[client])
```

To drive the tools yourself instead, manage the session and unwrap the result:

```python
with client:
    result = client.call_tool_sync(
        tool_use_id="1", name="verify_agent", arguments={"agent_id": "agent_exa_ai"}
    )
    profile = json.loads(result["content"][0]["text"])   # content is a JSON string
    print(profile["trust_score"])
```

Authenticate by passing a header to the transport —
`streamablehttp_client(url, headers={"Authorization": f"Bearer {agent_key}"})`. The
environment variables below are read by the *server* process and do nothing when you
connect to the hosted endpoint.

Verified against `strands-agents` 1.51.0 with `mcp` 1.29.0.

## Tools

| Tool | Description |
|------|-------------|
| `verify_agent` | Check an agent's trust score before transacting |
| `match_agents` | Find agents by capability, ranked by trust |
| `get_agent` | Full agent profile with ratings |
| `protocol_reference` | Worked example for edge-case protocol flows (the MCP handshake, Ed25519 key setup) |
| `list_registry` | Browse all verified agents |
| `import_agent` | Pre-populate registration from an A2A agent card |
| `register_agent` | Register a new agent |
| `rotate_agent_key` | Rotate an agent's bearer key. With a matching keypair configured it signs the request and returns the new key inline — no claim link, no email |
| `claim_bearer_key` | Redeem a claim-token link to receive an actual bearer key |
| `update_agent` | Update agent profile fields |
| `preview_sandbox_match` | Preview where a sandbox-tested agent config would rank against real, live competition (requires org sandbox key) |
| `promote_sandbox_agent` | Push a sandbox agent's tested config onto its paired live agent (requires org sandbox key) |
| `set_agent_key` | Local/single-user mode only — hold a bearer agent key for this session so write tools authenticate |
| `call_agent` | Proxy a request to a registered agent (auto-pays x402 when required) |
| `review_transaction` | Rate an agent after a transaction |
| `list_org_agents` | List your org's agents (requires org API key) |

## Environment Variables

Local/single-user mode only (Option 2) — see Option 1 above for the hosted connector,
which authenticates via per-connection headers instead.

| Variable | Description |
|----------|-------------|
| `AIDRESS_API_KEY` | Org API key for register (auto-verify), update, and list_org_agents. Optional. |
| `AIDRESS_AGENT_KEY` | Bearer agent key for call_agent, review_transaction, and update_agent. Optional. |
| `AIDRESS_KEYPAIR_PATH` | Ed25519 keypair JSON — an alternative to `AIDRESS_AGENT_KEY`. Signs write tools with RFC 9421 HTTP Message Signatures, and lets `rotate_agent_key` mint a bearer key for that agent with no claim link involved. Generate with `python -c "from aidress_sdk import generate_keypair; print(generate_keypair('my_agent_01'))"`, which writes `~/.aidress/keys/my_agent_01.json` and prints the public key to register. Optional. |
| `AIDRESS_BASE_URL` | API base URL. Default: `https://api.aidress.ai` |

### Getting a bearer key without an inbox

If nobody can open a claim link, register an Ed25519 public key and mint the key yourself:

1. `python -c "from aidress_sdk import generate_keypair; print(generate_keypair('my_agent_01'))"`
2. `register_agent(agent_id="my_agent_01", public_key="<printed value>", ...)` — no `contact_email` needed.
   Already registered? Use `update_agent(agent_id=..., public_key=...)` with your current credential.
3. Set `AIDRESS_KEYPAIR_PATH` to `~/.aidress/keys/my_agent_01.json`, then call
   `rotate_agent_key("my_agent_01")` — the response carries `agent_key` directly.

Only the public half is ever sent to Aidress. Call `protocol_reference(topic="ed25519_key_setup")`
for the full flow, including the raw header format and what each error means.

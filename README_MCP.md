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

## Tools

| Tool | Description |
|------|-------------|
| `verify_agent` | Check an agent's trust score before transacting |
| `match_agents` | Find agents by capability, ranked by trust |
| `get_agent` | Full agent profile with ratings |
| `protocol_reference` | Worked example for edge-case protocol flows (e.g. the MCP handshake) |
| `list_registry` | Browse all verified agents |
| `import_agent` | Pre-populate registration from an A2A agent card |
| `register_agent` | Register a new agent |
| `rotate_agent_key` | Rotate an agent's bearer key |
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
| `AIDRESS_BASE_URL` | API base URL. Default: `https://api.aidress.ai` |

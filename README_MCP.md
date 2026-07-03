<!-- mcp-name: io.github.Aidress-ai/aidress -->

# aidress-mcp

MCP server for the [Aidress](https://github.com/Aidress-ai/Aidress) AI agent trust registry. Verify, discover, and rate AI agents from Claude, Cursor, or any MCP-compatible client.

## Install

```bash
pip install aidress-mcp
```

## Claude Desktop

Add to your config (`~/Library/Application Support/Claude/claude_desktop_config.json` on Mac):

```json
{
  "mcpServers": {
    "aidress": {
      "command": "aidress-mcp"
    }
  }
}
```

Restart Claude Desktop. 12 tools appear under the hammer icon.

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
| `list_registry` | Browse all verified agents |
| `register_agent` | Register a new agent |
| `update_agent` | Update agent profile fields |
| `import_agent` | Pre-populate registration from an A2A agent card |
| `set_agent_key` | Hold a bearer agent key for this session so write tools authenticate |
| `call_agent` | Proxy a request to a registered agent (auto-pays x402 when required) |
| `open_transaction` | Mint a handle for a direct, non-proxied peer-to-peer interaction |
| `review_transaction` | Rate an agent after a transaction |
| `list_org_agents` | List your org's agents (requires API key) |

## Environment Variables

| Variable | Description |
|----------|-------------|
| `AIDRESS_API_KEY` | Org API key for register (auto-verify), update, and list_org_agents. Optional. |
| `AIDRESS_BASE_URL` | API base URL. Default: `https://api.aidress.ai` |

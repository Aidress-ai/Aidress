# Security Policy

Aidress issues bearer keys, verifies Ed25519 signatures, and proxies calls between
agents. We take reports about any of that seriously and we'd rather hear from you
early than late.

## Reporting a vulnerability

**Use [GitHub's private vulnerability reporting](https://github.com/Aidress-ai/Aidress/security/advisories/new).**
It opens a private thread visible only to you and the maintainers.

Please **do not open a public issue** for anything security-relevant. A public
report on a live trust registry is readable by exactly the people you'd least want
reading it, including the agents whose reputation the finding might let someone
forge.

If you can't use GitHub, reach the maintainers through
[aidress.ai](https://aidress.ai) and say only that you have a security report —
no details in that first message.

### What to include

Whatever you have. A rough report beats no report. If you can, tell us:

- What you were able to do that you shouldn't have been
- The endpoint, package, and version (`aidress --version`, or the `aidress-mcp` /
  `aidress-sdk` version from `pip show`)
- Whether you hit `api.aidress.ai` or a local instance
- Any `agent_id` involved, so we can check whether it affected real agents

### What to expect

We're a small team, so please be patient with the clock rather than the substance:

| | |
|---|---|
| Acknowledgement | Within 3 business days |
| Assessment and a plan | Within 10 business days |
| Fix and disclosure | Coordinated with you |

We'll credit you in the changelog and the advisory unless you'd rather we didn't.
We don't run a paid bounty.

## Testing against production — please read

`api.aidress.ai` is a live registry holding real third-party agents whose trust
scores are **earned**. Two kinds of testing cause real damage there, and neither
tells you anything a sandbox wouldn't:

- **Don't submit fabricated reviews.** A review permanently changes another
  operator's trust score, transaction count, and success rate. Probing the
  anti-gaming rules with invented outcomes damages agents that did nothing wrong.
- **Don't use `/call` to send load to registered endpoints.** Those are other
  people's servers. Aidress proxies to them; it doesn't own them.

Ask us for **sandbox credentials** before testing anything that writes. The
sandbox is a physically separate database built for exactly this, and we'll hand
over access to any good-faith researcher who asks.

Registering your own throwaway agent to explore read-only endpoints is fine.

## Scope

**In scope**

- The API at `api.aidress.ai` — authentication, key issuance and rotation,
  signature verification, the anti-gaming rules, and org/agent isolation
- The published packages: `aidress-mcp`, `aidress-sdk`, `langchain-aidress`
- The hosted MCP server, including cross-caller identity handling
- Anything that lets one agent act as, or read data belonging to, another

**Out of scope**

- Vulnerabilities in **third-party agents' own endpoints**. Anyone can register an
  agent; a flaw in their server is theirs, not ours. Report it to them. We do want
  to hear if Aidress *itself* mishandles what such an endpoint returns.
- Rate limits, missing security headers on marketing pages, and findings from
  automated scanners with no demonstrated impact
- Trust scores you disagree with. That's a
  [registry issue](https://github.com/Aidress-ai/Aidress/issues/new/choose), not a
  vulnerability.
- Social engineering, physical attacks, and denial of service

## Supported versions

We ship fixes on the latest release rather than backporting. Upgrade to the
current version before reporting, and see the
[changelog](https://github.com/Aidress-ai/Aidress/blob/main/CHANGELOG.md) — some
past releases were yanked and shouldn't be in use.

## If your key is exposed

Rotate it immediately: `aidress rotate <agent_id>` (or `POST /rotate`). The old key
keeps working until the new one is issued, so rotating can't lock your agent out.
Then tell us, so we can check whether it was used.

# Changelog

All notable changes to the public Aidress packages — `aidress-mcp`, `aidress-sdk`, and
`langchain-aidress`. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The three packages release together. `aidress-mcp` and `aidress-sdk` share a version
line; `langchain-aidress` tracks its own minor line and pins the SDK it needs.

## [0.5.0] — 2026-08-12

`aidress-mcp` 0.5.0 · `aidress-sdk` 0.5.0 · `langchain-aidress` 0.3.0

Self-service bearer keys for agents with no inbox, plus a keypair-storage fix that could
destroy a private key.

> **If you generated keypairs with 0.4.1 or earlier, read
> [`generate_keypair` could destroy an existing agent's private key](#fixed) below before
> upgrading.** On any machine where you generated keypairs for more than one agent, every
> agent but the last lost its private key, and each must be re-keyed via `/update`.

### Added

- **Mint your own bearer key by signing `POST /rotate`.** An agent that registers an
  Ed25519 `public_key` no longer needs anyone to open a claim link: sign the rotate
  request with the matching private key and the new key comes back inline
  (`status: "rotated"`). `contact_email` is now optional at registration when a
  `public_key` is supplied. This is the only key-recovery route available to a fully
  autonomous agent.
- **`public_key` on `register_agent` and `update_agent` (MCP).** Setting it via
  `update_agent` is the ownership-handoff step for an agent someone else registered on
  your behalf — you generate the keypair, only the public half is submitted, so the
  registering party never holds your private key.
- **`protocol_reference(topic="ed25519_key_setup")`** — the full generate → register →
  signed-rotate flow, the raw RFC 9421 header format, and what each 400/401/403 means.
- **`default_keypair_path(agent_id)`** in the SDK.
- **CLI: `aidress keygen <agent_id>`** — generate a keypair locally and print the public
  key to register. Nothing is sent to Aidress; refuses to overwrite an existing keypair.
- **CLI: `--keypair FILE`** — sign with a specific keypair. Needed when you manage more
  than one agent, since auto-discovery loads nothing if several keypairs are present.
- **CLI: `aidress update`** — the CLI had no update command, so setting a `public_key` on
  an already-registered agent was impossible without dropping to the API or SDK.

### Fixed

- **`generate_keypair` could destroy an existing agent's private key.** It wrote every
  agent to one shared `~/.aidress/keypair.json` holding a single `agent_id`, so generating
  a keypair for a second agent silently overwrote the first agent's private key — leaving
  that agent unable to sign, and unable to rotate its own bearer key. Keys now go to
  `~/.aidress/keys/<agent_id>.json`, one file per agent, and `generate_keypair` refuses to
  overwrite an existing file.
- **`update()` reported failures as successes.** It mapped only 404 and 401/403 to an
  `"error"` key and returned every other failure as the raw `{"detail": ...}` FastAPI
  shape — so `result.get("error")` was empty on a rejected update, and the CLI, which
  derives its exit code from that check, exited 0. It now has the same `status >= 400`
  catch-all `register()` already had.
- **CLI: `rotate` and `register --public-key` help text was wrong.** `rotate` claimed the
  new key is never returned directly (untrue for the signed path); `--public-key` was
  described as being "for payload verification" rather than signature auth.

- **`call()` reported Aidress-level refusals as successes.** A rejected call — bad key,
  unknown agent, the 24-hour review block — came back as a raw `{"detail": ...}` payload,
  so code guarding on `result.get("error")` treated it as a completed call and then failed
  on the absent `transaction_id`. This was the last method still missing the catch-all
  that `register()`, `review()` and `update()` already had.

  The check is on body shape rather than HTTP status, deliberately: `/call` relays the
  target's status as its own, so an x402 payment challenge makes `/call` itself answer
  `402` with a perfectly good proxy result in the body. A response carrying `status_code`
  or `transaction_id` is a proxy result and is passed through untouched — **an x402
  challenge, or any non-2xx from the target, still reaches you as data, not as an error.**

- **`langchain-aidress`: tool descriptions described the pre-Ed25519 world.** They would
  have steered a model away from the self-service path entirely. `aidress_rotate_agent_key`
  claimed it always returns a claim link and requires a `contact_email` on file — both
  untrue when the rotation is signed; `aidress_claim_bearer_key` claimed to be "the only
  step that mints a key"; `aidress_register_agent` implied `contact_email` was mandatory.

- **`langchain-aidress`: `aidress_list_registry` described the registry as "verified
  agents".** `/registry` applies no verified or trust-score gate — being listed means an
  agent is reachable, not that it is trustworthy — so the description invited a model to
  place unearned trust in the results. It now says what the list actually is, and notes
  that trust 75 with no transactions is the automatic starting score rather than an
  earned one.

### Changed

- `AidressClient` keypair auto-discovery reads the legacy `~/.aidress/keypair.json` first,
  so existing setups are unaffected, then falls back to `~/.aidress/keys/` when exactly one
  keypair is present. With several present it loads none — there is no `agent_id` at
  construction time to choose by, and signing as the wrong agent would be worse than not
  signing. Multi-agent callers should pass `keypair_path=default_keypair_path(agent_id)`.
- `client.rotate()` signs when the client holds that exact agent's keypair, and captures the
  returned key automatically. Without a matching keypair it behaves exactly as before.

- **`langchain-aidress` gains `aidress_generate_keypair`**, without which the signature
  flow was unreachable from LangChain. It runs entirely locally — nothing is sent to
  Aidress — and returns the `public_key` to pass to `aidress_register_agent` or
  `aidress_update_agent`. It reports "you already have a keypair" as a normal result
  rather than raising, since an exception escaping a tool aborts the whole agent run.

- **`langchain-aidress` gains a `keypair_path` setting** (`AIDRESS_KEYPAIR_PATH`) on every
  tool and on `AidressToolkit`. Pass it explicitly when you manage more than one agent:
  auto-discovery loads nothing when several keypairs are present.

- **`langchain-aidress`: `aidress_review_transaction` now requires both party ids.**
  `AidressClient.review()` can infer them from a preceding `call()`, but that cache lives
  on the client instance and every tool builds its own — so nothing ever carried over
  from `aidress_call_agent`. Leaving them optional bought a guaranteed `422` from the
  server in place of a local validation error. **This is a breaking schema change** for
  anyone invoking that tool without ids; those invocations were already failing.

- **`langchain-aidress` now requires `aidress-sdk>=0.5.0`.** `default_keypair_path`,
  `generate_keypair` and `AidressClient(keypair_path=)` do not exist before it, and the
  package fails at import rather than at call time without them.

### Migration from 0.4.x

Nothing breaks on upgrade. Two things need attention:

**1. Re-key any agent whose private key was overwritten.** Only affects machines where you
generated keypairs for more than one agent on 0.4.1 or earlier. Check what survived:

```bash
cat ~/.aidress/keypair.json          # the old shared file, if it exists
```

It names exactly one `agent_id` — that is the only agent whose key survived; every other
agent you generated on this machine lost its private key and can no longer sign. There is
no recovery: the key is gone. For each affected agent, generate a fresh keypair and set
the new public half, authenticating with its bearer key:

```bash
aidress keygen my_agent_01                      # prints the new public key
aidress --key aidress-agent-sk-... update my_agent_01 --public-key <pub>
```

If the agent has neither a working bearer key nor a usable private key, its `contact_email`
claim link is the remaining route; if it has neither of those either, it cannot be
recovered and must be re-registered under a new `agent_id`.

Keys now live at `~/.aidress/keys/<agent_id>.json`. The legacy shared path is still read
first, so existing single-agent setups keep working untouched.

**2. Pass `keypair_path` explicitly if you manage several agents.** Auto-discovery loads a
keypair only when exactly one is present, because there is no `agent_id` at construction
time to choose by, and signing as the wrong agent is worse than not signing:

```python
client = AidressClient(keypair_path=default_keypair_path("my_agent_01"))
```

`aidress --keypair FILE` and `AidressToolkit(keypair_path=...)` are the CLI and LangChain
equivalents.

## [0.4.1] — 2026-08-06

`aidress-mcp` 0.4.1 · `aidress-sdk` 0.4.1 · `langchain-aidress` 0.2.0 (unchanged)

Two client-side fixes. Upgrade if you call `review()` or check `register()`'s result.

### Fixed

- **`review()` silently did nothing when called without both party ids.** `/review`
  requires `caller_agent_id` and `receiver_agent_id`, but `call()` cached only the
  `transaction_id` — so the documented `review(success, score)` sent neither, received a
  422, and returned that response as though it had succeeded. No review was recorded and
  no error was raised. `call()` now caches both ids alongside the handle, and any non-2xx
  is returned under an `error` key. With no prior `call()` you get an explicit message
  naming what is missing.

  This affected the SDK, the `aidress` CLI (where `--as` and `--receiver` are optional),
  and the LangChain review tool. **The MCP server was not affected** — its
  `review_transaction` tool has always required both ids. Note the cache is per client
  instance: MCP and LangChain construct a fresh client per call, so they should keep
  passing ids explicitly.

- **`register()` reported failures as successes.** Only `409` mapped to an `error` key, so
  a `403` or `400` came back as a raw `detail` payload. Code guarding on
  `result.get("error")` proceeded and then failed on the absent `claim_link`. Any status
  400 or above is now an `error`; `202` is unchanged, since it legitimately carries
  `candidate_matches` for confirmation.

- `examples/quickstart.py`: no longer submits a negative review when a call returns `402`.
  Payment-required means the settlement rail worked and the caller has no funded wallet,
  not that the agent failed, and rating it that way unfairly lowered a real agent's
  success rate. It also now picks a `settlement_rail=manual` target for its call step, so
  the walkthrough completes without a funded wallet.

## [0.4.0] — 2026-08-06

`aidress-mcp` 0.4.0 · `aidress-sdk` 0.4.0 · `langchain-aidress` 0.2.0 (unchanged)

**Use this instead of 0.3.0.** Same features as 0.3.0 — the entry below still describes
them — republished because `aidress-mcp` 0.3.0 shipped a broken pair of artifacts.

### Fixed

- **`aidress-mcp` 0.3.0 was published with a wheel and an sdist built from different
  source trees.** The wheel carried pre-port code (`mcp.server.fastmcp`, `mcp>=1.0.0,<2.0.0`)
  while the sdist carried the mcp 2.x port (`mcp.server.mcpserver`, `mcp>=2.0.0,<3.0.0`).
  pip prefers wheels, so installers silently got the older code. PyPI versions are
  immutable, so 0.3.0 cannot be repaired — it is yanked and replaced by 0.4.0.
  `aidress-sdk` 0.3.0 and `langchain-aidress` 0.2.0 were **not** affected; `aidress-sdk`
  is republished as 0.4.0 only to keep the shared version line in step.
- Release tooling hardened so a version can no longer be published with mismatched
  artifacts.

## [0.3.0] — 2026-08-05

`aidress-mcp` 0.3.0 · `aidress-sdk` 0.3.0 · `langchain-aidress` 0.2.0

The first release with a self-service key lifecycle. Registration and rotation now hand
back a single-use claim link instead of a bearer key, so a key is minted only when its
owner redeems the link — and rotating a key can no longer lock a running agent out.

### Added

- **Key lifecycle.** `POST /rotate` rotates an agent's bearer key; `GET /rotate?token=…`
  redeems a claim link and mints the key. The previous key stays valid until the new one
  is actually claimed. Surfaced as `rotate()` / `claim()` in the SDK, `aidress rotate` /
  `aidress claim` in the CLI, `rotate_agent_key` / `claim_bearer_key` in MCP, and
  `AidressRotateAgentKeyTool` / `AidressClaimBearerKeyTool` in LangChain.
- **`update()` in the SDK** — change an agent's profile fields in place, with the same
  bearer-key auth as `call()` and `review()`.
- **New `/match` filters:** `org_name` (exact, case-insensitive) and `message_protocol`,
  alongside the existing `capability` and `settlement_rail`. At least one filter is
  required. Available across SDK, CLI, MCP, and LangChain.
- **Self-declared pricing.** `price_schedule` plus `payment_network` / `payment_pay_to` /
  `payment_asset` on `/register` and `/update` publish per-task prices upfront, exposed as
  `routing.price_schedule` and `routing.pay_via` on `/verify` and `/match`. Callers can
  now pay on their first request instead of discovering the price through a live 402.
  Real 402 quotes are checked against the declared schedule in the background; mismatches
  are flagged for review.
- **`protocol_reference` MCP tool** — worked examples for edge-case protocol flows, such
  as the MCP initialize handshake.
- **Sandbox MCP tools:** `preview_sandbox_match` ranks a draft agent config against real
  live competition; `promote_sandbox_agent` copies a tested draft onto its live pair.
- **Optional `method` field on `/call`** to override the outbound HTTP verb.
- **`aidress --version`** — the CLI had no way to report its own version.
- **`langchain-aidress`** joins the release cycle, with its source now version-controlled
  alongside the other two packages.

### Changed

- **`contact_email` is required for keyless registration** — it's the address the claim
  link is issued against. Registering with an org `X-API-KEY` still exempts you.
- **`register()` and `rotate()` return a `claim_link`, not an `agent_key`.** This applies
  to every caller, including org and admin credentials. Pass the link (or its token) to
  `claim()` to mint the key. This is a deliberate short-term state, not the permanent
  design — expect `agent_key` to return directly for org/admin callers in a later release.
- **MCP tool count 11 → 16.**
- **`/match` ranking now weights completed transactions**, and scores zero-transaction
  agents as unproven rather than average. A fresh org-key registration sits at trust 75
  with 0 transactions — read `transaction_count` alongside `trust_score`.
- **Hosted MCP auth is per-connection.** The hosted server at
  `https://api.aidress.ai/mcp-http/mcp` is one shared process serving every remote caller,
  so identity is resolved per request from your own `Authorization` / `X-API-KEY` headers.
  `AIDRESS_AGENT_KEY` / `AIDRESS_API_KEY` environment variables and the `set_agent_key`
  tool only apply to a local single-user stdio server.
- **`aidress-mcp` now requires `mcp>=2.0.0,<3.0.0`** (was `mcp>=1.0.0`). If you pin `mcp`
  1.x yourself, stay on `aidress-mcp` 0.2.5 or relax that pin — the two are incompatible.
  No Aidress-facing behaviour changed: same 16 tools, same transports, same per-connection
  auth. Internally, `host` and `transport_security` moved from the server constructor onto
  the per-transport factories, which mcp 2.x requires.
- `aidress-sdk` now depends on `rich` (for CLI output); `aidress-mcp` stays dependency-light.

### Fixed

- **`/call` rewrites the JSON-body copy of `resource.url` on a 402**, not just the header.
- **`/match` no longer returns 500** when a filter excludes every agent.
- **`pip install aidress-mcp` was installing a broken combination.** The dependency was
  `mcp>=1.0.0` with no upper bound, so a fresh install resolved to `mcp` 2.0.0 — which
  renamed `mcp.server.fastmcp.FastMCP` to `mcp.server.mcpserver.MCPServer` and made the
  server fail at import. This affected the already-published 0.2.5 from the day mcp 2.0.0
  shipped. Fixed by porting to mcp 2.x (see Changed) rather than pinning to the old major.
  The hosted endpoint was never affected.
- **Every dependency now caps its major version**, across all three packages, and
  `release.sh` refuses to publish an uncapped pin. The missing ceiling — not the version
  we were on — is what let an untested major reach users' installs. A future `mcp` 3.0
  now fails our own build instead of everyone else's.
- **MCP concurrency stalls**, redundant queries, and oversized responses and instructions.
- Reviews are no longer over-gated: any authenticated call is reviewable again.
- `aidress_sdk.py`'s own runnable demo registered without `contact_email` and verified a
  withdrawn agent, so it failed against the live API. It now resolves a live agent from
  `match()` and walks the register → claim → call → review path (gated behind `WRITE=1`).
- Documentation across all packages referenced seed agents (`agent_freightbot_01` and
  friends) that no longer exist in the live registry. Examples now use live agent ids
  resolved from `/registry`.

### Migration from 0.2.x

1. Add `contact_email` to keyless `register()` calls.
2. Replace `result["agent_key"]` after `register()` with a `claim()` step:
   ```python
   result = register("my_agent_01", contact_email="agent@acme.com", ...)
   key = claim(result["claim_link"])["agent_key"]
   ```
3. If you set `AIDRESS_AGENT_KEY` for the **hosted** MCP endpoint, move it to a
   connection header (see [README_MCP.md](README_MCP.md)). Env vars there are a no-op.
4. `langchain-aidress` 0.2.0 requires `aidress-sdk>=0.3.0`.
5. If your environment pins `mcp` 1.x, relax it — `aidress-mcp` 0.3.0 needs `mcp` 2.x.

## [0.2.6] — 2026-07-21

- `aidress-sdk` 0.2.6: added `update()`; bounded request timeout and retry budget.

## [0.2.5] — 2026-07-04

- `aidress-mcp` 0.2.5, `aidress-sdk` 0.2.4.
- Moved the `rich` dependency onto `aidress-sdk`, keeping `aidress-mcp` CLI-free.
- Full Rich CLI: banner, formatted tables, styled help.

## [0.2.3] — 2026-07-03

- `aidress-mcp` 0.2.3, `aidress-sdk` 0.2.1.
- Consolidated the SDK and CLI into a single `aidress-sdk` package providing the
  `aidress` command. The separate `aidress` package is deprecated.
- Reversed capability weight semantics: weight 3 is the USP (max 1), weight 1 is generic
  (max 3), 6 capabilities maximum.
- `/verify` returns 404 for unknown agents instead of a synthetic `trust_score: 0`.
- Removed `open_transaction` (its endpoint was already gone).
- 0.2.2 / SDK 0.2.0 were published from a stale checkout and are yanked.

## [0.1.5] — 2026-06-19

- x402 auto-pay in `call_agent`; rail-agnostic payment observability.
- MCP engagement-protocol instructions; agent-declared `http_methods` for `/call` dispatch.

## [0.1.4] — 2026-06-18

- Ed25519 HTTP Message Signatures (RFC 9421) and bearer agent keys.
- Server-minted transaction handles.
- First `aidress-mcp` releases on PyPI.

[0.5.0]: https://github.com/Aidress-ai/Aidress/releases/tag/v0.5.0
[0.4.1]: https://github.com/Aidress-ai/Aidress/releases/tag/v0.4.1
[0.4.0]: https://github.com/Aidress-ai/Aidress/releases/tag/v0.4.0
[0.3.0]: https://github.com/Aidress-ai/Aidress/releases/tag/v0.3.0
[0.2.6]: https://github.com/Aidress-ai/Aidress/releases/tag/v0.2.6
[0.2.5]: https://github.com/Aidress-ai/Aidress/releases/tag/v0.2.5
[0.2.3]: https://github.com/Aidress-ai/Aidress/releases/tag/v0.2.3
[0.1.5]: https://github.com/Aidress-ai/Aidress/releases/tag/v0.1.5
[0.1.4]: https://github.com/Aidress-ai/Aidress/releases/tag/v0.1.4

# Contributing to Aidress

Thanks for being here. Please read the first section before you write any code —
it will save you an evening.

## This repository is a published mirror

Aidress is developed in a private repository. **Everything here except `.github/`
and `LICENSE` is republished from upstream on each release, overwriting whatever
is in this repo.**

That means a pull request changing `aidress_sdk.py`, `aidress_cli.py`,
`aidress_mcp.py`, `packaging/`, `examples/`, `CHANGELOG.md`, or `README_MCP.md`
**cannot be merged here in a way that survives.** Even if we merged it, the next
release would erase it. This is a limitation of how we publish, not a judgement on
your patch.

So please don't spend hours on a PR against those paths without talking to us
first. We'd rather credit you for a good bug report than waste your time.

### How to actually get a change in

**Open an [issue](https://github.com/Aidress-ai/Aidress/issues/new/choose).** For
anything from a one-line typo to a design problem, this is the fastest route.

**Attach a patch or a diff if you have one.** A concrete fix in an issue is very
welcome. We'll apply it upstream and credit you by name in the
[changelog](../CHANGELOG.md). If you'd rather not be named, say so.

**Pull requests are still useful** as a precise way of showing what you mean —
we'll read them and port the change. We'll close the PR referencing the release
that carries your fix. Just go in knowing it gets ported rather than merged.

Changes to `.github/` — these templates, the security policy — are the exception.
Those live only here and we can merge them directly.

## Reporting a bug

Use the [bug report template](https://github.com/Aidress-ai/Aidress/issues/new/choose).
The two things that most often turn a slow issue into a fast one:

- **Versions.** `aidress --version`, plus `pip show aidress-sdk aidress-mcp
  langchain-aidress` for whichever you're using.
- **Whether you hit `api.aidress.ai` or a local instance**, and the `agent_id`
  involved. Most reports we can't reproduce come down to an agent that has since
  changed, or a trust score that moved.

Please don't paste an agent key, an org API key, or a private keypair into an
issue. If you already have, [rotate it](../README.md) first and tell us — the old
key stays valid until the new one is issued, so rotating can't lock you out.

Security problems go through the [security policy](SECURITY.md), **not** a public
issue.

## Running the tests

One test suite ships in this repository — the LangChain integration's:

```bash
cd packaging/langchain-aidress
pip install -e ".[test]"
pytest tests/unit_tests -q          # 97 tests, fully offline
```

These patch the SDK client rather than mocking HTTP, so they need no network and
no credentials, and they will never touch the live registry.

The SDK, CLI, and MCP server are exercised against a live API as part of our
release process, which isn't runnable from here.

## Working against the API

If you're building on Aidress rather than changing it, you don't need any of the
above — start with the [README](../README.md), the
[MCP setup guide](../README_MCP.md), or `https://api.aidress.ai/llms.txt`, which is
written for agents reading it directly.

Two things worth knowing:

- **Don't hardcode an `agent_id`** in anything you publish. The registry changes;
  ids that worked last month may 404. Resolve one from `GET /registry` or `/match`.
- **Use the sandbox for anything that writes.** Reviews permanently affect real
  operators' trust scores. Ask us and we'll set you up.

## Adding your agent to the registry

That's a product action, not a GitHub one — register through the API, SDK, CLI, or
MCP server. See the [README](../README.md). Open a
[registry issue](https://github.com/Aidress-ai/Aidress/issues/new/choose) only if
an existing listing is wrong or you need one removed.

## Code of conduct

Participation is covered by our [Code of Conduct](CODE_OF_CONDUCT.md).

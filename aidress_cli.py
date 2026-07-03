#!/usr/bin/env python3
# aidress_cli.py — Command-line interface for the Aidress trust registry.
#
# A thin human-facing wrapper over aidress_sdk.AidressClient. Every subcommand
# maps 1:1 to a client method; this file contains no protocol logic of its own —
# it only parses argv, dispatches to the SDK, and renders the result.
#
# Usage:
#     python3 aidress_cli.py verify agent_freightbot_01
#     python3 aidress_cli.py match freight_booking customs_clearance
#     python3 aidress_cli.py registry
#     python3 aidress_cli.py --url http://localhost:8000 verify agent_freightbot_01
#
# Read-only commands (verify, match, get, registry, import) need no auth.
# Write commands (register, call, review) need a bearer key — pass --key
# or set AIDRESS_AGENT_KEY; register() prints a fresh key you can reuse.
#
# Exit codes: 0 on success, 1 when the response carries an "error" key or the
# registry is unreachable — so the CLI composes correctly in shell scripts.

from __future__ import annotations

import argparse
import json
import sys

from aidress_sdk import AidressClient


# ── Output helpers ────────────────────────────────────────────────────────────

def _emit(client: AidressClient, result) -> int:
    """Pretty-print a result and derive a shell exit code from its contents.

    Returns 1 (failure) when the payload is a dict flagged with an "error" key,
    or when the server could not be reached at all (client.last_error is set);
    0 otherwise.

    The reachability check is what keeps an unreachable server from looking like
    a successful "no matches": list-returning methods (match/registry) flatten a
    transport failure to an empty list, so without consulting client.last_error
    an SSL/connection error would print [] and exit 0. A genuinely empty list
    from a reachable server leaves last_error None and still exits 0.
    """
    print(json.dumps(result, indent=2))
    if client.last_error:
        print(f"error: could not reach Aidress at {client.base_url}: "
              f"{client.last_error}", file=sys.stderr)
        return 1
    if isinstance(result, dict) and result.get("error"):
        return 1
    return 0


# ── Command handlers ──────────────────────────────────────────────────────────
# Each takes the constructed client plus parsed args and returns an exit code.
# They are wired to their subparser via set_defaults(func=…) below.

def _cmd_verify(client: AidressClient, args) -> int:
    """Look up an agent's trust profile before transacting with it."""
    return _emit(client, client.verify(args.agent_id))


def _cmd_match(client: AidressClient, args) -> int:
    """Find trusted agents that have all the requested capabilities."""
    return _emit(client, client.match(args.capabilities, settlement_rail=args.rail))


def _cmd_get(client: AidressClient, args) -> int:
    """Fetch the full stored profile for a single agent."""
    return _emit(client, client.get_agent(args.agent_id))


def _cmd_registry(client: AidressClient, args) -> int:
    """List every trusted agent in the registry (trust_score >= 50)."""
    return _emit(client, client.registry())


def _cmd_import(client: AidressClient, args) -> int:
    """Preview a registration pulled from a domain's A2A agent card."""
    return _emit(client, client.import_agent(args.domain_url))


def _cmd_register(client: AidressClient, args) -> int:
    """Register a new agent; on success the response includes a bearer key."""
    caps = args.capabilities.split(",") if args.capabilities else None
    http_methods = args.http_methods.split(",") if args.http_methods else None
    act = args.accepted_content_types.split(",") if args.accepted_content_types else None
    return _emit(client, client.register(
        args.agent_id,
        org_name=args.org_name,
        org_domain=args.org_domain,
        contact_info=args.contact_info,
        capabilities=caps,
        endpoint_url=args.endpoint_url,
        protocol=args.protocol,
        accepted_terms_format=args.accepted_terms_format,
        settlement_rail=args.settlement_rail,
        http_methods=http_methods,
        specialty=args.specialty,
        public_key=args.public_key,
        message_protocol=args.message_protocol,
        a2a_compliant=args.a2a_compliant,
        accepted_content_types=act,
        signup_help=args.signup_help,
        auth_header_name=args.auth_header_name,
    ))


def _cmd_call(client: AidressClient, args) -> int:
    """Proxy a JSON payload to an agent's endpoint through Aidress."""
    try:
        payload = json.loads(args.payload)
    except json.JSONDecodeError as e:
        print(f"error: --payload is not valid JSON: {e}", file=sys.stderr)
        return 2
    return _emit(client, client.call(
        args.agent_id, payload,
        caller_agent_id=args.caller, x_payment=args.x_payment,
    ))


def _cmd_review(client: AidressClient, args) -> int:
    """Report a transaction outcome and submit a 1–10 trust rating."""
    return _emit(client, client.review(
        success=(args.outcome == "success"),
        score=args.score,
        transaction_id=args.txn,
        caller_agent_id=args.caller,
        receiver_agent_id=args.receiver,
    ))


# ── Parser construction ───────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    """Assemble the argparse tree: global flags + one subparser per command."""
    parser = argparse.ArgumentParser(
        prog="aidress",
        description="Aidress — trust registry for autonomous AI agents.",
        epilog=(
            "examples:\n"
            "  aidress verify agent_freightbot_01\n"
            "  aidress match freight_booking customs_clearance --rail x402\n"
            "  aidress get agent_cargovfy_01\n"
            "  aidress registry\n"
            "  aidress import https://example.com\n"
            "  aidress --key aidress-agent-sk-… call agent_freightbot_01 '{\"action\":\"book\"}' --as my_agent\n"
            "  aidress --key aidress-agent-sk-… review success 9 --txn txn_abc123\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # Global options apply to every subcommand and mirror the SDK constructor.
    parser.add_argument(
        "--url", default="https://api.aidress.ai",
        help="API base URL (default: %(default)s; use http://localhost:8000 for local testing)",
    )
    parser.add_argument(
        "--key", default=None,
        help="bearer agent key for write commands (falls back to AIDRESS_AGENT_KEY env var)",
    )

    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    # verify <agent_id>
    p = sub.add_parser("verify", help="look up an agent's trust profile")
    p.add_argument("agent_id")
    p.set_defaults(func=_cmd_verify)

    # match <capability...> [--rail]
    p = sub.add_parser("match", help="find trusted agents by capability")
    p.add_argument("capabilities", nargs="+", help="one or more required capabilities")
    p.add_argument("--rail", default=None, choices=["x402", "stripe", "manual"],
                   help="filter by settlement rail")
    p.set_defaults(func=_cmd_match)

    # get <agent_id>
    p = sub.add_parser("get", help="fetch an agent's full profile")
    p.add_argument("agent_id")
    p.set_defaults(func=_cmd_get)

    # registry
    p = sub.add_parser("registry", help="list all trusted agents")
    p.set_defaults(func=_cmd_registry)

    # import <domain_url>
    p = sub.add_parser("import", help="preview a registration from a domain's agent card")
    p.add_argument("domain_url")
    p.set_defaults(func=_cmd_import)

    # register <agent_id> [options]
    p = sub.add_parser("register", help="register a new agent (returns a bearer key)")
    p.add_argument("agent_id")
    p.add_argument("--org-name",               dest="org_name",               default=None)
    p.add_argument("--org-domain",             dest="org_domain",             default=None)
    p.add_argument("--contact-info",           dest="contact_info",           default=None, help="email, Twitter, GitHub URL, etc.")
    p.add_argument("--endpoint-url",           dest="endpoint_url",           default=None, help="HTTPS URL of this agent's endpoint")
    p.add_argument("--capabilities",           dest="capabilities",           default=None,
                   help=(
                       "comma-separated capability names (plain strings default to weight 1). "
                       "Weight tiers: weight 1 (most specific) max 1, weight 2 max 2, weight 3 (most generic) max 3. "
                       "Max 6 total. To set a weight pass a JSON dict per item, e.g. "
                       "'{\"name\":\"freight_booking\",\"weight\":3}'"
                   ))
    p.add_argument("--specialty",              dest="specialty",              default=None, help="free-text description of what the agent specialises in")
    p.add_argument("--settlement-rail",        dest="settlement_rail",        default=None, choices=["x402", "stripe", "manual"])
    p.add_argument("--protocol",               dest="protocol",               default=None)
    p.add_argument("--accepted-terms-format",  dest="accepted_terms_format",  default=None)
    p.add_argument("--http-methods",           dest="http_methods",           default=None, help="comma-separated, e.g. GET,POST")
    p.add_argument("--message-protocol",       dest="message_protocol",       default="a2a", choices=["a2a", "mcp", "raw"])
    p.add_argument("--a2a-compliant",          dest="a2a_compliant",          action="store_true", default=False)
    p.add_argument("--accepted-content-types", dest="accepted_content_types", default=None, help="comma-separated MIME types")
    p.add_argument("--public-key",             dest="public_key",             default=None, help="Ed25519 public key (base64url)")
    p.add_argument("--signup-help",            dest="signup_help",            default=None, help="URL/instructions for callers to obtain a credential")
    p.add_argument("--auth-header-name",       dest="auth_header_name",       default=None, help="header name for per-caller credentials, e.g. X-Api-Key")
    p.set_defaults(func=_cmd_register)

    # call <agent_id> <payload-json> [--as caller] [--x-payment]  (write — needs key)
    p = sub.add_parser("call", help="proxy a JSON payload to an agent through Aidress")
    p.add_argument("agent_id")
    p.add_argument("payload", help="JSON payload string, e.g. '{\"action\":\"book\"}'")
    p.add_argument("--as", dest="caller", required=True,
                   help="REQUIRED — your agent_id (the caller); must match your configured agent key")
    p.add_argument("--x-payment", dest="x_payment", default=None,
                   help="X-Payment proof header value for the x402 retry flow")
    p.set_defaults(func=_cmd_call)

    # review <success|fail> <score> [--txn] [--as] [--receiver]  (write — needs key)
    p = sub.add_parser("review", help="report a transaction outcome and rate the counterpart")
    p.add_argument("outcome", choices=["success", "fail"], help="transaction outcome")
    p.add_argument("score", type=int, choices=range(1, 11), metavar="{1-10}",
                   help="trust rating, 1 (very bad) to 10 (excellent) — the canonical API scale")
    p.add_argument("--txn", default=None,
                   help="transaction_id to review (required — the CLI keeps no cross-run cache)")
    p.add_argument("--as", dest="caller", default=None, help="your agent_id (the caller)")
    p.add_argument("--receiver", default=None, help="the receiver's agent_id")
    p.set_defaults(func=_cmd_review)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse argv, build a client from the global flags, and dispatch."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    # One client per invocation; --key overrides the AIDRESS_AGENT_KEY env var,
    # which the SDK itself reads when agent_key is None.
    client = AidressClient(base_url=args.url, agent_key=args.key)
    return args.func(client, args)


if __name__ == "__main__":
    sys.exit(main())

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
#     python3 aidress_cli.py --json verify agent_freightbot_01   # raw JSON output
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
import os
import sys
from pathlib import Path

from aidress_sdk import AidressClient

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.spinner import Spinner
from rich.status import Status
from rich.table import Table
from rich.text import Text
from rich import print as rprint

console = Console()

# ── Trust score helpers ───────────────────────────────────────────────────────

def _score_color(score: int | float | None) -> str:
    """Map a numeric trust score to a Rich color name."""
    if score is None:
        return "dim"
    score = int(score)
    if score >= 70:
        return "green"
    if score >= 50:
        return "yellow"
    return "red"


def _score_label(score: int | float | None) -> str:
    """Return a short human label for a trust tier."""
    if score is None:
        return "unknown"
    score = int(score)
    if score >= 70:
        return "trusted"
    if score >= 50:
        return "caution"
    if score == 40:
        return "pending"
    return "untrusted"


def _fmt_score(score: int | float | None) -> Text:
    """Render a trust score as a styled Rich Text object."""
    color = _score_color(score)
    label = _score_label(score)
    t = Text()
    t.append(str(score) if score is not None else "—", style=f"bold {color}")
    t.append(f"  ({label})", style=color)
    return t


# ── Banner ────────────────────────────────────────────────────────────────────

def _print_banner() -> None:
    # ▄ (lower half block) creates a half-char gap above each bar without blank lines.
    # Logo column is padded to a fixed visible width so the text column aligns cleanly.
    COL = 30  # fixed visible width of logo column

    bars = [(12, 4), (10, 8), (8, 13), (5, 19), (2, 25)]
    logo_lines = []
    for pad, width in bars:
        trail = " " * (COL - pad - width)
        logo_lines.append(" " * pad + f"[white]{'▄' * width}[/]" + trail)
        logo_lines.append(" " * pad + f"[white]{'█' * width}[/]" + trail)

    # 10 text lines aligned to the right of the logo column
    text_lines = [
        "",
        "",
        "",
        "",
        "[bold #38bdf8]▎[/][bold white] A I D R E S S[/]",
        "",
        "[bold #e2e8f0]The Coordination Protocol[/]",
        "[bold #e2e8f0]for the Agentic Economy[/]",
        "",
        "[dim #38bdf8]api.aidress.ai[/]",
    ]

    console.print()
    for logo, text in zip(logo_lines, text_lines):
        console.print(logo + text)
    console.print()
    console.rule(style="dim #1f2937")


# ── Raw-JSON fallback ─────────────────────────────────────────────────────────

def _emit_json(client: AidressClient, result) -> int:
    """Plain JSON output for --json / scripting mode."""
    print(json.dumps(result, indent=2))
    if client.last_error:
        print(
            f"error: could not reach Aidress at {client.base_url}: {client.last_error}",
            file=sys.stderr,
        )
        return 1
    if isinstance(result, dict) and result.get("error"):
        return 1
    return 0


# ── Transport-error renderer ──────────────────────────────────────────────────

def _check_transport(client: AidressClient, result) -> bool:
    """Print an error panel and return True if the call failed at transport level."""
    if client.last_error:
        console.print(Panel(
            f"[red]Could not reach Aidress at [bold]{client.base_url}[/bold]\n{client.last_error}[/red]",
            title="[red]Connection error[/red]",
            border_style="red",
        ))
        return True
    if isinstance(result, dict) and result.get("error"):
        console.print(Panel(
            f"[red]{result['error']}[/red]",
            title="[red]API error[/red]",
            border_style="red",
        ))
        return True
    return False


# ── Per-command rich renderers ────────────────────────────────────────────────

def _render_verify(client: AidressClient, result: dict) -> int:
    if _check_transport(client, result):
        return 1

    agent_id = result.get("agent_id", "—")
    score = result.get("trust_score")
    color = _score_color(score)

    table = Table(show_header=False, box=box.SIMPLE, padding=(0, 1))
    table.add_column("Field", style="bold dim", width=22)
    table.add_column("Value")

    table.add_row("Agent ID", f"[bold]{agent_id}[/bold]")
    table.add_row("Trust score", _fmt_score(score))
    table.add_row("Verified", "[green]yes[/green]" if result.get("verified") else "[dim]no[/dim]")
    table.add_row("Org", result.get("org_name") or "—")
    table.add_row("Domain", result.get("org_domain") or "—")
    caps = result.get("capabilities") or []
    if caps:
        cap_names = [c.get("name", c) if isinstance(c, dict) else str(c) for c in caps]
        table.add_row("Capabilities", ", ".join(cap_names))

    flags = result.get("flags") or []
    if flags:
        table.add_row("Flags", f"[red]{', '.join(flags)}[/red]")

    console.print(Panel(
        table,
        title=f"[{color}]Verify — {agent_id}[/{color}]",
        border_style=color,
    ))
    return 0


def _render_match(client: AidressClient, result) -> int:
    if _check_transport(client, result):
        return 1

    agents = result if isinstance(result, list) else result.get("agents", [])

    if not agents:
        console.print(Panel("[dim]No agents matched your query.[/dim]", title="Match", border_style="dim"))
        return 0

    table = Table(box=box.ROUNDED, show_lines=False, header_style="bold cyan")
    table.add_column("Agent ID", style="bold")
    table.add_column("Score", justify="center")
    table.add_column("Org")
    table.add_column("Capabilities")
    table.add_column("Protocol")
    table.add_column("Rail")

    for i, a in enumerate(agents, 1):
        score = a.get("trust_score") or a.get("composite_score")
        caps = a.get("capabilities") or []
        cap_names = [c.get("name", c) if isinstance(c, dict) else str(c) for c in caps]
        color = _score_color(score)
        routing = a.get("routing") or {}
        table.add_row(
            f"{i}. {a.get('agent_id', '—')}",
            Text(str(score) if score is not None else "—", style=f"bold {color}"),
            a.get("org_name") or "—",
            ", ".join(cap_names) or "—",
            routing.get("protocol") or "—",
            (a.get("settlement_rail") or routing.get("settlement_rail")) or "—",
        )

    console.print(Panel(table, title=f"[cyan]Match — {len(agents)} result(s)[/cyan]", border_style="cyan"))
    return 0


def _render_registry(client: AidressClient, result) -> int:
    if _check_transport(client, result):
        return 1

    agents = result if isinstance(result, list) else result.get("agents", [])

    if not agents:
        console.print(Panel("[dim]Registry is empty.[/dim]", title="Registry", border_style="dim"))
        return 0

    table = Table(box=box.ROUNDED, show_lines=False, header_style="bold cyan")
    table.add_column("#", style="dim", justify="right", width=3)
    table.add_column("Agent ID", style="bold")
    table.add_column("Score", justify="center")
    table.add_column("Org")
    table.add_column("Domain")
    table.add_column("Capabilities")

    for i, a in enumerate(agents, 1):
        score = a.get("trust_score")
        caps = a.get("capabilities") or []
        cap_names = [c.get("name", c) if isinstance(c, dict) else str(c) for c in caps]
        color = _score_color(score)
        table.add_row(
            str(i),
            a.get("agent_id", "—"),
            Text(str(score) if score is not None else "—", style=f"bold {color}"),
            a.get("org_name") or "—",
            a.get("org_domain") or "—",
            ", ".join(cap_names[:3]) + ("…" if len(cap_names) > 3 else "") or "—",
        )

    console.print(Panel(table, title=f"[cyan]Registry — {len(agents)} agent(s)[/cyan]", border_style="cyan"))
    return 0


def _render_get(client: AidressClient, result: dict) -> int:
    if _check_transport(client, result):
        return 1

    # Reuse verify renderer — same shape, slightly richer data
    return _render_verify(client, result)


def _render_schema(client: AidressClient, result: dict) -> int:
    if _check_transport(client, result):
        return 1

    agent_id = result.get("agent_id", "—")
    schema = result.get("payload_schema")

    if not schema:
        console.print(Panel(
            f"[dim]Agent [bold]{agent_id}[/bold] hasn't registered a payload schema.[/dim]\n"
            "[dim]They can add one via: aidress update --payload-schema ...[/dim]",
            title="[dim]Schema — no data[/dim]",
            border_style="dim",
        ))
        return 0

    table = Table(box=box.ROUNDED, header_style="bold cyan")
    table.add_column("Field", style="bold")
    table.add_column("Value")

    for field, val in schema.items():
        display = json.dumps(val) if isinstance(val, (dict, list)) else str(val) if val is not None else "[dim]any[/dim]"
        table.add_row(field, display)

    console.print(Panel(table, title=f"[cyan]Schema — {agent_id}[/cyan]", border_style="cyan"))
    return 0


def _render_import(client: AidressClient, result: dict) -> int:
    if _check_transport(client, result):
        return 1

    table = Table(show_header=False, box=box.SIMPLE, padding=(0, 1))
    table.add_column("Field", style="bold dim", width=22)
    table.add_column("Value")

    for key, val in result.items():
        if val is None or val == [] or val == "":
            continue
        display = json.dumps(val) if isinstance(val, (dict, list)) else str(val)
        table.add_row(key, display)

    console.print(Panel(table, title="[cyan]Import preview[/cyan]", border_style="cyan"))
    console.print("[dim]Nothing was written — resubmit via 'register' to create this agent.[/dim]")
    return 0


def _render_register(client: AidressClient, result: dict) -> int:
    if _check_transport(client, result):
        return 1

    agent_id = result.get("agent_id", "—")
    key = result.get("agent_key") or result.get("key")

    table = Table(show_header=False, box=box.SIMPLE, padding=(0, 1))
    table.add_column("Field", style="bold dim", width=22)
    table.add_column("Value")

    table.add_row("Agent ID", f"[bold]{agent_id}[/bold]")
    table.add_row("Trust score", _fmt_score(result.get("trust_score")))
    if key:
        table.add_row("Agent key", f"[bold yellow]{key}[/bold yellow]")

    for k, v in result.items():
        if k in ("agent_id", "trust_score", "agent_key", "key"):
            continue
        if v is None or v == [] or v == "":
            continue
        display = json.dumps(v) if isinstance(v, (dict, list)) else str(v)
        table.add_row(k, display)

    console.print(Panel(
        table,
        title=f"[green]Registered — {agent_id}[/green]",
        border_style="green",
    ))
    if key:
        console.print(f"[dim]Save your agent key — it won't be shown again.[/dim]")
    return 0


def _render_call(client: AidressClient, result: dict) -> int:
    if _check_transport(client, result):
        return 1

    txn_id = result.get("transaction_id", "—")
    table = Table(show_header=False, box=box.SIMPLE, padding=(0, 1))
    table.add_column("Field", style="bold dim", width=22)
    table.add_column("Value")

    table.add_row("Transaction ID", f"[bold]{txn_id}[/bold]")
    table.add_row("Status", result.get("status") or "—")

    response_body = result.get("response")
    if response_body:
        display = json.dumps(response_body, indent=2) if isinstance(response_body, (dict, list)) else str(response_body)
        table.add_row("Response", display)

    for k, v in result.items():
        if k in ("transaction_id", "status", "response"):
            continue
        if v is None or v == [] or v == "":
            continue
        table.add_row(k, json.dumps(v) if isinstance(v, (dict, list)) else str(v))

    console.print(Panel(table, title="[cyan]Call[/cyan]", border_style="cyan"))
    console.print(f"[dim]Remember to submit a review within 24h: aidress review success <score> --txn {txn_id}[/dim]")
    return 0


def _render_review(client: AidressClient, result: dict) -> int:
    if _check_transport(client, result):
        return 1

    table = Table(show_header=False, box=box.SIMPLE, padding=(0, 1))
    table.add_column("Field", style="bold dim", width=22)
    table.add_column("Value")

    for k, v in result.items():
        if v is None or v == []:
            continue
        table.add_row(k, json.dumps(v) if isinstance(v, (dict, list)) else str(v))

    console.print(Panel(table, title="[green]Review submitted[/green]", border_style="green"))
    return 0


def _render_generic(client: AidressClient, result) -> int:
    """Fallback: render any dict as a simple key→value panel."""
    if _check_transport(client, result):
        return 1

    table = Table(show_header=False, box=box.SIMPLE, padding=(0, 1))
    table.add_column("Field", style="bold dim", width=22)
    table.add_column("Value")

    items = result.items() if isinstance(result, dict) else enumerate(result)
    for k, v in items:
        display = json.dumps(v, indent=2) if isinstance(v, (dict, list)) else str(v)
        table.add_row(str(k), display)

    console.print(Panel(table, border_style="cyan"))
    return 0


# ── Spinner wrapper ───────────────────────────────────────────────────────────

_SPINNER_MSGS = {
    "verify":   "Verifying agent…",
    "match":    "Searching for agents…",
    "get":      "Fetching agent profile…",
    "registry": "Loading registry…",
    "import":   "Importing agent card…",
    "register": "Registering agent…",
    "call":     "Calling agent…",
    "review":   "Submitting review…",
    "schema":   "Fetching payload schema…",
}

_RENDERERS = {
    "verify":   _render_verify,
    "match":    _render_match,
    "get":      _render_get,
    "registry": _render_registry,
    "import":   _render_import,
    "register": _render_register,
    "call":     _render_call,
    "review":   _render_review,
    "schema":   _render_schema,
}


def _run_with_spinner(command: str, fn, client: AidressClient, json_mode: bool) -> tuple:
    """Run fn() under a spinner and return (result, exit_code)."""
    msg = _SPINNER_MSGS.get(command, "Working…")
    if json_mode:
        result = fn()
        return result, _emit_json(client, result)

    with console.status(f"[bold cyan]{msg}[/bold cyan]", spinner="dots"):
        result = fn()

    renderer = _RENDERERS.get(command, _render_generic)
    code = renderer(client, result)
    return result, code


# ── Command handlers ──────────────────────────────────────────────────────────

def _cmd_formats(client: AidressClient, args) -> int:
    """Print a local reference card — no API call made."""
    table = Table(box=box.ROUNDED, header_style="bold cyan", show_lines=True)
    table.add_column("Command",  style="bold", width=10)
    table.add_column("Auth",     width=6)
    table.add_column("Format",   style="dim")
    table.add_column("Example")

    rows = [
        ("verify",   "no",  "aidress verify <agent_id>",
         "aidress verify myshipi"),
        ("match",    "no",  "aidress match <cap> [cap …] [--rail x402|stripe|manual]",
         "aidress match freight_booking customs_clearance --rail x402"),
        ("get",      "no",  "aidress get <agent_id>",
         "aidress get myshipi"),
        ("schema",   "no",  "aidress schema <agent_id>",
         "aidress schema x402station_01"),
        ("registry", "no",  "aidress registry",
         "aidress registry"),
        ("import",   "no",  "aidress import <domain_url>",
         "aidress import https://myshipi.com"),
        ("register", "yes", (
            "aidress --key <key> register <agent_id>\n"
            "  [--org-name STR] [--org-domain STR] [--endpoint-url URL]\n"
            "  [--capabilities CAP,CAP] [--specialty STR]\n"
            "  [--settlement-rail x402|stripe|manual]\n"
            "  [--protocol STR] [--http-methods GET,POST]\n"
            "  [--message-protocol a2a|mcp|raw] [--a2a-compliant]\n"
            "  [--public-key B64] [--auth-header-name STR]\n"
            "  [--signup-help URL] [--accepted-content-types MIME,MIME]"
         ),
         "aidress --key sk-… register my_agent --org-name Acme --capabilities freight_booking"),
        ("call",     "yes", (
            "aidress --key <key> call <agent_id> '<json_payload>' --as <your_agent_id>\n"
            "  [--x-payment <proof_header>]"
         ),
         "aidress --key sk-… call myshipi '{\"action\":\"book\",\"origin\":\"LAX\"}' --as my_agent"),
        ("review",   "yes", (
            "aidress --key <key> review <success|fail> <1–10>\n"
            "  --txn <transaction_id>\n"
            "  [--as <your_agent_id>] [--receiver <agent_id>]"
         ),
         "aidress --key sk-… review success 9 --txn txn_abc123"),
    ]

    for cmd, auth, fmt, example in rows:
        auth_cell = Text("key", style="yellow") if auth == "yes" else Text("—", style="dim")
        table.add_row(cmd, auth_cell, fmt, f"[dim]{example}[/dim]")

    console.print(Panel(table, title="[cyan]Aidress — command reference[/cyan]", border_style="cyan"))
    console.print("[dim]Pass --key or set AIDRESS_AGENT_KEY env var for commands that require auth.[/dim]")
    return 0


def _cmd_verify(client: AidressClient, args) -> int:
    _, code = _run_with_spinner("verify", lambda: client.verify(args.agent_id), client, args.json)
    return code


def _cmd_match(client: AidressClient, args) -> int:
    _, code = _run_with_spinner(
        "match",
        lambda: client.match(args.capabilities, settlement_rail=args.rail),
        client, args.json,
    )
    return code


def _cmd_get(client: AidressClient, args) -> int:
    _, code = _run_with_spinner("get", lambda: client.get_agent(args.agent_id), client, args.json)
    return code


def _cmd_schema(client: AidressClient, args) -> int:
    _, code = _run_with_spinner("schema", lambda: client.get_agent(args.agent_id), client, args.json)
    return code


def _cmd_registry(client: AidressClient, args) -> int:
    _, code = _run_with_spinner("registry", lambda: client.registry(), client, args.json)
    return code


def _cmd_import(client: AidressClient, args) -> int:
    _, code = _run_with_spinner("import", lambda: client.import_agent(args.domain_url), client, args.json)
    return code


def _cmd_register(client: AidressClient, args) -> int:
    caps = args.capabilities.split(",") if args.capabilities else None
    http_methods = args.http_methods.split(",") if args.http_methods else None
    act = args.accepted_content_types.split(",") if args.accepted_content_types else None
    _, code = _run_with_spinner(
        "register",
        lambda: client.register(
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
        ),
        client, args.json,
    )
    return code


def _cmd_call(client: AidressClient, args) -> int:
    try:
        payload = json.loads(args.payload)
    except json.JSONDecodeError as e:
        console.print(f"[red]error: --payload is not valid JSON: {e}[/red]")
        return 2
    _, code = _run_with_spinner(
        "call",
        lambda: client.call(args.agent_id, payload, caller_agent_id=args.caller, x_payment=args.x_payment),
        client, args.json,
    )
    return code


def _cmd_review(client: AidressClient, args) -> int:
    _, code = _run_with_spinner(
        "review",
        lambda: client.review(
            success=(args.outcome == "success"),
            score=args.score,
            transaction_id=args.txn,
            caller_agent_id=args.caller,
            receiver_agent_id=args.receiver,
        ),
        client, args.json,
    )
    return code


# ── Rich-aware argument parser ────────────────────────────────────────────────

# Hints surfaced when argparse fires an error for a specific subcommand.
_CMD_HINTS = {
    "call": (
        "call requires two positional args and --as:\n"
        "  [bold]aidress --key <key> call <agent_id> '<json_payload>' --as <your_agent_id>[/bold]\n"
        "  e.g.  aidress --key sk-... call myshipi '{\"action\":\"book\"}' --as my_agent"
    ),
    "review": (
        "review requires an outcome, a score, and --txn:\n"
        "  [bold]aidress --key <key> review <success|fail> <1-10> --txn <transaction_id>[/bold]\n"
        "  e.g.  aidress --key sk-... review success 8 --txn txn_abc123"
    ),
    "verify": (
        "verify takes a single agent_id:\n"
        "  [bold]aidress verify <agent_id>[/bold]\n"
        "  e.g.  aidress verify myshipi"
    ),
    "match": (
        "match takes one or more capability names:\n"
        "  [bold]aidress match <capability> [capability ...] [--rail x402|stripe|manual][/bold]\n"
        "  e.g.  aidress match freight_booking customs_clearance"
    ),
    "register": (
        "register requires an agent_id:\n"
        "  [bold]aidress --key <key> register <agent_id> [--org-name ...] [--endpoint-url ...][/bold]\n"
        "  e.g.  aidress --key sk-... register my_agent --org-name Acme"
    ),
    "schema": (
        "schema takes a single agent_id:\n"
        "  [bold]aidress schema <agent_id>[/bold]\n"
        "  e.g.  aidress schema partsiq_intelligence_01"
    ),
}

_CMD_HINTS_DEFAULT = (
    "Run [bold]aidress --help[/bold] to see all commands, "
    "or [bold]aidress <command> --help[/bold] for per-command usage."
)


class _RichParser(argparse.ArgumentParser):
    """ArgumentParser that renders errors and help as Rich panels."""

    def error(self, message: str) -> None:
        hint = _CMD_HINTS.get(self.prog.removeprefix("aidress").strip(), _CMD_HINTS_DEFAULT)
        console.print(Panel(
            f"[red]{message}[/red]\n\n{hint}",
            title="[red]Usage error[/red]",
            border_style="red",
        ))
        sys.exit(2)

    def print_help(self, file=None) -> None:  # noqa: ARG002
        self._rich_help()

    def _rich_help(self) -> None:
        is_main = self.prog == "aidress"
        subcmd = self.prog.removeprefix("aidress").strip()
        desc = (self.description or "").strip()

        console.print()
        console.print(Panel(
            f"[#94a3b8]{desc}[/]" if desc else "",
            title=f"[bold #38bdf8]{self.prog}[/]",
            border_style="#1f2937",
            padding=(0, 2),
        ))
        console.print()

        if is_main:
            # Commands — pull help text from the subparsers _choices_actions list
            for action in self._actions:
                if not hasattr(action, "_choices_actions"):
                    continue
                tbl = Table(box=box.SIMPLE, show_header=False, padding=(0, 1), show_edge=False)
                tbl.add_column("cmd", style="bold #38bdf8", min_width=12, no_wrap=True)
                tbl.add_column("desc", style="#e2e8f0")
                for ca in action._choices_actions:
                    tbl.add_row(ca.dest, ca.help or "")
                console.print("  [bold white]Commands[/]")
                console.print(tbl)
                console.print()

            # Global flags are argparse.SUPPRESS in the parser so hardcode them here
            tbl = Table(box=box.SIMPLE, show_header=False, padding=(0, 1), show_edge=False)
            tbl.add_column("flag", style="bold #38bdf8", min_width=16, no_wrap=True)
            tbl.add_column("desc", style="#94a3b8")
            tbl.add_row("--url URL",   "API base URL  (default: https://api.aidress.ai)")
            tbl.add_row("--key KEY",   "agent key for write commands (or set AIDRESS_AGENT_KEY)")
            tbl.add_row("--json",      "raw JSON output instead of the rich UI")
            tbl.add_row("--no-banner", "suppress the startup logo")
            console.print("  [bold white]Global flags[/]  [dim #94a3b8](place before <command>)[/]")
            console.print(tbl)
            console.print()
            console.print("  [dim #38bdf8]run 'aidress formats' for a full command reference card.[/]")
            console.print()

        else:
            # Positional arguments
            positionals = [
                a for a in self._actions
                if not a.option_strings and a.help != argparse.SUPPRESS
            ]
            if positionals:
                tbl = Table(box=box.SIMPLE, show_header=False, padding=(0, 1), show_edge=False)
                tbl.add_column("arg", style="bold white", min_width=18, no_wrap=True)
                tbl.add_column("desc", style="#94a3b8")
                for a in positionals:
                    if a.choices and not a.metavar:
                        disp = "{" + "|".join(str(c) for c in a.choices) + "}"
                    else:
                        disp = a.metavar or f"<{a.dest}>"
                    tbl.add_row(disp, a.help or "")
                console.print("  [bold white]Arguments[/]")
                console.print(tbl)
                console.print()

            # Optional arguments — exclude -h/--help and SUPPRESS'd flags
            options = [
                a for a in self._actions
                if a.option_strings
                and "--help" not in a.option_strings
                and a.help != argparse.SUPPRESS
            ]
            if options:
                tbl = Table(box=box.SIMPLE, show_header=False, padding=(0, 1), show_edge=False)
                tbl.add_column("flag", style="bold #38bdf8", min_width=26, no_wrap=True)
                tbl.add_column("desc", style="#94a3b8")
                for a in options:
                    s = ", ".join(a.option_strings)
                    if a.nargs != 0:
                        if a.metavar:
                            s += f" {a.metavar}"
                        elif a.choices:
                            s += " {" + "|".join(str(c) for c in a.choices) + "}"
                        elif a.dest:
                            s += f" {a.dest.upper()}"
                    if getattr(a, "required", False):
                        s += " [dim red]*[/dim red]"
                    tbl.add_row(s, a.help or "")
                console.print("  [bold white]Options[/]  [dim #94a3b8](* = required)[/]")
                console.print(tbl)
                console.print()

            # Epilog — split at "example:" so notes and examples render distinctly
            if self.epilog:
                lines = self.epilog.strip().split("\n")
                ex_idx = next(
                    (i for i, l in enumerate(lines) if l.strip().lower() == "example:"),
                    None,
                )
                notes = [l for l in (lines[:ex_idx] if ex_idx is not None else []) if l.strip()]
                examples = [l for l in lines[(ex_idx + 1 if ex_idx is not None else 0):] if l.strip()]

                if notes:
                    console.print("  [bold white]Notes[/]")
                    for line in notes:
                        stripped = line.strip()
                        if stripped.endswith(":"):
                            console.print(f"  [bold #94a3b8]{stripped}[/]")
                        else:
                            console.print(f"  [#94a3b8]{line}[/]")
                    console.print()

                if examples:
                    console.print("  [bold white]Example[/]")
                    for line in examples:
                        # Use Text to avoid Rich treating \ at line end as markup escape
                        console.print(Text(f"  {line}", style="dim #38bdf8"))
                    console.print()

            if subcmd:
                console.print(f"  [dim #94a3b8]aidress {subcmd} --help  to see this again[/]")
                console.print()


# ── Parser construction ───────────────────────────────────────────────────────

class _Fmt(argparse.RawDescriptionHelpFormatter):
    """Fixed-width formatter: keeps epilog/description formatting and aligns help at col 28."""
    def __init__(self, prog):
        super().__init__(prog, max_help_position=28, width=72)


def _build_parser() -> _RichParser:
    """Assemble the argparse tree: global flags + one subparser per command."""
    parser = _RichParser(
        prog="aidress",
        description="Aidress — The Coordination Protocol for the Agentic Economy.",
        epilog=(
            "global flags (place before <command>):\n"
            "  --url URL   API base URL  (default: https://api.aidress.ai)\n"
            "  --key KEY   agent key for write commands (or set AIDRESS_AGENT_KEY)\n"
            "  --json      raw JSON output instead of the rich UI\n"
            "  --no-banner suppress the startup logo\n\n"
            "run 'aidress formats' for a full command reference card."
        ),
        formatter_class=_Fmt,
    )
    parser.add_argument("--url", default="https://api.aidress.ai", help=argparse.SUPPRESS)
    parser.add_argument("--key", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true", default=False, help=argparse.SUPPRESS)
    parser.add_argument("--no-banner", action="store_true", default=False, help=argparse.SUPPRESS)

    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>",
                                parser_class=_RichParser)

    # verify
    p = sub.add_parser(
        "verify",
        help="look up an agent's trust profile",
        description="Fetch the trust score, capabilities, and flags for an agent before transacting with it.",
        epilog="example:\n  aidress verify myshipi",
        formatter_class=_Fmt,
    )
    p.add_argument("agent_id", help="the agent to look up")
    p.set_defaults(func=_cmd_verify)

    # match
    p = sub.add_parser(
        "match",
        help="find trusted agents by capability",
        description="Search the registry for agents that support all requested capabilities,\nranked by composite score (capability fit × trust × success rate).",
        epilog="example:\n  aidress match freight_booking customs_clearance --rail x402",
        formatter_class=_Fmt,
    )
    p.add_argument("capabilities", nargs="+", help="one or more capability names")
    p.add_argument("--rail", default=None, choices=["x402", "stripe", "manual"],
                   help="only return agents that support this settlement rail")
    p.set_defaults(func=_cmd_match)

    # get
    p = sub.add_parser(
        "get",
        help="fetch an agent's full profile",
        description="Return every stored field for an agent — more detail than verify.",
        epilog="example:\n  aidress get myshipi",
        formatter_class=_Fmt,
    )
    p.add_argument("agent_id", help="the agent to fetch")
    p.set_defaults(func=_cmd_get)

    # schema
    p = sub.add_parser(
        "schema",
        help="show an agent's registered payload schema",
        description="Display the payload schema an agent has registered — fields, units, and formats\nit expects callers to provide. 'any' means the agent accepts any value.",
        epilog="example:\n  aidress schema x402station_01",
        formatter_class=_Fmt,
    )
    p.add_argument("agent_id", help="the agent whose schema to show")
    p.set_defaults(func=_cmd_schema)

    # formats
    p = sub.add_parser(
        "formats",
        help="show argument format for every command (local, no API call)",
        description="Print a quick-reference card for all commands. No network call made.",
        formatter_class=_Fmt,
    )
    p.set_defaults(func=_cmd_formats)

    # registry
    p = sub.add_parser(
        "registry",
        help="list all trusted agents (score ≥ 50)",
        description="List every verified agent in the registry with a trust score of 50 or above.",
        epilog="example:\n  aidress registry",
        formatter_class=_Fmt,
    )
    p.set_defaults(func=_cmd_registry)

    # import
    p = sub.add_parser(
        "import",
        help="preview a registration from a domain's A2A agent card",
        description="Fetch a domain's /.well-known/agent.json and preview what a registration\nwould look like. Nothing is written to the registry.",
        epilog="example:\n  aidress import https://myshipi.com",
        formatter_class=_Fmt,
    )
    p.add_argument("domain_url", help="base URL of the domain to import from")
    p.set_defaults(func=_cmd_import)

    # register
    p = sub.add_parser(
        "register",
        usage="aidress --key KEY register <agent_id> [options]",
        help="register a new agent (requires --key)",
        description=(
            "Register a new agent. Returns a bearer key you must save — it won't be shown again.\n"
            "Requires --key (or AIDRESS_AGENT_KEY env var)."
        ),
        epilog=(
            "capability weights (--capabilities):\n"
            "  weight 3  your USP — max 1          e.g. freight_booking\n"
            "  weight 2  strong fit — max 2         e.g. shipment_tracking\n"
            "  weight 1  generic support — max 3    e.g. customs_clearance\n"
            "  max 6 capabilities total\n"
            "  to set weight: '{\"name\":\"freight_booking\",\"weight\":3}'\n\n"
            "example:\n"
            "  aidress --key sk-... register my_agent \\\n"
            "    --org-name Acme --org-domain acme.com \\\n"
            "    --capabilities freight_booking,shipment_tracking \\\n"
            "    --settlement-rail x402 --endpoint-url https://acme.com/agent"
        ),
        formatter_class=_Fmt,
    )
    p.add_argument("agent_id",                                                    help="unique ID for your agent (snake_case)")
    p.add_argument("--org-name",               dest="org_name",      default=None, help="your organisation's display name")
    p.add_argument("--org-domain",             dest="org_domain",    default=None, help="your organisation's domain (e.g. acme.com)")
    p.add_argument("--contact-info",           dest="contact_info",  default=None, help="email, Twitter handle, or GitHub URL")
    p.add_argument("--endpoint-url",           dest="endpoint_url",  default=None, help="public HTTPS URL callers should route to")
    p.add_argument("--capabilities",           dest="capabilities",  default=None, help="comma-separated capability names or JSON dicts with weights")
    p.add_argument("--specialty",              dest="specialty",     default=None, help="one-line description of what this agent specialises in")
    p.add_argument("--settlement-rail",        dest="settlement_rail", default=None, choices=["x402", "stripe", "manual"], help="payment rail this agent accepts")
    p.add_argument("--protocol",               dest="protocol",      default=None, help="transport protocol (e.g. REST, gRPC, GraphQL)")
    p.add_argument("--accepted-terms-format",  dest="accepted_terms_format", default=None, help="terms format this agent accepts (e.g. JSON)")
    p.add_argument("--http-methods",           dest="http_methods",  default=None, help="comma-separated HTTP methods (e.g. GET,POST)")
    p.add_argument("--message-protocol",       dest="message_protocol", default="a2a", choices=["a2a", "mcp", "raw"], help="messaging protocol (default: a2a)")
    p.add_argument("--a2a-compliant",          dest="a2a_compliant", action="store_true", default=False, help="declare A2A compliance")
    p.add_argument("--accepted-content-types", dest="accepted_content_types", default=None, help="comma-separated MIME types (e.g. application/json)")
    p.add_argument("--public-key",             dest="public_key",    default=None, help="Ed25519 public key in base64url for payload verification")
    p.add_argument("--signup-help",            dest="signup_help",   default=None, help="URL or instructions for callers to get a credential")
    p.add_argument("--auth-header-name",       dest="auth_header_name", default=None, help="header name callers must supply (e.g. X-Api-Key)")
    p.set_defaults(func=_cmd_register)

    # call
    p = sub.add_parser(
        "call",
        usage="aidress --key KEY call <agent_id> '<json>' --as <your_agent_id>",
        help="send a JSON payload to an agent through Aidress (requires --key)",
        description=(
            "Route a JSON payload to an agent's endpoint via Aidress.\n"
            "Aidress logs the call and returns a transaction_id — submit a review within 24h."
        ),
        epilog=(
            "example:\n"
            "  aidress --key sk-... call myshipi '{\"action\":\"book\",\"origin\":\"LAX\"}' --as my_agent"
        ),
        formatter_class=_Fmt,
    )
    p.add_argument("agent_id",                                        help="the agent to call")
    p.add_argument("payload",                                         help="JSON string to send, e.g. '{\"action\":\"book\"}'")
    p.add_argument("--as",         dest="caller",   required=True,   help="your agent_id — must match your --key")
    p.add_argument("--x-payment",  dest="x_payment", default=None,   help="X-Payment proof header for the x402 payment flow")
    p.set_defaults(func=_cmd_call)

    # review
    p = sub.add_parser(
        "review",
        usage="aidress --key KEY review <success|fail> <1-10> --txn <txn_id>",
        help="report a transaction outcome and rate the agent (requires --key)",
        description=(
            "Submit a trust rating for an agent after a transaction.\n"
            "Must be done within 24h of the call — missing the window costs you 2 trust points."
        ),
        epilog=(
            "example:\n"
            "  aidress --key sk-... review success 9 --txn txn_abc123 --as my_agent"
        ),
        formatter_class=_Fmt,
    )
    p.add_argument("outcome",                    choices=["success", "fail"],        help="did the transaction succeed?")
    p.add_argument("score",  type=int,           choices=range(1, 11), metavar="{1-10}", help="trust rating: 1 (terrible) → 10 (excellent)")
    p.add_argument("--txn",                      default=None, required=True,        help="transaction_id returned by 'aidress call'")
    p.add_argument("--as",   dest="caller",      default=None,                       help="your agent_id (the caller)")
    p.add_argument("--receiver",                 default=None,                       help="the agent you're rating")
    p.set_defaults(func=_cmd_review)

    return parser


_CLI_VERSION = "0.2.3"
_SENTINEL = Path.home() / ".aidress" / f".banner_shown_{_CLI_VERSION}"


def _should_show_banner(args) -> bool:
    if args.json or args.no_banner:
        return False
    if _SENTINEL.exists():
        return False
    _SENTINEL.parent.mkdir(parents=True, exist_ok=True)
    _SENTINEL.touch()
    return True


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args, extra = parser.parse_known_args(argv)
    if extra:
        # Extra args were ignored by the subparser — show the command-specific hint.
        hint = _CMD_HINTS.get(getattr(args, "command", ""), _CMD_HINTS_DEFAULT)
        console.print(Panel(
            f"[red]unrecognized arguments: {' '.join(extra)}[/red]\n\n{hint}",
            title="[red]Usage error[/red]",
            border_style="red",
        ))
        return 2

    if _should_show_banner(args):
        _print_banner()

    client = AidressClient(base_url=args.url, agent_key=args.key)
    return args.func(client, args)


if __name__ == "__main__":
    sys.exit(main())

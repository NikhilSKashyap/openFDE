"""CLI entry point: openfde watch <path> · openfde cc/codex "<prompt>"

Usage:
    openfde watch /path/to/repo
    openfde watch /path/to/repo --port 8080
    openfde watch /path/to/repo --no-open
    python -m openfde watch /path/to/repo

    openfde cc "make the login button async"       # Claude Code, prompt captured
    openfde codex "tighten error handling in api"  # Codex (workspace-write)
    # Both EDIT the repo, capture the prompt as an OpenFDE episode, and leave the
    # changes in the work tree for OpenFDE to Review and Land (no auto-commit).
"""

import argparse
import asyncio
import sys


def main() -> None:
    """Parse CLI arguments and dispatch to the appropriate command.

    Args:
        None (reads from sys.argv)

    Returns:
        None
    """
    parser = argparse.ArgumentParser(
        prog="openfde",
        description="OpenFDE — draw architecture, let the agent build it",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    watch_parser = sub.add_parser(
        "watch",
        help="Watch a repo path and start the IDE server",
    )
    watch_parser.add_argument(
        "path",
        help="Repository path to watch (created if it does not exist)",
    )
    watch_parser.add_argument(
        "--port",
        type=int,
        default=7373,
        help="Port to listen on (default: 7373)",
    )
    watch_parser.add_argument(
        "--no-open",
        action="store_true",
        help="Do not auto-open browser tab",
    )

    # ── Prompt-capture wrappers: run an external agent, capture the prompt ──────
    #    `openfde cc "…"`  (alias: claude-code)   ·   `openfde codex "…"`
    for name, aliases, agent in (
        ("cc", ["claude-code"], "Claude Code"),
        ("codex", [], "Codex"),
    ):
        wp = sub.add_parser(
            name, aliases=aliases,
            help=f"Run {agent} on a prompt; capture it as an OpenFDE prompt episode",
        )
        wp.add_argument("prompt", help="The prompt to run (quote it)")
        wp.add_argument("--path", default=".", help="Repository path (default: current directory)")
        wp.add_argument("--model", default=None, help="Optional model override")

    # ── External council inbox: a session self-orients (no native chat injection) ──
    #    `openfde council status --role codex`   ·   `openfde council status --role claude`
    council_parser = sub.add_parser("council", help="External Codex + Claude Code council")
    council_sub = council_parser.add_subparsers(dest="council_command", metavar="<action>")
    status_parser = council_sub.add_parser("status", help="Show this role's current council inbox")
    status_parser.add_argument("--role", choices=["codex", "claude"], required=True,
                               help="Whose inbox to render")
    status_parser.add_argument("--path", default=".",
                               help="Repository path (default: current directory)")

    args = parser.parse_args()

    if args.command == "watch":
        from openfde.server import start  # local import avoids loading aiohttp on --help
        try:
            asyncio.run(start(args.path, args.port, auto_open=not args.no_open))
        except KeyboardInterrupt:
            print("\n  openfde stopped.")
            sys.exit(0)
    elif args.command in ("cc", "claude-code", "codex"):
        from openfde.prompt_wrapper import run_prompt_wrapper
        kind = "codex" if args.command == "codex" else "claude-code"
        agent = "Codex" if kind == "codex" else "Claude Code"
        print(f"openfde: running {agent} — editing only, OpenFDE will review & land. Working…")
        out = run_prompt_wrapper(kind, args.prompt, args.path, args.model)
        print(out["message"])
        # Non-zero exit only when the agent truly failed with no useful change.
        sys.exit(0 if out["episode"]["status"] in ("reviewing", "complete_no_changes") else 1)
    elif args.command == "council":
        if getattr(args, "council_command", None) == "status":
            from openfde.external_council import render_session_inbox
            print(render_session_inbox(args.path, args.role), end="")
        else:
            council_parser.print_help()
            sys.exit(1)
    else:
        parser.print_help()
        sys.exit(1)

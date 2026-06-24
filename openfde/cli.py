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

    handoff_parser = council_sub.add_parser(
        "handoff", help="Senior Dev (Claude Code): hand the active task to Codex for verification")
    handoff_parser.add_argument("--role", choices=["claude"], default="claude",
                                help="Senior-dev handoff (Claude Code)")
    handoff_parser.add_argument("--commit", default="HEAD", help="Commit to verify (default: HEAD)")
    handoff_parser.add_argument("--summary", default="", help="What changed")
    handoff_parser.add_argument("--checks", default="",
                                help="What passed/failed, e.g. 'tests passed; build passed'")
    handoff_parser.add_argument("--path", default=".",
                                help="Repository path (default: current directory)")

    verdict_parser = council_sub.add_parser(
        "verdict", help="Codex: record a verification verdict (never commits)")
    verdict_parser.add_argument("--status", choices=["verified", "changes-requested"], required=True)
    verdict_parser.add_argument("--summary", default="", help="Verdict summary")
    verdict_parser.add_argument("--finding", action="append", default=[],
                                help="A specific finding / requested change (repeatable)")
    verdict_parser.add_argument("--path", default=".",
                                help="Repository path (default: current directory)")

    ack_parser = council_sub.add_parser(
        "ack", help="Acknowledge your pending council delivery (your session has resumed)")
    ack_parser.add_argument("--role", choices=["codex", "claude"], required=True,
                            help="Which role's pending delivery to acknowledge")
    ack_parser.add_argument("--path", default=".",
                            help="Repository path (default: current directory)")

    # Autonomous Program Mode — the session bridge (status) + optional start/continue.
    program_parser = sub.add_parser("program", help="Autonomous Program Mode — status bridge + start")
    program_sub = program_parser.add_subparsers(dest="program_command", metavar="<action>")
    pstatus = program_sub.add_parser("status", help="Show the active program's status for your role (session bridge)")
    pstatus.add_argument("--role", choices=["architect", "senior-dev", "verifier"], default="architect",
                         help="Which role's status to print")
    pstatus.add_argument("--path", default=".", help="Repository path (default: current directory)")
    pstart = program_sub.add_parser("start", help="Start a program from a prompt file (runs synchronously)")
    pstart.add_argument("--prompt-file", required=True, help="File holding the high-level product direction")
    pstart.add_argument("--allow-edits", action="store_true", help="Let the senior dev write files (real commit)")
    pstart.add_argument("--path", default=".", help="Repository path (default: current directory)")
    pcontinue = program_sub.add_parser("continue", help="Resume the active blocked program")
    pcontinue.add_argument("--path", default=".", help="Repository path (default: current directory)")

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
        from openfde import external_council as ec
        from openfde import handoff_broker
        cc = getattr(args, "council_command", None)
        if cc == "status":
            banner = handoff_broker.delivery_banner(args.path, args.role)   # pending delivery first
            if banner:
                print(banner)
            print(ec.render_session_inbox(args.path, args.role), end="")
        elif cc == "ack":
            d = handoff_broker.acknowledge_delivery(args.path, args.role)
            if d:
                print(f"Acknowledged delivery {d['deliveryId']} "
                      f"({d.get('fromRole')} → {args.role}). Now do the work.")
            else:
                print("No pending delivery to acknowledge.")
        elif cc == "handoff":
            sha = ec._resolve_commit(args.path, args.commit)
            res = ec.record_claude_handoff(args.path, commit_sha=sha,
                                           summary=args.summary, checks=args.checks)
            if not res["found"]:
                print("No active Claude Code task to hand off "
                      "(has Codex started work — READY_FOR_CC?).")
                sys.exit(1)
            print(f"Handed off {res['episodeId']} → READY_FOR_CODEX_VERIFICATION "
                  f"(commit {sha[:12]}).")
            if not res["trailerOk"]:
                print(f"  ⚠ that commit has no OpenFDE-Episode trailer — Codex can still verify, but "
                      f"stamp next time: OpenFDE-Episode: {res['episodeId']}, OpenFDE-Role: senior_dev, "
                      "OpenFDE-Handoff: ready_for_codex_verification")
            print("\nCodex can now run:  openfde council status --role codex")
        elif cc == "verdict":
            status = "VERIFIED" if args.status == "verified" else "CHANGES_REQUESTED"
            res = ec.record_codex_verdict_cli(args.path, status=status, summary=args.summary,
                                              findings="\n".join(args.finding))
            if not res["found"]:
                print("No task awaiting Codex verification (READY_FOR_CODEX_VERIFICATION).")
                sys.exit(1)
            print(f"Recorded {status} for {res['episodeId']}.")
            if status == "CHANGES_REQUESTED":
                print("Claude Code can now run:  openfde council status --role claude")
        else:
            council_parser.print_help()
            sys.exit(1)
    elif args.command == "program":
        from pathlib import Path
        from openfde import program as pg
        from openfde.persistence import Persistence
        pc = getattr(args, "program_command", None)
        if pc == "status":
            prog = pg.active_program(args.path) or pg.latest_program(args.path)
            print(pg.program_status(prog, args.role))
        elif pc == "start":
            try:
                prompt = Path(args.prompt_file).read_text()
            except OSError as e:
                print(f"Cannot read prompt file: {e}")
                sys.exit(1)
            persistence = Persistence(Path(args.path) / ".openfde")
            providers = {"architect": "codex", "srDev": "claude-code", "verifier": "codex"}
            prog = pg.start_program(persistence, prompt=prompt, providers=providers,
                                    allow_edits=args.allow_edits)
            if prog["status"] == pg.STATUS_RUNNING:
                prog = pg.advance_program(persistence, prog)     # synchronous (real providers)
            print(pg.program_status(prog, "architect"))
        elif pc == "continue":
            prog = pg.active_program(args.path) or pg.latest_program(args.path)
            if not prog:
                print("No program to continue.")
                sys.exit(1)
            persistence = Persistence(Path(args.path) / ".openfde")
            prog = pg.continue_program(persistence, prog["programId"])
            print(pg.program_status(prog, "architect"))
        else:
            program_parser.print_help()
            sys.exit(1)
    else:
        parser.print_help()
        sys.exit(1)

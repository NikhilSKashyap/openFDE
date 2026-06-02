"""CLI entry point: openfde watch <path>

Usage:
    openfde watch /path/to/repo
    openfde watch /path/to/repo --port 8080
    openfde watch /path/to/repo --no-open
    python -m openfde watch /path/to/repo
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

    args = parser.parse_args()

    if args.command == "watch":
        from openfde.server import start  # local import avoids loading aiohttp on --help
        try:
            asyncio.run(start(args.path, args.port, auto_open=not args.no_open))
        except KeyboardInterrupt:
            print("\n  openfde stopped.")
            sys.exit(0)
    else:
        parser.print_help()
        sys.exit(1)

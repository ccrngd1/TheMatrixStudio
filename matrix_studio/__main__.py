# SPDX-License-Identifier: Apache-2.0
"""
CLI entrypoint for TheMatrix Simulation Studio.

Subcommands:
    run <request.json> [options]   Run a simulation from a request file (Phase 0
                                   file-in / JSON-out behavior, unchanged).
    serve [--host --port]          Start the FastAPI control-room web server
                                   (Phase 1).

Usage:
    python -m matrix_studio run request.json [-o out.json] [--max-messages N] [--no-db] [-v]
    python -m matrix_studio serve [--host 0.0.0.0] [--port 8000]
    matrix-studio run request.json
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Optional

from matrix_studio.engine import run_simulation
from matrix_studio.settings import get_settings
from matrix_studio.storage import Database


def setup_logging(verbose: bool = False):
    """Configure logging."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


async def run_from_file(
    request_path: Path,
    output_path: Optional[Path],
    max_messages: Optional[int],
    no_db: bool,
) -> int:
    """
    Run simulation from a request file.

    Args:
        request_path: Path to request JSON file
        output_path: Optional output path for result
        max_messages: Optional max messages override
        no_db: Skip database persistence

    Returns:
        Exit code (0 for success)
    """
    settings = get_settings()
    logger = logging.getLogger(__name__)

    # Load request
    try:
        with open(request_path) as f:
            request = json.load(f)
    except Exception as e:
        logger.error(f"Failed to load request file: {e}")
        return 1

    # Override max_messages if specified
    if max_messages is not None:
        if "config" not in request:
            request["config"] = {}
        request["config"]["max_messages"] = max_messages

    # Setup database
    db = None
    if not no_db:
        db_path = Path(settings.data_dir) / "matrix_studio.db"
        db = Database(str(db_path))
        await db.connect()
        logger.info(f"Using database: {db_path}")

    try:
        # Run simulation
        result = await run_simulation(request, db=db)

        # Write output
        if output_path:
            with open(output_path, "w") as f:
                json.dump(result, f, indent=2)
            logger.info(f"Results written to: {output_path}")
        else:
            print(json.dumps(result, indent=2))

        if result["status"] == "complete":
            logger.info(
                f"Simulation complete: {result['total_turns']} turns, "
                f"${result.get('total_cost_usd', 0):.4f} cost"
            )
            return 0
        else:
            logger.error(f"Simulation failed: {result.get('error', 'Unknown error')}")
            return 1

    finally:
        if db:
            await db.close()


def _cmd_run(args: argparse.Namespace) -> int:
    """Handle the ``run`` subcommand (Phase 0 behavior)."""
    setup_logging(args.verbose)

    if not args.request.exists():
        print(f"Error: Request file not found: {args.request}", file=sys.stderr)
        return 1

    try:
        return asyncio.run(
            run_from_file(
                args.request,
                args.output,
                args.max_messages,
                args.no_db,
            )
        )
    except KeyboardInterrupt:
        print("\nSimulation interrupted by user", file=sys.stderr)
        return 130
    except Exception as e:
        logging.getLogger(__name__).error(f"Fatal error: {e}", exc_info=True)
        return 1


def _cmd_serve(args: argparse.Namespace) -> int:
    """Handle the ``serve`` subcommand (Phase 1 web server)."""
    setup_logging(args.verbose)
    # Imported lazily so the ``run`` path has no hard dependency on FastAPI/uvicorn.
    from matrix_studio.api.server import serve

    try:
        serve(host=args.host, port=args.port)
        return 0
    except KeyboardInterrupt:
        print("\nServer stopped", file=sys.stderr)
        return 130
    except Exception as e:
        logging.getLogger(__name__).error(f"Fatal error: {e}", exc_info=True)
        return 1


def build_parser() -> argparse.ArgumentParser:
    """Build the subcommand-based argument parser."""
    parser = argparse.ArgumentParser(
        prog="matrix-studio",
        description="TheMatrix Simulation Studio - Multi-agent conversation simulator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run a simulation from request.json (Phase 0 behavior)
  matrix-studio run request.json

  # Run with custom output path and turn limit
  matrix-studio run request.json -o results.json --max-messages 10

  # Run without database persistence
  matrix-studio run request.json --no-db

  # Start the control-room web server (Phase 1)
  matrix-studio serve --host 0.0.0.0 --port 8000
        """,
    )
    parser.add_argument("--version", action="version", version="matrix-sim-studio 0.1.0")

    subparsers = parser.add_subparsers(dest="command")

    # ----- run subcommand (Phase 0 file-in/out) -----
    run_p = subparsers.add_parser(
        "run", help="Run a simulation from a request JSON file"
    )
    run_p.add_argument("request", type=Path, help="Path to simulation request JSON file")
    run_p.add_argument("-o", "--output", type=Path, help="Output path for results (default: stdout)")
    run_p.add_argument("--max-messages", type=int, help="Override maximum number of conversation turns")
    run_p.add_argument("--no-db", action="store_true", help="Skip database persistence")
    run_p.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging")
    run_p.set_defaults(func=_cmd_run)

    # ----- serve subcommand (Phase 1 web server) -----
    serve_p = subparsers.add_parser("serve", help="Start the FastAPI control-room web server")
    serve_p.add_argument("--host", type=str, default=None, help="Bind host (default: MATRIX_HOST)")
    serve_p.add_argument("--port", type=int, default=None, help="Bind port (default: MATRIX_PORT)")
    serve_p.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging")
    serve_p.set_defaults(func=_cmd_serve)

    return parser


def main():
    """Main CLI entrypoint."""
    parser = build_parser()
    args = parser.parse_args()

    if not getattr(args, "command", None):
        parser.print_help()
        return 1

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

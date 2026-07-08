# SPDX-License-Identifier: Apache-2.0
"""
CLI entrypoint for TheMatrix Simulation Studio.

Usage:
    python -m matrix_studio <request.json> [options]
    matrix-studio <request.json> [options]
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


def main():
    """Main CLI entrypoint."""
    parser = argparse.ArgumentParser(
        description="TheMatrix Simulation Studio - Multi-agent conversation simulator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run a simulation from request.json
  matrix-studio request.json

  # Run with custom output path and turn limit
  matrix-studio request.json -o results.json --max-messages 10

  # Run without database persistence
  matrix-studio request.json --no-db

For more information, see: https://github.com/yourusername/matrix-sim-studio
        """,
    )

    parser.add_argument(
        "request",
        type=Path,
        help="Path to simulation request JSON file",
    )

    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Output path for results (default: stdout)",
    )

    parser.add_argument(
        "--max-messages",
        type=int,
        help="Override maximum number of conversation turns",
    )

    parser.add_argument(
        "--no-db",
        action="store_true",
        help="Skip database persistence (faster for testing)",
    )

    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )

    parser.add_argument(
        "--version",
        action="version",
        version="matrix-sim-studio 0.1.0",
    )

    args = parser.parse_args()

    # Setup logging
    setup_logging(args.verbose)

    # Validate request file exists
    if not args.request.exists():
        print(f"Error: Request file not found: {args.request}", file=sys.stderr)
        return 1

    # Run simulation
    try:
        exit_code = asyncio.run(
            run_from_file(
                args.request,
                args.output,
                args.max_messages,
                args.no_db,
            )
        )
        return exit_code
    except KeyboardInterrupt:
        print("\nSimulation interrupted by user", file=sys.stderr)
        return 130
    except Exception as e:
        logging.getLogger(__name__).error(f"Fatal error: {e}", exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())

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

from matrix_studio import __version__
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
        db_path = settings.db_file
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


async def _docs_action(args: argparse.Namespace) -> int:
    """Phase 5: attach / list / search / reindex documents for a run.

    Talks to the database directly rather than over HTTP so it works without a
    running server — the same reason the ``run`` subcommand exists.
    """
    from matrix_studio.documents import ExtractionError, ingest_file
    from matrix_studio.retrieval import apply_budget, build_fts_query, extract_terms
    from matrix_studio.settings import get_settings
    from matrix_studio.storage import Database

    settings = get_settings()
    db = Database(str(settings.db_file))
    await db.connect()
    try:
        run = await db.get_run_by_ref(args.run)
        if not run:
            print(f"Error: run not found: {args.run}", file=sys.stderr)
            return 1
        run_id = run["id"]

        if args.docs_action == "attach":
            try:
                doc = ingest_file(args.path, title=args.title)
            except ExtractionError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 1
            doc_id = await db.add_document(
                run_id=run_id,
                title=doc.title,
                chunks=[c.content for c in doc.chunks],
                persona_name=args.persona,
                source_path=doc.source_path,
                media_type=doc.media_type,
                char_count=doc.char_count,
            )
            scope = args.persona or "(whole cast)"
            print(
                f"Attached {doc.title} to {scope}: "
                f"{len(doc.chunks)} chunks, {doc.char_count} chars  [{doc_id}]"
            )
            return 0

        if args.docs_action == "list":
            docs = await db.list_documents(run_id, persona_name=args.persona)
            if not docs:
                print("No documents attached to this run.")
                return 0
            print(f"{'id':14s} {'persona':14s} {'chunks':>6s} {'chars':>8s}  title")
            for d in docs:
                print(
                    f"{d['id']:14s} {(d['persona_name'] or '(all)'):14s} "
                    f"{d['chunk_count']:6d} {d['char_count']:8d}  {d['title']}"
                )
            return 0

        if args.docs_action == "search":
            query = " ".join(args.query)
            fts = build_fts_query(query)
            if not fts:
                print("No searchable terms after removing stopwords.")
                return 0
            print(f"terms : {', '.join(extract_terms(query))}")
            print(f"query : {fts}")
            rows = await db.search_documents(
                run_id=run_id, query=fts, persona_name=args.persona, k=args.k
            )
            passages = apply_budget(rows, max_chars=args.max_chars)
            if not passages:
                # An honest empty result is the useful signal here: it is how the
                # lexical limitation shows itself.
                print(f"\nNo passages matched (searched {len(rows)} candidates).")
                return 0
            print(f"\n{len(passages)} passage(s), "
                  f"{sum(len(p.content) for p in passages)} chars:")
            for p in passages:
                print(f"\n  [{p.citation}] score={p.score:.4f}")
                print(f"  {p.content}")
            return 0

        if args.docs_action == "reindex":
            count = await db.reindex_documents()
            print(f"Rebuilt the document index from doc_chunks: {count} chunks.")
            return 0

        if args.docs_action == "embed":
            from matrix_studio.retrieval import embed_pending_chunks

            stats = await embed_pending_chunks(
                db, run_id, embedding_model=args.model or ""
            )
            if stats.get("error"):
                print(f"Error: {stats['error']}", file=sys.stderr)
                return 1
            already = await db.count_chunk_vectors(run_id)
            print(
                f"Embedded {stats['embedded']} chunk(s) with {stats['model']}: "
                f"{stats['tokens']:,} tokens, ${stats['cost_usd']:.6f}. "
                f"{already} chunk(s) now have vectors."
            )
            return 0

        print(f"Error: unknown docs action: {args.docs_action}", file=sys.stderr)
        return 2
    finally:
        await db.close()


def _cmd_docs(args: argparse.Namespace) -> int:
    """Handle the ``docs`` subcommand (Phase 5 document management)."""
    setup_logging(getattr(args, "verbose", False))
    try:
        return asyncio.run(_docs_action(args))
    except KeyboardInterrupt:
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
    parser.add_argument(
        "--version", action="version",
        version=f"matrix-sim-studio {__version__}",
    )

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

    # ----- docs subcommand (Phase 5 per-persona documents) -----
    docs_p = subparsers.add_parser(
        "docs", help="Attach, list, search or reindex a run's background documents"
    )
    docs_p.add_argument("run", help="Run id, name or slug")
    docs_p.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging")
    docs_sub = docs_p.add_subparsers(dest="docs_action", required=True)

    attach_p = docs_sub.add_parser("attach", help="Attach a document to a persona")
    attach_p.add_argument("path", help="Path to a .pdf/.docx/.txt/.md file")
    attach_p.add_argument(
        "-p", "--persona", default=None,
        help="Persona that may retrieve it (default: the whole cast)",
    )
    attach_p.add_argument("-t", "--title", default=None, help="Override the title")

    list_p = docs_sub.add_parser("list", help="List a run's documents")
    list_p.add_argument(
        "-p", "--persona", default=None,
        help="Restrict to what this persona can see (its own + cast-wide)",
    )

    search_p = docs_sub.add_parser(
        "search", help="Inspect what a query retrieves (measures retrieval quality)"
    )
    search_p.add_argument("query", nargs="+", help="Free text; sanitised automatically")
    search_p.add_argument("-p", "--persona", default=None, help="Search one persona's slice")
    search_p.add_argument("-k", type=int, default=5, help="Max passages (default 5)")
    search_p.add_argument(
        "--max-chars", type=int, default=2000,
        help="Hard ceiling on returned characters (default 2000)",
    )

    docs_sub.add_parser("reindex", help="Rebuild the lexical index from doc_chunks")

    embed_p = docs_sub.add_parser(
        "embed",
        help="Embed chunks for vector/hybrid retrieval (needs the 'vectors' extra)",
    )
    embed_p.add_argument(
        "-m", "--model", default=None,
        help="LiteLLM embedding model (default: Titan Embed v2)",
    )

    docs_p.set_defaults(func=_cmd_docs)

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

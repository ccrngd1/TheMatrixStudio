# SPDX-License-Identifier: Apache-2.0
"""
Event-sourced SQLite storage layer.

Schema:
- runs: one row per simulation run
- events: append-only event log (source of truth)
- snapshots: full state snapshots for fast restoration
"""

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite

from matrix_studio.state import SimSnapshot
from matrix_studio.tenancy import LOCAL_USER_SUB

logger = logging.getLogger(__name__)

# Tokenizer for every FTS5 index here. Named once because the scratch index used for
# run-scoped scoring MUST tokenize identically to the main one — if they drifted, a
# chunk found by the main index's stemming could score zero under the scratch index.
FTS_TOKENIZER = "porter unicode61"

# Scratch FTS index used to score a single run's slice. Lives in the `temp` schema, so
# it never touches the database file and disappears with the connection.
_SCOPE_TABLE = "retrieval_scope"


class Database:
    """Async SQLite database with event sourcing."""

    def __init__(self, db_path: str):
        """
        Initialize database connection.

        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None
        # Serialises the scratch-index sequence in _search_documents_run_scoped.
        # Created here rather than on first use: two coroutines racing to create a
        # lock each get their own, which locks nothing.
        self._scope_lock = asyncio.Lock()
        # Set during schema creation; document retrieval degrades to "no results"
        # rather than raising if this SQLite build has no FTS5.
        self._fts5_available: bool = False
        # Phase 5f: sqlite-vec is optional. When the extension cannot be loaded
        # (package absent, or a Python built without extension support) vector
        # retrieval degrades to lexical rather than failing.
        self._vec_available: bool = False
        # Dimension of the live vec0 table, discovered from embedding_meta or set
        # when the table is created. Vec tables are fixed-width, so a model change
        # must be detected rather than silently mixing dimensions.
        self._vec_dim: Optional[int] = None

    async def connect(self):
        """Connect to database and ensure schema exists."""
        # Whether the file pre-existed has to be sampled BEFORE connecting, since
        # connecting creates it. It is the difference between "your runs are here"
        # and "this is a brand-new, empty database" — see the log below.
        resolved = Path(self.db_path).resolve()
        pre_existing = resolved.is_file()

        # Ensure directory exists
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        self._conn = await aiosqlite.connect(self.db_path)

        # Enable row factory for dict-like access
        self._conn.row_factory = aiosqlite.Row

        # Enable WAL mode for concurrent reads
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")

        # Phase 5f: load sqlite-vec if it is installed and this Python's sqlite3
        # allows extensions. Optional by design — everything except vector
        # retrieval works without it.
        await self._load_vec_extension()

        # Create schema
        await self._create_schema()

        # Say plainly which file is in use and how much is in it. A silently
        # created empty database looks exactly like data loss from the UI, so the
        # absolute path and the run count are logged at startup rather than left
        # to be reconstructed after someone reports missing conversations.
        cursor = await self._conn.execute("SELECT COUNT(*) FROM runs")
        row = await cursor.fetchone()
        run_count = row[0] if row else 0
        if pre_existing:
            logger.info("Database: %s (%d existing run(s))", resolved, run_count)
        else:
            logger.warning(
                "Database: %s — CREATED NEW AND EMPTY (no previous conversations "
                "will be listed). If you expected existing runs, check DATA_DIR.",
                resolved,
            )

    async def _load_vec_extension(self) -> None:
        """Load the sqlite-vec extension, tolerating every way it can be absent."""
        try:
            import sqlite_vec
        except ImportError:
            logger.debug(
                "sqlite-vec not installed; vector retrieval unavailable "
                "(install with: pip install 'matrix-sim-studio[vectors]')"
            )
            return
        try:
            await self._conn.enable_load_extension(True)
            await self._conn.load_extension(sqlite_vec.loadable_path())
            self._vec_available = True
        except Exception as exc:  # noqa: BLE001
            # Some distro/macOS Pythons ship sqlite3 with extension loading
            # compiled out; that is a deployment fact, not an error to raise.
            logger.warning(
                "sqlite-vec present but could not be loaded (%s); "
                "vector retrieval unavailable, lexical retrieval unaffected.", exc
            )
        finally:
            try:
                await self._conn.enable_load_extension(False)
            except Exception:  # noqa: BLE001
                pass

    async def close(self):
        """Close database connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def _create_schema(self):
        """Create database schema if it doesn't exist."""
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                name TEXT,
                description TEXT,
                slug TEXT,
                topic TEXT NOT NULL,
                cast_json TEXT NOT NULL,
                config_json TEXT,
                status TEXT DEFAULT 'pending',
                parent_run_id TEXT,
                branch_turn INTEGER,
                created_at INTEGER NOT NULL,
                completed_at INTEGER,
                owner_sub TEXT
            )
        """)

        # Phase 1 additive migration: existing Phase 0 databases have a `runs`
        # table without the `description`/`slug` columns. Add them if missing so
        # older rows/queries keep working (nullable, backward-compatible).
        async with self._conn.execute("PRAGMA table_info(runs)") as cursor:
            existing_cols = {row[1] for row in await cursor.fetchall()}
        for col in ("name", "description", "slug", "owner_sub"):
            if col not in existing_cols:
                await self._conn.execute(f"ALTER TABLE runs ADD COLUMN {col} TEXT")

        # Phase 0.2 tenancy migration. Existing rows predate ownership, and a NULL
        # owner is a run nobody can read — so back-fill them to the single local
        # user, which is who actually created them. Doing this before the unique
        # index is built matters: the index is over (owner_sub, name), and NULL is
        # not equal to itself in SQL, so a table of NULL owners would silently keep
        # global-looking uniqueness semantics.
        await self._conn.execute(
            "UPDATE runs SET owner_sub = ? WHERE owner_sub IS NULL",
            (LOCAL_USER_SUB,),
        )

        # Name uniqueness is PER USER, not global. Two people must both be able to
        # have a run called `trusted-robot` — with a global index the second one to
        # generate that name would collide with a row they cannot even see, which
        # is both a bug and an information leak about another tenant's data.
        #
        # The old global index has to be dropped explicitly. Leaving it in place
        # would defeat the new one entirely: `CREATE INDEX IF NOT EXISTS` is
        # satisfied by adding the per-user index, and the surviving global index
        # would go on rejecting the cross-user duplicate anyway. Silent, and
        # invisible to any test that only checks the new index exists.
        await self._conn.execute("DROP INDEX IF EXISTS runs_name_unique")
        await self._conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS runs_owner_name_unique
            ON runs(owner_sub, name) WHERE name IS NOT NULL
        """)
        # Every tenant-scoped read filters on this.
        await self._conn.execute("""
            CREATE INDEX IF NOT EXISTS runs_owner_created
            ON runs(owner_sub, created_at DESC)
        """)

        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES runs(id),
                turn INTEGER NOT NULL,
                seq INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                agent_name TEXT,
                payload TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                UNIQUE(run_id, turn, seq)
            )
        """)

        await self._conn.execute("""
            CREATE INDEX IF NOT EXISTS events_run_turn
            ON events(run_id, turn)
        """)

        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES runs(id),
                turn INTEGER NOT NULL,
                state_json TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                UNIQUE(run_id, turn)
            )
        """)

        # ------------------------------------------------------------------ #
        # Phase 1.5 additive tables (post-run analysis layer).
        #
        # These are NEW tables only. The Phase 0/1 tables above (runs, events,
        # snapshots) are untouched: no columns added, no semantics changed, so
        # every existing query/row keeps working. Summaries and aside threads
        # are read-only analysis attached to a run — they never write to the
        # canonical event log, snapshot, or the run's recorded cost.
        # ------------------------------------------------------------------ #

        # Model-generated (or imported) structured analysis of a completed run.
        # `kind` distinguishes a freshly generated summary from an imported
        # source summary carried in by the importer — the generated one never
        # overwrites the imported original.
        # `instructions` holds the effective analyst-role framing that created a
        # summary (NULL = the default framing was used); it lets the regenerate
        # UI prefill "the prompt that created this summary." Additive/nullable.
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES runs(id),
                kind TEXT NOT NULL DEFAULT 'generated',
                payload_json TEXT NOT NULL,
                tokens_in INTEGER NOT NULL DEFAULT 0,
                tokens_out INTEGER NOT NULL DEFAULT 0,
                cost_usd REAL NOT NULL DEFAULT 0.0,
                instructions TEXT,
                created_at INTEGER NOT NULL
            )
        """)
        await self._conn.execute("""
            CREATE INDEX IF NOT EXISTS summaries_run
            ON summaries(run_id, kind, created_at)
        """)

        # Additive migration: existing databases created before the editable
        # summary prompt have a `summaries` table without `instructions`. Add it
        # if missing so older rows/queries keep working (nullable = default).
        async with self._conn.execute("PRAGMA table_info(summaries)") as cursor:
            summary_cols = {row[1] for row in await cursor.fetchall()}
        if "instructions" not in summary_cols:
            await self._conn.execute(
                "ALTER TABLE summaries ADD COLUMN instructions TEXT"
            )

        # A scoped aside thread over a run. `mode` is always 'aside' in Phase
        # 1.5; the column exists now so Phase 2 can add 'contribute' without a
        # migration. `target` is 'analyst' | 'persona' | 'room'; persona_name is
        # set only for a persona-target thread.
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS threads (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(id),
                target TEXT NOT NULL,
                persona_name TEXT,
                mode TEXT NOT NULL DEFAULT 'aside',
                created_at INTEGER NOT NULL
            )
        """)
        await self._conn.execute("""
            CREATE INDEX IF NOT EXISTS threads_run
            ON threads(run_id, created_at)
        """)

        # Messages within an aside thread. `role` is 'user' | 'target';
        # `speaker` labels the responding voice (e.g. 'analyst', a persona name,
        # or 'user'). Token/cost are tracked per message and counted SEPARATELY
        # from the canonical run cost.
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS thread_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id TEXT NOT NULL REFERENCES threads(id),
                role TEXT NOT NULL,
                speaker TEXT,
                content TEXT NOT NULL,
                tokens_in INTEGER NOT NULL DEFAULT 0,
                tokens_out INTEGER NOT NULL DEFAULT 0,
                cost_usd REAL NOT NULL DEFAULT 0.0,
                created_at INTEGER NOT NULL
            )
        """)
        await self._conn.execute("""
            CREATE INDEX IF NOT EXISTS thread_messages_thread
            ON thread_messages(thread_id, id)
        """)

        # ------------------------------------------------------------------ #
        # Phase 5 additive tables (per-persona document retrieval).
        #
        # NEW tables only — nothing above is altered, so existing databases pick
        # these up on connect and every existing query keeps working.
        #
        # `persona_name` scopes a document to one persona; NULL means the whole
        # cast may retrieve it. Scoping is a SQL predicate, so a persona provably
        # cannot retrieve outside its own slice.
        # ------------------------------------------------------------------ #
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(id),
                persona_name TEXT,
                title TEXT NOT NULL,
                source_path TEXT,
                media_type TEXT,
                char_count INTEGER NOT NULL DEFAULT 0,
                chunk_count INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL
            )
        """)
        await self._conn.execute("""
            CREATE INDEX IF NOT EXISTS documents_run
            ON documents(run_id, persona_name)
        """)

        # doc_chunks is the SOURCE OF TRUTH for document text. The FTS5 table
        # below is an index over it and holds no content of its own.
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS doc_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id TEXT NOT NULL REFERENCES documents(id),
                run_id TEXT NOT NULL REFERENCES runs(id),
                persona_name TEXT,
                ordinal INTEGER NOT NULL,
                content TEXT NOT NULL,
                UNIQUE(document_id, ordinal)
            )
        """)
        await self._conn.execute("""
            CREATE INDEX IF NOT EXISTS doc_chunks_run
            ON doc_chunks(run_id, persona_name)
        """)

        # FTS5 EXTERNAL-CONTENT index (content='doc_chunks'): it stores only the
        # inverted index, never the text. That is what makes the index a pure
        # derivative of doc_chunks and lets `reindex_documents()` rebuild it with
        # a single statement — the index cannot hold content the source table
        # does not, so the two cannot semantically diverge.
        #
        # FTS5 is compiled into SQLite by default; if this build lacks it we log
        # and continue so a run without documents is unaffected.
        try:
            await self._conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS doc_chunks_fts USING fts5(
                    content,
                    content='doc_chunks',
                    content_rowid='id',
                    tokenize='""" + FTS_TOKENIZER + """'
                )
            """)
            self._fts5_available = True
        except Exception as exc:  # pragma: no cover - depends on SQLite build
            self._fts5_available = False
            logger.warning(
                "SQLite FTS5 unavailable (%s); document retrieval is disabled. "
                "Everything else works normally.",
                exc,
            )

        # ------------------------------------------------------------------ #
        # Phase 5f: vector-embedding support (optional, additive).
        #
        # embedding_meta records which model and dimension the vec0 table was
        # built for. vec0 tables are FIXED-WIDTH, so switching embedding model
        # must be detected and reported rather than silently mixing dimensions
        # that would produce meaningless distances.
        # ------------------------------------------------------------------ #
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS embedding_meta (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                model TEXT NOT NULL,
                dim INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            )
        """)
        # Maps a vec0 rowid back to its chunk, and records which model produced it.
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS chunk_vectors (
                chunk_id INTEGER PRIMARY KEY REFERENCES doc_chunks(id),
                run_id TEXT NOT NULL,
                persona_name TEXT,
                model TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
        """)
        await self._conn.execute("""
            CREATE INDEX IF NOT EXISTS chunk_vectors_run
            ON chunk_vectors(run_id, persona_name)
        """)

        await self._conn.commit()

        # Recreate the vec0 table on connect if a previous session recorded its
        # dimension. The virtual table itself persists, but _vec_dim is in-memory.
        if self._vec_available:
            async with self._conn.execute(
                "SELECT model, dim FROM embedding_meta WHERE id = 1"
            ) as cursor:
                row = await cursor.fetchone()
            if row:
                self._vec_dim = int(row[1])
                await self._ensure_vec_table(self._vec_dim)

    async def _ensure_vec_table(self, dim: int) -> None:
        """Create the fixed-width vec0 table for ``dim``, if not already present."""
        if not self._vec_available:
            return
        await self._conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS chunk_vec USING vec0("
            f"embedding float[{dim}])"
        )
        self._vec_dim = dim

    @property
    def vec_available(self) -> bool:
        """Whether vector retrieval can be used at all in this process."""
        return self._vec_available

    async def create_run(
        self,
        run_id: str,
        topic: str,
        cast: List[Dict[str, Any]],
        name: Optional[str] = None,
        description: Optional[str] = None,
        slug: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
        parent_run_id: Optional[str] = None,
        branch_turn: Optional[int] = None,
        owner_sub: str = LOCAL_USER_SUB,
    ) -> None:
        """
        Create a new simulation run.

        Args:
            run_id: Unique run identifier
            topic: Simulation topic
            cast: List of persona definitions
            name: Optional memorable run name (unique per owner when present)
            description: Optional one-line human description
            slug: Optional normalized slug (defaults to name)
            config: Optional configuration dict
            parent_run_id: Parent run ID if this is a branch
            branch_turn: Turn number branched from
            owner_sub: Who owns this run.

        ``owner_sub`` has a default, unlike the read methods below, and the
        asymmetry is deliberate. A forgotten owner on a *read* fails OPEN — it
        would return another tenant's run — so those arguments are required and
        omitting one is a ``TypeError``. A forgotten owner on a *write* fails
        CLOSED: the run belongs to an identity no authenticated user has, so
        nobody can read it. That is a visible bug (the creator cannot find their
        own run) rather than a leak, and it keeps ~40 test and script call sites
        that have no notion of a user from having to invent one.
        """
        await self._conn.execute(
            """
            INSERT INTO runs (id, name, description, slug, topic, cast_json,
                              config_json, status, parent_run_id, branch_turn,
                              created_at, owner_sub)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
            """,
            (
                run_id,
                name,
                description,
                slug or name,
                topic,
                json.dumps(cast),
                json.dumps(config) if config else None,
                parent_run_id,
                branch_turn,
                int(time.time()),
                owner_sub,
            ),
        )
        await self._conn.commit()

    async def name_exists(self, name: str, *, owner_sub: str) -> bool:
        """Return True if THIS OWNER already has a run with this memorable name.

        Scoped, because uniqueness is per user. An unscoped check would make one
        tenant's chosen codename unavailable to everyone else, and the resulting
        "that name is taken" would be a statement about data the caller cannot see.
        """
        async with self._conn.execute(
            "SELECT 1 FROM runs WHERE name = ? AND owner_sub = ? LIMIT 1",
            (name, owner_sub),
        ) as cursor:
            return await cursor.fetchone() is not None

    async def update_run_status(
        self, run_id: str, status: str, completed_at: Optional[int] = None
    ) -> None:
        """
        Update run status.

        Args:
            run_id: Run identifier
            status: New status (pending|running|complete|failed|branched)
            completed_at: Optional completion timestamp
        """
        if completed_at is not None:
            await self._conn.execute(
                "UPDATE runs SET status = ?, completed_at = ? WHERE id = ?",
                (status, completed_at, run_id),
            )
        else:
            await self._conn.execute(
                "UPDATE runs SET status = ? WHERE id = ?", (status, run_id)
            )
        await self._conn.commit()

    async def append_event(
        self,
        run_id: str,
        turn: int,
        seq: int,
        event_type: str,
        payload: Dict[str, Any],
        agent_name: Optional[str] = None,
    ) -> None:
        """
        Append an event to the event log.

        Args:
            run_id: Run identifier
            turn: Turn number
            seq: Sequence number within turn
            event_type: Event type (e.g., 'sim.started', 'speaker.selected')
            payload: Event payload dict
            agent_name: Optional agent name for agent-specific events
        """
        await self._conn.execute(
            """
            INSERT INTO events (run_id, turn, seq, event_type, agent_name,
                                payload, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                turn,
                seq,
                event_type,
                agent_name,
                json.dumps(payload),
                int(time.time()),
            ),
        )
        await self._conn.commit()

    async def save_snapshot(self, snapshot: SimSnapshot) -> None:
        """
        Save a full state snapshot.

        Args:
            snapshot: SimSnapshot to save
        """
        await self._conn.execute(
            """
            INSERT OR REPLACE INTO snapshots (run_id, turn, state_json, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                snapshot.run_id,
                snapshot.turn,
                snapshot.model_dump_json(),
                int(time.time()),
            ),
        )
        await self._conn.commit()

    async def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        """
        Get run metadata by id, WITHOUT an ownership check.

        Args:
            run_id: Run identifier

        Returns:
            Run dict or None if not found

        **Internal, post-authorisation use only.** Callers are the engine and the
        startup sweep, which operate on a run whose ownership has already been
        established (or, in the sweep's case, across all tenants by design).
        Anything reachable from an HTTP route must go through
        ``get_run_by_ref``, which enforces the scope in SQL.
        """
        async with self._conn.execute(
            "SELECT * FROM runs WHERE id = ?", (run_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return dict(row)
            return None

    async def get_run_by_ref(
        self, ref: str, *, owner_sub: str
    ) -> Optional[Dict[str, Any]]:
        """
        Resolve one of THIS OWNER's runs by either its UUID id or its memorable name.

        Args:
            ref: A run_id (UUID) or a memorable name.
            owner_sub: The caller's identity. Required and keyword-only.

        Returns:
            Run dict, or None if it does not exist *or* is not this owner's.

        This is the single authorisation choke point for the ~25 routes that take a
        ``ref``, and the reason ``owner_sub`` is required rather than defaulted: a
        route that forgets it raises ``TypeError`` at call time instead of quietly
        serving another tenant's conversation. One missed route is the whole
        vulnerability, so the check cannot be something each route remembers to do.

        Returning None rather than raising is what collapses "not found" and "not
        yours" into an identical 404 at the edge. A 403 would confirm that a given
        run name exists under some other account, and run names are guessable —
        they come from a generator with a small vocabulary.
        """
        async with self._conn.execute(
            """
            SELECT * FROM runs
            WHERE (id = ? OR name = ?) AND owner_sub = ?
            LIMIT 1
            """,
            (ref, ref, owner_sub),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def list_runs(
        self, q: Optional[str] = None, limit: int = 200, *, owner_sub: str
    ) -> List[Dict[str, Any]]:
        """
        List THIS OWNER's runs (newest first), optionally filtered by a
        case-insensitive substring matching name, description, or topic.

        Each returned dict includes derived aggregates (turn_count,
        total_cost_usd) computed from the event log so the history list needs
        no extra round-trips.

        ``owner_sub`` is keyword-only and required for the same reason as in
        ``get_run_by_ref``: this is the history list, so an omitted filter would
        show every tenant every other tenant's conversations at once.
        """
        if q:
            like = f"%{q.lower()}%"
            query = """
                SELECT * FROM runs
                WHERE owner_sub = ?
                  AND (lower(COALESCE(name, '')) LIKE ?
                       OR lower(COALESCE(description, '')) LIKE ?
                       OR lower(topic) LIKE ?)
                ORDER BY created_at DESC
                LIMIT ?
            """
            params = (owner_sub, like, like, like, limit)
        else:
            query = """
                SELECT * FROM runs WHERE owner_sub = ?
                ORDER BY created_at DESC LIMIT ?
            """
            params = (owner_sub, limit)

        async with self._conn.execute(query, params) as cursor:
            rows = [dict(r) for r in await cursor.fetchall()]

        for run in rows:
            stats = await self.get_run_stats(run["id"])
            run.update(stats)
        return rows

    async def list_runs_by_status(self, status: str) -> List[Dict[str, Any]]:
        """
        Return raw run rows in a given lifecycle status (newest first).

        Used by the startup stale-run sweep: on a fresh process no run can have a
        live background task, so any row still marked ``running`` was orphaned by
        a crash/restart mid-generation. Rows are returned unenriched (no event
        aggregates) since the sweep only needs id + last recorded turn, which it
        derives from the event log directly.

        **Deliberately cross-tenant, and the only read that is.** A crash orphans
        every user's in-flight run, so a sweep scoped to one tenant would leave
        everyone else's showing as live forever. This is a system operation with no
        caller identity: nothing here is returned to a user, and there is no HTTP
        route to it.
        """
        async with self._conn.execute(
            "SELECT * FROM runs WHERE status = ? ORDER BY created_at DESC",
            (status,),
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]

    async def last_event_turn(self, run_id: str) -> int:
        """Highest turn number recorded in the event log, or 0 if none."""
        async with self._conn.execute(
            "SELECT MAX(turn) FROM events WHERE run_id = ?", (run_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row and row[0] is not None else 0

    async def last_checkpoint_turn(self, run_id: str) -> Optional[int]:
        """Highest per-turn snapshot (checkpoint) turn for a run, or None."""
        async with self._conn.execute(
            "SELECT MAX(turn) FROM snapshots WHERE run_id = ?", (run_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row and row[0] is not None else None

    async def truncate_after_turn(self, run_id: str, turn: int) -> int:
        """
        Delete this run's events and snapshots with ``turn > turn``.

        Used only when resuming an ``interrupted``/``failed`` run in place: the
        tail past the last complete checkpoint is a partial/dangling turn (e.g. a
        ``speaker.selected`` with no response) plus the ``sim.interrupted``
        marker — never a completed canonical turn. Trimming it lets the run
        continue cleanly from the checkpoint and keeps the event log replayable
        (no phantom mid-stream terminal event). Returns the number of events
        removed. Never called on a ``complete`` run.
        """
        async with self._conn.execute(
            "SELECT COUNT(*) FROM events WHERE run_id = ? AND turn > ?",
            (run_id, turn),
        ) as cursor:
            row = await cursor.fetchone()
            removed = int(row[0]) if row else 0
        await self._conn.execute(
            "DELETE FROM events WHERE run_id = ? AND turn > ?", (run_id, turn)
        )
        await self._conn.execute(
            "DELETE FROM snapshots WHERE run_id = ? AND turn > ?", (run_id, turn)
        )
        await self._conn.commit()
        return removed

    async def get_run_stats(self, run_id: str) -> Dict[str, Any]:
        """Aggregate turn count and total cost from the event log for a run."""
        async with self._conn.execute(
            """
            SELECT COUNT(*) AS turns FROM events
            WHERE run_id = ? AND event_type = 'agent.response'
            """,
            (run_id,),
        ) as cursor:
            turn_row = await cursor.fetchone()

        async with self._conn.execute(
            """
            SELECT payload FROM events
            WHERE run_id = ? AND event_type = 'agent.response'
            """,
            (run_id,),
        ) as cursor:
            payloads = await cursor.fetchall()

        total_cost = 0.0
        for row in payloads:
            try:
                total_cost += float(json.loads(row[0]).get("cost_usd", 0.0) or 0.0)
            except (ValueError, TypeError, json.JSONDecodeError):
                continue

        # Wall-clock time of the most recent event (any type), so the client can
        # detect a run that is still marked "running" but has gone quiet — a
        # stalled/orphaned run with a live server but no live stream.
        async with self._conn.execute(
            "SELECT MAX(created_at) FROM events WHERE run_id = ?", (run_id,)
        ) as cursor:
            last_row = await cursor.fetchone()
        last_event_at = int(last_row[0]) if last_row and last_row[0] is not None else None

        return {
            "turn_count": turn_row[0] if turn_row else 0,
            "total_cost_usd": total_cost,
            "last_event_at": last_event_at,
        }

    async def get_events_after(
        self, run_id: str, after_seq: int = -1, limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Get events for a run ordered by (turn, seq), starting strictly after the
        given global sequence number. Used for replay / late-join / paging.

        The engine assigns a monotonic per-run ``seq`` across all events, so a
        client can pass the highest seq it has already seen to resume.
        """
        query = """
            SELECT * FROM events
            WHERE run_id = ? AND seq > ?
            ORDER BY turn, seq
        """
        params: tuple = (run_id, after_seq)
        if limit is not None:
            query += " LIMIT ?"
            params = (run_id, after_seq, limit)

        async with self._conn.execute(query, params) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    async def get_events(
        self, run_id: str, from_turn: int = 0, to_turn: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Get events for a run.

        Args:
            run_id: Run identifier
            from_turn: Starting turn (inclusive)
            to_turn: Ending turn (inclusive), None for all

        Returns:
            List of event dicts
        """
        if to_turn is not None:
            query = """
                SELECT * FROM events
                WHERE run_id = ? AND turn >= ? AND turn <= ?
                ORDER BY turn, seq
            """
            params = (run_id, from_turn, to_turn)
        else:
            query = """
                SELECT * FROM events
                WHERE run_id = ? AND turn >= ?
                ORDER BY turn, seq
            """
            params = (run_id, from_turn)

        async with self._conn.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def list_snapshots(self, run_id: str) -> List[Dict[str, Any]]:
        """
        List available checkpoint turns for a run (Phase 2a).

        Returns a list of ``{turn, status, created_at}`` dicts ordered by turn.
        ``status`` is read from the stored snapshot JSON so the client can tell a
        running per-turn checkpoint from the completion snapshot. Cheap: it only
        parses the small status field, not the full agent state.
        """
        async with self._conn.execute(
            """
            SELECT turn, state_json, created_at FROM snapshots
            WHERE run_id = ?
            ORDER BY turn ASC
            """,
            (run_id,),
        ) as cursor:
            rows = await cursor.fetchall()

        out: List[Dict[str, Any]] = []
        for row in rows:
            status = None
            try:
                status = json.loads(row[1]).get("status")
            except (json.JSONDecodeError, TypeError):
                status = None
            out.append(
                {"turn": row[0], "status": status, "created_at": row[2]}
            )
        return out

    async def get_run_tree(
        self, run_id: str, *, owner_sub: str
    ) -> Dict[str, Any]:
        """
        Return the full lineage tree rooted at the **ancestor** of ``run_id``
        (i.e. the original run that was forked to eventually produce this run).

        The result is a dict-of-dicts keyed by ``run_id``. Each entry has:
          id, name, slug, status, branch_turn, parent_run_id, config_json,
          created_at, turn_count, total_cost_usd

        The config_json is included so the caller can extract
        ``config.branch_mutation`` for edge labels without a round-trip.

        Every hop is scoped to ``owner_sub``. A branch always inherits its
        parent's owner, so in a correct system the recursion could never leave the
        tenant and this filter would be redundant — which is exactly why it is
        here. The one query in the codebase that walks *from* an authorised run
        *to* rows nobody checked should not depend on an invariant holding
        elsewhere; if branching ever loses the owner, the failure is a missing node
        rather than another tenant's run names in the tree view.
        """
        # Walk up to the root (the run whose parent_run_id IS NULL).
        root_id = run_id
        async with self._conn.execute(
            """
            WITH RECURSIVE ancestors(id, parent_run_id) AS (
                SELECT id, parent_run_id FROM runs
                WHERE id = ? AND owner_sub = ?
                UNION ALL
                SELECT r.id, r.parent_run_id
                FROM runs r
                JOIN ancestors a ON r.id = a.parent_run_id
                WHERE r.owner_sub = ?
            )
            SELECT id FROM ancestors WHERE parent_run_id IS NULL
            """,
            (run_id, owner_sub, owner_sub),
        ) as cursor:
            row = await cursor.fetchone()
        if row:
            root_id = row[0]

        # Fetch the full subtree starting from the root.
        async with self._conn.execute(
            """
            WITH RECURSIVE tree(id) AS (
                SELECT id FROM runs WHERE id = ? AND owner_sub = ?
                UNION ALL
                SELECT r.id FROM runs r JOIN tree t ON r.parent_run_id = t.id
                WHERE r.owner_sub = ?
            )
            SELECT r.id, r.name, r.slug, r.status, r.branch_turn, r.parent_run_id,
                   r.config_json, r.created_at,
                   COALESCE((SELECT COUNT(*) FROM events e
                             WHERE e.run_id = r.id AND e.event_type = 'agent.response'), 0),
                   COALESCE((SELECT SUM(CAST(json_extract(e.payload, '$.cost_usd') AS REAL))
                             FROM events e
                             WHERE e.run_id = r.id AND e.event_type = 'agent.response'), 0.0)
            FROM runs r JOIN tree t ON r.id = t.id
            ORDER BY r.created_at ASC
            """,
            (root_id, owner_sub, owner_sub),
        ) as cursor:
            rows = await cursor.fetchall()

        nodes: Dict[str, Any] = {}
        for row in rows:
            nodes[row[0]] = {
                "id": row[0],
                "name": row[1],
                "slug": row[2],
                "status": row[3],
                "branch_turn": row[4],
                "parent_run_id": row[5],
                "config_json": row[6],
                "created_at": row[7],
                "turn_count": row[8],
                "total_cost_usd": row[9],
            }
        return {"root_id": root_id, "nodes": nodes}

    async def list_branches(
        self, run_id: str, *, owner_sub: str
    ) -> List[Dict[str, Any]]:
        """
        List child branches forked from ``run_id`` (Phase 2a lineage).

        Returns ``{run_id, name, branch_turn, status, created_at}`` for each run
        whose ``parent_run_id`` is this run, newest first. Read-only; used by the
        run detail view to show "this run's branches".

        Scoped for the same reason as ``get_run_tree``: the caller authorised the
        parent, not the children.
        """
        async with self._conn.execute(
            """
            SELECT id, name, branch_turn, status, created_at FROM runs
            WHERE parent_run_id = ? AND owner_sub = ?
            ORDER BY created_at DESC
            """,
            (run_id, owner_sub),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            {
                "run_id": row[0],
                "name": row[1],
                "branch_turn": row[2],
                "status": row[3],
                "created_at": row[4],
            }
            for row in rows
        ]

    async def copy_events_upto(
        self, source_run_id: str, dest_run_id: str, upto_turn: int
    ) -> int:
        """
        Copy the source run's event log UP TO AND INCLUDING ``upto_turn`` into
        ``dest_run_id``, preserving (turn, seq, event_type, agent_name, payload)
        so the branch replays byte-for-byte identically to the parent up to the
        fork. Phase 2a branch primitive.

        The source run is only READ here — it is never modified (immutability
        invariant). Returns the number of events copied.

        NOTE: this reads the source and writes the destination in the SAME
        database/connection. The destination run row must already exist (FK).
        """
        async with self._conn.execute(
            """
            SELECT turn, seq, event_type, agent_name, payload, created_at
            FROM events
            WHERE run_id = ? AND turn <= ?
            ORDER BY turn, seq
            """,
            (source_run_id, upto_turn),
        ) as cursor:
            rows = await cursor.fetchall()

        for row in rows:
            await self._conn.execute(
                """
                INSERT INTO events (run_id, turn, seq, event_type, agent_name,
                                    payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    dest_run_id,
                    row["turn"],
                    row["seq"],
                    row["event_type"],
                    row["agent_name"],
                    row["payload"],
                    row["created_at"],
                ),
            )
        await self._conn.commit()
        return len(rows)

    async def max_seq(self, run_id: str) -> int:
        """Highest per-run event ``seq`` for a run, or -1 if it has no events."""
        async with self._conn.execute(
            "SELECT MAX(seq) FROM events WHERE run_id = ?", (run_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row and row[0] is not None else -1

    async def get_snapshot(
        self, run_id: str, turn: Optional[int] = None
    ) -> Optional[SimSnapshot]:
        """
        Get a snapshot for a run.

        Args:
            run_id: Run identifier
            turn: Specific turn, or None for latest

        Returns:
            SimSnapshot or None if not found
        """
        if turn is not None:
            query = """
                SELECT state_json FROM snapshots
                WHERE run_id = ? AND turn = ?
            """
            params = (run_id, turn)
        else:
            query = """
                SELECT state_json FROM snapshots
                WHERE run_id = ?
                ORDER BY turn DESC
                LIMIT 1
            """
            params = (run_id,)

        async with self._conn.execute(query, params) as cursor:
            row = await cursor.fetchone()
            if row:
                return SimSnapshot.model_validate_json(row[0])
            return None

    # --------------------------------------------------------------------- #
    # Phase 1.5 — post-run analysis (summaries + aside threads).
    #
    # All methods below operate ONLY on the additive tables. None of them write
    # to `events`, `snapshots`, or mutate a run's recorded cost — the read-only
    # invariant is enforced by construction here.
    # --------------------------------------------------------------------- #

    async def save_summary(
        self,
        run_id: str,
        payload: Dict[str, Any],
        kind: str = "generated",
        tokens_in: int = 0,
        tokens_out: int = 0,
        cost_usd: float = 0.0,
        instructions: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Persist a summary for a run. Appends a new row (versioned by created_at)
        rather than replacing, so history is retained; the getters return the
        latest per kind. A generated summary NEVER overwrites an imported one
        (they are distinct ``kind`` values).

        ``instructions`` is the effective analyst-role framing that created the
        summary (NULL when the default framing was used), so the regenerate UI
        can prefill the prompt that produced it.
        """
        created_at = int(time.time())
        cursor = await self._conn.execute(
            """
            INSERT INTO summaries (run_id, kind, payload_json, tokens_in,
                                   tokens_out, cost_usd, instructions, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                kind,
                json.dumps(payload),
                tokens_in,
                tokens_out,
                cost_usd,
                instructions,
                created_at,
            ),
        )
        await self._conn.commit()
        return {
            "id": cursor.lastrowid,
            "run_id": run_id,
            "kind": kind,
            "payload": payload,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost_usd": cost_usd,
            "instructions": instructions,
            "created_at": created_at,
        }

    async def get_summaries(self, run_id: str) -> List[Dict[str, Any]]:
        """
        Return the latest summary of each kind for a run (newest per kind),
        each with its parsed payload. Kinds are typically 'generated' and
        'imported'; both may be present simultaneously.
        """
        async with self._conn.execute(
            """
            SELECT * FROM summaries
            WHERE run_id = ?
            ORDER BY created_at DESC, id DESC
            """,
            (run_id,),
        ) as cursor:
            rows = [dict(r) for r in await cursor.fetchall()]

        latest_by_kind: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            if row["kind"] in latest_by_kind:
                continue
            try:
                payload = json.loads(row["payload_json"])
            except json.JSONDecodeError:
                payload = {}
            latest_by_kind[row["kind"]] = {
                "id": row["id"],
                "run_id": row["run_id"],
                "kind": row["kind"],
                "payload": payload,
                "tokens_in": row["tokens_in"],
                "tokens_out": row["tokens_out"],
                "cost_usd": row["cost_usd"],
                "instructions": row["instructions"],
                "created_at": row["created_at"],
            }
        return list(latest_by_kind.values())

    async def create_thread(
        self,
        thread_id: str,
        run_id: str,
        target: str,
        persona_name: Optional[str] = None,
        mode: str = "aside",
    ) -> Dict[str, Any]:
        """Create an aside thread over a run. ``mode`` is always 'aside' in 1.5."""
        created_at = int(time.time())
        await self._conn.execute(
            """
            INSERT INTO threads (id, run_id, target, persona_name, mode, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (thread_id, run_id, target, persona_name, mode, created_at),
        )
        await self._conn.commit()
        return {
            "id": thread_id,
            "run_id": run_id,
            "target": target,
            "persona_name": persona_name,
            "mode": mode,
            "created_at": created_at,
        }

    async def get_thread(self, thread_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a thread's metadata (without messages)."""
        async with self._conn.execute(
            "SELECT * FROM threads WHERE id = ?", (thread_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def list_threads(self, run_id: str) -> List[Dict[str, Any]]:
        """List aside threads for a run (oldest first) with message counts."""
        async with self._conn.execute(
            """
            SELECT t.*, COUNT(m.id) AS message_count,
                   COALESCE(SUM(m.cost_usd), 0.0) AS total_cost_usd
            FROM threads t
            LEFT JOIN thread_messages m ON m.thread_id = t.id
            WHERE t.run_id = ?
            GROUP BY t.id
            ORDER BY t.created_at ASC, t.id ASC
            """,
            (run_id,),
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]

    async def add_thread_message(
        self,
        thread_id: str,
        role: str,
        content: str,
        speaker: Optional[str] = None,
        tokens_in: int = 0,
        tokens_out: int = 0,
        cost_usd: float = 0.0,
    ) -> Dict[str, Any]:
        """Append a message (user or target) to an aside thread."""
        created_at = int(time.time())
        cursor = await self._conn.execute(
            """
            INSERT INTO thread_messages (thread_id, role, speaker, content,
                                         tokens_in, tokens_out, cost_usd, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                thread_id,
                role,
                speaker,
                content,
                tokens_in,
                tokens_out,
                cost_usd,
                created_at,
            ),
        )
        await self._conn.commit()
        return {
            "id": cursor.lastrowid,
            "thread_id": thread_id,
            "role": role,
            "speaker": speaker,
            "content": content,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost_usd": cost_usd,
            "created_at": created_at,
        }

    async def get_thread_messages(self, thread_id: str) -> List[Dict[str, Any]]:
        """Return all messages in a thread, oldest first."""
        async with self._conn.execute(
            """
            SELECT * FROM thread_messages
            WHERE thread_id = ?
            ORDER BY id ASC
            """,
            (thread_id,),
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]

    async def thread_cost(self, thread_id: str) -> float:
        """Total USD cost of all target messages in a thread (asides only)."""
        async with self._conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0.0) FROM thread_messages WHERE thread_id = ?",
            (thread_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return float(row[0]) if row else 0.0

    # ---------------------------------------------------------------- #
    # Phase 5: per-persona document storage and retrieval.
    # ---------------------------------------------------------------- #

    async def add_document(
        self,
        run_id: str,
        title: str,
        chunks: List[str],
        persona_name: Optional[str] = None,
        source_path: Optional[str] = None,
        media_type: Optional[str] = None,
        char_count: int = 0,
        document_id: Optional[str] = None,
    ) -> str:
        """Store a document and its chunks, indexing them for search.

        The document row, its chunks and their FTS5 index entries are written in
        ONE transaction. That atomicity is the reason the index lives in this
        database rather than in a separate store: a crash cannot leave the index
        disagreeing with the source of truth.

        Args:
            run_id: Run the document belongs to
            title: Human-facing document title
            chunks: Ordered chunk texts (from ``documents.ingest_*``)
            persona_name: Persona that may retrieve it; None = whole cast
            source_path: Original path/URI, retained for citation
            media_type: pdf|docx|txt|md
            char_count: Length of the extracted text
            document_id: Optional explicit id (defaults to a fresh 12-hex id)

        Returns:
            The document id.
        """
        doc_id = document_id or uuid.uuid4().hex[:12]
        now = int(time.time())
        await self._conn.execute(
            """
            INSERT INTO documents (
                id, run_id, persona_name, title, source_path, media_type,
                char_count, chunk_count, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                doc_id, run_id, persona_name, title, source_path, media_type,
                char_count, len(chunks), now,
            ),
        )
        for ordinal, content in enumerate(chunks):
            cursor = await self._conn.execute(
                """
                INSERT INTO doc_chunks (
                    document_id, run_id, persona_name, ordinal, content
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (doc_id, run_id, persona_name, ordinal, content),
            )
            if self._fts5_available:
                # External-content FTS5 is maintained explicitly rather than by
                # trigger: we only ever bulk-insert, and an explicit write keeps
                # the index update visibly inside this transaction.
                await self._conn.execute(
                    "INSERT INTO doc_chunks_fts(rowid, content) VALUES (?, ?)",
                    (cursor.lastrowid, content),
                )
        await self._conn.commit()
        return doc_id

    async def list_documents(
        self, run_id: str, persona_name: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """List a run's documents, newest first.

        ``persona_name`` filters to what that persona can see: its own documents
        plus cast-wide ones. Omitting it returns everything attached to the run.
        """
        if persona_name is None:
            sql = "SELECT * FROM documents WHERE run_id = ? ORDER BY created_at DESC, id"
            params: tuple = (run_id,)
        else:
            sql = (
                "SELECT * FROM documents WHERE run_id = ? "
                "AND (persona_name = ? OR persona_name IS NULL) "
                "ORDER BY created_at DESC, id"
            )
            params = (run_id, persona_name)
        async with self._conn.execute(sql, params) as cursor:
            return [dict(r) for r in await cursor.fetchall()]

    async def document_text(self, document_id: str) -> str:
        """A document's full text, rebuilt from its chunks.

        ``doc_chunks`` is the source of truth for document text, and it is the only
        place the text survives — the original upload is not retained. Chunks
        overlap, so this goes through ``join_chunks`` rather than concatenating.
        """
        from matrix_studio.documents import join_chunks

        async with self._conn.execute(
            "SELECT content FROM doc_chunks WHERE document_id = ? ORDER BY ordinal",
            (document_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return join_chunks([r[0] for r in rows])

    async def delete_document(self, document_id: str) -> bool:
        """Delete a document, its chunks and their index entries. Returns True if it existed."""
        async with self._conn.execute(
            "SELECT id, content FROM doc_chunks WHERE document_id = ?", (document_id,)
        ) as cursor:
            rows = [(row[0], row[1]) for row in await cursor.fetchall()]
        if self._fts5_available:
            # External-content FTS5 cannot delete by rowid alone — it needs the
            # original text to un-index the terms, so it is read back first (while
            # doc_chunks still holds it) and passed to the 'delete' command.
            for chunk_id, content in rows:
                await self._conn.execute(
                    "INSERT INTO doc_chunks_fts(doc_chunks_fts, rowid, content) "
                    "VALUES ('delete', ?, ?)",
                    (chunk_id, content),
                )
        await self._conn.execute(
            "DELETE FROM doc_chunks WHERE document_id = ?", (document_id,)
        )
        cursor = await self._conn.execute(
            "DELETE FROM documents WHERE id = ?", (document_id,)
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def reindex_documents(self) -> int:
        """Rebuild the FTS5 index from ``doc_chunks``, the source of truth.

        This is the documented recovery path for a stale or corrupt index. It is
        one statement because the index is external-content: it holds no text of
        its own, so it can always be regenerated from the chunks table.

        Returns:
            Number of chunks indexed.
        """
        if not self._fts5_available:
            return 0
        await self._conn.execute(
            "INSERT INTO doc_chunks_fts(doc_chunks_fts) VALUES('rebuild')"
        )
        await self._conn.commit()
        async with self._conn.execute("SELECT COUNT(*) FROM doc_chunks") as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row else 0

    def _scope_sql(self, persona_name: Optional[str], alias: str = "c") -> tuple:
        """The run-slice predicate: a persona sees its own documents plus cast-wide."""
        if persona_name is None:
            return "", ()
        return f"AND ({alias}.persona_name = ? OR {alias}.persona_name IS NULL)", (persona_name,)

    async def _search_documents_run_scoped(
        self, run_id: str, query: str, persona_name: Optional[str], k: int
    ) -> List[Dict[str, Any]]:
        """BM25 over ONLY this run's slice, by scoring in a scratch index.

        Why this exists: ``bm25()`` is computed by FTS5 over the whole index it is
        given, and the main index holds every run in the database. ``run_id`` was only
        an outer filter on already-scored rows, so a run's scores moved when unrelated
        runs were added. Measured on identical run/documents/query: -0.0000 with that
        run alone in the database, -1.8331 once an unrelated second run existed. A
        score that depends on a neighbour is not a measurement.

        Rather than reimplement BM25 (which would mean reimplementing the porter
        stemmer to agree with the index, or accepting a scoring/recall mismatch), the
        run's chunks are copied into a temp FTS5 index with the SAME tokenizer and
        scored there. Identical tokenisation, exact BM25, statistics that are the run's
        own. Runs hold well under a thousand chunks, so the copy is cheap relative to
        the model call the retrieval feeds.

        This does NOT change how duplication inside one run behaves: eight copies of a
        document really are eight documents in that run's corpus, and diluting their
        IDF is correct.
        """
        scope_sql, scope_params = self._scope_sql(persona_name)
        # One multi-statement sequence over a shared connection and a shared scratch
        # table: concurrent runs would otherwise interleave and score against each
        # other's slice — the very bug being fixed, in a harder-to-see form.
        async with self._scope_lock:
            await self._conn.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS temp.{_SCOPE_TABLE} "
                f"USING fts5(content, tokenize='{FTS_TOKENIZER}')"
            )
            await self._conn.execute(f"DELETE FROM temp.{_SCOPE_TABLE}")
            await self._conn.execute(
                f"INSERT INTO temp.{_SCOPE_TABLE}(rowid, content) "
                f"SELECT c.id, c.content FROM doc_chunks c "
                f"WHERE c.run_id = ? {scope_sql}",
                (run_id, *scope_params),
            )
            # Unqualified table name here: bm25() and MATCH both reject a
            # schema-qualified name ("no such column: temp.retrieval_scope"). SQLite
            # resolves temp first, so the bare name is the temp table.
            async with self._conn.execute(
                f"SELECT rowid AS chunk_id, bm25({_SCOPE_TABLE}) AS score "
                f"FROM {_SCOPE_TABLE} WHERE {_SCOPE_TABLE} MATCH ? "
                f"ORDER BY score ASC LIMIT ?",
                (query, k),
            ) as cursor:
                scored = [(int(r[0]), float(r[1])) for r in await cursor.fetchall()]
            # Writing to the scratch table opens an implicit transaction, and leaving
            # it open pins this connection's read snapshot — so a later read would
            # miss writes committed elsewhere in the meantime, and writers would queue
            # behind a lock held by a *search*. Committing is release, not durability:
            # the temp table is not in the database file.
            await self._conn.commit()

        if not scored:
            return []

        # Metadata for the winners, then reorder to the scored ranking: SQL IN gives no
        # ordering guarantee, and losing the ranking would silently return the k
        # matches in rowid order.
        ids = [cid for cid, _ in scored]
        placeholders = ",".join("?" * len(ids))
        async with self._conn.execute(
            f"""
            SELECT c.id AS chunk_id, c.document_id, c.ordinal, c.content,
                   d.title, d.source_path, d.media_type
            FROM doc_chunks c JOIN documents d ON d.id = c.document_id
            WHERE c.id IN ({placeholders})
            """,
            ids,
        ) as cursor:
            by_id = {int(r["chunk_id"]): dict(r) for r in await cursor.fetchall()}

        out: List[Dict[str, Any]] = []
        for chunk_id, score in scored:
            row = by_id.get(chunk_id)
            if row is None:
                continue
            row["score"] = score
            out.append(row)
        return out

    async def search_documents(
        self,
        run_id: str,
        query: str,
        persona_name: Optional[str] = None,
        k: int = 3,
        corpus: str = "run",
    ) -> List[Dict[str, Any]]:
        """BM25 search over a persona's document slice, best match first.

        ``corpus`` selects what BM25's statistics are drawn from:

        - ``"run"`` (default) — this run's slice only, so a score is a property of the
          run and is reproducible regardless of what else the database holds.
        - ``"database"`` — the whole index, which is what this did before and is kept
          only so the difference stays measurable and regression-locked.

        ``query`` MUST already be sanitised into FTS5 syntax by
        ``documents_retrieval.build_fts_query`` — raw conversation text contains
        operators and quotes that would raise a syntax error or silently change
        the query's meaning.

        Returns dicts with chunk_id, document_id, title, ordinal, content and
        score (BM25; more negative is a better match).
        """
        if not self._fts5_available or not query or k <= 0:
            return []
        if corpus not in ("run", "database"):
            raise ValueError(f"corpus must be 'run' or 'database', not {corpus!r}")
        if corpus == "run":
            try:
                return await self._search_documents_run_scoped(
                    run_id, query, persona_name, k
                )
            except Exception as exc:  # noqa: BLE001
                # Same contract as below: a retrieval failure degrades to "no
                # supporting passage found", which the prompt handles honestly, rather
                # than taking a run down.
                logger.warning(
                    "Run-scoped document search failed for run %s: %s", run_id, exc
                )
                return []
        scope_sql, scope_params = self._scope_sql(persona_name)
        sql = f"""
            SELECT c.id AS chunk_id, c.document_id, c.ordinal, c.content,
                   d.title, d.source_path, d.media_type,
                   bm25(doc_chunks_fts) AS score
            FROM doc_chunks_fts
            JOIN doc_chunks c ON c.id = doc_chunks_fts.rowid
            JOIN documents d ON d.id = c.document_id
            WHERE doc_chunks_fts MATCH ?
              AND c.run_id = ?
              {scope_sql}
            ORDER BY score ASC
            LIMIT ?
        """
        try:
            async with self._conn.execute(
                sql, (query, run_id, *scope_params, k)
            ) as cursor:
                return [dict(r) for r in await cursor.fetchall()]
        except Exception as exc:
            # A malformed MATCH must never take a run down; retrieval degrades to
            # "no supporting passage found", which the prompt handles honestly.
            logger.warning("Document search failed for run %s: %s", run_id, exc)
            return []

    async def count_documents(self, run_id: str) -> int:
        """Number of documents attached to a run."""
        async with self._conn.execute(
            "SELECT COUNT(*) FROM documents WHERE run_id = ?", (run_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row else 0

    async def copy_documents_to_run(self, from_run_id: str, to_run_id: str) -> int:
        """Copy a run's documents to another run (used when branching).

        A branch inherits its parent's attached background, otherwise personas
        would silently lose the material they had been reasoning from. New
        document ids are minted so the branch owns its own rows.

        Returns:
            Number of documents copied.
        """
        docs = await self.list_documents(from_run_id)
        for doc in docs:
            async with self._conn.execute(
                "SELECT content FROM doc_chunks WHERE document_id = ? ORDER BY ordinal",
                (doc["id"],),
            ) as cursor:
                chunks = [row[0] for row in await cursor.fetchall()]
            await self.add_document(
                run_id=to_run_id,
                title=doc["title"],
                chunks=chunks,
                persona_name=doc["persona_name"],
                source_path=doc["source_path"],
                media_type=doc["media_type"],
                char_count=doc["char_count"],
            )
        return len(docs)

    async def term_document_frequencies(
        self,
        run_id: str,
        terms: List[str],
        persona_name: Optional[str] = None,
    ) -> Dict[str, int]:
        """How many chunks in a persona's slice contain each term.

        Used to pick the DISCRIMINATIVE terms for a query. A term present in
        almost every chunk carries no ranking signal, and a term present in none
        cannot match at all; both dilute an OR query built from conversational
        text. Returned counts are FTS5-tokenised (so stemming applies), which is
        the right basis because it is how matching will actually happen.

        One small MATCH per term. Terms are checked individually rather than via
        an fts5vocab table because vocab rows hold stemmed forms, which would not
        line up with the raw query terms callers pass in.
        """
        if not self._fts5_available or not terms:
            return {}
        scope_sql, scope_params = self._scope_sql(persona_name)
        sql = f"""
            SELECT COUNT(*) FROM doc_chunks_fts
            JOIN doc_chunks c ON c.id = doc_chunks_fts.rowid
            WHERE doc_chunks_fts MATCH ? AND c.run_id = ? {scope_sql}
        """
        out: Dict[str, int] = {}
        for term in terms:
            if '"' in term:
                continue
            try:
                async with self._conn.execute(
                    sql, (f'"{term}"', run_id, *scope_params)
                ) as cursor:
                    row = await cursor.fetchone()
                    out[term] = int(row[0]) if row else 0
            except Exception as exc:  # noqa: BLE001
                # A term that cannot be probed is simply unknown; never fatal.
                logger.debug("term frequency probe failed for %r: %s", term, exc)
                out[term] = 0
        return out

    async def chunk_count(self, run_id: str, persona_name: Optional[str] = None) -> int:
        """Number of indexed chunks in a persona's slice (for df ratios)."""
        if persona_name is None:
            sql = "SELECT COUNT(*) FROM doc_chunks WHERE run_id = ?"
            params: tuple = (run_id,)
        else:
            sql = (
                "SELECT COUNT(*) FROM doc_chunks WHERE run_id = ? "
                "AND (persona_name = ? OR persona_name IS NULL)"
            )
            params = (run_id, persona_name)
        async with self._conn.execute(sql, params) as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row else 0

    # ---------------------------------------------------------------- #
    # Phase 5f: vector storage and KNN search.
    # ---------------------------------------------------------------- #

    async def embedding_model(self) -> Optional[str]:
        """The model the stored vectors were produced with, if any."""
        async with self._conn.execute(
            "SELECT model FROM embedding_meta WHERE id = 1"
        ) as cursor:
            row = await cursor.fetchone()
            return str(row[0]) if row else None

    async def store_chunk_vectors(
        self,
        run_id: str,
        vectors: List[tuple],
        model: str,
    ) -> int:
        """Store embeddings for chunks. ``vectors`` is ``[(chunk_id, [floats]), ...]``.

        The vec0 table is created on first use with the dimension of the incoming
        vectors, and that dimension is recorded in ``embedding_meta``. A later call
        carrying a different dimension is REFUSED rather than written, because vec0
        is fixed-width and mixing dimensions silently produces meaningless
        distances. Switching embedding model therefore requires a re-embed, which
        the error message says.

        Returns the number of vectors stored.
        """
        if not self._vec_available or not vectors:
            return 0
        from matrix_studio.embeddings import serialise

        dim = len(vectors[0][1])
        if dim <= 0:
            return 0

        existing_model = await self.embedding_model()
        if self._vec_dim is not None and self._vec_dim != dim:
            raise ValueError(
                f"Stored embeddings are {self._vec_dim}-dimensional (model "
                f"{existing_model!r}) but {model!r} produced {dim}. Vector tables "
                "are fixed-width; delete the documents and re-attach them to "
                "switch embedding model."
            )
        await self._ensure_vec_table(dim)

        now = int(time.time())
        if existing_model is None:
            await self._conn.execute(
                "INSERT OR REPLACE INTO embedding_meta (id, model, dim, created_at) "
                "VALUES (1, ?, ?, ?)",
                (model, dim, now),
            )

        stored = 0
        for chunk_id, vector in vectors:
            if not vector or len(vector) != dim:
                continue
            async with self._conn.execute(
                "SELECT run_id, persona_name FROM doc_chunks WHERE id = ?",
                (chunk_id,),
            ) as cursor:
                row = await cursor.fetchone()
            if not row:
                continue
            # Replace rather than duplicate, so re-embedding a chunk is safe.
            await self._conn.execute("DELETE FROM chunk_vec WHERE rowid = ?", (chunk_id,))
            await self._conn.execute(
                "INSERT INTO chunk_vec(rowid, embedding) VALUES (?, ?)",
                (chunk_id, serialise(vector)),
            )
            await self._conn.execute(
                "INSERT OR REPLACE INTO chunk_vectors "
                "(chunk_id, run_id, persona_name, model, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (chunk_id, row[0], row[1], model, now),
            )
            stored += 1
        await self._conn.commit()
        return stored

    async def vector_search(
        self,
        run_id: str,
        vector: List[float],
        persona_name: Optional[str] = None,
        k: int = 3,
    ) -> List[Dict[str, Any]]:
        """KNN search over a persona's embedded chunks, nearest first.

        Returns the same row shape as ``search_documents`` so the two can be fused
        without the caller special-casing them, with ``score`` carrying the vector
        distance (LOWER is nearer — the opposite polarity of BM25, which is why
        fusion is done by RANK rather than by raw score).

        Scoping is applied AFTER the KNN scan. vec0's MATCH does not accept
        arbitrary joins in the same predicate, so the scan is over-fetched and then
        filtered; correctness is preserved (a persona still cannot see another's
        material) at the cost of some wasted candidates on multi-persona runs.
        """
        if not self._vec_available or not vector or k <= 0:
            return []
        if self._vec_dim is not None and len(vector) != self._vec_dim:
            logger.warning(
                "Query vector is %d-dimensional but the index is %d; "
                "skipping vector search.", len(vector), self._vec_dim,
            )
            return []
        from matrix_studio.embeddings import serialise

        # Over-fetch so post-filtering by persona/run still yields k results.
        scan_k = max(k * 8, 32)
        try:
            async with self._conn.execute(
                """
                SELECT rowid, distance FROM chunk_vec
                WHERE embedding MATCH ? ORDER BY distance LIMIT ?
                """,
                (serialise(vector), scan_k),
            ) as cursor:
                hits = [(int(r[0]), float(r[1])) for r in await cursor.fetchall()]
        except Exception as exc:  # noqa: BLE001
            logger.warning("Vector search failed for run %s: %s", run_id, exc)
            return []
        if not hits:
            return []

        by_id = {cid: dist for cid, dist in hits}
        placeholders = ",".join("?" for _ in by_id)
        if persona_name is None:
            scope_sql = ""
            scope_params: tuple = ()
        else:
            scope_sql = "AND (c.persona_name = ? OR c.persona_name IS NULL)"
            scope_params = (persona_name,)
        sql = f"""
            SELECT c.id AS chunk_id, c.document_id, c.ordinal, c.content,
                   d.title, d.source_path, d.media_type
            FROM doc_chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE c.id IN ({placeholders}) AND c.run_id = ? {scope_sql}
        """
        async with self._conn.execute(
            sql, (*by_id.keys(), run_id, *scope_params)
        ) as cursor:
            rows = [dict(r) for r in await cursor.fetchall()]
        for row in rows:
            row["score"] = by_id[int(row["chunk_id"])]
        rows.sort(key=lambda r: r["score"])
        return rows[:k]

    async def chunks_missing_vectors(
        self, run_id: str, limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Chunks in a run that have no stored embedding yet.

        Used by the reindex path so embedding a corpus is resumable: a run
        interrupted part-way through does not have to start over.
        """
        sql = """
            SELECT c.id AS chunk_id, c.content
            FROM doc_chunks c
            LEFT JOIN chunk_vectors v ON v.chunk_id = c.id
            WHERE c.run_id = ? AND v.chunk_id IS NULL
            ORDER BY c.id
        """
        params: tuple = (run_id,)
        if limit is not None:
            sql += " LIMIT ?"
            params = (run_id, limit)
        async with self._conn.execute(sql, params) as cursor:
            return [dict(r) for r in await cursor.fetchall()]

    async def count_chunk_vectors(self, run_id: str) -> int:
        """Number of chunks in a run that have a stored embedding."""
        async with self._conn.execute(
            "SELECT COUNT(*) FROM chunk_vectors WHERE run_id = ?", (run_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row else 0

# SPDX-License-Identifier: Apache-2.0
"""
Event-sourced SQLite storage layer.

Schema:
- runs: one row per simulation run
- events: append-only event log (source of truth)
- snapshots: full state snapshots for fast restoration
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite

from matrix_studio.state import SimSnapshot

logger = logging.getLogger(__name__)


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

    async def connect(self):
        """Connect to database and ensure schema exists."""
        # Ensure directory exists
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        self._conn = await aiosqlite.connect(self.db_path)

        # Enable row factory for dict-like access
        self._conn.row_factory = aiosqlite.Row

        # Enable WAL mode for concurrent reads
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")

        # Create schema
        await self._create_schema()

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
                completed_at INTEGER
            )
        """)

        # Phase 1 additive migration: existing Phase 0 databases have a `runs`
        # table without the `description`/`slug` columns. Add them if missing so
        # older rows/queries keep working (nullable, backward-compatible).
        async with self._conn.execute("PRAGMA table_info(runs)") as cursor:
            existing_cols = {row[1] for row in await cursor.fetchall()}
        for col in ("name", "description", "slug"):
            if col not in existing_cols:
                await self._conn.execute(f"ALTER TABLE runs ADD COLUMN {col} TEXT")

        # Enforce name uniqueness for the runs that have one (nullable names are
        # exempt so legacy rows and unnamed runs never collide).
        await self._conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS runs_name_unique
            ON runs(name) WHERE name IS NOT NULL
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

        await self._conn.commit()

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
    ) -> None:
        """
        Create a new simulation run.

        Args:
            run_id: Unique run identifier
            topic: Simulation topic
            cast: List of persona definitions
            name: Optional memorable run name (unique when present)
            description: Optional one-line human description
            slug: Optional normalized slug (defaults to name)
            config: Optional configuration dict
            parent_run_id: Parent run ID if this is a branch
            branch_turn: Turn number branched from
        """
        await self._conn.execute(
            """
            INSERT INTO runs (id, name, description, slug, topic, cast_json,
                              config_json, status, parent_run_id, branch_turn,
                              created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
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
            ),
        )
        await self._conn.commit()

    async def name_exists(self, name: str) -> bool:
        """Return True if a run with this memorable name already exists."""
        async with self._conn.execute(
            "SELECT 1 FROM runs WHERE name = ? LIMIT 1", (name,)
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
        Get run metadata.

        Args:
            run_id: Run identifier

        Returns:
            Run dict or None if not found
        """
        async with self._conn.execute(
            "SELECT * FROM runs WHERE id = ?", (run_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return dict(row)
            return None

    async def get_run_by_ref(self, ref: str) -> Optional[Dict[str, Any]]:
        """
        Resolve a run by either its UUID id or its memorable name.

        Args:
            ref: A run_id (UUID) or a memorable name.

        Returns:
            Run dict or None if not found.
        """
        async with self._conn.execute(
            "SELECT * FROM runs WHERE id = ? OR name = ? LIMIT 1", (ref, ref)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def list_runs(
        self, q: Optional[str] = None, limit: int = 200
    ) -> List[Dict[str, Any]]:
        """
        List runs (newest first), optionally filtered by a case-insensitive
        substring matching name, description, or topic.

        Each returned dict includes derived aggregates (turn_count,
        total_cost_usd) computed from the event log so the history list needs
        no extra round-trips.
        """
        if q:
            like = f"%{q.lower()}%"
            query = """
                SELECT * FROM runs
                WHERE lower(COALESCE(name, '')) LIKE ?
                   OR lower(COALESCE(description, '')) LIKE ?
                   OR lower(topic) LIKE ?
                ORDER BY created_at DESC
                LIMIT ?
            """
            params = (like, like, like, limit)
        else:
            query = "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?"
            params = (limit,)

        async with self._conn.execute(query, params) as cursor:
            rows = [dict(r) for r in await cursor.fetchall()]

        for run in rows:
            stats = await self.get_run_stats(run["id"])
            run.update(stats)
        return rows

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

        return {
            "turn_count": turn_row[0] if turn_row else 0,
            "total_cost_usd": total_cost,
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

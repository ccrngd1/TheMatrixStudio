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

        await self._conn.commit()

    async def create_run(
        self,
        run_id: str,
        topic: str,
        cast: List[Dict[str, Any]],
        name: Optional[str] = None,
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
            name: Optional run name
            config: Optional configuration dict
            parent_run_id: Parent run ID if this is a branch
            branch_turn: Turn number branched from
        """
        await self._conn.execute(
            """
            INSERT INTO runs (id, name, topic, cast_json, config_json, status,
                              parent_run_id, branch_turn, created_at)
            VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)
            """,
            (
                run_id,
                name,
                topic,
                json.dumps(cast),
                json.dumps(config) if config else None,
                parent_run_id,
                branch_turn,
                int(time.time()),
            ),
        )
        await self._conn.commit()

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

# SPDX-License-Identifier: Apache-2.0
"""
Run manager — orchestrates background simulations and fans events out to live
WebSocket subscribers.

The engine always runs to completion at full speed in a background asyncio task
(Phase 0 behavior, unchanged). The manager wires the engine's additive
``on_event`` callback to an in-memory pub/sub broker so any number of connected
clients receive events live. Persistence still happens in the engine via the
``Database``; the broker is purely for live delivery. A late-joining client
replays persisted events from the DB, then subscribes for the tail — so the
buffered stream a client sees is identical whether the run is live or finished.
"""

import asyncio
import logging
import uuid
from typing import Any, Dict, List, Optional, Set

from matrix_studio.engine import run_simulation
from matrix_studio.naming import generate_run_name
from matrix_studio.storage import Database

logger = logging.getLogger(__name__)

# Terminal event types that tell a subscriber the stream is finished.
TERMINAL_EVENTS = {"sim.completed", "sim.failed"}


class RunBroker:
    """In-memory fan-out of a single run's live events to N subscribers."""

    def __init__(self) -> None:
        self._subscribers: Set["asyncio.Queue[Optional[Dict[str, Any]]]"] = set()
        self.finished = False

    def subscribe(self) -> "asyncio.Queue[Optional[Dict[str, Any]]]":
        q: "asyncio.Queue[Optional[Dict[str, Any]]]" = asyncio.Queue()
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: "asyncio.Queue[Optional[Dict[str, Any]]]") -> None:
        self._subscribers.discard(q)

    async def publish(self, event: Dict[str, Any]) -> None:
        for q in list(self._subscribers):
            await q.put(event)

    async def close(self) -> None:
        """Signal end-of-stream to all current subscribers (sentinel None)."""
        self.finished = True
        for q in list(self._subscribers):
            await q.put(None)


class RunManager:
    """
    Owns the DB connection, the set of live brokers, and the background tasks.

    One RunManager instance lives for the lifetime of the FastAPI app.
    """

    def __init__(self, db: Database) -> None:
        self.db = db
        self._brokers: Dict[str, RunBroker] = {}
        self._tasks: Dict[str, asyncio.Task] = {}

    def get_broker(self, run_id: str) -> Optional[RunBroker]:
        return self._brokers.get(run_id)

    async def create_run(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        Resolve the run's name/description, then start the simulation as a
        background task. Returns immediately with run metadata — NEVER blocks on
        completion.
        """
        topic = request["topic"]
        cast = request.get("cast", [])
        cast_names = [c.get("name", "") for c in cast]
        model = request.get("model")

        run_id = str(uuid.uuid4())

        # Resolve a memorable name. Honour a user-supplied name; otherwise
        # generate one. Naming never blocks a run — generate_run_name falls back
        # internally on any LLM failure.
        supplied_name = (request.get("name") or "").strip().lower() or None
        description = request.get("description")
        slug = None
        name_source = "user" if supplied_name else None

        if supplied_name and await self.db.name_exists(supplied_name):
            # Disambiguate a user-supplied duplicate rather than rejecting.
            base = supplied_name
            for suffix in range(2, 100):
                candidate = f"{base}-{suffix}"
                if not await self.db.name_exists(candidate):
                    supplied_name = candidate
                    break

        if supplied_name:
            name = supplied_name
            if not description:
                description = topic[:80]
            slug = supplied_name
        else:
            naming = await generate_run_name(
                topic=topic,
                cast_names=cast_names,
                model=model,
                name_exists=self.db.name_exists,
            )
            name = naming["name"]
            description = description or naming["description"]
            slug = naming["slug"]
            name_source = naming["source"]

        # Build the engine request (name/description are additive fields).
        engine_request = dict(request)
        engine_request["name"] = name
        engine_request["description"] = description
        if model:
            # Engine reads model from settings; per-run model override is not a
            # Phase 0 feature, so we only record it for now (kept additive).
            engine_request["model"] = model

        broker = RunBroker()
        self._brokers[run_id] = broker

        async def _on_event(event: Dict[str, Any]) -> None:
            await broker.publish(event)

        async def _runner() -> None:
            try:
                await run_simulation(
                    engine_request,
                    db=self.db,
                    run_id=run_id,
                    on_event=_on_event,
                )
            except Exception:  # noqa: BLE001
                logger.exception("Background run %s crashed", run_id)
            finally:
                await broker.close()

        task = asyncio.create_task(_runner())
        self._tasks[run_id] = task
        task.add_done_callback(lambda _t: self._tasks.pop(run_id, None))

        return {
            "run_id": run_id,
            "name": name,
            "description": description,
            "slug": slug,
            "name_source": name_source,
            "topic": topic,
            "status": "running",
        }

    async def shutdown(self) -> None:
        """Cancel any in-flight background runs (used on app shutdown)."""
        for task in list(self._tasks.values()):
            task.cancel()
        for task in list(self._tasks.values()):
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


def event_row_to_wire(row: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalize a persisted event row (payload stored as JSON text) into the same
    wire shape the live callback emits (payload as a dict).
    """
    import json

    payload = row.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = {}
    return {
        "run_id": row["run_id"],
        "turn": row["turn"],
        "seq": row["seq"],
        "event_type": row["event_type"],
        "agent_name": row.get("agent_name"),
        "payload": payload or {},
    }

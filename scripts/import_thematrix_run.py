# SPDX-License-Identifier: Apache-2.0
"""Import a legacy TheMatrix result JSON into the matrix-sim-studio SQLite store.

Maps the old {conversation:[{speaker,message}], metadata, summary} shape into a
completed run (runs row + full event log + completion snapshot) so it appears
and replays in the Phase 1 UI. Costs are unknown for imported runs and are set
to 0.0 (never fabricated); the description notes the import.

Usage:
    python scripts/import_thematrix_run.py <request.json> <result.json>
"""
import asyncio
import json
import sys
import time
import uuid

from matrix_studio.storage import Database
from matrix_studio.state import AgentState, SimSnapshot
from matrix_studio.naming import generate_run_name
from matrix_studio.settings import get_settings


async def main(request_path: str, result_path: str) -> None:
    req = json.load(open(request_path))
    res = json.load(open(result_path))

    meta = res.get("metadata", {})
    topic = meta.get("topic") or req.get("topic") or "Imported conversation"
    conversation_raw = res.get("conversation", [])

    # Persona map: name -> system_message (persona text). Request uses
    # {name, description, system_message}; fall back to metadata name list.
    personas = req.get("personas") or req.get("cast") or []
    persona_by_name = {}
    for p in personas:
        persona_by_name[p["name"]] = p.get("system_message") or p.get("persona") or p.get("description") or ""
    for n in meta.get("personas", []):
        persona_by_name.setdefault(n, "")

    # Normalize conversation to studio shape {speaker, content}.
    conversation = [
        {"speaker": m.get("speaker"), "content": m.get("message") or m.get("content") or ""}
        for m in conversation_raw
    ]
    speakers = [m["speaker"] for m in conversation]

    db = Database(get_settings().data_dir + "/matrix_studio.db")
    await db.connect()

    naming = await generate_run_name(
        topic=topic,
        cast_names=list(persona_by_name.keys()),
        name_exists=db.name_exists,
    )
    name = naming["name"]
    description = (naming.get("description") or "").strip()
    description = f"[Imported from TheMatrix] {description}".strip()
    slug = naming.get("slug") or name

    run_id = str(uuid.uuid4())
    cast = [{"name": n, "persona": persona_by_name.get(n, ""), "goals": []} for n in persona_by_name]

    await db.create_run(
        run_id=run_id, topic=topic, cast=cast,
        name=name, description=description, slug=slug,
        config={"imported": True, "source": "thematrix", "model": meta.get("model")},
    )
    await db.update_run_status(run_id, "running")

    # Event log: sim.started, then per message speaker.selected + agent.response.
    # The engine assigns a GLOBALLY MONOTONIC seq across all events (the frontend
    # keys/dedupes by it), so we must do the same — not per-turn seq.
    seq_counter = 0

    def nxt() -> int:
        nonlocal seq_counter
        s = seq_counter
        seq_counter += 1
        return s

    await db.append_event(run_id, turn=0, seq=nxt(), event_type="sim.started",
                          payload={"topic": topic, "cast": list(persona_by_name.keys()),
                                   "config": {"imported": True}})
    for i, msg in enumerate(conversation, start=1):
        sp = msg["speaker"]
        await db.append_event(run_id, turn=i, seq=nxt(), event_type="speaker.selected",
                              payload={"turn": i}, agent_name=sp)
        await db.append_event(run_id, turn=i, seq=nxt(), event_type="agent.response",
                              payload={"content": msg["content"], "tokens_in": 0,
                                       "tokens_out": 0, "cost_usd": 0.0}, agent_name=sp)
    total_turns = len(conversation)
    await db.append_event(run_id, turn=total_turns, seq=nxt(), event_type="sim.completed",
                          payload={"total_turns": total_turns, "total_cost_usd": 0.0,
                                   "imported": True})

    # Completion snapshot: agents (for the dossier) + full conversation.
    now = int(time.time())
    agents = {}
    for n, persona in persona_by_name.items():
        agents[n] = AgentState(name=n, persona=persona, goals=[],
                               conversation_history=[], total_tokens_in=0,
                               total_tokens_out=0, total_cost_usd=0.0)
    snapshot = SimSnapshot(run_id=run_id, turn=total_turns, topic=topic, agents=agents,
                           conversation=conversation, status="complete",
                           created_at=now, completed_at=now, total_turns=total_turns)
    await db.save_snapshot(snapshot)

    # Phase 1.5: if the legacy result carried a source summary, preserve it as an
    # 'imported' summary — surfaced separately from any freshly generated one and
    # NEVER overwritten. Costs are unknown for imports, recorded as 0.0 (never
    # fabricated).
    source_summary = res.get("summary")
    if source_summary:
        payload = (
            {"overview": source_summary}
            if isinstance(source_summary, str)
            else source_summary
        )
        await db.save_summary(run_id, payload=payload, kind="imported",
                              tokens_in=0, tokens_out=0, cost_usd=0.0)

    await db.update_run_status(run_id, "complete", now)
    await db.close()

    print(f"Imported run: name={name!r} run_id={run_id} turns={total_turns} "
          f"speakers={len(persona_by_name)} name_source={naming.get('source')}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2]))

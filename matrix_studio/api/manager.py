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
import os
import uuid
from functools import partial
from typing import Any, Dict, List, Optional, Sequence, Set

from matrix_studio import branching, orchestration
from matrix_studio.engine import run_simulation
from matrix_studio.naming import generate_run_name
from matrix_studio.service import maybe_autogenerate_summary
from matrix_studio.tenancy import LOCAL_USER_SUB
from matrix_studio.storage import Database

logger = logging.getLogger(__name__)

# Terminal event types that tell a subscriber the stream is finished.
TERMINAL_EVENTS = {
    "sim.completed", "sim.failed", "sim.interrupted", "sim.capped", "sim.stopped",
}

#: Default seconds between starting one ensemble member and the next. See
#: `create_ensemble` for what this does and does not buy.
ENSEMBLE_STAGGER_SECONDS = 1.0


def _ensemble_stagger() -> float:
    """The stagger to use for this fan-out, read at call time.

    Read per call rather than bound to a module constant at import. A constant looked
    tidier and was wrong twice over: `_MSS_TEST_MODE` is set by a pytest fixture that runs
    long after this module is imported, so the test-mode branch never fired and the suite
    silently paid a second per member (a flat 5.05 s per fan-out, which is how it was
    found); and on Lambda it froze the value into the sandbox, so changing the environment
    variable would have needed a redeploy to take effect.
    """
    if os.environ.get("_MSS_TEST_MODE"):
        return 0.0
    raw = os.environ.get("ENSEMBLE_STAGGER_SECONDS")
    if raw is None:
        return ENSEMBLE_STAGGER_SECONDS
    try:
        return max(0.0, float(raw))
    except ValueError:
        logger.warning(
            "ENSEMBLE_STAGGER_SECONDS=%r is not a number; using %.1fs",
            raw, ENSEMBLE_STAGGER_SECONDS,
        )
        return ENSEMBLE_STAGGER_SECONDS


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
        # Run ids an operator has asked to stop. The engine polls this between
        # turns, so it is a request rather than a cancellation: the turn in flight
        # finishes and is persisted. In-memory on purpose — a stop only has to
        # outlive the request, and a process restart already ends the run (the
        # startup sweep marks orphaned runs interrupted).
        self._stop_requested: Set[str] = set()

    def get_broker(self, run_id: str) -> Optional[RunBroker]:
        return self._brokers.get(run_id)

    async def create_run(
        self,
        request: Dict[str, Any],
        *,
        owner_sub: str,
        groups: Optional[Sequence[str]] = None,
        run_id: Optional[str] = None,
        ensemble_id: Optional[str] = None,
        ensemble_cell: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Resolve the run's name/description, then start the simulation as a
        background task. Returns immediately with run metadata — NEVER blocks on
        completion.

        ``owner_sub`` is required and keyword-only: this is the one place a run's
        owner is established, and defaulting it would attribute somebody's
        conversation to an identity they do not have.

        ``run_id`` lets a caller supply the id instead of having one generated. Only the
        ensemble fan-out does: it writes the parent row listing every member id *before*
        creating any member, so an interrupted fan-out leaves a parent that knows what is
        missing rather than orphan runs with nothing to aggregate them.

        ``ensemble_id`` / ``ensemble_cell`` mark this run as a member of a cell. Carried
        onto the row rather than inferred later — in the default replicates-only ensemble
        every member's config is identical by design, so the label is unrecoverable from
        the config alone.
        """
        topic = request["topic"]
        cast = request.get("cast", [])
        cast_names = [c.get("name", "") for c in cast]
        model = request.get("model")

        run_id = run_id or str(uuid.uuid4())

        # Bound up front, because the name checks below are the first storage calls and
        # `owner_sub=` alone does NOT attach the tenant-scoped credentials — it scopes
        # the query only. That distinction cost two deploys: the reads used the
        # function's own role, which by design holds no storage rights, so every
        # create returned 500 with an AccessDenied that named permissions rather than
        # the mixed idiom.
        owned = self.db.for_owner(owner_sub)

        # Resolve a memorable name. Honour a user-supplied name; otherwise
        # generate one. Naming never blocks a run — generate_run_name falls back
        # internally on any LLM failure.
        supplied_name = (request.get("name") or "").strip().lower() or None
        description = request.get("description")
        slug = None
        name_source = "user" if supplied_name else None

        if supplied_name and await owned.name_exists(supplied_name):
            # Disambiguate a user-supplied duplicate rather than rejecting.
            base = supplied_name
            for suffix in range(2, 100):
                candidate = f"{base}-{suffix}"
                if not await owned.name_exists(candidate):
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
                # Uniqueness is per user; the bound store carries the owner.
                name_exists=owned.name_exists,
            )
            name = naming["name"]
            description = description or naming["description"]
            slug = naming["slug"]
            name_source = naming["source"]

        # Build the engine request (name/description are additive fields).
        engine_request = dict(request)
        engine_request["name"] = name
        engine_request["description"] = description
        engine_request["owner_sub"] = owner_sub

        # `owned` (bound above) is what the engine, the summariser and everything
        # downstream receive. Binding once removed the need for `owner_sub` at 15 call
        # sites in the engine and 13 in `branching.py`, none of which has any business
        # knowing about tenants: the engine's job is to run a conversation, and WHOSE
        # conversation was decided at the request boundary.


        # Phase 1.5: fold an optional top-level `summary` config into the run's
        # stored config so it persists in config_json and drives auto-summary at
        # completion. Omitted → default (enabled + full field set) applies later.
        summary_cfg = request.get("summary")
        if summary_cfg is not None:
            cfg = dict(engine_request.get("config") or {})
            cfg["summary"] = summary_cfg
            engine_request["config"] = cfg
        # Record the per-run model in config so analysis calls default to it.
        if model:
            cfg = dict(engine_request.get("config") or {})
            cfg.setdefault("model", model)
            engine_request["config"] = cfg
        if model:
            # Engine reads model from settings; per-run model override is not a
            # Phase 0 feature, so we only record it for now (kept additive).
            engine_request["model"] = model

        # Phase 5: when there is an orchestrator, the run row is written HERE and the
        # state machine generates the turns.
        #
        # Writing it synchronously is the whole point. On the Phase 1 deployment this
        # route returned 201 with a real generated codename and then the run vanished:
        # `run_simulation` wrote the row from a background task, and Lambda freezes the
        # sandbox when the handler returns, so the row was never written. The client had
        # a 201 for a run that did not exist — nothing to poll, and not even a broken
        # run to resume.
        if orchestration.turn_loop_arn():
            await owned.create_run(
                run_id=run_id,
                topic=topic,
                cast=cast,
                name=name,
                description=description,
                config=engine_request.get("config") or {},
                owner_sub=owner_sub,
                groups=groups,
                ensemble_id=ensemble_id,
                ensemble_cell=ensemble_cell,
            )
            # `pending`, not `running`: the first slice owns that flip, so a run left
            # at `pending` is visibly one whose execution never started rather than one
            # that appears to be working.
            execution = await orchestration.start_execution(
                run_id, owner_sub,
                max_messages=int(
                    (engine_request.get("config") or {}).get("max_messages")
                    or request.get("max_messages")
                    or 0
                ),
            )
            if execution is None:
                logger.warning(
                    "Run %s was created but no execution started; it will sit at "
                    "'pending' until one is.", run_id,
                )
            return {
                "run_id": run_id,
                "name": name,
                "description": description,
                "slug": slug,
                "name_source": name_source,
                "topic": topic,
                "status": "pending",
            }

        # Local path, unchanged: one long-lived uvicorn process, where a background
        # asyncio task really does run to completion and the broker really can fan
        # events to a WebSocket.
        broker = RunBroker()
        self._brokers[run_id] = broker

        async def _on_event(event: Dict[str, Any]) -> None:
            await broker.publish(event)

        async def _runner() -> None:
            try:
                result = await run_simulation(
                    engine_request,
                    db=owned,
                    run_id=run_id,
                    on_event=_on_event,
                    should_stop=lambda: run_id in self._stop_requested,
                )
                # Phase 1.5: after a run completes, auto-generate the structured
                # summary (unless disabled in the run's summary config). This is
                # read-only — it writes only to the additive `summaries` table,
                # never a canonical event/snapshot/run-cost — so it happens after
                # the broker has already streamed the terminal event and never
                # affects the canonical run. Best-effort: it never raises.
                if result.get("status") == "complete":
                    await maybe_autogenerate_summary(owned, run_id)
                # The local counterpart of `orchestration._report_ensemble_once`. Attempted
                # for ANY terminal status, not just `complete`: a failed member will never
                # finish, so waiting for it would leave an ensemble with one bad run
                # permanently unreportable. The durable claim is what makes it safe for
                # several members to finish at once and all reach here.
                if ensemble_id:
                    from matrix_studio import ensemble_reporting

                    row = await owned.get_run(run_id)
                    if row:
                        await ensemble_reporting.maybe_report_for_member(owned, row)
            except Exception:  # noqa: BLE001
                logger.exception("Background run %s crashed", run_id)
            finally:
                await broker.close()

        task = asyncio.create_task(_runner())
        self._tasks[run_id] = task
        task.add_done_callback(lambda _t: self._finish(run_id))

        return {
            "run_id": run_id,
            "name": name,
            "description": description,
            "slug": slug,
            "name_source": name_source,
            "topic": topic,
            "status": "running",
        }

    async def create_ensemble(
        self,
        request: Dict[str, Any],
        cells: Sequence[Any],
        *,
        owner_sub: str,
        groups: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        """Fan one brief out into N member runs and return immediately.

        `cells` is a sequence of `ensemble_spec.Cell`. The plan is validated and proven
        isolated by `ensemble_spec.plan` BEFORE the parent row is written, so an ensemble
        whose cells differ in some way nobody declared never reaches storage — see
        docs/ENSEMBLE-CONVERSATIONS.md §3.3 for the sweep that produced that rule.

        Ordering, and why it is this way round:

          1. name the parent (one naming call for the whole ensemble, not one per member)
          2. write the parent row, already listing every member id
          3. create each member and start its execution

        Step 2 before step 3 means a fan-out that dies halfway leaves a parent that knows
        it is short. The reverse leaves N orphan runs indistinguishable from conversations
        somebody started by hand, and the ensemble itself simply gone.

        A member that fails to create is recorded and the rest continue. Refusing the whole
        ensemble because the seventh run failed would throw away six paid-for
        conversations, and a short cell is reportable — `list_ensemble_members` returns
        `run: None` for a missing member precisely so the aggregator can say "4 of 5"
        rather than silently reporting 4 of 4.
        """
        from matrix_studio import ensemble_spec

        members = ensemble_spec.plan(request.get("config") or {}, cells)

        topic = request["topic"]
        owned = self.db.for_owner(owner_sub)
        ensemble_id = str(uuid.uuid4())

        # One naming call for the ensemble; members get derived names, which take
        # `create_run`'s supplied-name path and make no LLM call of their own. Eight
        # generated codenames would cost eight calls to produce names that deliberately
        # hide the one thing a member's name should say — which cell it is in.
        supplied = (request.get("name") or "").strip().lower() or None
        if supplied:
            base_name, description = supplied, request.get("description") or topic[:80]
        else:
            naming = await generate_run_name(
                topic=topic,
                cast_names=[c.get("name", "") for c in request.get("cast", [])],
                model=request.get("model"),
                # Deliberately NOT `owned.name_exists`: that checks the run-name space, and
                # the ensemble's own name does not live there — only its members' derived
                # names do. Passing it would reject a perfectly free ensemble name because
                # some old run happened to hold it.
                name_exists=None,
            )
            base_name = naming["name"]
            description = request.get("description") or naming["description"]

        member_rows = [
            {"run_id": str(uuid.uuid4()), "cell": m.cell, "index": m.index}
            for m in members
        ]
        await owned.create_ensemble(
            ensemble_id=ensemble_id,
            topic=topic,
            spec=ensemble_spec.describe(cells),
            members=member_rows,
            base_config=request.get("config") or {},
            name=base_name,
            description=description,
            owner_sub=owner_sub,
        )

        started: List[Dict[str, Any]] = []
        failed: List[Dict[str, Any]] = []
        for member, row in zip(members, member_rows):
            member_request = dict(request)
            member_request["config"] = member.config
            member_request["name"] = member.name_for(base_name)
            member_request["description"] = description
            member_request["ensemble_id"] = ensemble_id
            member_request["ensemble_cell"] = member.cell
            try:
                created = await self.create_run(
                    member_request,
                    owner_sub=owner_sub,
                    groups=groups,
                    run_id=row["run_id"],
                    ensemble_id=ensemble_id,
                    ensemble_cell=member.cell,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "Ensemble %s member %s/%s failed to start", ensemble_id,
                    member.cell, member.index,
                )
                failed.append({**row, "error": str(exc)})
                continue
            started.append({**row, "name": created.get("name")})

            # Spread the FIRST turn, which is the one moment every member calls the model
            # at once. Later turns drift apart on their own as replies come back at
            # different lengths, so this is not general throttle protection and is not
            # pretending to be — §8.1 records that Bedrock TPM headroom for a full fan-out
            # is still unverified. It costs a second of request latency and removes one
            # real, predictable spike.
            stagger = _ensemble_stagger()
            if stagger:
                await asyncio.sleep(stagger)

        if not started:
            await owned.update_ensemble(ensemble_id, status="failed", owner_sub=owner_sub)
        else:
            await owned.update_ensemble(ensemble_id, status="running", owner_sub=owner_sub)

        return {
            "ensemble_id": ensemble_id,
            "name": base_name,
            "description": description,
            "topic": topic,
            "status": "failed" if not started else "running",
            "spec": ensemble_spec.describe(cells),
            "members": started,
            "failed": failed,
        }

    async def create_branch(
        self,
        parent_run: Dict[str, Any],
        from_turn: int,
        name: Optional[str] = None,
        description: Optional[str] = None,
        model: Optional[str] = None,
        mutation: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Fork ``parent_run`` at ``from_turn`` into a NEW run that resumes
        generating forward. Creates the branch run row synchronously (so it is
        immediately resolvable + watchable), then runs the copy+resume half as a
        background task. Returns immediately with the branch's metadata — NEVER
        blocks on generation.

        ``model`` optionally overrides the branch's generation model; otherwise
        the branch inherits the parent's model (or the settings default when the
        parent was imported). The parent run is only READ; it is never modified
        or re-run (immutability invariant, enforced by construction here: the
        background task's DB writes all target the new branch run id).
        """
        # A branch inherits its parent's owner, so the binding comes off the parent
        # ROW rather than from a caller. A separately supplied owner could disagree
        # with it, and a branch that escaped its parent's tenant would be invisible
        # to the person who forked it.
        owned = self.db.for_owner(parent_run.get("owner_sub") or LOCAL_USER_SUB)

        meta = await branching.create_branch_run(
            owned,
            parent_run,
            from_turn=from_turn,
            name=name,
            description=description,
            gen_model=model,
            mutation=mutation,
        )
        branch_run_id = meta["run_id"]
        max_messages = meta["max_messages"]
        branch_model = meta.get("model")

        # Phase 5: hand the branch to the state machine. `create_branch_run` above has
        # already written the row synchronously — which is why a branch never showed the
        # Phase 4 symptom that `POST /api/runs` did — but everything after it ran in a
        # background task, so on the deployed stack a branch was created, named, and
        # then never generated a turn.
        #
        # The copy-and-seed half becomes the machine's `branch` prepare mode; the
        # generating half is the shared loop. §6 said branch and resume "need no new
        # machinery" because both already reconstruct-and-generate-forward, and that
        # holds: only the first state differs.
        if orchestration.turn_loop_arn():
            execution = await orchestration.start_execution(
                branch_run_id,
                parent_run.get("owner_sub") or LOCAL_USER_SUB,
                max_messages=max_messages,
                mode="branch",
                extra={
                    "parent_run_id": parent_run["id"],
                    "from_turn": from_turn,
                    # The mutation travels in the payload rather than being applied
                    # here, because applying it needs the reconstructed fork state —
                    # and reconstructing that in the request is the O(N) read this
                    # phase moved out of the request path.
                    **({"mutation": mutation} if mutation else {}),
                },
            )
            if execution is None:
                logger.warning(
                    "Branch %s was created but no execution started; it will sit at "
                    "'running' with no turns until one is.", branch_run_id,
                )
            return meta

        broker = RunBroker()
        self._brokers[branch_run_id] = broker

        async def _on_event(event: Dict[str, Any]) -> None:
            await broker.publish(event)

        async def _runner() -> None:
            try:
                result = await branching.execute_branch(
                    owned,
                    parent_run,
                    branch_run_id=branch_run_id,
                    from_turn=from_turn,
                    max_messages=max_messages,
                    on_event=_on_event,
                    model=branch_model,
                    mutation=mutation,
                )
                # Auto-summarize a completed branch, same as a fresh run. Purely
                # additive (writes only the summaries table); never touches the
                # canonical event log/snapshot/cost of the branch OR the parent.
                if result.get("status") == "complete":
                    await maybe_autogenerate_summary(owned, branch_run_id)
            except Exception:  # noqa: BLE001
                logger.exception("Background branch %s crashed", branch_run_id)
            finally:
                await broker.close()

        task = asyncio.create_task(_runner())
        self._tasks[branch_run_id] = task
        task.add_done_callback(lambda _t: self._finish(branch_run_id))

        return meta

    def _finish(self, run_id: str) -> None:
        """Drop a finished run's task handle and any pending stop request.

        Clearing the stop matters: without it a run stopped once would stop again
        one turn into every later resume, which reads as the resume silently not
        working.
        """
        self._tasks.pop(run_id, None)
        self._stop_requested.discard(run_id)

    async def request_stop_durable(self, run: Dict[str, Any]) -> Dict[str, Any]:
        """Ask a run to stop, via the flag on its row rather than this process's memory.

        The Step Functions path has no live task on this server to find, so
        `request_stop`'s "is it in `self._tasks`" check would refuse every stop — the
        turn is being generated by a worker Lambda, and the API process has never heard
        of it. The flag is what both paths now agree on.

        Still idempotent, and still a request rather than a cancellation: the engine
        polls it after each turn is persisted, so the turn in flight finishes.
        """
        run_id = run["id"]
        owner_sub = run.get("owner_sub") or LOCAL_USER_SUB
        owned = self.db.for_owner(owner_sub)
        if not await owned.set_stop_requested(run_id):
            raise ValueError("Run no longer exists, so there is nothing to stop.")
        # Also set the in-memory flag, so a local run generating in THIS process stops
        # at its next turn boundary without waiting to re-read the row.
        self._stop_requested.add(run_id)
        logger.info(
            "Stop requested for run %s (currently %r)", run_id, run.get("status")
        )
        return {"run_id": run_id, "status": "stopping", "stop_requested": True}

    def request_stop(self, run: Dict[str, Any]) -> Dict[str, Any]:
        """Ask a live run to stop after the turn it is generating.

        A request, not a cancellation. The engine polls it between turns, so the
        turn in flight is finished and persisted and the run ends in the terminal
        ``stopped`` status — which is resumable, and distinguishable from
        ``interrupted`` (the process died) when reading a run list later.

        Idempotent: asking twice is not an error, because a second click on a
        button whose effect takes a turn to appear is expected, not a mistake.
        """
        run_id = run["id"]
        status = run.get("status")
        if run_id not in self._tasks:
            # Either terminal, or running in a process that is no longer here. Both
            # mean this server cannot stop it, and saying so beats accepting a
            # request that will never take effect.
            raise ValueError(
                f"Run is '{status}' with no live generation on this server; "
                "there is nothing to stop."
            )
        self._stop_requested.add(run_id)
        logger.info("Stop requested for run %s (currently '%s')", run_id, status)
        return {"run_id": run_id, "status": "stopping", "stop_requested": True}

    async def resume_run(self, run: Dict[str, Any]) -> Dict[str, Any]:
        """
        Error-recovery: resume an ``interrupted``/``failed`` run forward IN
        PLACE (same run id/codename) from its last checkpoint. Flips the run to
        ``running`` synchronously and runs generation as a background task, so
        this returns immediately — never blocks on generation.

        Refuses a run that is not resumable (only interrupted/failed) or one that
        already has a live task. On a crash mid-resume the run is set back to
        ``failed`` so it can be resumed again.
        """
        run_id = run["id"]
        status = run.get("status")
        if status not in branching.RESUMABLE_STATUSES:
            raise ValueError(
                f"Run is '{status}'; only "
                f"{'/'.join(sorted(branching.RESUMABLE_STATUSES))} runs can be resumed "
                "(use a branch to continue a completed run)."
            )
        if run_id in self._tasks:
            raise ValueError("Run already has a live generation task.")

        # Flip synchronously so an immediate re-read reflects the resume and a
        # duplicate resume is rejected by the live-task guard above.
        # Resume binds to the run's OWN owner, read off the row that was already
        # authorised by the route — not to a caller-supplied value, which could
        # disagree with it.
        owned = self.db.for_owner(run.get("owner_sub") or LOCAL_USER_SUB)
        await owned.update_run_status(run_id, "running")

        # Phase 5: the resume runs on the state machine. The trim-and-reconstruct half
        # is the machine's `resume` prepare mode; generating forward is the shared loop.
        #
        # The status flip above stays synchronous and stays HERE, because it is what
        # makes a duplicate resume fail the live-task guard and what an immediate
        # re-read of the run has to reflect.
        if orchestration.turn_loop_arn():
            # Clear the stop flag before starting. A run stopped once would otherwise
            # stop one turn into this resume, which reads as the resume not working —
            # `prepare_resume` clears it too, and doing it here as well means a resume
            # is honest even if its execution is slow to start.
            await owned.set_stop_requested(run_id, False)
            execution = await orchestration.start_execution(
                run_id,
                run.get("owner_sub") or LOCAL_USER_SUB,
                # A placeholder: `prepare_resume` computes the real budget with
                # `branch_budget`, which may EXTEND it so the resume always moves
                # forward, and persists it on the run row for the slices to read.
                max_messages=0,
                mode="resume",
            )
            if execution is None:
                logger.warning(
                    "Resume of %s started no execution; the run will sit at 'running' "
                    "with no turns until one is.", run_id,
                )
            return {"run_id": run_id, "name": run.get("name"), "status": "running"}

        broker = RunBroker()
        self._brokers[run_id] = broker

        async def _on_event(event: Dict[str, Any]) -> None:
            await broker.publish(event)

        async def _runner() -> None:
            try:
                result = await branching.resume_run_in_place(
                    owned, run, on_event=_on_event,
                    should_stop=lambda: run_id in self._stop_requested,
                )
                if result.get("status") == "complete":
                    await maybe_autogenerate_summary(owned, run_id)
            except Exception:  # noqa: BLE001
                logger.exception("Background resume %s crashed", run_id)
                # Leave it resumable again rather than stuck in "running".
                try:
                    await owned.update_run_status(run_id, "failed")
                except Exception:  # noqa: BLE001
                    logger.exception("Failed to reset status after resume crash")
            finally:
                await broker.close()

        task = asyncio.create_task(_runner())
        self._tasks[run_id] = task
        task.add_done_callback(lambda _t: self._finish(run_id))

        return {
            "run_id": run_id,
            "name": run.get("name"),
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

# SPDX-License-Identifier: Apache-2.0
"""Phase 6: which knowledge bases a speaker may search on a given turn.

Two levels of binding, which generalise Phase 5's
``persona_name = ? OR persona_name IS NULL`` exactly:

    {"knowledge_bases": ["kb-migration-policy"],          # run level: every persona
     "cast": [{"name": "Priya", "knowledge_bases": [...]}]}  # hers alone

The effective scope for a speaker is ``run ∪ persona``, then **intersected with what the
caller may actually read**. Keeping those two steps distinct is the point: the union is a
statement about this conversation, and the intersection is an authorisation decision.

Why a module rather than a method on the store: resolving a binding needs the run's
config and its cast, which is domain knowledge, while the grant check needs credentials
and a table. Splitting them keeps `may_read_kb` the only place permission is decided and
this the only place bindings are read — so neither can grow a copy of the other's job.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)


def _config(run: Dict[str, Any]) -> Dict[str, Any]:
    raw = run.get("config_json")
    if not raw:
        return {}
    try:
        cfg = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


def _cast(run: Dict[str, Any]) -> List[Dict[str, Any]]:
    try:
        cast = json.loads(run["cast_json"])
    except (KeyError, TypeError, json.JSONDecodeError):
        return []
    return cast if isinstance(cast, list) else []


def _clean(values: Any) -> List[str]:
    """Non-empty strings, order preserved, duplicates removed.

    Duplicates matter: a KB bound at BOTH run and persona level must be queried once, not
    twice — the fan-out would otherwise pay for it twice and the merge would rank the same
    passage against itself.
    """
    if not isinstance(values, (list, tuple)):
        return []
    out: List[str] = []
    seen: set = set()
    for value in values:
        # Only strings. `str(None)` is `"None"` — a perfectly non-empty string — so a
        # JSON `null` in the array would become a KB id literally named "None" and every
        # turn would query an index called `prefix-kb-none` and report a failure. Found
        # by testing what a form with a blank row actually sends.
        if not isinstance(value, str):
            continue
        text = value.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def bound_kbs(run: Dict[str, Any], persona_name: Optional[str] = None) -> List[str]:
    """The KBs bound to this turn: run-level, then this persona's own.

    Run-level first so a cast-wide collection ranks ahead of a personal one when the
    fan-out is trimmed — arbitrary, but deterministic, and a stable order is what makes a
    retrieval regression reproducible.

    ``persona_name=None`` returns the run-level bindings alone, which is the right answer
    for an operator-facing listing: it is what EVERY persona can see, without attributing
    one persona's private collection to the whole cast.
    """
    kbs = _clean(_config(run).get("knowledge_bases"))
    if persona_name is None:
        return kbs
    for member in _cast(run):
        if str(member.get("name") or "") != persona_name:
            continue
        kbs = kbs + [k for k in _clean(member.get("knowledge_bases")) if k not in kbs]
        break
    return kbs


async def searchable_for_turn(
    db: Any,
    run: Dict[str, Any],
    persona_name: Optional[str],
    owner_sub: str,
    groups: Optional[Sequence[str]] = None,
) -> List[str]:
    """The authorised KB list for one turn — bindings, intersected with grants.

    This is the call retrieval makes. It is deliberately a thin composition of two
    already-tested pieces rather than logic of its own: `bound_kbs` reads the config and
    `db.searchable_kbs` decides permission and fails closed.

    Re-resolved **every turn**, not cached on the run. §8b's requirement is that a
    revoked grant stops working at query time; a cached list is exactly the stale binding
    that requirement exists to rule out.
    """
    bound = bound_kbs(run, persona_name)
    if not bound:
        return []
    permitted = await db.searchable_kbs(bound, owner_sub, groups)
    if len(permitted) != len(bound):
        logger.info(
            "Run %s persona %s: %d of %d bound knowledge base(s) are readable by %s",
            run.get("id"), persona_name, len(permitted), len(bound), owner_sub,
        )
    return permitted


async def unreadable_bindings(
    db: Any,
    kb_ids: Sequence[str],
    owner_sub: str,
    groups: Optional[Sequence[str]] = None,
) -> List[str]:
    """Which of these KB ids the caller may NOT read. For validation at creation.

    Separate from `searchable_for_turn` because the two want opposite answers: a turn
    wants what it CAN search and carries on regardless, while `POST /api/runs` wants what
    is wrong so it can say so in a 422.

    Fails closed in the same direction — a KB whose check errors is reported as
    unreadable, so a transient fault refuses the run rather than creating one whose
    bindings were never verified.
    """
    bad: List[str] = []
    for kb_id in _clean(kb_ids):
        try:
            permitted = await db.may_read_kb(kb_id, owner_sub, groups)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Could not verify access to KB %s (%s); refusing the binding rather "
                "than accepting a run whose bindings were never checked", kb_id, exc,
            )
            permitted = False
        if not permitted:
            bad.append(kb_id)
    return bad


def declared_kbs(request: Dict[str, Any]) -> List[str]:
    """Every KB id a create-run request mentions, at either level.

    One list so validation is a single pass and cannot check the run level while
    forgetting the cast — which is the shape of bug that lets a persona bind a
    collection nobody verified.
    """
    config = request.get("config") or {}
    out = _clean(config.get("knowledge_bases"))
    seen = set(out)
    for member in request.get("cast") or []:
        if not isinstance(member, dict):
            continue
        for kb_id in _clean(member.get("knowledge_bases")):
            if kb_id not in seen:
                seen.add(kb_id)
                out.append(kb_id)
    return out

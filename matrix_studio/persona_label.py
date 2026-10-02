# SPDX-License-Identifier: Apache-2.0
"""Every persona name in a text output says it is a simulated persona: "(bot) Ruth".

The UI draws a robot glyph before each persona's name; a Markdown or HTML file has no glyph that
survives being pasted, printed or read by somebody who never opened the app, so it gets words. The
owner's request (2026-10-02) was "even something as simple as '(bot) Ruth'", and a file is exactly where
a simulated statement is most likely to be taken for a real one.

**Display only.** The stored name never changes and the marker never reaches a prompt — personas must
not address each other as "(bot) Ruth" — so it is applied here, to the export's data model, on the way
to a renderer, and nowhere upstream of it.

**Who is marked.** Personas and consultants (both simulated). Not an operator's injected or scheduled
message, which is a real person's words in the room, and not a narrator. In the model's summary, only
the fields that ARE a name — a dissenter's `speaker`, an evidence row's `asked_by`, a key idea's
`proposed_by` — and only when they name somebody in the cast; the analyst's prose is never rewritten.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Dict, Iterable, Optional, Set

#: What goes before a persona's name in text. A word rather than a symbol: it reads the same in every
#: font, in a terminal and to a screen reader.
BOT_PREFIX = "(bot) "


def bot(name: Any) -> str:
    """``name`` marked as a simulated persona. Idempotent, so a twice-labelled model is not "(bot) (bot)"."""
    text = str(name or "")
    if not text or text.startswith(BOT_PREFIX):
        return text
    return BOT_PREFIX + text


def _key(name: Any) -> str:
    return " ".join(str(name or "").split()).lower()


def _mark_if_known(value: Any, known: Set[str]) -> Any:
    """Mark a name field when it names somebody known — itself, or each item of "Ruth, Sam and Ada"."""
    if not isinstance(value, str) or not value.strip():
        return value
    if _key(value) in known:
        return bot(value.strip())
    parts = [p for p in re.split(r"\s*(?:,|;|&|\band\b)\s*", value.strip()) if p]
    if len(parts) > 1 and all(_key(p) in known for p in parts):
        return ", ".join(bot(p) for p in parts)
    return value


def label_run_model(model: Dict[str, Any], extra_names: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """A copy of `export.run_model`'s output with every persona and consultant name marked.

    Known names are the cast's, plus every speaker of a generated (not injected) message — a persona
    added at a branch's fork speaks without being in the stored cast, and is a persona all the same.
    """
    if model.get("kind") != "run":
        return model
    m = copy.deepcopy(model)
    known: Set[str] = {_key(c.get("name")) for c in m.get("cast") or [] if c.get("name")}
    known |= {_key(t.get("speaker")) for t in m.get("transcript") or []
              if t.get("speaker") and not t.get("injected")}
    known |= {_key(n) for n in extra_names or () if n}
    known.discard("")

    for c in m.get("cast") or []:
        c["name"] = bot(c.get("name"))
    for t in m.get("transcript") or []:
        if not t.get("injected"):
            t["speaker"] = bot(t.get("speaker"))
        # A shift credits personas and consultants by name (shifts.py says which kind each is); an assumption
        # or a scheduled message it credits is not a persona and stays as it is.
        for c in (t.get("shift") or {}).get("credits") or []:
            if isinstance(c, dict) and c.get("kind") in ("persona", "consultant"):
                c["name"] = bot(c.get("name"))
    for a in m.get("assumptions") or []:
        for d in a.get("disputes") or []:
            if isinstance(d, dict):
                d["speaker"] = _mark_if_known(d.get("speaker"), known)
    s = m.get("summary")
    if isinstance(s, dict):
        for d in s.get("dissenters") or []:
            if isinstance(d, dict):
                d["speaker"] = _mark_if_known(d.get("speaker"), known)
        for row in s.get("evidence_plan") or []:
            if isinstance(row, dict):
                row["asked_by"] = _mark_if_known(row.get("asked_by"), known)
        for idea in s.get("key_ideas") or []:
            if isinstance(idea, dict):
                for field in ("proposed_by", "speaker"):
                    if field in idea:
                        idea[field] = _mark_if_known(idea.get(field), known)
    return m

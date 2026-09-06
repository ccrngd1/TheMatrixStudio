# SPDX-License-Identifier: Apache-2.0
"""Phase 5i: citation provenance — first-hand, second-hand, or unsupported.

## The failure this exists for

Observed in a real run: Priya retrieved ``phase4-report.md #31`` (her own
document) and cited it. On the next turn Dana — whose only document is
``project-spec.md``, and who retrieved only from it — wrote *"which is what
phase4-report.md #31 actually specifies"*. Dana had never had access to that
document. She lifted the label out of Priya's turn and attributed a technical
claim to it as though she had read it.

SQL scoping stops a persona *reading* another's slice. Nothing stopped it
*citing* one, so a persona could borrow another's evidential authority by name —
which defeats the point of per-persona slices.

## The model: attributed hearsay, not suppression

The fix is NOT to forbid citing documents you have not read. In a real review,
evidence legitimately propagates through people: an SME shows you a document, you
report back, and the team records "Priya cited X as saying Y". Forbidding that
would destroy information the discussion needs.

What must be preserved is the *evidential relationship*. So a citation is
legitimate when it is either:

- **first-hand** — the label is among the passages this turn actually retrieved; or
- **second-hand** — the utterance attributes it to a participant who really did
  cite it, with it in their own retrieved passages.

Anything else attributive is **unverified**: either a document nobody in the run
has, or another persona's document claimed without attribution.

This completes a distinction the engine already draws — ``document_refs`` (I
retrieved this), ``memory_refs`` (I remember this), the 5g disclosure (I have
nothing) — by adding "someone else surfaced this".

Pure functions only: no LLM, no database, no state. The engine supplies the
context and decides what to do with the verdict.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

# A citation as the prompt teaches personas to write it, e.g. "spec.pdf #3",
# "[distribution-constraints.md #0]", or a bare "phase4-report.md".
# NOTE: the title must not admit spaces. Allowing them made the match greedy
# leftwards — "Per spec.md" captured "per spec.md" as the title — which then never
# matched anything in context and produced spurious violations. A title that
# genuinely contains a space is matched from its last word, which is a harmless
# degradation next to swallowing arbitrary preceding prose.
CITATION_RE = re.compile(
    r"\[?\b(?P<title>[\w][\w.\-]*\.(?:md|markdown|txt|text|pdf|docx))\b"
    r"(?:\s*#\s*(?P<ordinal>\d+))?\]?",
    re.IGNORECASE,
)

# Words that turn a mention into an ASSERTION about what a document contains.
# Only attributive use is judged: merely naming a document ("I haven't seen that
# spec you mentioned") is honest and must never be flagged.
ATTRIBUTION_CUES = (
    "per", "according to", "specifies", "specified", "states", "stated",
    "says", "said", "shows", "showed", "confirms", "confirmed", "requires",
    "required", "documents", "documented", "cites", "notes", "noted",
    "makes clear", "establishes", "mandates", "defines", "sets out",
    "lays out", "spells out", "in line with", "as written in", "backs",
)

# How much text either side of the label to inspect for an attribution cue.
CUE_WINDOW = 90

# Phrases that explicitly disclaim first-hand access. These make a mention
# honest even next to an attribution cue, because the speaker is not claiming to
# have read the document.
DISCLAIMERS = (
    "haven't seen", "have not seen", "haven't read", "have not read",
    "don't have", "do not have", "never seen", "never read",
    "not in front of me", "you're referencing", "you are referencing",
    "you mentioned", "you cited", "second-hand", "secondhand",
)


@dataclass(frozen=True)
class Citation:
    """One citation found in an utterance, with its provenance verdict."""

    title: str
    ordinal: Optional[int]
    attributive: bool
    kind: str                      # firsthand | secondhand | mention | unverified
    via: Optional[str] = None      # participant credited for a second-hand cite
    reason: Optional[str] = None   # why it is unverified

    @property
    def label(self) -> str:
        return f"{self.title} #{self.ordinal}" if self.ordinal is not None else self.title


@dataclass
class CitationContext:
    """What the speaker may legitimately cite this turn.

    ``own`` holds the titles retrieved by THIS speaker on THIS turn (first-hand).
    ``by_speaker`` maps a document title to the participants who have already
    cited it first-hand earlier in the run, which is what makes a second-hand
    attribution verifiable rather than merely plausible.
    """

    own: Set[str] = field(default_factory=set)
    own_ordinals: Set[Tuple[str, int]] = field(default_factory=set)
    by_speaker: Dict[str, Set[str]] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        own_passages: Iterable[Any] = (),
        prior_firsthand: Iterable[Tuple[str, str]] = (),
    ) -> "CitationContext":
        """Build from this turn's passages and prior ``(speaker, title)`` pairs."""
        own: Set[str] = set()
        own_ordinals: Set[Tuple[str, int]] = set()
        for p in own_passages:
            title = (getattr(p, "title", None) or (p.get("title") if isinstance(p, dict) else None))
            if not title:
                continue
            title = str(title).lower()
            own.add(title)
            ordinal = getattr(p, "ordinal", None)
            if ordinal is None and isinstance(p, dict):
                ordinal = p.get("ordinal")
            if ordinal is not None:
                own_ordinals.add((title, int(ordinal)))
        by_speaker: Dict[str, Set[str]] = {}
        for speaker, title in prior_firsthand:
            by_speaker.setdefault(str(title).lower(), set()).add(speaker)
        return cls(own=own, own_ordinals=own_ordinals, by_speaker=by_speaker)


def _window(text: str, start: int, end: int) -> str:
    return text[max(0, start - CUE_WINDOW):end + CUE_WINDOW].lower()


def analyse_citations(
    utterance: str,
    speaker_name: str,
    agent_names: Sequence[str],
    context: CitationContext,
) -> List[Citation]:
    """Classify every citation in an utterance by its evidential provenance.

    A label is judged **attributive** only when an attribution cue sits near it
    and no disclaimer does. A bare mention is left as ``kind="mention"`` and never
    treated as a problem — flagging honest talk about a document would punish
    exactly the behaviour this design wants.
    """
    out: List[Citation] = []
    others = [n for n in agent_names if n != speaker_name]

    for match in CITATION_RE.finditer(utterance):
        title = match.group("title").strip().lower()
        raw_ordinal = match.group("ordinal")
        ordinal = int(raw_ordinal) if raw_ordinal is not None else None
        near = _window(utterance, match.start(), match.end())

        disclaimed = any(d in near for d in DISCLAIMERS)
        attributive = (not disclaimed) and any(cue in near for cue in ATTRIBUTION_CUES)

        # First-hand: the speaker actually retrieved this document this turn.
        if title in context.own:
            if ordinal is not None and context.own_ordinals and (title, ordinal) not in context.own_ordinals:
                out.append(Citation(
                    title, ordinal, attributive, "unverified",
                    reason=(
                        f"cites {title} #{ordinal} but retrieved only "
                        f"{sorted(o for t, o in context.own_ordinals if t == title)}"
                    ),
                ))
            else:
                out.append(Citation(title, ordinal, attributive, "firsthand"))
            continue

        # Second-hand: credited to a participant who really did cite it.
        holders = context.by_speaker.get(title, set())
        credited = next((o for o in others if o.lower() in near), None)
        if credited and credited in holders:
            out.append(Citation(title, ordinal, attributive, "secondhand", via=credited))
            continue

        if not attributive:
            # An honest mention of a document the speaker does not hold.
            out.append(Citation(title, ordinal, attributive, "mention"))
            continue

        if credited and credited not in holders:
            reason = (
                f"attributes {title} to {credited}, who never cited it first-hand"
            )
        elif holders:
            reason = (
                f"asserts what {title} says without having retrieved it; it was "
                f"surfaced by {', '.join(sorted(holders))} and is not attributed to them"
            )
        else:
            reason = (
                f"asserts what {title} says, but no participant has retrieved that "
                "document in this run"
            )
        out.append(Citation(title, ordinal, attributive, "unverified", via=credited, reason=reason))

    return out


def citation_violation(citations: Sequence[Citation]) -> Optional[str]:
    """The reason for the first unverified attributive citation, if any.

    Returned as a plain string so the validation gate can treat it exactly like
    its other deterministic checks.
    """
    for c in citations:
        if c.kind == "unverified" and c.attributive:
            return c.reason
    return None


def provenance_payload(citations: Sequence[Citation]) -> List[Dict[str, Any]]:
    """Serialise citations for the ``agent.response`` event.

    This is what makes an evidence chain machine-readable: a later export can
    trace a claim back through the participant who surfaced it to the document
    itself, instead of the hop being invisible.
    """
    return [
        {
            "label": c.label,
            "kind": c.kind,
            "attributive": c.attributive,
            **({"via": c.via} if c.via else {}),
            **({"reason": c.reason} if c.reason else {}),
        }
        for c in citations
    ]

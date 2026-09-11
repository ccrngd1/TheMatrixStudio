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
    # Added after reading real output: the model uses these freely and the
    # original list missed them, so genuine assertions were scored as mentions.
    "mentions", "mentioned", "is explicit", "explicitly", "describes",
    "described", "covers", "records", "recorded", "reports", "acknowledges",
    "admits", "warns", "recommends", "concludes", "claims", "asserts",
    "indicates", "suggests", "outlines", "flags",
)

# A bracketed citation — "[spec.md #3]" — is the exact form the retrieval prompt
# teaches for supporting a claim, so using it IS an assertion about the document
# regardless of nearby wording. This matters because the commonest real style is
# TRAILING: "snapshots are full per-turn, not deltas [readme.md #37]." There is no
# cue word anywhere near the label, so cue proximity alone scored those as
# harmless mentions and the gate under-fired on them.
BRACKETED_RE = re.compile(r"\[[^\]\[]*?\.(?:md|markdown|txt|text|pdf|docx)[^\]\[]*?\]", re.I)

# ...except a markdown link, "[spec.md](spec.md)", which is just a reference and
# was observed in genuinely non-attributive use ("whoever has [x](x) open").
MARKDOWN_LINK_RE = re.compile(
    r"\[[^\]\[]*?\.(?:md|markdown|txt|text|pdf|docx)[^\]\[]*?\]\s*\(", re.I
)

# How much text either side of the label to inspect for an attribution cue.
CUE_WINDOW = 90

# Disclaimers are checked in a TIGHTER window than cues. A disclaimer anywhere
# within the wide cue window let an unrelated "I haven't read X" elsewhere in the
# sentence suppress the check on a different document — an evasion surface.
DISCLAIMER_WINDOW = 45

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


def _window(text: str, start: int, end: int, size: int = CUE_WINDOW) -> str:
    return text[max(0, start - size):end + size].lower()


def _is_bracketed(text: str, start: int, end: int) -> bool:
    """Whether this label sits inside a citation bracket that is not a markdown link."""
    near = text[max(0, start - 4):min(len(text), end + 4)]
    if MARKDOWN_LINK_RE.search(near):
        return False
    return bool(BRACKETED_RE.search(near))


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
        close = _window(utterance, match.start(), match.end(), DISCLAIMER_WINDOW)

        disclaimed = any(d in close for d in DISCLAIMERS)
        # Either an explicit cue nearby, or the bracketed citation form the prompt
        # teaches for backing a claim (which is commonly used TRAILING, with no cue
        # word at all).
        asserted = any(cue in near for cue in ATTRIBUTION_CUES) or _is_bracketed(
            utterance, match.start(), match.end()
        )
        attributive = asserted and not disclaimed

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

    ``title`` is carried alongside ``label`` even though ``label`` contains it,
    because the two are not interchangeable in the direction that matters.
    ``label`` is ``"title #ordinal"`` when there is an ordinal, so recovering the
    title from it means splitting on `` #`` — which is a guess about titles, and
    wrong for any document whose own name contains that sequence. The first-hand
    ledger is rebuilt from these events (`reconstruct_at_turn`), and it keys on
    the title, so the log has to state the title rather than imply it.
    """
    return [
        {
            "label": c.label,
            "title": c.title,
            "kind": c.kind,
            "attributive": c.attributive,
            **({"via": c.via} if c.via else {}),
            **({"reason": c.reason} if c.reason else {}),
        }
        for c in citations
    ]

# SPDX-License-Identifier: Apache-2.0
"""Which passage a message QUOTES, found from the words themselves.

## Why this exists

Personas are given passages labelled ``title #ordinal`` and asked to cite them, and they do not:
measured on 25 stored runs (2026-09-27), 816 of 824 messages had passages in their prompt and not
one cited a passage by its label. So a claim could be traced only to the three-or-so passages that
were in view, not to the one it came from.

## Why only quotation, and not a best guess

The same measurement tried a best guess — the in-view passage sharing the most distinctive terms
with the message — and it does not separate them: the top passage led the runner-up by under 0.10
of the message's terms in 350 of 377 messages. The passages in view are about the same subject, so
they share its vocabulary. A "likeliest source" from that would be a coin toss presented as a
finding.

A shared run of words is different in kind: four content words in the same order in the message
and in the passage is evidence of use that a reader can check by looking. Measured, ~9% of messages
have one (a further ~8% share three). That is the whole of what this reports, and the quoted words
are returned so the evidence is on screen rather than a score.

Pure functions: no model, no database.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

WORD = re.compile(r"[A-Za-z0-9]+(?:['’\-][A-Za-z0-9]+)*")

#: Words that carry no evidence of use on their own: a shared "of the and it" is grammar, not quotation.
STOPWORDS = frozenset(
    """a an the and or but if so of to in on for at by from with as into over under about after before
    is are was were be been being it its this that these those there here we you they i he she him her
    his their our your them us me my not no nor do does did have has had than then what which who whom
    when where why how all any each more most some such only own same very just also can will would
    should could may might must""".split()
)

#: Content words a shared run must contain to count as quotation. At 3 the probe's hits included
#: generic phrasing ("uploadable knowledge bases"); at 4 they were recognisably lifted sentences.
MIN_CONTENT_WORDS = 4


def _content(word: str) -> bool:
    return word not in STOPWORDS and len(word) > 2 and not word.isdigit()


def _words(text: str) -> List[Tuple[str, int, int]]:
    return [(m.group(0).lower(), m.start(), m.end()) for m in WORD.finditer(text or "")]


def longest_quote(message: str, passage: str) -> Optional[Dict[str, Any]]:
    """The run of consecutive words the message shares with the passage that holds the most content
    words — or None when no run reaches `MIN_CONTENT_WORDS`. The phrase is sliced from the MESSAGE,
    so it is exactly what the reader sees highlighted."""
    mw, pw = _words(message), [w for w, _, _ in _words(passage)]
    where: Dict[str, List[int]] = {}
    for j, w in enumerate(pw):
        where.setdefault(w, []).append(j)
    best: Tuple[int, int, int] = (0, 0, 0)  # content words, start, length (in message words)
    for i, (w, _, _) in enumerate(mw):
        for j in where.get(w, ()):
            k = content = 0
            while i + k < len(mw) and j + k < len(pw) and mw[i + k][0] == pw[j + k]:
                content += _content(mw[i + k][0])
                k += 1
            if content > best[0]:
                best = (content, i, k)
    content, i, k = best
    if content < MIN_CONTENT_WORDS:
        return None
    return {
        "phrase": message[mw[i][1]:mw[i + k - 1][2]],
        "content_words": content,
        "words": k,
    }


def attribute(message: str, passages: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """The in-view passages this message quotes, strongest first.

    `passages` are the turn's retrieved passages, each with its `text` added. A passage without text
    (unreadable, or outside the caller's reach) is skipped, not scored as unquoted.
    """
    out = []
    for p in passages:
        text = p.get("text")
        if not text:
            continue
        q = longest_quote(message, text)
        if q:
            out.append({
                "chunk_id": p.get("chunk_id"),
                "document_id": p.get("document_id"),
                "title": p.get("title"),
                "ordinal": p.get("ordinal"),
                **q,
            })
    out.sort(key=lambda a: a["content_words"], reverse=True)
    return out

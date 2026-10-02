# SPDX-License-Identifier: Apache-2.0
"""Personas never carry a real, well-known person's name.

## Why

The owner wants to explore personas grounded in well-known people's public statements, and agreed
(2026-10-02) that such a persona must never carry the real person's name. A simulated "Jeff Bezos"
saying something in a transcript, an export or a brief reads as a quote from Jeff Bezos, and nothing
downstream of the name can undo that. So a persona or consultant whose name matches a real, widely
known public figure is renamed to a playful, clearly fictional sound-alike ("Jeff Bezos" ->
"Geoff Beesoh"), and the person who typed it is told.

## Detection: deterministic first, model second

1. **The curated list** (`public_figures.json`): full names and common variants, matched case- and
   accent-insensitively, each with a hand-written parody. Free, instant and reviewable, so it covers
   the names this tool is most likely to see: tech, AI and business leaders, heads of state and
   government, and a set of widely known public and historical figures.
2. **A model check** for a full name not on the list: one cheap call per new name (the `name_check`
   role, Haiku at temperature 0, a JSON schema), cached in process. It also suggests a parody, which
   is checked here rather than trusted.

**Conservative on purpose.** A false positive renames somebody's "Mike Johnson" and tells them they
typed a famous name, which is wrong and irritating; a false negative is a name the curated list can
gain. So: a single word ("Ruth", "Jeff", "Bezos") never triggers either check; the model is told that
ordinary names shared by many people are not famous; and only a `famous: true` at `high` confidence
counts. A failed or unreadable model reply counts as not famous, and says so in the log.

## The replacement

A curated entry's parody is hand-written. A model suggestion must pass `acceptable_parody`: letters
only, spelled differently from the real name in every word, not too close to the real spelling
overall, free of a short list of unkind words, not itself on the list, and — re-asked — not itself a
famous name. If it fails, `sound_alike` derives one deterministically. Whatever is chosen is unique in
the cast.

## Where it applies

`screen_request` is the one function every entry point calls: run and ensemble creation, the
check-names endpoint behind the new-run form, cast templates (save and load), the persona wizard,
the `add_persona` branch mutation and the CLI. It renames the persona or consultant, and replaces the
real name with the same parody in every persona's description, goals and structured fields, the
consultants' expertise, the topic, the working assumptions and the scheduled messages — so the prompt
never says "You are Jeff Bezos" under a fictional name. Documents are NOT rewritten: they are
evidence, and quoted passages must match their source.

Stored runs are never renamed. Display marks every persona name as simulated anyway (the UI's robot
glyph, "(bot) " in exports); that is `persona_label.py`'s job, not this module's.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from matrix_studio.jsonio import extract_json_object

# Deferred: importing litellm costs 1.7 s and this module's callers include the API Lambda, whose read
# routes never generate. See matrix_studio/lazy_litellm.py.
from matrix_studio.lazy_litellm import litellm

logger = logging.getLogger(__name__)

DATA_FILE = Path(__file__).with_name("public_figures.json")

#: The `reason` on every rename. The UI builds its sentence around it; kept short and fixed so a client
#: can rely on it.
REASON = "real public figure"

#: Most names one request may ask about. The new-run form sends a cast's worth; the bound is what keeps
#: the check endpoint from being a way to buy model calls in bulk.
MAX_NAMES = 40
MAX_NAME_CHARS = 80

#: Model calls in flight at once for one request.
CONCURRENCY = 8

#: A model call that has not answered by now counts as "not famous". The check sits in front of run
#: creation, and a hung provider must not hang the create.
CALL_TIMEOUT_S = 15

#: `famous` counts only at this confidence. "medium" is where ordinary names that a minor public figure
#: happens to share land, which is exactly the false positive this module refuses.
REQUIRED_CONFIDENCE = "high"

#: A model-suggested parody this close to the real spelling (difflib ratio, 0..1) is rejected: past it, a
#: "parody" reads as a typo of the real name. Every curated parody passes it too (the closest is 0.898).
MAX_SIMILARITY = 0.9

#: Words before a name that are not part of it: honorifics, offices and royal style.
_TITLES = frozenset({
    "mr", "mrs", "ms", "miss", "mx", "dr", "doctor", "prof", "professor", "sir", "dame", "lord", "lady",
    "hon", "rev", "reverend", "president", "vice", "pm", "senator", "sen", "governor", "gov", "rep",
    "representative", "congressman", "congresswoman", "judge", "justice", "general", "gen", "king",
    "queen", "prince", "princess", "pope", "saint", "st", "ceo", "cto", "cfo", "founder", "chancellor",
    "minister", "premier", "chairman", "chairwoman", "chair", "ayatollah", "sheikh", "emperor",
    "pontiff",
})
#: Words after a name that are not part of it.
_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv", "phd", "md", "esq", "obe", "mbe", "kbe", "cbe"})
#: Words inside a name that a parody may keep: "da Silva", "von der Leyen", "bin Salman".
_PARTICLES = frozenset({
    "da", "de", "del", "della", "der", "di", "du", "dos", "van", "von", "bin", "bint", "ibn", "al", "el",
    "la", "le", "den", "ter", "the", "of", "y",
})

#: What a parody must not contain. The parody is shown next to a real person's likeness, so a pun that
#: lands as an insult is worse than a dull respelling. Not exhaustive — the model is also told — but it is
#: the check that does not depend on the model listening. Two lists, because real surnames contain short
#: words innocently ("Butterfield", "Hassabis", "Michelle"):
#:
#: - `_UNKIND_ANYWHERE`: never innocent inside a word, so matched as a substring of any word;
#: - `_UNKIND_WORDS`: matched only as the whole word, or the start of a word at most two letters longer.
_UNKIND_ANYWHERE = (
    "bozo", "fart", "suck", "moron", "idiot", "stupid", "loser", "turd", "puke", "vomit", "nazi", "slut",
    "whore", "shit", "piss", "crap", "scam", "fraud", "liar", "creep", "sleaze", "psycho", "wacko",
    "dimwit", "dunce", "weasel", "stink", "kook",
)
_UNKIND_WORDS = (
    "fat", "pig", "rat", "nut", "poo", "lie", "rot", "ass", "dope", "dumb", "dork", "fake", "evil",
    "ugly", "nark", "jerk", "boob", "butt", "clown", "toad", "trash", "hate", "kill", "dick", "hell",
    "damn", "smell", "crook", "slob", "nerd", "geek", "weird", "creepy",
    # Innuendo, which a sound-alike lands on more easily than an insult: the live check's first run offered
    # "Shag Rukk Kahn" for Shah Rukh Khan.
    "shag", "bonk", "hump", "knob", "prick", "cock", "tit", "tits", "booty", "sex", "sexy", "horny", "screw",
    "bum", "poop", "porn",
)


# --------------------------------------------------------------------------- #
# Normalising and matching
# --------------------------------------------------------------------------- #


def normalise(text: Any) -> str:
    """Lower-case, accent-free, punctuation-free, single-spaced. "Tobi Lütke" and "tobi lutke" agree.

    Apostrophes and full stops vanish ("O'Rourke" -> "orourke", "J.D." -> "jd"); hyphens and other
    punctuation become spaces, so "Berners-Lee" and "Berners Lee" agree too.
    """
    s = unicodedata.normalize("NFKD", str(text or ""))
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).casefold()
    s = s.replace("ł", "l").replace("ø", "o").replace("đ", "d").replace("ı", "i")
    s = re.sub(r"[’'`´.]", "", s)
    s = re.sub(r"[\W_]+", " ", s)
    return " ".join(s.split())


def _merge_initials(tokens: List[str]) -> List[str]:
    """"j d vance" -> "jd vance": leading initials typed with spaces are one token, as "JD" is."""
    i = 0
    while i < len(tokens) and len(tokens[i]) == 1 and tokens[i].isalpha():
        i += 1
    return ["".join(tokens[:i])] + tokens[i:] if i >= 2 else tokens


def _significant(tokens: Iterable[str]) -> List[str]:
    """The tokens that make a name this person's: not a leading title, a suffix, a particle or an initial.

    A title only counts as one at the START: "King" in "King Charles" is a style, in "Stephen King" it is
    the surname, and dropping it there would make a famous full name look like a single first name.
    """
    t = list(tokens)
    while len(t) > 1 and t[0] in _TITLES:
        t = t[1:]
    return [x for x in t if x not in _SUFFIXES and x not in _PARTICLES and len(x) > 1]


def _forms(name: Any) -> List[str]:
    """Every normalised form of ``name`` worth looking up, most literal first.

    The whole name; the part before a parenthesis, comma or spaced dash ("Jeff Bezos (founder)",
    "Jeff Bezos, CEO"); each with leading titles and trailing suffixes removed; and with middle
    initials dropped ("Jeffrey P. Bezos" -> "jeffrey bezos").
    """
    raw = str(name or "")
    cut = re.split(r"\s*(?:\(|,|;|:|\s[-–—]\s)", raw, maxsplit=1)[0]
    out: List[str] = []
    for text in (raw, cut):
        tokens = _merge_initials(normalise(text).split())
        if not tokens:
            continue
        out.append(" ".join(tokens))
        t = list(tokens)
        while len(t) > 1 and t[0] in _TITLES:
            t = t[1:]
        while len(t) > 1 and t[-1] in _SUFFIXES:
            t = t[:-1]
        out.append(" ".join(t))
        if len(t) > 2:
            out.append(" ".join(x for i, x in enumerate(t) if not (0 < i < len(t) - 1 and len(x) == 1)))
    seen: List[str] = []
    for f in out:
        if f and f not in seen:
            seen.append(f)
    return seen


@dataclass(frozen=True)
class Figure:
    """One curated person: the name as written, its parody, and the forms that match it."""

    name: str
    parody: str
    group: str = ""
    variants: Tuple[str, ...] = ()

    @property
    def spellings(self) -> Tuple[str, ...]:
        """Every spelling of this person's name, for finding mentions in text."""
        return (self.name, *self.variants)


@lru_cache(maxsize=1)
def figures() -> Tuple[Figure, ...]:
    """The curated list, loaded once."""
    data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    return tuple(
        Figure(str(f["name"]), str(f["parody"]), str(f.get("group") or ""),
               tuple(str(v) for v in f.get("variants") or ()))
        for f in data["figures"]
    )


@lru_cache(maxsize=1)
def _index() -> Dict[str, Figure]:
    """Normalised form -> figure. Only forms of two or more words: one word is never a full name."""
    index: Dict[str, Figure] = {}
    for fig in figures():
        for spelling in fig.spellings:
            for form in _forms(spelling):
                if len(form.split()) >= 2:
                    index.setdefault(form, fig)
    return index


def lookup(name: Any) -> Optional[Figure]:
    """The curated figure ``name`` is a full name of, or None. Deterministic and free."""
    index = _index()
    for form in _forms(name):
        if len(form.split()) >= 2 and form in index:
            return index[form]
    return None


def listed_name(name: Any) -> Any:
    """``name``, or its curated parody when it is on the list. Free, so a listing can use it."""
    fig = lookup(name)
    return fig.parody if fig is not None else name


def looks_like_full_name(name: Any) -> bool:
    """Whether ``name`` is worth asking the model about: two or more real words, no digits, not long.

    A single word — "Ruth", "Jeff", "Bezos" — is never a full name, so it costs nothing and never
    triggers; that is the first half of being conservative. Something with digits or eight words is a
    label ("Analyst 2", "the head of the regional sales team"), not a person.
    """
    text = str(name or "").strip()
    if not text or len(text) > MAX_NAME_CHARS or re.search(r"\d", text):
        return False
    forms = _forms(text)
    if not forms:
        return False
    words = _significant(forms[-1].split())
    return 2 <= len(words) <= 6


# --------------------------------------------------------------------------- #
# Parodies
# --------------------------------------------------------------------------- #


def _unkind(text: str) -> bool:
    words = normalise(text).split()
    if any(root in w for w in words for root in _UNKIND_ANYWHERE):
        return True
    return any(w == root or (w.startswith(root) and len(w) <= len(root) + 2)
               for w in words for root in _UNKIND_WORDS)


def acceptable_parody(real: str, candidate: Any) -> bool:
    """Whether ``candidate`` may stand in for ``real``. The rules a model's suggestion has to pass.

    - letters, spaces, hyphens, apostrophes and full stops only, and at most 60 characters;
    - as many significant words as the real name has (up to two), so "Elon Musk" does not become "Ilo";
    - spelled differently in EVERY significant word, so no real first name or surname survives;
    - not too close overall (`MAX_SIMILARITY`), so it does not read as a typo of the real name;
    - nothing unkind (`_UNKIND_ANYWHERE`, `_UNKIND_WORDS`);
    - not itself on the curated list.
    """
    text = str(candidate or "").strip()
    if not text or len(text) > 60:
        return False
    if not all(ch.isalpha() or ch in " -'’." for ch in text):
        return False
    real_n, cand_n = normalise(real), normalise(text)
    if not cand_n or cand_n == real_n:
        return False
    real_sig = set(_significant(real_n.split()))
    cand_sig = _significant(cand_n.split())
    if len(cand_sig) < min(2, len(real_sig)):
        return False
    if real_sig & set(cand_sig):
        return False
    if SequenceMatcher(None, real_n, cand_n).ratio() >= MAX_SIMILARITY:
        return False
    if _unkind(text):
        return False
    return lookup(text) is None


# The fallback's two moves, both chosen to keep a word sounding like itself while no longer being
# spelled like it. `_LEAD` swaps the first sound for a near neighbour ("Jeff" -> "Geff", "Werner" ->
# "Verner", the shape of the owner's own examples); `_RESPELL` then changes one more spelling further in,
# so the word is not a one-letter typo of the real one. Each has alternatives, picked by `salt`, so a
# second attempt gives a different answer.
_LEAD: Dict[str, Tuple[str, ...]] = {
    "b": ("p", "bh"), "c": ("k", "ch"), "d": ("t", "dh"), "f": ("ph", "v"), "g": ("k", "gh"),
    "h": ("wh", "kh"), "j": ("g", "y"), "k": ("c", "kh"), "l": ("ll", "lh"), "m": ("mh", "n"),
    "n": ("kn", "nh"), "p": ("b", "ph"), "q": ("kw", "k"), "r": ("wr", "rh"), "s": ("z", "ps"),
    "t": ("d", "th"), "v": ("w", "f"), "w": ("v", "wh"), "y": ("j", "i"), "z": ("s", "ts"),
    "a": ("ah", "e"), "e": ("ee", "i"), "i": ("ee", "y"), "o": ("oh", "u"), "u": ("oo", "yu"),
}
_RESPELL: Tuple[Tuple[str, str], ...] = (
    (r"ph", "f"), (r"ck", "k"), (r"ee", "ea"), (r"oo", "ou"), (r"th", "t"), (r"qu", "kw"), (r"x", "ks"),
    (r"s$", "z"), (r"y$", "ie"), (r"ie$", "y"), (r"er$", "ur"), (r"an$", "un"), (r"on$", "un"),
    (r"in$", "yn"), (r"a$", "ah"), (r"o$", "oh"), (r"i(?=[^aeiou])", "y"), (r"c(?=[aou])", "k"),
    (r"([aeiou])([bdfglmnprstvz])$", r"\1\2\2"),
)


def _respell(word: str, salt: int) -> str:
    """One word with its first sound swapped and one more spelling changed. Never the input."""
    low = word.lower()
    if low in _PARTICLES or low in _SUFFIXES or len(low) <= 1:
        return word
    options = _LEAD.get(low[0])
    head = options[salt % len(options)] if options else low[0]
    rest = low[1:]
    n = len(_RESPELL)
    for k in range(n):
        pattern, repl = _RESPELL[(k + salt) % n]
        new = re.sub(pattern, repl, rest, count=1)
        if new != rest:
            rest = new
            break
    out = head + rest
    if out == low:
        out = low + "e"
    return out[:1].upper() + out[1:]


def sound_alike(real: str, salt: int = 0) -> str:
    """A deterministic, clearly fictional respelling of ``real``: the fallback when no parody is usable.

    Every significant word changes; particles ("da", "von") and suffixes stay. Tried with increasing
    ``salt`` until the result passes `acceptable_parody`, and after ten tries the last attempt is
    returned anyway, because the alternative — keeping the real name — is the one outcome ruled out.
    """
    words = re.split(r"(\s+|-)", str(real or "").strip())
    attempt = ""
    for s in range(salt, salt + 10):
        attempt = "".join(w if not w.strip() or w == "-" else _respell(w, s + i) for i, w in enumerate(words))
        if acceptable_parody(real, attempt):
            return attempt
    return attempt


# --------------------------------------------------------------------------- #
# The model check
# --------------------------------------------------------------------------- #

_SYSTEM = (
    "You screen persona names for a conversation simulator. Personas are fictional and must never "
    "carry the name of a real, widely known person.\n\n"
    "Decide whether the given name is the FULL NAME of a real, widely known public figure: someone most "
    "educated adults, or most people in that person's country or field, would recognise from the name "
    "alone. That includes heads of state and government, prominent politicians, business and technology "
    "leaders, famous scientists, writers, artists, entertainers and athletes, and famous historical "
    "figures.\n\n"
    "Be conservative. Answer famous=false for: a first name or a surname on its own; an ordinary name "
    "shared by many people (for example 'John Smith', 'Maria Garcia', 'Mike Johnson', 'Sarah Chen', "
    "'David Miller'), even if some public figure has that name; an invented name; a fictional "
    "character; a job title or description. Use confidence 'high' only when you are sure.\n\n"
    "When famous is true, also suggest a parody: a playful, clearly fictional sound-alike of the full "
    "name, gentle and affectionate — never insulting, mocking, rude or suggestive (no slang, no innuendo), "
    "never the name of another real "
    "person — spelled differently from the real name in every word. Examples: 'Jeff Bezos' -> "
    "'Geoff Beesoh'; 'Werner Vogels' -> 'Verner Fogles'. When famous is false, parody is ''.\n\n"
    "Reply with the JSON object only."
)

#: The reply's shape. Sent as a JSON schema, which litellm turns into a forced tool on Bedrock — a
#: structural constraint rather than an instruction (see the `response_format` note in the engine).
SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "famous": {"type": "boolean"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "who": {"type": "string", "description": "Who this is, in at most twelve words; '' if not famous."},
        "parody": {"type": "string"},
    },
    "required": ["famous", "confidence", "who", "parody"],
}


def check_model(config: Optional[Mapping[str, Any]] = None) -> str:
    """The model the check uses: an explicit `models.name_check`, else the pinned default.

    NOT the run's conversation `model` — see `RUN_INDEPENDENT_ROLES` in `models.py`. The form's check
    and the create's check must give the same verdict, and the form has no run to read a model from.
    """
    from matrix_studio.models import ModelSet
    from matrix_studio.settings import get_settings

    return ModelSet.from_config(config).resolve("name_check") or get_settings().litellm_model


async def _complete(messages: List[Dict[str, str]], model: str) -> Dict[str, Any]:
    """One completion. Returns ``{content, cost_usd, tokens_in, tokens_out}``.

    The only place this module talks to a model, so the suite replaces it (`tests/conftest.py`) and no
    test can make a billable call by accident.
    """
    response = await litellm.acompletion(
        model=model,
        messages=messages,
        temperature=0.0,
        max_tokens=200,
        timeout=CALL_TIMEOUT_S,
        response_format={"type": "json_schema", "json_schema": {"name": "name_check", "schema": SCHEMA}},
    )
    usage = getattr(response, "usage", None)
    hidden = getattr(response, "_hidden_params", None)
    cost = 0.0
    if isinstance(hidden, dict) and hidden.get("response_cost") is not None:
        cost = float(hidden["response_cost"] or 0.0)
    return {
        "content": response.choices[0].message.content or "",
        "cost_usd": cost,
        "tokens_in": getattr(usage, "prompt_tokens", 0) if usage else 0,
        "tokens_out": getattr(usage, "completion_tokens", 0) if usage else 0,
    }


@dataclass(frozen=True)
class ModelVerdict:
    famous: bool
    who: str = ""
    parody: str = ""


@dataclass
class Meter:
    """What a screening spent. Charged to the owner's month by the caller."""

    cost_usd: float = 0.0
    calls: int = 0


#: (model, normalised name) -> verdict. In process: a warm Lambda or a local server asks once per name.
_CACHE: "OrderedDict[Tuple[str, str], ModelVerdict]" = OrderedDict()
_CACHE_MAX = 4096


def clear_cache() -> None:
    _CACHE.clear()


async def ask_model(name: str, model: str, meter: Meter) -> ModelVerdict:
    """Whether the model says ``name`` is a famous person's full name. Never raises.

    A failed call counts as "not famous" and is NOT cached, so the next request asks again. An
    unreadable reply also counts as "not famous", and IS cached: at temperature 0 the same question
    gets the same unreadable answer, and paying for it twice buys nothing.
    """
    key = (model, normalise(name))
    if key in _CACHE:
        _CACHE.move_to_end(key)
        return _CACHE[key]
    messages = [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": f"Name: {json.dumps(name)}"}]
    try:
        result = await _complete(messages, model)
    except Exception as exc:  # noqa: BLE001 - a check that fails must not fail the create
        logger.warning("Name check for %r failed (%s); treating it as not a public figure.", name, exc)
        return ModelVerdict(False)
    meter.calls += 1
    meter.cost_usd += float(result.get("cost_usd") or 0.0)
    obj = extract_json_object(str(result.get("content") or ""))
    if not isinstance(obj, dict) or not isinstance(obj.get("famous"), bool):
        logger.warning("Name check for %r returned an unreadable reply (%r); treating it as not a public "
                       "figure.", name, str(result.get("content") or "")[:200])
        verdict = ModelVerdict(False)
    else:
        confident = str(obj.get("confidence") or "").strip().lower() == REQUIRED_CONFIDENCE
        verdict = ModelVerdict(
            famous=bool(obj["famous"]) and confident,
            who=" ".join(str(obj.get("who") or "").split())[:120],
            parody=" ".join(str(obj.get("parody") or "").split())[:80],
        )
        if obj["famous"] and not confident:
            logger.info("Name check: %r may be a public figure (confidence %s); not renamed.",
                        name, obj.get("confidence"))
    _CACHE[key] = verdict
    while len(_CACHE) > _CACHE_MAX:
        _CACHE.popitem(last=False)
    return verdict


# --------------------------------------------------------------------------- #
# Verdicts for names
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Verdict:
    """What to do with one name. ``replacement`` is empty when the name may stay."""

    name: str
    replacement: str = ""
    source: str = ""  # "list" | "model"
    who: str = ""

    @property
    def renamed(self) -> bool:
        return bool(self.replacement)


async def _is_known(name: str, *, use_model: bool, model: str, meter: Meter) -> bool:
    """Whether a CANDIDATE replacement is itself somebody: on the list, or famous per the model."""
    if lookup(name) is not None:
        return True
    if use_model and looks_like_full_name(name):
        return (await ask_model(name, model, meter)).famous
    return False


async def check_name(name: str, *, use_model: bool = True, model: Optional[str] = None,
                     meter: Optional[Meter] = None) -> Verdict:
    """The verdict for one persona or consultant name."""
    meter = meter if meter is not None else Meter()
    fig = lookup(name)
    if fig is not None:
        return Verdict(name, fig.parody, "list", fig.group)
    if not use_model or not looks_like_full_name(name):
        return Verdict(name)
    model = model or check_model()
    verdict = await ask_model(name, model, meter)
    if not verdict.famous:
        return Verdict(name)
    # The model's parody first, re-checked; then the deterministic fallback, re-checked the same way.
    candidates = [verdict.parody] if acceptable_parody(name, verdict.parody) else []
    if verdict.parody and not candidates:
        logger.info("Name check: rejected the suggested parody %r for %r.", verdict.parody, name)
    candidates += [sound_alike(name, salt) for salt in range(0, 30, 10)]
    for cand in candidates:
        if cand and not await _is_known(cand, use_model=use_model, model=model, meter=meter):
            return Verdict(name, cand, "model", verdict.who)
    # Every candidate is somebody, which should not happen. The last fallback is still not the real name.
    return Verdict(name, candidates[-1], "model", verdict.who)


# --------------------------------------------------------------------------- #
# Applying renames to a request
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Rename:
    original: str
    replacement: str
    source: str
    role: str  # "persona" | "consultant"
    who: str = ""

    def as_dict(self) -> Dict[str, str]:
        """The API's shape: ``{from, to, reason, source, role}``."""
        return {"from": self.original, "to": self.replacement, "reason": REASON, "source": self.source,
                "role": self.role}


@dataclass
class Screening:
    renamed: List[Rename] = field(default_factory=list)
    cost_usd: float = 0.0
    calls: int = 0

    def as_list(self) -> List[Dict[str, str]]:
        return [r.as_dict() for r in self.renamed]


def _spellings(rename: Rename) -> List[str]:
    """Every way the renamed person's name might be written in the request's text, longest first."""
    out = {rename.original.strip()}
    fig = lookup(rename.original)
    if fig is not None:
        out.update(fig.spellings)
    # Accent-free versions too: "Lütke" is often typed "Lutke".
    out.update("".join(ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch))
               for s in list(out))
    return sorted((s for s in out if len(normalise(s).split()) >= 2), key=len, reverse=True)


def _pattern(spelling: str) -> "re.Pattern[str]":
    parts = [re.escape(p) for p in re.split(r"[\s\-]+", spelling.strip()) if p]
    return re.compile(r"(?<!\w)" + r"[\s\-]+".join(parts) + r"(?!\w)", re.IGNORECASE)


def rewrite_mentions(value: Any, renames: Sequence[Rename]) -> Any:
    """``value`` with every renamed person's real name replaced by their parody. Strings inside dicts
    and lists are rewritten; the input is not mutated."""
    subs = [(_pattern(s), r.replacement) for r in renames for s in _spellings(r)]
    if not subs:
        return value

    def _walk(v: Any) -> Any:
        if isinstance(v, str):
            for pattern, repl in subs:
                v = pattern.sub(lambda _m, repl=repl: repl, v)
            return v
        if isinstance(v, list):
            return [_walk(x) for x in v]
        if isinstance(v, dict):
            return {k: _walk(x) for k, x in v.items()}
        return v

    return _walk(value)


#: Keys of a cast member or consultant whose text is SOURCE material, never rewritten: a quoted passage
#: has to match its document.
_UNTOUCHED = frozenset({"documents", "document_texts", "knowledge_bases", "name"})


def _rewrite_member(member: Dict[str, Any], renames: Sequence[Rename]) -> Dict[str, Any]:
    out = dict(member)
    for key, value in member.items():
        if key not in _UNTOUCHED:
            out[key] = rewrite_mentions(value, renames)
    return out


async def screen_names(
    names: Iterable[Tuple[str, str]], *, use_model: bool = True, model: Optional[str] = None,
    taken: Iterable[str] = (),
) -> Screening:
    """Verdicts for ``(name, role)`` pairs, as renames. ``taken`` are names a replacement must not equal.

    Model calls for different names run concurrently, bounded by `CONCURRENCY`. Replacements are made
    unique against ``taken`` and each other, so a rename can never collide with somebody already in
    the cast — two personas sharing a name would silently become one agent.
    """
    pairs: List[Tuple[str, str]] = []
    seen: set = set()
    for name, role in names:
        n = str(name or "").strip()
        if n and n.lower() not in seen:
            seen.add(n.lower())
            pairs.append((n, role))
    meter = Meter()
    model = model or check_model()
    gate = asyncio.Semaphore(CONCURRENCY)

    async def _one(name: str) -> Verdict:
        async with gate:
            return await check_name(name, use_model=use_model, model=model, meter=meter)

    verdicts = await asyncio.gather(*(_one(n) for n, _role in pairs))
    used = {str(t).strip().lower() for t in taken if str(t or "").strip()}
    used.update(n.lower() for n, _r in pairs)
    renames: List[Rename] = []
    for (name, role), v in zip(pairs, verdicts):
        if not v.renamed:
            continue
        replacement, salt = v.replacement, 0
        while replacement.lower() in used and salt < 50:
            salt += 1
            replacement = sound_alike(name, salt * 7)
        used.add(replacement.lower())
        renames.append(Rename(name, replacement, v.source, role, v.who))
    if renames:
        logger.info("Renamed %d real public figure(s): %s", len(renames),
                    ", ".join(f"{r.original!r} -> {r.replacement!r} ({r.source})" for r in renames))
    return Screening(renames, round(meter.cost_usd, 6), meter.calls)


async def screen_cast(
    cast: Sequence[Dict[str, Any]], *, use_model: bool = True, model: Optional[str] = None,
    taken: Iterable[str] = (), experts: Sequence[Dict[str, Any]] = (),
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Screening]:
    """A cast (and its consultants) with real names replaced, and what was replaced."""
    names = [(str(c.get("name") or ""), "persona") for c in cast if isinstance(c, dict)]
    names += [(str(e.get("name") or ""), "consultant") for e in experts if isinstance(e, dict)]
    screening = await screen_names(names, use_model=use_model, model=model, taken=taken)
    by_name = {r.original.lower(): r.replacement for r in screening.renamed}

    def _fix(member: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(member, dict):
            return member
        out = _rewrite_member(member, screening.renamed)
        name = str(member.get("name") or "").strip()
        if name.lower() in by_name:
            out["name"] = by_name[name.lower()]
        return out

    return [_fix(c) for c in cast], [_fix(e) for e in experts], screening


async def screen_request(
    request: Mapping[str, Any], *, use_model: bool = True, model: Optional[str] = None,
) -> Tuple[Dict[str, Any], Screening]:
    """A run or ensemble request with real names replaced throughout, and what was replaced.

    The one function each entry point calls. Never mutates ``request``. Renames the cast and the
    consultants (`config.experts`); rewrites mentions in their texts, the topic, the working
    assumptions and the scheduled messages (whose speaker follows a renamed persona exactly).
    """
    out = copy.deepcopy(dict(request))
    config = dict(out.get("config") or {})
    cast, experts, screening = await screen_cast(
        list(out.get("cast") or []), use_model=use_model,
        model=model or check_model(config), experts=list(config.get("experts") or []),
    )
    if not screening.renamed:
        return out, screening
    renames = screening.renamed
    by_name = {r.original.lower(): r.replacement for r in renames}
    out["cast"] = cast
    if config.get("experts"):
        config["experts"] = experts
    if isinstance(out.get("topic"), str):
        out["topic"] = rewrite_mentions(out["topic"], renames)
    if config.get("assumptions"):
        config["assumptions"] = rewrite_mentions(config["assumptions"], renames)
    if config.get("injections"):
        injections = []
        for inj in config["injections"]:
            if isinstance(inj, dict):
                inj = dict(inj)
                who = str(inj.get("speaker") or "").strip().lower()
                if who in by_name:
                    inj["speaker"] = by_name[who]
                if isinstance(inj.get("content"), str):
                    inj["content"] = rewrite_mentions(inj["content"], renames)
            injections.append(inj)
        config["injections"] = injections
    if "config" in out:
        out["config"] = config
    return out, screening


async def record_spend(db: Any, owner_sub: Optional[str], cost: float) -> None:
    """Charge a screening's model calls to the owner's month, as every other model call is.

    Best-effort, for the reason `orchestration.record_spend` gives: a missed increment delays the cap
    rather than losing a run that has already been checked.
    """
    if cost <= 0 or not owner_sub or db is None:
        return
    try:
        await db.add_user_spend(cost, owner_sub=owner_sub)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not record $%.6f of name-check spend for %s: %s", cost, owner_sub, exc)

# SPDX-License-Identifier: Apache-2.0
"""A one-page decision brief for a conversation or an ensemble — what a leadership reader gets.

The organising rule is one a brainstorm run of this tool settled on: a single confidence number
would be indefensible in front of a leadership reader, so

    No number without its group and replicate count. Where either is missing, it says
    "not yet measured".

For a SINGLE conversation that means the confidence line is always "not yet measured": one run is
one draw, and `ENSEMBLE-CONVERSATIONS.md` §3.3 is explicit that differences at n = 1 are
uninterpretable. The brief says how to get a measurement — run it as an ensemble — rather than
inventing one.

For an ENSEMBLE the brief leads with conclusions that RECURRED, counted per group and never pooled
(§4: "4 of 5 base, 1 of 4 hybrid" is a method-dependent finding; "5 of 9" would call it a weak
split). If nothing recurred, that is the headline, not a buried footnote.

## One page, and it says when it is not the whole story

Every list is capped, and a cap that cut something says how much ("and 12 more in the full report").
A brief that silently drops the eleventh open question reads as having only ten.

## Built on the export's data model

`export.run_model` / `export.ensemble_model` already gather everything from stored data, omit the
operator-private persona fields, and carry the whole cost. The brief is a different VIEW of that
model, so it inherits those guarantees instead of re-implementing them — and the HTML goes through
the same escaping, because a brief is also a file somebody opens in a browser.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from matrix_studio import export as ex

#: Caps that keep the brief to one page. Each cut is stated in the brief itself.
MAX_CONCLUSIONS = 5
MAX_AGREED = 4
MAX_OPEN = 4
MAX_DISSENT = 3
#: Evidence requests shown, one clipped line each. The full table (with what each unlocks and the
#: cheapest way to get it) is in the report; here it has to share one page.
MAX_EVIDENCE = 3
MAX_ASSUMPTIONS = 3
MAX_ASSUMPTION_CHARS = 80
#: "Where they agreed" gives up two items to the assumptions it may rest on. Measured: a brief at every
#: cap with assumptions was 723 words against the one-page bound of 650; with these caps, 645.
MAX_AGREED_WITH_ASSUMPTIONS = 2
MAX_EVIDENCE_CHARS = 150
#: Open questions and objections shown when there IS an evidence plan. The plan is the specific form
#: of what is open — which data, what result, what to do meanwhile — and restates much of what the
#: objections turn on, so the vaguer lists give up space to it. Measured: without this, a brief at
#: every cap grew from 629 to 730 words, past one printed page.
MAX_OPEN_WITH_PLAN = 1
MAX_DISSENT_WITH_PLAN = 2
MAX_CONDITIONAL_CHARS = 260
MAX_QUESTION_CHARS = 320
#: Per-item and bottom-line ceilings. Measured: an Opus brainstorm's brief was 812 words with
#: whole-paragraph dissents — past one page. A clipped item ends in "…", so it never reads as
#: the complete text; the full report has it.
MAX_ITEM_CHARS = 170
MAX_BOTTOM_LINE_CHARS = 600

NOT_MEASURED = "not yet measured"

#: `analysis.NOT_STATED`, repeated rather than imported so the brief does not pull in the model layer.
NOT_STATED = "not stated"

#: The brief's own analysis label. The export's says "the transcript(s) above", which a brief does
#: not have, so it would point a reader at text that is not there.
BRIEF_LABEL = (
    "Model-generated analysis of the conversation transcript(s), not a record of them. It can be "
    "wrong — check it against the full report before relying on it."
)
NO_AGREEMENT = "Nothing was held by every run of any group."


def _cap(items: Sequence[Any], n: int) -> Tuple[List[Any], int]:
    items = list(items or [])
    return items[:n], max(0, len(items) - n)


def _text(item: Any) -> str:
    """A summary list item as one line. Key ideas are sometimes structured objects."""
    if isinstance(item, dict):
        for key in ("idea", "claim", "text", "position", "question"):
            if item.get(key):
                who = item.get("proposed_by") or item.get("speaker")
                return f"{item[key]}" + (f" — {who}" if who else "")
        return "; ".join(str(v) for v in item.values() if isinstance(v, str))[:300]
    return str(item)


def _clip(text: Any, n: int = 0) -> str:
    text = " ".join(str(text or "").split())
    n = n or MAX_ITEM_CHARS
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0].rstrip(",;:") + "…"


def _question(topic: str) -> str:
    topic = " ".join(str(topic or "").split())
    return topic if len(topic) <= MAX_QUESTION_CHARS else topic[:MAX_QUESTION_CHARS].rstrip() + "…"


# --------------------------------------------------------------------------- #
# Building
# --------------------------------------------------------------------------- #


def run_brief(model: Dict[str, Any]) -> Dict[str, Any]:
    """The brief for ONE conversation, from `export.run_model`'s output."""
    s = model.get("summary") or {}
    agreed, agreed_more = _cap([_clip(_text(x)) for x in s.get("consensus") or []],
                               MAX_AGREED_WITH_ASSUMPTIONS if model.get("assumptions") else MAX_AGREED)
    plan = [row for row in s.get("evidence_plan") or [] if isinstance(row, dict) and row.get("data")]
    open_, open_more = _cap([_clip(_text(x)) for x in s.get("open_questions") or []],
                            MAX_OPEN_WITH_PLAN if plan else MAX_OPEN)
    dissent, dissent_more = _cap(
        [_clip(f"{d.get('speaker')}: {d.get('position')}") for d in s.get("dissenters") or []
         if isinstance(d, dict)], MAX_DISSENT_WITH_PLAN if plan else MAX_DISSENT)
    evidence, evidence_more = _cap([
        _clip(f"{row.get('data')} ({row.get('asked_by') or NOT_STATED}) — moves them: "
              f"{row.get('moves_them') or NOT_STATED}; best guess: {row.get('best_guess') or NOT_STATED}",
              MAX_EVIDENCE_CHARS)
        for row in plan
    ], MAX_EVIDENCE)
    converged = model.get("converged")
    return {
        "kind": "run",
        "id": model["id"],
        "name": model["name"],
        "question": _question(model.get("topic")),
        "has_analysis": bool(s),
        "bottom_line": _clip(s.get("overview"), MAX_BOTTOM_LINE_CHARS),
        "agreed": agreed, "agreed_more": agreed_more,
        "open": open_, "open_more": open_more,
        "dissent": dissent, "dissent_more": dissent_more,
        # What would settle it, and what to do until then. Built from what the cast said; a column
        # they never supplied reads "not stated", which is the gap worth seeing.
        "evidence": evidence, "evidence_more": evidence_more,
        "conditional": _clip(s.get("conditional_recommendation"), MAX_CONDITIONAL_CHARS),
        # What the conclusion rests on. Capped like everything else, and the cut is said.
        **dict(zip(("assumptions", "assumptions_more"), _cap(
            # Statement only; the basis and who set it are in the full report.
            # Disputed ones first: a reader who sees only the top of this list must see those.
            # Who disputed it, and the words, are in the full report; here only that it was.
            [_clip(f"{a.get('id')}" + (f" (disputed ×{len(a['disputes'])})" if a.get("disputes") else "")
                   + f": {a.get('statement')}", MAX_ASSUMPTION_CHARS + (6 if a.get("disputes") else 0))
             for a in sorted(model.get("assumptions") or [], key=lambda a: not a.get("disputes"))],
            MAX_ASSUMPTIONS))),
        # Never a percentage. One conversation is one draw.
        "confidence": (
            f"{NOT_MEASURED} — this is one conversation, and one run cannot say which of its "
            "conclusions would recur. Run the same brief as an ensemble to measure that."
        ),
        "facts": [
            f"{len(model.get('cast') or [])} personas, {model.get('turn_count') or 0} turns"
            + (f", converged at turn {converged.get('at_turn')}" if converged else ""),
            *[line for line in model.get("settings") or [] if line.startswith("Personas' voice")],
            f"Cost {ex._money(model.get('cost_usd'))}",
        ],
        "created_at": model.get("created_at"),
        "exported_at": model.get("exported_at"),
    }


def ensemble_brief(model: Dict[str, Any]) -> Dict[str, Any]:
    """The brief for an ENSEMBLE, from `export.ensemble_model`'s output."""
    report = model.get("report") or {}
    cells = [c for c in model.get("cells") or [] if c]
    members = model.get("members") or []
    finished = sum(1 for m in members if m.get("status") == "complete")
    brief: Dict[str, Any] = {
        "kind": "ensemble",
        "id": model["id"],
        "name": model["name"],
        "question": _question(model.get("topic")),
        "cells": cells,
        "has_analysis": bool(report),
        "facts": [
            f"{len(members)} conversations in {len(cells)} group(s): "
            + ", ".join(f"{c.get('label')} ×{c.get('n')}" for c in model.get("spec") or []),
            f"{finished} of {len(members)} finished",
        ],
        "created_at": model.get("created_at"),
        "exported_at": model.get("exported_at"),
    }
    if not report:
        brief["state"] = "no_report"
        return brief

    cv = ex.conclusions_view(report, cells)
    brief["state"] = cv["state"]
    brief["conclusions"], brief["conclusions_more"] = _cap(cv["recurring"], MAX_CONCLUSIONS)
    brief["single_run_conclusions"] = len(cv["single"])
    brief["agreed"], brief["agreed_more"] = _cap(cv["agreed"], MAX_AGREED)
    unresolved = ((report.get("agreements") or {}).get("unresolved_by_frequency")) or []
    brief["open"], brief["open_more"] = _cap(
        [(len(u.get("runs") or []), _clip(u.get("claim"))) for u in unresolved], MAX_OPEN)
    usable = {c.get("cell"): c.get("usable") for c in report.get("cells") or []}
    brief["confidence"] = (
        "Counted per group, against that group's own runs: "
        + "; ".join(f"{c}: {usable.get(c, NOT_MEASURED)} usable run(s)" for c in cells)
        + ". A conclusion reached in one run only is not a finding and is not listed above."
    )
    return brief


# --------------------------------------------------------------------------- #
# Markdown
# --------------------------------------------------------------------------- #


def _more(n: int) -> List[str]:
    return [f"- *…and {n} more in the full report.*"] if n else []


def render_markdown(b: Dict[str, Any]) -> str:
    title = f"# Decision brief — {b['name']}" + (" (ensemble)" if b["kind"] == "ensemble" else "")
    out = [title, "", f"**The question.** {b['question']}", ""]
    if b.get("has_analysis"):
        out += [f"> *{BRIEF_LABEL}*", ""]

    if b["kind"] == "run":
        out += ["## Bottom line", "", b["bottom_line"] or "*No summary has been generated for this "
                "conversation yet.*", ""]
        if b["agreed"]:
            out += ["## Where they agreed", ""] + [f"- {x}" for x in b["agreed"]] + _more(b["agreed_more"]) + [""]
    else:
        out += ["## What the runs concluded", ""]
        state = b.get("state")
        if state == "no_report":
            out += ["*No report has been generated for this ensemble yet.*", ""]
        elif state in ex._CONCLUSION_NOTES and state != "recurring":
            out += [ex._CONCLUSION_NOTES[state], ""]
        if b.get("conclusions"):
            cells = b["cells"]
            out += ["| conclusion | " + " | ".join(ex._md_cell(c) for c in cells) + " |",
                    "|---|" + "---|" * len(cells)]
            for cl in b["conclusions"]:
                per = cl.get("per_cell") or {}
                out.append(f"| {ex._md_cell(cl.get('claim'))} | "
                           + " | ".join(ex._cell_count(per.get(c)) for c in cells) + " |")
            out += _more(b.get("conclusions_more", 0)) + [""]
        if b.get("single_run_conclusions"):
            out += [f"*{b['single_run_conclusions']} further conclusion(s) were never reached twice "
                    "within any group — rare, not findings — and are in the full report.*", ""]
        if b.get("state") not in (None, "no_report") and not b.get("agreed"):
            # Said, not omitted: for a leadership reader, "nothing was unanimous" is a finding.
            out += ["## What every run in a group agreed on", "", NO_AGREEMENT, ""]
        if b.get("agreed"):
            out += ["## What every run in a group agreed on", ""]
            for a in b["agreed"]:
                where = ", ".join(f"{c}: all {(a['claim']['per_cell'].get(c) or {}).get('of')}" for c in a["cells"])
                out.append(f"- {a['claim'].get('claim')} ({where})")
            out += _more(b.get("agreed_more", 0)) + [""]

    if b.get("open"):
        out += ["## Still open", ""]
        if b["kind"] == "run":
            out += [f"- {x}" for x in b["open"]]
        else:
            out += [f"- ({n} run(s)) {x}" for n, x in b["open"]]
        out += _more(b.get("open_more", 0)) + [""]
    if b.get("dissent"):
        out += ["## Standing objections", ""] + [f"- {x}" for x in b["dissent"]] + _more(b["dissent_more"]) + [""]
    if b.get("assumptions"):
        out += (["## Assumed, not established", ""] + [f"- {x}" for x in b["assumptions"]]
                + _more(b.get("assumptions_more", 0)) + [""])
    if b.get("conditional") or b.get("evidence"):
        out += ["## What would settle it", ""]
        if b.get("conditional"):
            out += [f"**Meanwhile:** {b['conditional']}", ""]
        if b.get("evidence"):
            out += [f"- {x}" for x in b["evidence"]] + _more(b.get("evidence_more", 0)) + [""]

    out += ["## How much to trust this", "", f"**Confidence:** {b.get('confidence', NOT_MEASURED)}", ""]
    out += [f"<sub>{' · '.join(b['facts'])} · `{b['id']}` · created {ex._when(b.get('created_at'))} · "
            f"brief generated {ex._when(b.get('exported_at'))}</sub>"]
    return "\n".join(out).rstrip() + "\n"


# --------------------------------------------------------------------------- #
# HTML — one printed page
# --------------------------------------------------------------------------- #

_CSS = """
body{font:14px/1.45 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:760px;margin:1.6em auto;
padding:0 1.1em;color:#1f2933}
h1{font-size:1.35em;margin:0 0 .3em}h2{font-size:1.02em;margin:1.1em 0 .35em;color:#243b53;
border-bottom:1px solid #d9dee3;padding-bottom:.15em}
.q{color:#334e68;margin:.2em 0 .6em}.label{font-style:italic;color:#8d6708;font-size:.85em;
border-left:3px solid #f0b429;padding:.2em .6em;background:#fffbea;margin:.4em 0}
ul{margin:.2em 0;padding-left:1.2em}li{margin:.12em 0}.more{color:#627d98;font-style:italic}
table{border-collapse:collapse;width:100%;font-size:.9em;margin:.3em 0}
th,td{border:1px solid #d9dee3;padding:.25em .45em;text-align:left;vertical-align:top}th{background:#f5f7fa}
.trust{background:#f0f4f8;border-radius:4px;padding:.45em .7em}.foot{color:#829ab1;font-size:.78em;margin-top:1.2em}
.bottom{white-space:pre-wrap}
@page{size:auto;margin:14mm}
@media print{body{margin:0;max-width:none;font-size:12.5px}h2{break-after:avoid}table,ul{break-inside:avoid}}
"""


def _ul(items: Sequence[str], more: int = 0) -> str:
    li = "".join(f"<li>{ex._e(i)}</li>" for i in items)
    if more:
        li += f"<li class=\"more\">…and {more} more in the full report.</li>"
    return f"<ul>{li}</ul>"


def render_html(b: Dict[str, Any]) -> str:
    e = ex._e
    body = [f"<h1>Decision brief — {e(b['name'])}{' (ensemble)' if b['kind'] == 'ensemble' else ''}</h1>",
            f"<p class=\"q\"><strong>The question.</strong> {e(b['question'])}</p>"]
    if b.get("has_analysis"):
        body.append(f"<div class=\"label\">{e(BRIEF_LABEL)}</div>")

    if b["kind"] == "run":
        body += ["<h2>Bottom line</h2>",
                 f"<p class=\"bottom\">{e(b['bottom_line'])}</p>" if b["bottom_line"]
                 else "<p><em>No summary has been generated for this conversation yet.</em></p>"]
        if b["agreed"]:
            body += ["<h2>Where they agreed</h2>", _ul(b["agreed"], b["agreed_more"])]
    else:
        body.append("<h2>What the runs concluded</h2>")
        state = b.get("state")
        if state == "no_report":
            body.append("<p><em>No report has been generated for this ensemble yet.</em></p>")
        elif state in ex._CONCLUSION_NOTES and state != "recurring":
            body.append(f"<p>{e(ex._CONCLUSION_NOTES[state])}</p>")
        if b.get("conclusions"):
            cells = b["cells"]
            rows = "".join(
                f"<tr><td>{e(cl.get('claim'))}</td>"
                + "".join(f"<td>{e(ex._cell_count((cl.get('per_cell') or {}).get(c)))}</td>" for c in cells)
                + "</tr>" for cl in b["conclusions"])
            body.append("<table><tr><th>conclusion</th>" + "".join(f"<th>{e(c)}</th>" for c in cells)
                        + f"</tr>{rows}</table>")
            if b.get("conclusions_more"):
                body.append(f"<p class=\"more\">…and {b['conclusions_more']} more in the full report.</p>")
        if b.get("single_run_conclusions"):
            body.append(f"<p class=\"more\">{b['single_run_conclusions']} further conclusion(s) were never "
                        "reached twice within any group — rare, not findings — and are in the full report.</p>")
        if b.get("state") not in (None, "no_report") and not b.get("agreed"):
            body += ["<h2>What every run in a group agreed on</h2>", f"<p>{e(NO_AGREEMENT)}</p>"]
        if b.get("agreed"):
            body += ["<h2>What every run in a group agreed on</h2>", _ul([
                f"{a['claim'].get('claim')} ("
                + ", ".join(f"{c}: all {(a['claim']['per_cell'].get(c) or {}).get('of')}" for c in a["cells"]) + ")"
                for a in b["agreed"]], b.get("agreed_more", 0))]

    if b.get("open"):
        items = b["open"] if b["kind"] == "run" else [f"({n} run(s)) {x}" for n, x in b["open"]]
        body += ["<h2>Still open</h2>", _ul(items, b.get("open_more", 0))]
    if b.get("dissent"):
        body += ["<h2>Standing objections</h2>", _ul(b["dissent"], b.get("dissent_more", 0))]
    if b.get("assumptions"):
        body += ["<h2>Assumed, not established</h2>", _ul(b["assumptions"], b.get("assumptions_more", 0))]
    if b.get("conditional") or b.get("evidence"):
        body.append("<h2>What would settle it</h2>")
        if b.get("conditional"):
            body.append(f"<p><strong>Meanwhile:</strong> {e(b['conditional'])}</p>")
        if b.get("evidence"):
            body.append(_ul(b["evidence"], b.get("evidence_more", 0)))

    body += ["<h2>How much to trust this</h2>",
             f"<div class=\"trust\"><strong>Confidence:</strong> {e(b.get('confidence', NOT_MEASURED))}</div>",
             f"<p class=\"foot\">{e(' · '.join(b['facts']))} · {e(b['id'])} · created "
             f"{e(ex._when(b.get('created_at')))} · brief generated {e(ex._when(b.get('exported_at')))}</p>"]
    return ex._doc(f"Decision brief — {b['name']}", body).replace(ex._CSS, _CSS)


def render(b: Dict[str, Any], fmt: str) -> str:
    if fmt == "md":
        return render_markdown(b)
    if fmt == "html":
        return render_html(b)
    raise ValueError(f"unknown brief format {fmt!r}; known: md, html")


def filename(b: Dict[str, Any], fmt: str) -> str:
    return ex.filename(b, fmt).replace(f"-{b['kind']}.", f"-{b['kind']}-brief.")

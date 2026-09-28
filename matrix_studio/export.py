# SPDX-License-Identifier: Apache-2.0
"""Export a conversation, or an ensemble, as Markdown or a self-contained HTML file.

`private/private/docs/BACKLOG.md` (kept out of git) asked for Markdown, HTML and PDF. The split is: this module builds ONE plain data
model from stored data and renders it two ways. PDF is the browser printing the HTML export — see
"Why PDF is print" below.

## Three rules an export must keep, because a flat file loses what the UI enforces

**It must not flatten an ensemble's cells.** `ENSEMBLE-CONVERSATIONS.md` §4: the same "5 of 9" is a
strong method-dependent finding or a coin flip depending on how it splits across cells, so the claim
table has one column per cell and NO pooled total. A Markdown table is exactly where somebody adds a
total for tidiness; `ClaimTable.test.tsx` asserts its absence in the UI and `tests/test_export.py`
asserts it here.

**Model-generated analysis is labelled in the TEXT.** The summary and the ensemble synthesis are
analysis of transcripts, visually distinct in the UI and never presented as ground truth. A file
loses the styling that carries that distinction, and an exported PDF is the artefact most likely to
be read by somebody who never saw the app. So the label is words, next to the analysis.

**Operator-private persona fields never leave.** A viewpoint's `underlying_concern` (withheld from
the conversation) and `validity` (the operator's private calibration note, never rendered into any
prompt) are omitted, exactly as the dossier API omits them. A file sent outside the tool is the worst
place for either to appear.

## Model text is escaped in HTML

A transcript is model output. The HTML export is a file someone will open in a browser, so every
piece of stored text goes through `html.escape` — no Markdown-to-HTML conversion of model text, which
would need a sanitiser this project does not carry. Summaries and syntheses keep their line breaks
via `white-space: pre-wrap` instead.

## Why PDF is print

Every serious server-side PDF route is either a headless browser in Lambda (a Chromium layer, a much
larger image, slower cold starts) or native libraries (WeasyPrint needs Pango) the image does not
have. Printing the self-contained HTML — which carries its own print stylesheet — gives a real PDF
with no new infrastructure. The cost is that its quality is the reader's browser's print engine;
the backlog named this "the cheap version" and it was chosen deliberately.
"""

from __future__ import annotations

import html
import json
import re
import time
from typing import Any, Dict, List, Optional, Sequence

#: The words that go next to any model-generated analysis, in both formats.
ANALYSIS_LABEL = (
    "Model-generated analysis of the transcript(s) above. It is a reading of what was said, not a "
    "record of it, and it can be wrong — check it against the transcript before relying on it."
)

#: Viewpoint fields that are the operator's and never leave the tool. Mirrors the dossier's guard.
PRIVATE_VIEWPOINT_FIELDS = frozenset({"underlying_concern", "validity"})

FORMATS = {"md": "text/markdown; charset=utf-8", "html": "text/html; charset=utf-8"}


# --------------------------------------------------------------------------- #
# Building the model
# --------------------------------------------------------------------------- #


def _payload(event: Dict[str, Any]) -> Dict[str, Any]:
    p = event.get("payload")
    if isinstance(p, str):
        try:
            return json.loads(p)
        except json.JSONDecodeError:
            return {}
    return p or {}


def _json(raw: Any, default: Any) -> Any:
    if not raw:
        return default
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def public_cast(cast: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The cast as an export may show it: persona, goals, and viewpoints WITHOUT private fields."""
    out = []
    for member in cast or []:
        structured = member.get("structured") or {}
        viewpoints = []
        for vp in structured.get("viewpoints") or []:
            if not isinstance(vp, dict):
                continue
            viewpoints.append({k: v for k, v in vp.items() if k not in PRIVATE_VIEWPOINT_FIELDS})
        out.append({
            "name": member.get("name") or "",
            "persona": member.get("persona") or "",
            "goals": list(member.get("goals") or []),
            "role": structured.get("role"),
            "viewpoints": viewpoints,
        })
    return out


def settings_lines(config: Dict[str, Any]) -> List[str]:
    """The settings a reader needs to interpret the transcript, as plain sentences."""
    lines = []
    if config.get("max_messages"):
        sel = config.get("selection") or {}
        ceiling = " (a ceiling: the run could stop when the room converged)" if sel.get(
            "stop_when_converged") else ""
        lines.append(f"Turn budget: {config['max_messages']}{ceiling}")
    sel = config.get("selection") or {}
    lines.append(f"Speaker selection: {sel.get('method') or 'moderated'}")
    retrieval = config.get("retrieval") or {}
    if retrieval.get("enabled"):
        extra = []
        if retrieval.get("authority_floor"):
            extra.append("a reserved slot for controlling authority")
        if retrieval.get("standing_query"):
            extra.append("a standing query from each persona's stated conditions")
        lines.append(
            f"Document retrieval: on, {retrieval.get('k', 3)} passages per turn"
            + (f", with {' and '.join(extra)}" if extra else "")
        )
    else:
        lines.append("Document retrieval: off")
    research = config.get("research") or {}
    lines.append("Pre-conversation research: " + ("on" if research.get("enabled") else "off"))
    cognition = config.get("cognition") or {}
    lines.append("Cognition (memory, reflection, goals): " + ("on" if cognition.get("enabled") else "off"))
    # The RESOLVED voice model, not `config.get("model")`: with nothing configured the voice is the
    # deployment default, and printing nothing then would hide what actually spoke.
    from matrix_studio.models import ModelSet

    voice = ModelSet.from_config(config).as_dict().get("voice")
    if voice:
        lines.append(f"Personas' voice model: {voice.rsplit('/', 1)[-1].split('anthropic.')[-1]}")
    return lines


async def run_model(db: Any, run: Dict[str, Any]) -> Dict[str, Any]:
    """Everything a run export shows, from stored data only. Makes no model call."""
    run_id = str(run["id"])
    config = _json(run.get("config_json"), {})
    events = sorted(await db.get_events(run_id), key=lambda e: (e.get("turn") or 0, e.get("seq") or 0))
    stats = await db.get_run_stats(run_id)

    # A turn's retrieval is emitted before its response, so passages are keyed (turn, speaker) and
    # attached to the message that used them — simultaneous mode can have several speakers per turn.
    retrieved: Dict[Any, List[Dict[str, Any]]] = {}
    transcript: List[Dict[str, Any]] = []
    converged: Optional[Dict[str, Any]] = None
    for e in events:
        p = _payload(e)
        if e["event_type"] == "document.retrieved":
            key = (e.get("turn"), p.get("speaker") or e.get("agent_name"))
            retrieved[key] = [
                {k: s.get(k) for k in ("title", "ordinal", "origin", "authority")}
                for s in p.get("passages") or []
            ]
        elif e["event_type"] == "agent.response":
            speaker = p.get("speaker") or e.get("agent_name") or ""
            transcript.append({
                "turn": e.get("turn"),
                "speaker": speaker,
                "message": str(p.get("message") or ""),
                "passages": retrieved.get((e.get("turn"), speaker), []),
            })
        elif e["event_type"] == "expert.answered":
            # A consultant's answer is part of what the room read, so it is part of the record — marked
            # as a consultant's, with the question it answered, so no reader mistakes it for a participant.
            expert = str(p.get("expert") or e.get("agent_name") or "")
            transcript.append({
                "turn": e.get("turn"),
                "speaker": str(p.get("speaker") or f"{expert} (consultant)"),
                "message": f"Asked by {p.get('asked_by')}: \u201c{p.get('question')}\u201d\n\n{p.get('answer') or ''}",
                "passages": retrieved.get((e.get("turn"), expert), []),
            })
        elif e["event_type"] == "sim.completed" and p.get("converged"):
            converged = {"at_turn": p.get("converged_at_turn"), "reason": p.get("converged_reason")}

    summaries = await db.get_summaries(run_id)
    generated = next((s for s in summaries if s.get("kind") == "generated"), None)
    research = _json(run.get("research_json"), None)
    # The whole cost, as the run page reports it: in-run calls from the event log, plus the
    # summary and the research pass, which are stored outside it.
    whole_cost = (float(stats.get("total_cost_usd") or 0.0)
                  + float((generated or {}).get("cost_usd") or 0.0)
                  + float((research or {}).get("cost_usd") or 0.0))
    return {
        "kind": "run",
        "id": run_id,
        "name": run.get("name") or run_id,
        "description": run.get("description"),
        "topic": run.get("topic") or "",
        "status": run.get("status"),
        "created_at": run.get("created_at"),
        "turn_count": stats.get("turn_count"),
        "cost_usd": whole_cost,
        "converged": converged,
        "cast": public_cast(_json(run.get("cast_json"), [])),
        "settings": settings_lines(config),
        "research": research,
        "transcript": transcript,
        "summary": (generated or {}).get("payload"),
        "exported_at": int(time.time()),
    }


async def ensemble_model(db: Any, parent: Dict[str, Any]) -> Dict[str, Any]:
    """Everything an ensemble export shows. The report is read as stored, never regenerated."""
    members = []
    for m in await db.list_ensemble_members(parent["id"]):
        run = m.get("run") or {}
        members.append({
            "cell": m.get("cell"), "index": m.get("index"),
            "name": run.get("name") or m.get("run_id"),
            "status": run.get("status") or "never started",
            "turn_count": run.get("turn_count"),
            "cost_usd": run.get("total_cost_usd"),
        })
    report = _json(parent.get("report_json"), None)
    spec = _json(parent.get("spec_json"), [])
    return {
        "kind": "ensemble",
        "id": parent["id"],
        "name": parent.get("name") or parent["id"],
        "description": parent.get("description"),
        "topic": parent.get("topic") or "",
        "status": parent.get("status"),
        "created_at": parent.get("created_at"),
        "cells": [c.get("label") for c in spec],
        "spec": spec,
        "members": members,
        "research": _json(parent.get("research_json"), None),
        "report": report,
        "exported_at": int(time.time()),
    }


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #


def _when(ts: Any) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(int(ts)))
    except (TypeError, ValueError):
        return "—"


def _money(v: Any) -> str:
    try:
        return f"${float(v):.2f}"
    except (TypeError, ValueError):
        return "—"


def _cell_count(entry: Optional[Dict[str, Any]]) -> str:
    """One cell's count for one claim. `—` for a cell that produced nothing, never `0 of N`."""
    if not entry or not entry.get("of"):
        return "—"
    return f"{entry.get('held', 0)} of {entry['of']}"


def filename(model: Dict[str, Any], fmt: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(model.get("name") or model["id"]).lower()).strip("-")
    return f"{slug or model['kind']}-{model['kind']}.{fmt}"


def _research_lines(research: Optional[Dict[str, Any]]) -> List[str]:
    if not research:
        return []
    scopes = research.get("scopes") or []
    docs = sum(int(s.get("documents") or 0) for s in scopes)
    ctrl = sum(int(s.get("controlling") or 0) for s in scopes)
    negs = sum((1 if s.get("negative") else 0) + int(s.get("query_negatives") or 0) for s in scopes)
    lines = [f"Status: {research.get('status')}"
             + (f", via {research['provider']}" if research.get("provider") else "")
             + (f", {_money(research.get('cost_usd'))}" if research.get("cost_usd") is not None else "")]
    if scopes:
        lines.append(f"{docs} sources, {ctrl} tiered as controlling authority, "
                     f"{negs} documented negative(s) — searches that found no controlling authority.")
    if research.get("error"):
        lines.append(f"Note: {research['error']}")
    return lines


def conclusions_view(report: Dict[str, Any], cells: Sequence[str]) -> Dict[str, Any]:
    """What the runs concluded and agreed on — the export's version of the view's panel.

    ``state`` is one of:

    - ``predates`` — the report was built before conclusions were extracted. NOT the same as
      "concluded nothing", which is a finding, so the two must not render alike.
    - ``none`` — no run reached a conclusion.
    - ``diverged`` — conclusions exist and none recurred in two runs of any group. The divergence is
      the result; no conclusion is promoted to "the" conclusion.
    - ``recurring`` — at least one conclusion was reached in two or more runs of a group.

    Agreements are derived from the claim table (a claim `unanimous` in some group), not stored,
    so the two cannot disagree.
    """
    conclusions = report.get("conclusions")
    agreed = []
    for cl in report.get("claims") or []:
        per = cl.get("per_cell") or {}
        in_cells = [c for c in cells if (per.get(c) or {}).get("tier") == "unanimous"]
        if in_cells:
            agreed.append({"claim": cl, "cells": in_cells})
    if conclusions is None:
        state = "predates"
    elif not conclusions:
        state = "none"
    elif not any(
        (cell or {}).get("held", 0) >= 2
        for c in conclusions for cell in (c.get("per_cell") or {}).values()
    ):
        state = "diverged"
    else:
        state = "recurring"
    # Split by replication, the boundary `ensemble_spec.tier` uses: a conclusion reached in ONE run
    # of every group is `rare` — not a finding — and measured on renewal-cells it was 35 of 41, so
    # listing them together buried the six that recurred.
    recurring = [c for c in conclusions or []
                 if any((cell or {}).get("held", 0) >= 2 for cell in (c.get("per_cell") or {}).values())]
    single = [c for c in conclusions or [] if c not in recurring]
    return {"state": state, "conclusions": conclusions or [], "recurring": recurring,
            "single": single, "agreed": agreed}


_CONCLUSION_NOTES = {
    "predates": "This report was built before conclusions were extracted. Regenerate it to see "
                "what the runs concluded.",
    "none": "No run reached a conclusion. Every conversation ended without the group deciding "
            "anything — which is itself the finding.",
    "diverged": "No conclusion recurred: no two runs in any group concluded the same thing. The "
                "runs diverged, and that divergence is the result — none of the conclusions below "
                "is \"the\" conclusion.",
}


# --------------------------------------------------------------------------- #
# Markdown
# --------------------------------------------------------------------------- #


def _md_cell(text: Any) -> str:
    """Safe inside a Markdown table cell: pipes escaped, line breaks flattened."""
    return str(text if text is not None else "").replace("|", "\\|").replace("\n", " ").strip()


def _md_summary(summary: Dict[str, Any]) -> List[str]:
    out = ["## Summary", "", f"> *{ANALYSIS_LABEL}*", ""]
    if summary.get("overview"):
        out += [str(summary["overview"]), ""]
    for key, heading in (("key_ideas", "Key ideas"), ("consensus", "Where they agreed"),
                         ("open_questions", "Left open")):
        items = summary.get(key) or []
        if items:
            out += [f"### {heading}", ""] + [f"- {i}" for i in items] + [""]
    dissent = summary.get("dissenters") or []
    if dissent:
        out += ["### Dissent", ""]
        out += [f"- **{d.get('speaker')}**: {d.get('position')}" for d in dissent if isinstance(d, dict)]
        out.append("")
    return out


def render_markdown(model: Dict[str, Any]) -> str:
    if model["kind"] == "ensemble":
        return _ensemble_markdown(model)
    m = model
    out = [f"# {m['name']}", ""]
    if m.get("description"):
        out += [f"*{m['description']}*", ""]
    out += [f"**Topic.** {m['topic']}", ""]
    status = f"{m.get('status')}, {m.get('turn_count') or 0} turns, {_money(m.get('cost_usd'))}"
    if m.get("converged"):
        status += f" — the room converged at turn {m['converged'].get('at_turn')}"
    out += [f"- Run: `{m['id']}`, created {_when(m.get('created_at'))}", f"- {status}",
            f"- Exported {_when(m.get('exported_at'))}", ""]

    out += ["## Settings", ""] + [f"- {s}" for s in m["settings"]] + [""]
    research = _research_lines(m.get("research"))
    if research:
        out += ["## Pre-conversation research", ""] + [f"- {s}" for s in research] + [""]

    out += ["## Cast", ""]
    for c in m["cast"]:
        out += [f"### {c['name']}" + (f" — {c['role']}" if c.get("role") else ""), "", c["persona"], ""]
        if c["goals"]:
            out += ["Goals: " + "; ".join(c["goals"]), ""]
        for vp in c["viewpoints"]:
            line = f"- **{vp.get('firmness', 'held')}**: {vp.get('position', '')}"
            shifts = vp.get("evidence_that_shifts") or []
            if shifts:
                line += f" *(would change their mind: {'; '.join(shifts)})*"
            out.append(line)
        if c["viewpoints"]:
            out.append("")

    out += ["## Transcript", ""]
    for t in m["transcript"]:
        out += [f"**Turn {t['turn']} — {t['speaker']}**", "", t["message"], ""]
        if t["passages"]:
            refs = []
            for p in t["passages"]:
                tag = " (found by research)" if p.get("origin") == "researched" else ""
                tier = f", {p['authority']}" if p.get("authority") and p["authority"] != "unknown" else ""
                refs.append(f"{p.get('title')} #{p.get('ordinal')}{tag}{tier}")
            out += ["<sub>Sources in front of them: " + "; ".join(refs) + "</sub>", ""]

    if m.get("summary"):
        out += _md_summary(m["summary"])
    return "\n".join(out).rstrip() + "\n"


def _ensemble_markdown(m: Dict[str, Any]) -> str:
    out = [f"# {m['name']} — ensemble", ""]
    if m.get("description"):
        out += [f"*{m['description']}*", ""]
    out += [f"**Topic.** {m['topic']}", "",
            f"- Ensemble: `{m['id']}`, created {_when(m.get('created_at'))}, status {m.get('status')}",
            f"- Exported {_when(m.get('exported_at'))}", ""]

    out += ["## Groups", ""]
    for c in m["spec"]:
        overrides = ", ".join(f"{k}={v}" for k, v in (c.get("overrides") or {}).items())
        out.append(f"- **{c.get('label')}**: {c.get('n')} runs — {overrides or 'nothing varied'}")
    out.append("")

    research = _research_lines(m.get("research"))
    if research:
        out += ["## Pre-conversation research", "",
                "Researched ONCE, before any conversation started; every conversation read the same "
                "corpus.", ""] + [f"- {s}" for s in research] + [""]

    out += ["## Conversations", "", "| group | run | status | turns | cost |", "|---|---|---|---|---|"]
    for r in m["members"]:
        out.append(f"| {_md_cell(r['cell'])} | {_md_cell(r['name'])} | {_md_cell(r['status'])} | "
                   f"{r.get('turn_count') if r.get('turn_count') is not None else '—'} | "
                   f"{_money(r.get('cost_usd'))} |")
    out.append("")

    report = m.get("report")
    if not report:
        out += ["## Report", "", "No report has been generated for this ensemble.", ""]
        return "\n".join(out).rstrip() + "\n"

    cells = [c for c in m["cells"] if c] or sorted({k for cl in report.get("claims") or []
                                                    for k in (cl.get("per_cell") or {})})
    cv = conclusions_view(report, cells)
    out += ["## What the runs concluded", "", f"> *{ANALYSIS_LABEL}*", ""]
    if cv["state"] in _CONCLUSION_NOTES:
        out += [_CONCLUSION_NOTES[cv["state"]], ""]
    def _md_table(rows):
        t = ["| conclusion | " + " | ".join(_md_cell(c) for c in cells) + " |",
             "|---|" + "---|" * len(cells)]
        for cl in rows:
            per = cl.get("per_cell") or {}
            t.append(f"| {_md_cell(cl.get('claim'))} | "
                     + " | ".join(_cell_count(per.get(c)) for c in cells) + " |")
        return t + [""]

    if cv["recurring"]:
        out += ["### Reached in two or more runs of a group", ""] + _md_table(cv["recurring"])
    if cv["single"]:
        out += [f"### Never reached twice within a group ({len(cv['single'])})", "",
                "None of these recurred inside any group — rare, not findings. Listed so nothing is "
                "hidden, not because any of them is what the ensemble concluded.", ""]
        out += _md_table(cv["single"])
    out += ["## What every run in a group agreed on", ""]
    if cv["agreed"]:
        for a in cv["agreed"]:
            where = ", ".join(f"{c}: all {(a['claim']['per_cell'].get(c) or {}).get('of')} runs"
                              for c in a["cells"])
            out.append(f"- [{a['claim'].get('kind')}] {a['claim'].get('claim')} — {where}")
    else:
        out.append("No demand or refusal was held in every run of any group.")
    out.append("")

    out += ["## Claims, per group", "",
            "Counted within each group against that group's own runs. There is deliberately **no "
            "total column**: the same count means a strong method-dependent finding or a coin flip "
            "depending on how it splits across groups.", ""]
    out.append("| claim | kind | " + " | ".join(_md_cell(c) for c in cells) + " |")
    out.append("|---|---|" + "---|" * len(cells))
    for cl in report.get("claims") or []:
        per = cl.get("per_cell") or {}
        out.append(f"| {_md_cell(cl.get('claim'))} | {_md_cell(cl.get('kind'))} | "
                   + " | ".join(_cell_count(per.get(c)) for c in cells) + " |")
    out.append("")

    unresolved = ((report.get("agreements") or {}).get("unresolved_by_frequency")) or []
    if unresolved:
        out += ["## Left open", ""]
        out += [f"- ({len(u.get('runs') or [])} run(s)) {u.get('claim')}" for u in unresolved]
        out.append("")
    if report.get("synthesis"):
        out += ["## Synthesis", "", f"> *{ANALYSIS_LABEL}*", "", str(report["synthesis"]), ""]
    if report.get("caveats"):
        out += ["## Caveats", ""] + [f"- {c}" for c in report["caveats"]] + [""]
    return "\n".join(out).rstrip() + "\n"


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #

_CSS = """
body{font:15px/1.55 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:860px;margin:2em auto;
padding:0 1.2em;color:#1f2933}
h1{font-size:1.6em;margin-bottom:.2em}h2{margin-top:1.8em;border-bottom:1px solid #d9dee3;padding-bottom:.2em}
.meta{color:#52606d;font-size:.9em}.turn{margin:1em 0;padding:.6em .8em;border-left:3px solid #9aa5b1}
.who{font-weight:600}.msg{white-space:pre-wrap}.src{color:#616e7c;font-size:.82em;margin-top:.3em}
.analysis{border:1px solid #f0b429;background:#fffbea;padding:.6em .8em;border-radius:4px}
.label{font-style:italic;color:#8d6708;font-size:.9em;margin-bottom:.5em}
.pre{white-space:pre-wrap}table{border-collapse:collapse;width:100%;font-size:.9em}
th,td{border:1px solid #d9dee3;padding:.3em .5em;text-align:left;vertical-align:top}th{background:#f5f7fa}
.found{color:#616e7c}.ctrl{color:#0e7c48;font-weight:600}
@media print{body{margin:0;max-width:none}.turn{break-inside:avoid}h2{break-after:avoid}
a{color:inherit;text-decoration:none}}
"""


def _e(v: Any) -> str:
    return html.escape(str(v if v is not None else ""), quote=True)


def _doc(title: str, body: List[str]) -> str:
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            f"<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>{_e(title)}</title>"
            f"<style>{_CSS}</style></head><body>" + "".join(body) + "</body></html>\n")


def _html_list(items: Sequence[Any]) -> str:
    return "<ul>" + "".join(f"<li>{_e(i)}</li>" for i in items) + "</ul>"


def render_html(model: Dict[str, Any]) -> str:
    if model["kind"] == "ensemble":
        return _ensemble_html(model)
    m = model
    b = [f"<h1>{_e(m['name'])}</h1>"]
    if m.get("description"):
        b.append(f"<p class=\"meta\"><em>{_e(m['description'])}</em></p>")
    status = f"{m.get('status')}, {m.get('turn_count') or 0} turns, {_money(m.get('cost_usd'))}"
    if m.get("converged"):
        status += f" — the room converged at turn {m['converged'].get('at_turn')}"
    b += [f"<p><strong>Topic.</strong> {_e(m['topic'])}</p>",
          f"<p class=\"meta\">Run <code>{_e(m['id'])}</code> · created {_e(_when(m.get('created_at')))} · "
          f"{_e(status)} · exported {_e(_when(m.get('exported_at')))}</p>",
          "<h2>Settings</h2>", _html_list(m["settings"])]
    research = _research_lines(m.get("research"))
    if research:
        b += ["<h2>Pre-conversation research</h2>", _html_list(research)]

    b.append("<h2>Cast</h2>")
    for c in m["cast"]:
        b.append(f"<h3>{_e(c['name'])}{' — ' + _e(c['role']) if c.get('role') else ''}</h3>")
        b.append(f"<p>{_e(c['persona'])}</p>")
        if c["goals"]:
            b.append(f"<p class=\"meta\">Goals: {_e('; '.join(c['goals']))}</p>")
        if c["viewpoints"]:
            items = []
            for vp in c["viewpoints"]:
                shifts = vp.get("evidence_that_shifts") or []
                items.append(
                    f"<li><strong>{_e(vp.get('firmness', 'held'))}</strong>: {_e(vp.get('position', ''))}"
                    + (f" <em>(would change their mind: {_e('; '.join(shifts))})</em>" if shifts else "")
                    + "</li>")
            b.append("<ul>" + "".join(items) + "</ul>")

    b.append("<h2>Transcript</h2>")
    for t in m["transcript"]:
        src = ""
        if t["passages"]:
            refs = []
            for p in t["passages"]:
                cls = "ctrl" if p.get("authority") == "controlling" else (
                    "found" if p.get("origin") == "researched" else "")
                tag = " · found by research" if p.get("origin") == "researched" else ""
                tier = f" · {p['authority']}" if p.get("authority") and p["authority"] != "unknown" else ""
                refs.append(f"<span class=\"{cls}\">{_e(p.get('title'))} #{_e(p.get('ordinal'))}"
                            f"{_e(tag)}{_e(tier)}</span>")
            src = "<div class=\"src\">Sources in front of them: " + "; ".join(refs) + "</div>"
        b.append(f"<div class=\"turn\"><div class=\"who\">Turn {_e(t['turn'])} — {_e(t['speaker'])}</div>"
                 f"<div class=\"msg\">{_e(t['message'])}</div>{src}</div>")

    s = m.get("summary")
    if s:
        b += ["<h2>Summary</h2>", "<div class=\"analysis\">", f"<div class=\"label\">{_e(ANALYSIS_LABEL)}</div>"]
        if s.get("overview"):
            b.append(f"<p class=\"pre\">{_e(s['overview'])}</p>")
        for key, heading in (("key_ideas", "Key ideas"), ("consensus", "Where they agreed"),
                             ("open_questions", "Left open")):
            if s.get(key):
                b += [f"<h3>{heading}</h3>", _html_list(s[key])]
        dissent = [d for d in (s.get("dissenters") or []) if isinstance(d, dict)]
        if dissent:
            b.append("<h3>Dissent</h3><ul>" + "".join(
                f"<li><strong>{_e(d.get('speaker'))}</strong>: {_e(d.get('position'))}</li>" for d in dissent
            ) + "</ul>")
        b.append("</div>")
    return _doc(m["name"], b)


def _ensemble_html(m: Dict[str, Any]) -> str:
    b = [f"<h1>{_e(m['name'])} — ensemble</h1>"]
    if m.get("description"):
        b.append(f"<p class=\"meta\"><em>{_e(m['description'])}</em></p>")
    b += [f"<p><strong>Topic.</strong> {_e(m['topic'])}</p>",
          f"<p class=\"meta\">Ensemble <code>{_e(m['id'])}</code> · created {_e(_when(m.get('created_at')))} · "
          f"status {_e(m.get('status'))} · exported {_e(_when(m.get('exported_at')))}</p>", "<h2>Groups</h2>"]
    b.append(_html_list([
        f"{c.get('label')}: {c.get('n')} runs — "
        + (", ".join(f"{k}={v}" for k, v in (c.get("overrides") or {}).items()) or "nothing varied")
        for c in m["spec"]
    ]))
    research = _research_lines(m.get("research"))
    if research:
        b += ["<h2>Pre-conversation research</h2>",
              "<p>Researched once, before any conversation started; every conversation read the same corpus.</p>",
              _html_list(research)]
    b.append("<h2>Conversations</h2><table><tr><th>group</th><th>run</th><th>status</th><th>turns</th>"
             "<th>cost</th></tr>")
    for r in m["members"]:
        b.append(f"<tr><td>{_e(r['cell'])}</td><td>{_e(r['name'])}</td><td>{_e(r['status'])}</td>"
                 f"<td>{_e(r.get('turn_count') if r.get('turn_count') is not None else '—')}</td>"
                 f"<td>{_e(_money(r.get('cost_usd')))}</td></tr>")
    b.append("</table>")

    report = m.get("report")
    if not report:
        b.append("<h2>Report</h2><p>No report has been generated for this ensemble.</p>")
        return _doc(m["name"], b)

    cells = [c for c in m["cells"] if c] or sorted({k for cl in report.get("claims") or []
                                                    for k in (cl.get("per_cell") or {})})
    cv = conclusions_view(report, cells)
    b += ["<h2>What the runs concluded</h2><div class=\"analysis\">",
          f"<div class=\"label\">{_e(ANALYSIS_LABEL)}</div>"]
    if cv["state"] in _CONCLUSION_NOTES:
        b.append(f"<p>{_e(_CONCLUSION_NOTES[cv['state']])}</p>")
    def _html_table(rows):
        t = ["<table><tr><th>conclusion</th>" + "".join(f"<th>{_e(c)}</th>" for c in cells) + "</tr>"]
        for cl in rows:
            per = cl.get("per_cell") or {}
            t.append(f"<tr><td>{_e(cl.get('claim'))}</td>"
                     + "".join(f"<td>{_e(_cell_count(per.get(c)))}</td>" for c in cells) + "</tr>")
        return "".join(t) + "</table>"

    if cv["recurring"]:
        b += ["<h3>Reached in two or more runs of a group</h3>", _html_table(cv["recurring"])]
    if cv["single"]:
        b += [f"<h3>Never reached twice within a group ({len(cv['single'])})</h3>",
              "<p class=\"meta\">None of these recurred inside any group — rare, not findings. "
              "Listed so nothing is hidden, not because any of them is what the ensemble concluded.</p>",
              _html_table(cv["single"])]
    b.append("</div><h2>What every run in a group agreed on</h2>")
    if cv["agreed"]:
        b.append(_html_list([
            f"[{a['claim'].get('kind')}] {a['claim'].get('claim')} — "
            + ", ".join(f"{c}: all {(a['claim']['per_cell'].get(c) or {}).get('of')} runs" for c in a["cells"])
            for a in cv["agreed"]
        ]))
    else:
        b.append("<p>No demand or refusal was held in every run of any group.</p>")

    b += ["<h2>Claims, per group</h2>",
          "<p class=\"meta\">Counted within each group against that group's own runs. There is "
          "deliberately no total column: the same count means a strong method-dependent finding or a "
          "coin flip depending on how it splits across groups.</p>",
          "<table><tr><th>claim</th><th>kind</th>" + "".join(f"<th>{_e(c)}</th>" for c in cells) + "</tr>"]
    for cl in report.get("claims") or []:
        per = cl.get("per_cell") or {}
        b.append(f"<tr><td>{_e(cl.get('claim'))}</td><td>{_e(cl.get('kind'))}</td>"
                 + "".join(f"<td>{_e(_cell_count(per.get(c)))}</td>" for c in cells) + "</tr>")
    b.append("</table>")
    unresolved = ((report.get("agreements") or {}).get("unresolved_by_frequency")) or []
    if unresolved:
        b += ["<h2>Left open</h2>", _html_list(
            [f"({len(u.get('runs') or [])} run(s)) {u.get('claim')}" for u in unresolved])]
    if report.get("synthesis"):
        b += ["<h2>Synthesis</h2><div class=\"analysis\">",
              f"<div class=\"label\">{_e(ANALYSIS_LABEL)}</div>",
              f"<div class=\"pre\">{_e(report['synthesis'])}</div></div>"]
    if report.get("caveats"):
        b += ["<h2>Caveats</h2>", _html_list(report["caveats"])]
    return _doc(m["name"], b)


def render(model: Dict[str, Any], fmt: str) -> str:
    if fmt == "md":
        return render_markdown(model)
    if fmt == "html":
        return render_html(model)
    raise ValueError(f"unknown export format {fmt!r}; known: {sorted(FORMATS)}")

"""A per-question breakdown of the eval set, written to markdown.

    uv run python evals/walkthrough.py                  # every question
    uv run python evals/walkthrough.py q020 q029        # an id range
    uv run python evals/walkthrough.py --out /tmp/x.md

The same three calls as ``evals/run.py`` -- ``parse_question`` ->
``map_query`` -> ``answer`` -- and the same scorer, so the two always agree on
the grade. What differs is the output. ``run.py`` prints one line per question,
which is what you want for a number; this writes, for each question:

* the question as asked, and the filer a template was substituted with
* every element the parser produced, with the fields it set
* expected against observed, item by item
* the rows that came back, with their units and the windows they cover
* the provenance -- which concepts, under what expression, via alias or
  embedding
* every caveat raised, plan-level and per binding
* the exception, verbatim, when a stage raised one

That is what you read when a count moves and you need to know *why*. Budget
25-30 minutes for the whole set; it writes after every question, so the file is
readable while the run is still going.

**A grade here means the chain made the right call about whether to answer.**
It does not mean the figure is right -- nothing in this repo checks that. A
question can be graded ``pass`` while returning a wrong number, and has been.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import yaml

from app.parser import parse_question
from app.retrieval import answer
from app.semantic.query_mapper import map_query
from evals.run import (
    QUESTIONS,
    TEMPLATE_COMPANY,
    crashed,
    expected_for,
    grade,
    observe,
    pair,
    substitute,
)

DEFAULT_OUT = Path("data/question-walkthrough.md")

MARK = {"pass": "PASS", "fail": "FAIL", "unsafe": "UNSAFE", "ungraded": "n/a"}


def fmt(value: Decimal | None) -> str:
    """A figure a person can read.

    Not ``{:,.6g}``: that renders Apple's revenue as ``3.65817e+11``, which is
    unreadable next to a margin of ``0.4612`` in the same column.
    """
    if value is None:
        return "*(none)*"
    if abs(value) >= 1000:
        return f"{value:,.0f}"
    return f"{value:,.6f}".rstrip("0").rstrip(".")


def _elements(query) -> list[str]:
    rows = ["| element | kind | text | detail |", "|---|---|---|---|"]
    for element in query.elements:
        detail = ""
        if element.kind == "period":
            bits = []
            if element.fiscal_year:
                bits.append(f"fiscal_year {element.fiscal_year}")
            if element.fiscal_period:
                bits.append(f"fiscal_period {element.fiscal_period}")
            if element.last_n_years:
                bits.append(f"last_n_years {element.last_n_years}")
            detail = ", ".join(bits) or "—"
        elif element.kind == "company":
            detail = element.ticker or element.name or "—"
        elif element.kind == "company_group":
            detail = f"sic_description={element.sic_description!r}"
        kind = f"**{element.kind}**" if element.kind == "metric" else element.kind
        rows.append(f'| {element.id} | {kind} | "{element.text}" | {detail} |')
    return rows


def _rows(result) -> list[str]:
    out = [
        "| company | period | value | unit | window covered | cites |",
        "|---|---|---|---|---|---|",
    ]
    for annotated in result.rows[:25]:
        row = annotated.row
        who = row.ticker or str(row.company_cik)
        window = (
            row.period_end.isoformat()
            if row.is_instant
            else f"{row.period_start} → {row.period_end}"
        )
        out.append(
            f"| {who} | {row.fiscal_period} {row.fiscal_year} | {fmt(row.value)} | "
            f"{row.unit} | {window} | {', '.join(annotated.binding_keys)} |"
        )
    if len(result.rows) > 25:
        out.append(f"| … | | *{len(result.rows) - 25} more rows* | | | |")
    return out


def _provenance(result) -> list[str]:
    seen, lines = set(), []
    for key, citation in result.citations.items():
        names = ", ".join(f"`{c.taxonomy}:{c.name}`" for c in citation.concepts)
        signature = (names, citation.expression, citation.resolved_by)
        if signature in seen:
            continue
        seen.add(signature)
        lines.append(
            f"| {key} | {names} | `{citation.expression}` | "
            f"{citation.resolved_by} {citation.confidence:.2f} |"
        )
    if not lines:
        return []
    return [
        "**Provenance** *(one line per distinct binding shape)*",
        "",
        "| key | concept(s) | expression | via |",
        "|---|---|---|---|",
        *lines,
    ]


def _caveats(plan, result) -> list[str]:
    seen, lines = set(), []
    for note in list(plan.notes) + (list(result.notes) if result else []):
        if note.message not in seen:
            seen.add(note.message)
            lines.append(f"- `{note.kind}` — {note.message}")
    for key, citation in (result.citations.items() if result else []):
        for note in citation.notes:
            if note.message not in seen:
                seen.add(note.message)
                lines.append(f"- `{note.kind}` (on {key}) — {note.message}")
    return ["**Caveats raised**", "", *lines] if lines else []


async def one(entry: dict, out: list[str]) -> str:
    """Append one question's section to ``out``; return its grade."""
    qid = entry["id"]
    template = bool(entry.get("template"))
    text = substitute(entry["question"], template=template)
    expected = expected_for(entry)

    if entry.get("known_gap"):
        out += [
            f"## {qid} — SET ASIDE (known gap)",
            "",
            "**Question asked**",
            "",
            f"> {text}",
            "",
            "**Why it is set aside**",
            "",
            " ".join(entry["known_gap"].split()),
            "",
        ]
        return "gap"

    out += [f"## {qid}", "", "**Question asked**", "", f"> {text}", ""]
    if template:
        out += [f"*Template question — `<Company>` substituted with {TEMPLATE_COMPANY}.*", ""]
        if TEMPLATE_COMPANY in (entry.get("expect_by_company") or {}):
            out += [f"*Expectation specific to {TEMPLATE_COMPANY} (`expect_by_company`).*", ""]

    try:
        query = await parse_question(text)
    except Exception as exc:
        out += [f"**The parser refused it.** `{type(exc).__name__}: {exc}`", ""]
        items = pair(expected, [("(did not parse)", "confused")] * max(len(expected), 1))
        g = grade(items, expected_count=len(expected), stage="answer")
        out += [f"**Grade: {MARK[g]}**", ""]
        return g

    shape = f", shape `{query.shape}`" if query.shape else ""
    understood = f"**How it was understood** — intent `{query.intent}`{shape}"
    out += [understood, "", *_elements(query), ""]

    plan = await map_query(query)
    result, crash = None, ""
    if plan.has_answerable_part:
        try:
            result = await answer(plan)
        except Exception as exc:
            crash = f"{type(exc).__name__}: {exc}"

    if crash:
        observed = crashed(query, plan)
    else:
        observed = observe(query, plan, result)
    items = pair(expected, observed)
    g = grade(items, expected_count=len(expected), stage="answer")

    out += ["**Expected vs got**", "", "| # | item | expected | got | |", "|---|---|---|---|---|"]
    for index, item in enumerate(items, 1):
        flag = "ok" if item.ok else "**XX**"
        out.append(f"| {index} | {item.asked} | `{item.expected}` | `{item.got}` | {flag} |")
    out.append("")

    if crash:
        out += ["**The executor raised**", "", "```", crash[:800], "```", ""]
    elif result is not None:
        verdict = result.verdict
        out += [
            f"**Output** — {verdict.returned_rows} of {verdict.expected_rows} rows, "
            f"verdict `{verdict.status}`, answerable `{result.is_answerable}`",
            "",
        ]
        if result.rows:
            out += [*_rows(result), ""]
        prov = _provenance(result)
        if prov:
            out += [*prov, ""]
    else:
        out += ["**Output** — no rows. The chain stopped before any SQL ran.", ""]

    if plan.unresolved:
        out += ["**Refused**", "", *[f"- {u.reason}" for u in plan.unresolved], ""]
    for clarification in plan.clarifications:
        out += [
            f"**Asked back** — {clarification.question}",
            "",
            "| option | description |",
            "|---|---|",
            *[f"| {o.label} | {o.description} |" for o in clarification.options],
            "",
        ]
    if plan.ambiguous:
        out.append("**Confused by**")
        out.append("")
        for ambiguity in plan.ambiguous:
            candidates = ", ".join(
                f"`{c.concept.name}` {c.score:.2f}" for c in ambiguity.candidates
            )
            out.append(f'- "{ambiguity.element_text}" → {candidates}')
        out.append("")

    caveats = _caveats(plan, result)
    if caveats:
        out += [*caveats, ""]

    out += [f"**Grade: {MARK[g]}**", ""]
    return g


def _header(tally: dict[str, list[str]], scope: str) -> list[str]:
    head = [
        "# Eval walkthrough",
        "",
        f"{scope}, run {datetime.now(UTC).strftime('%Y-%m-%d %H:%M')} UTC "
        f"against the live chain.",
        f"Templates substituted with **{TEMPLATE_COMPANY}**.",
        "",
        "A grade here means the chain made the right call about whether to answer.",
        "It does **not** mean the figure is right — nothing checks that.",
        "",
    ]
    if tally:
        head += ["## Tally", ""]
        for key in ("pass", "fail", "unsafe", "gap"):
            if key in tally:
                head.append(f"- **{key}** ({len(tally[key])}): {', '.join(tally[key])}")
        head.append("")
    return head + ["---", ""]


async def main() -> None:
    parser = argparse.ArgumentParser(
        prog="evals/walkthrough.py",
        description="Write a per-question breakdown of the eval set to markdown.",
    )
    parser.add_argument("start", nargs="?", help="first question id, e.g. q020")
    parser.add_argument("end", nargs="?", help="last question id, e.g. q029")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output markdown file")
    args = parser.parse_args()

    document = yaml.safe_load(QUESTIONS.read_text("utf-8"))
    ids = [q["id"] for q in document["questions"]]
    start = args.start or ids[0]
    end = args.end or (args.start if args.start else ids[-1])
    todo = [q for q in document["questions"] if start <= q["id"] <= end]
    if not todo:
        raise SystemExit(f"no questions between {start!r} and {end!r}")

    scope = f"Questions {todo[0]['id']}–{todo[-1]['id']}"
    args.out.parent.mkdir(parents=True, exist_ok=True)

    sections: list[str] = []
    tally: dict[str, list[str]] = {}
    for index, entry in enumerate(todo, 1):
        print(f"[{index}/{len(todo)}] {entry['id']}", flush=True)
        try:
            g = await one(entry, sections)
        except Exception as exc:  # a harness fault is a data point, not a reason to stop
            sections += [f"**Harness error:** `{type(exc).__name__}: {exc}`", ""]
            g = "error"
        tally.setdefault(g, []).append(entry["id"])
        sections += ["---", ""]
        args.out.write_text(
            "\n".join(_header(tally, scope) + sections), encoding="utf-8", newline="\n"
        )

    print("TALLY " + repr({k: len(v) for k, v in tally.items()}), flush=True)
    for key, values in sorted(tally.items()):
        print(f"  {key}: {', '.join(values)}", flush=True)
    print(f"written: {args.out}", flush=True)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())

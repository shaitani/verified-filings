"""Run the eval set through the chain and score what it decided.

    uv run python evals/run.py                  # all 56, [C] -> [D] -> [E]
    uv run python evals/run.py --stage map      # stop at the plan; no Qwen
    uv run python evals/run.py --stage parse    # stop at the QueryIn; no database
    uv run python evals/run.py --id q001 q026   # just these
    uv run python evals/run.py --limit 5        # the first five

This is block [B] with the browser, the conversation state and the presenter
taken out: the same call sequence -- ``parse_question`` -> ``map_query`` ->
``answer`` -- wrapped in a loop and a scorer. It is the first caller of all
three, and the first measurement of how often the whole chain decides
correctly.

**It grades the decision, not the number.** A question tagged ``answerable``
passes when an answerable ``ResultSet`` comes back. Nothing here checks that
the figure inside it is right, or that a ``partial`` answer disclosed its gap.
Recording expected *answers* was rejected when this set was written
(``evals/README.md``) and that still holds: pinning numbers would turn a design
instrument into a brittle regression suite, and the few figures worth pinning
are pinned already, in ``HANDOFF.md`` §8.

One grade is not symmetric. ``unsafe`` is an answerable result for a question
tagged ``refuse`` -- the system answering something the data cannot support.
Every other failure costs a refusal, which this project prefers to a plausible
wrong number. So ``unsafe`` is counted apart from ``fail`` and printed last,
because it is the only line that means the thing the project exists to prevent
has happened.

Grading applies **only to a full ``--stage answer`` run**. The earlier stages
report outcomes without grades on purpose: [C] deliberately does not judge
whether a question is answerable at all (``app/producer/DESIGN.md`` §7), so a
clean parse of a ``refuse`` question is correct behaviour there, and scoring it
as a miss would be measuring the wrong component.

Sequential on purpose. Ollama serves its models on one GPU and serialises
anyway, so a worker pool would buy nothing and would make the running
commentary unreadable. Budget around fifteen minutes for a full run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import yaml
from sqlalchemy import func, select

from app.db import Company
from app.db.session import QueryMapperSessionLocal
from app.producer import ProposalError, UnacceptableProposal, parse_question
from app.retrieval import GenerationError, InvalidSQL, UnsupportedPlan, answer
from app.schemas.query import QueryPlan
from app.schemas.result import ResultSet
from app.semantic.query_mapper import map_query

QUESTIONS = Path(__file__).with_name("questions.yaml")

#: Where a run is written. Under ``data/``, which is gitignored: a run measures
#: one checkout against one database at one moment, which is not something to
#: review in a diff.
RUNS_DIR = Path(__file__).resolve().parent.parent / "data" / "eval_runs"

#: Seven questions carry a ``<Company>`` placeholder (``template: true``)
#: because they measure coverage of a common *ask* rather than one concrete
#: query, and a runner has to substitute before executing them. Apple is the
#: best-covered filer in the corpus, so it isolates the shape of the question
#: from gaps in one company's tagging. The substitution is recorded on every
#: run, so a reader always knows which filer a number came from.
TEMPLATE_COMPANY = "Apple"

#: Where a question stopped, ordered roughly by how far down the chain it got.
#:   "parse_failed"  -- [C] would not produce a QueryIn, even after its repair.
#:   "parsed"        -- terminal for --stage parse.
#:   "refused"       -- [D] left something unresolved with nothing to ask
#:                      about. A refusal, which is often the correct answer.
#:   "clarify"       -- the plan carries a curated question or candidates, so
#:                      one more reply from the asker would resolve it.
#:   "planned"       -- terminal for --stage map: a complete plan.
#:   "answered"      -- rows came back and the verdict stands behind them.
#:   "unanswerable"  -- rows came back and the verdict does not. A shortfall
#:                      the plan did not predict, refused on purpose.
#:   "error"         -- something raised. The machinery failed, as distinct
#:                      from the data being unable to support the question.
Outcome = Literal[
    "parse_failed",
    "parsed",
    "refused",
    "clarify",
    "planned",
    "answered",
    "unanswerable",
    "error",
]

Grade = Literal["pass", "fail", "unsafe", "untriaged", "ungraded"]

Stage = Literal["parse", "map", "answer"]


@dataclass
class Run:
    """One question's trip through the chain."""

    id: str
    question: str
    expect: str
    outcome: Outcome
    grade: Grade

    #: Why, in whatever terms the stage that stopped it used: an exception
    #: message, the unresolved reasons, the clarification put back to the
    #: asker. This is the field a person actually reads when a count moves, so
    #: it carries the specific text rather than a category.
    detail: str = ""

    template: bool = False
    seconds: float = 0.0

    #: The shape of the work, as far as it got. All optional: a question that
    #: never parsed has none of them, and a zero would read as a measurement.
    elements: int | None = None
    bindings: int | None = None
    expected_rows: int | None = None
    returned_rows: int | None = None

    #: ``NoteKind`` values from the plan and from the result. The caveats are
    #: the half of correctness that is not a row count.
    notes: list[str] = field(default_factory=list)


def substitute(question: str, *, template: bool) -> str:
    return question.replace("<Company>", TEMPLATE_COMPANY) if template else question


def _plan_detail(plan: QueryPlan) -> str:
    """Why an incomplete plan stopped, in the plan's own words."""
    parts = [f"unresolved: {u.reason}" for u in plan.unresolved]
    parts += [f"clarify {c.element_text!r}: {c.question}" for c in plan.clarifications]
    parts += [
        f"ambiguous {a.element_text!r}: "
        + ", ".join(f"{c.concept.name}@{c.score:.2f}" for c in a.candidates[:3])
        for a in plan.ambiguous
    ]
    return " | ".join(parts)


def _result_detail(result: ResultSet) -> str:
    verdict = result.verdict
    parts = [f"{verdict.status} {verdict.returned_rows}/{verdict.expected_rows}"]
    if verdict.unattributable:
        parts.append(f"{len(verdict.unattributable)} row(s) matched no binding")
    unanticipated = [cell for cell in verdict.missing if not cell.anticipated]
    if unanticipated:
        parts.append(f"{len(unanticipated)} unanticipated missing cell(s)")
    return ", ".join(parts)


def grade(expect: str, outcome: Outcome, *, stage: Stage) -> Grade:
    """Did the chain make the right call?

    Only a full run is graded -- see the module docstring. ``unsafe`` is kept
    out of ``fail`` because the two are not comparable: a miss costs a refusal,
    and this costs a number somebody might act on.
    """
    if stage != "answer":
        return "ungraded"
    if expect not in {"answerable", "partial", "refuse"}:
        return "untriaged"
    answered = outcome == "answered"
    if expect == "refuse":
        return "unsafe" if answered else "pass"
    return "pass" if answered else "fail"


async def run_one(entry: dict, *, stage: Stage, parser_model: str | None) -> Run:
    """One question, as far down the chain as ``stage`` allows.

    Never raises. A question that blows up is a data point, not a reason to
    lose the other fifty-five -- a full run costs a quarter of an hour.
    """
    template = bool(entry.get("template", False))
    question = substitute(entry["question"], template=template)
    expect = entry.get("expect", "unknown")
    started = time.monotonic()

    def done(outcome: Outcome, **fields) -> Run:
        return Run(
            id=entry["id"],
            question=question,
            expect=expect,
            outcome=outcome,
            grade=grade(expect, outcome, stage=stage),
            template=template,
            seconds=time.monotonic() - started,
            **fields,
        )

    try:
        kwargs = {"model": parser_model} if parser_model else {}
        query = await parse_question(question, **kwargs)
    except (UnacceptableProposal, ProposalError, ValueError) as exc:
        return done("parse_failed", detail=f"{type(exc).__name__}: {exc}")

    if stage == "parse":
        return done("parsed", elements=len(query.elements))

    try:
        plan = await map_query(query)
    except Exception as exc:  # the mapper does not raise by design; record it if it does
        return done(
            "error",
            detail=f"map_query {type(exc).__name__}: {exc}",
            elements=len(query.elements),
        )

    shared: dict = {
        "elements": len(query.elements),
        "bindings": len(plan.bindings),
        "expected_rows": plan.result.row_count,
        "notes": [note.kind for note in plan.notes],
    }

    if not plan.is_complete:
        outcome: Outcome = "clarify" if plan.needs_input else "refused"
        return done(outcome, detail=_plan_detail(plan), **shared)

    if stage == "map":
        return done("planned", **shared)

    try:
        result = await answer(plan)
    except (GenerationError, InvalidSQL, UnsupportedPlan) as exc:
        return done("error", detail=f"{type(exc).__name__}: {exc}", **shared)
    except Exception as exc:
        return done("error", detail=f"unexpected {type(exc).__name__}: {exc}", **shared)

    shared["notes"] = sorted({*shared["notes"], *(note.kind for note in result.notes)})
    shared["returned_rows"] = result.verdict.returned_rows
    return done(
        "answered" if result.is_answerable else "unanswerable",
        detail=_result_detail(result),
        **shared,
    )


async def preflight() -> None:
    """Fail once, clearly, rather than fifty-six times with a stack trace.

    The chain needs the database loaded and Ollama serving two models. Only the
    first is cheap to check from here; a missing model shows up as an ``error``
    outcome on the first question, which is soon enough.
    """
    try:
        async with QueryMapperSessionLocal() as session:
            loaded = await session.scalar(select(func.count()).select_from(Company))
    except Exception as exc:
        sys.exit(f"cannot reach the database ({type(exc).__name__}: {exc}). See BOOTSTRAP.md.")
    if not loaded:
        sys.exit("the store holds no companies. See BOOTSTRAP.md for the load step.")


def report(runs: list[Run], *, stage: Stage) -> None:
    """The summary. Ordered so the line that matters most is read last."""
    print()
    print(f"{len(runs)} question(s), stage={stage}")

    print("\noutcomes")
    for outcome in sorted({run.outcome for run in runs}):
        matching = [run for run in runs if run.outcome == outcome]
        print(f"  {outcome:14} {len(matching):3}")

    if stage != "answer":
        print("\nnot graded: only a full --stage answer run is scored.")
        return

    graded = [run for run in runs if run.grade != "untriaged"]
    passed = [run for run in graded if run.grade == "pass"]
    failed = [run for run in graded if run.grade == "fail"]
    unsafe = [run for run in graded if run.grade == "unsafe"]

    rate = f"{len(passed) / len(graded):.0%}" if graded else "n/a"
    print(f"\npass {len(passed)}/{len(graded)}  ({rate})")

    if failed:
        print(f"\nmissed -- should have answered, did not ({len(failed)}):")
        for run in failed:
            print(f"  {run.id}  {run.outcome:13} {run.detail[:90]}")

    print()
    if unsafe:
        print(f"UNSAFE -- answered a question tagged 'refuse' ({len(unsafe)}):")
        for run in unsafe:
            print(f"  {run.id}  {run.question[:70]}")
            print(f"        {run.detail}")
    else:
        print("UNSAFE: none. Nothing tagged 'refuse' came back with an answer.")


def write_run(runs: list[Run], *, stage: Stage) -> Path:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = RUNS_DIR / f"{stamp}-{stage}.json"
    payload = {
        "stamp": stamp,
        "stage": stage,
        "template_company": TEMPLATE_COMPANY,
        "runs": [asdict(run) for run in runs],
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n")
    return path


def select_questions(args: argparse.Namespace) -> list[dict]:
    document = yaml.safe_load(QUESTIONS.read_text("utf-8"))
    questions = document["questions"]
    if args.id:
        wanted = set(args.id)
        questions = [q for q in questions if q["id"] in wanted]
        missing = wanted - {q["id"] for q in questions}
        if missing:
            sys.exit(f"no such question id(s): {sorted(missing)}")
    if args.expect:
        questions = [q for q in questions if q.get("expect", "unknown") in args.expect]
    if args.limit:
        questions = questions[: args.limit]
    if not questions:
        sys.exit("no questions selected")
    return questions


async def main() -> None:
    parser = argparse.ArgumentParser(
        prog="evals/run.py", description="Run the eval set through the chain and score it."
    )
    parser.add_argument(
        "--stage",
        choices=["parse", "map", "answer"],
        default="answer",
        help="how far down the chain to go. 'parse' needs no database, 'map' no Qwen.",
    )
    parser.add_argument("--id", nargs="+", help="run only these question ids")
    parser.add_argument(
        "--expect",
        nargs="+",
        choices=["answerable", "partial", "refuse", "unknown"],
        help="run only questions carrying these expectations",
    )
    parser.add_argument("--limit", type=int, help="run only the first N selected")
    parser.add_argument("--parser-model", help="override the model [C] proposes with")
    parser.add_argument("--no-write", action="store_true", help="do not write the run file")
    args = parser.parse_args()

    questions = select_questions(args)
    if args.stage != "parse":
        await preflight()

    print(f"{len(questions)} question(s), stage={args.stage}, templates -> {TEMPLATE_COMPANY}\n")
    runs: list[Run] = []
    for index, entry in enumerate(questions, start=1):
        run = await run_one(entry, stage=args.stage, parser_model=args.parser_model)
        runs.append(run)
        print(
            f"[{index:2}/{len(questions)}] {run.id}  {run.expect:11} {run.outcome:13} "
            f"{run.grade:9} {run.seconds:5.1f}s  {run.detail[:60]}"
        )

    report(runs, stage=args.stage)
    if not args.no_write:
        print(f"\nwritten: {write_run(runs, stage=args.stage)}")


if __name__ == "__main__":
    asyncio.run(main())

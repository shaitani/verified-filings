"""Run the eval set through the chain and score what it decided, item by item.

    uv run python evals/run.py                  # all 56, [C] -> [D] -> [E]
    uv run python evals/run.py --stage map      # stop at the plan; no Qwen
    uv run python evals/run.py --stage parse    # stop at the QueryIn; no database
    uv run python evals/run.py --id q001 q026   # just these
    uv run python evals/run.py --limit 5        # the first five

This is block [B] with the browser, the conversation state and the presenter
taken out: the same call sequence -- ``parse_question`` -> ``map_query`` ->
``answer`` -- wrapped in a loop and a scorer.

**A question is a list of things asked for, not one thing.** "What are Apple's
assets, liabilities, equity, cash, goodwill, inventory" is six asks, and five
of them can succeed while the sixth fails. Scoring that with a single word
threw away the five and reported one misleading verdict. So both sides of the
comparison are lists, one entry per metric the question names, in the order it
names them:

    expect: [answered, answered, answered, answered, answered, answered]
    got:    [answered, answered, answered, answered, refused,  answered]

Companies and periods are **scope, not items**. A question that names a
company the dataset does not hold still asks for one metric, and the drop
rides on the plan's ``partial_coverage`` note rather than on this list --
otherwise every list would have to enumerate twenty filers nobody typed.

**It grades the decision, not the number.** Nothing here checks that a figure
is right. Recording expected answers was rejected when this set was written
(``evals/README.md``): pinning numbers turns a design instrument into a
brittle regression suite, and the few figures worth pinning are pinned in
``HANDOFF.md`` §8.

One grade is not symmetric. ``unsafe`` is the system **answering** an item a
person marked ``refused`` or ``asked`` -- it produced a figure where it should
have declined or put a question back. Every other failure costs a refusal,
which this project prefers to a plausible wrong number, so ``unsafe`` is
counted apart and printed last.

Sequential on purpose. Ollama serves its models on one GPU and serialises
anyway, so a worker pool would buy nothing and make the running commentary
unreadable.
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
from app.parser import ProposalError, UnacceptableProposal, parse_question
from app.retrieval import GenerationError, InvalidSQL, UnsupportedPlan, answer
from app.schemas.query import QueryIn, QueryPlan
from app.schemas.result import ResultSet
from app.semantic.query_mapper import map_query

QUESTIONS = Path(__file__).with_name("questions.yaml")

#: Where a run is written. Under ``data/``, which is gitignored: a run measures
#: one checkout against one database at one moment.
RUNS_DIR = Path(__file__).resolve().parent.parent / "data" / "eval_runs"

#: Eleven questions carry a ``<Company>`` placeholder (``template: true``)
#: because they measure coverage of a common *ask* rather than one concrete
#: query, and a runner has to substitute before executing them.
#:
#: Apple is **not** a neutral choice and this is a known weakness: Apple
#: reports no ``Goodwill`` at all, and stopped tagging ``InterestExpense``
#: after FY2023. Both show up as failures against expectations written for an
#: ordinary filer, which is the correct signal -- the substitution is wrong,
#: not the question. Recorded on every run so a reader knows which filer a
#: number came from.
TEMPLATE_COMPANY = "Apple"

#: What happened to **one thing a question asked for**. Both sides of the
#: comparison draw on this, except that the last two can only be observed --
#: nobody writes down that they want the system confused, or crashed.
#:
#:   "answered"  -- bound to concepts with facts behind them.
#:   "asked"     -- a curated question went back to the asker. `metric_aliases`
#:                  says the term is genuinely several things and a person
#:                  wrote the choices. A designed outcome, not a failure.
#:   "refused"   -- declined, with a reason. Often correct: Apple reports no
#:                  goodwill, and saying so beats inventing one.
#:   "confused"  -- the term was not understood. Either the parser could not
#:                  place it, or the embedding search returned candidates it
#:                  would not commit to. **Never** a thing to expect: the
#:                  candidates are raw XBRL names and cosine scores, which is
#:                  not a question anyone can answer.
#:   "error"     -- the machinery broke, or produced rows it would not stand
#:                  behind. Distinct from the data being unable to answer.
#:
#: "asked" and "confused" used to share one value. They are opposites: one is
#: the system working as designed, the other is it giving up, and merging them
#: scored a curated clarification and a failed embedding lookup identically.
Outcome = Literal["answered", "asked", "refused", "confused", "error"]

#: What a person may write in ``expect``. The two observation-only values are
#: absent by design -- see above.
EXPECTABLE = ("answered", "asked", "refused")

Grade = Literal["pass", "fail", "unsafe", "ungraded"]

#: Padding when the two lists are different lengths. Plain ASCII on purpose --
#: this prints to a Windows console, where an em dash arrives as a replacement
#: character and reads as corruption rather than as an empty slot.
NOT_EXPECTED = "(not expected)"
NOT_PRODUCED = "(not produced)"

Stage = Literal["parse", "map", "answer"]

#: Element kinds that count as "a thing the question asked for". A
#: `metric_qualifier` is excluded on purpose: its refusal is attached to the
#: metric it narrows, so counting it too would report one failure twice.
ITEM_KINDS = ("metric", "narrative")


@dataclass
class Item:
    """One thing a question asked for, and what became of it."""

    #: The phrase as the question worded it -- "goodwill", "gross revenue".
    asked: str
    expected: str
    got: str

    @property
    def ok(self) -> bool:
        return self.expected == self.got


@dataclass
class Run:
    """One question's trip through the chain."""

    id: str
    question: str
    grade: Grade
    items: list[Item] = field(default_factory=list)

    #: One line a person reads when a count moves: the specific reason the
    #: stage that stopped it gave, not a category.
    detail: str = ""

    template: bool = False
    seconds: float = 0.0

    bindings: int | None = None
    expected_rows: int | None = None
    returned_rows: int | None = None

    #: ``NoteKind`` values from the plan and the result. The caveats are the
    #: half of correctness that is not a row count -- a dropped company lives
    #: here, not in ``items``.
    notes: list[str] = field(default_factory=list)


def substitute(question: str, *, template: bool) -> str:
    return question.replace("<Company>", TEMPLATE_COMPANY) if template else question


def expected_for(entry: dict) -> list[str]:
    """The expectation for this run, per item.

    A template question is asked about whichever filer ``TEMPLATE_COMPANY``
    names, and filers differ: Apple reports no goodwill, so q052's fifth item
    is a correct refusal for Apple and a correct answer for Microsoft.
    ``expect_by_company`` records that difference as data, keyed by company,
    with its reason in a comment -- so changing the template company changes
    the expectation with it instead of turning a right answer into a fail.
    """
    overrides = entry.get("expect_by_company") or {}
    if entry.get("template") and TEMPLATE_COMPANY in overrides:
        return list(overrides[TEMPLATE_COMPANY])
    return list(entry.get("expect", []))


def crashed(query: QueryIn, plan: QueryPlan) -> list[tuple[str, Outcome]]:
    """What each item got when retrieval raised.

    Only the parts that were going to be answered failed. A part already
    refused or asked back had its answer before any SQL was written, and
    marking it ``error`` would hide a correct refusal behind a crash.
    """
    return [
        (phrase, "error" if outcome == "answered" else outcome)
        for phrase, outcome in observe(query, plan, None)
    ]


def observe(query: QueryIn, plan: QueryPlan, result: ResultSet | None) -> list[tuple[str, Outcome]]:
    """``(phrase, outcome)`` for everything the question asked for, in order.

    Precedence matters where an element lands in two buckets at once. A
    curated question outranks everything: it is the one outcome a person
    authored. Then confusion, then refusal, then success -- so an element that
    bound for some companies and hit a unit or sign violation on others reads
    as ``refused`` rather than being quietly counted as answered.
    """
    clarified = {c.element_id for c in plan.clarifications}
    ambiguous = {a.element_id for a in plan.ambiguous}
    unresolved = {u.element_id for u in plan.unresolved}
    bound = {b.element_id for b in plan.bindings}

    # Rows came back that the verdict will not stand behind, so nothing that
    # was going to be answered actually was.
    spoiled = result is not None and not result.is_answerable

    # A refusal on something that is not an item -- a company, a period --
    # blocks the whole question, so a metric that bound was still not
    # answered. q036: Samsung refused, Apple's revenue bound, nothing ran.
    # Kept to non-items on purpose: q044 expects "gross" refused and "net"
    # answered in one incomplete plan, and that per-item reading must stand.
    items = {e.id for e in query.elements if e.kind in ITEM_KINDS}
    blocked: Outcome | None = None
    for ids, outcome in ((unresolved, "refused"), (clarified, "asked"), (ambiguous, "confused")):
        if ids - items:
            blocked = outcome
            break

    observed: list[tuple[str, Outcome]] = []
    for element in query.elements:
        # Metrics and narrative spans only. A `narrative` span -- the "Why" in
        # "why did margins fall" -- is a thing the question asked for and got
        # an answer about, so it is an item. A `metric_qualifier` is not: its
        # refusal is attached to the *metric* it narrows, which already appears
        # here, and listing the qualifier too would double-count one failure.
        if element.kind not in ITEM_KINDS:
            continue
        if element.id in clarified:
            state: Outcome = "asked"
        elif element.id in ambiguous:
            state = "confused"
        elif element.id in unresolved:
            state = "refused"
        elif blocked is not None:
            state = blocked
        elif element.id in bound:
            state = "error" if spoiled else "answered"
        else:
            state = "error"
        observed.append((element.text, state))
    return observed


def pair(expected: list[str], observed: list[tuple[str, Outcome]]) -> list[Item]:
    """Line the two lists up positionally, padding whichever is shorter.

    A length mismatch is a finding in its own right, not a reason to give up
    on the comparison: the parser inventing a metric the question never named
    is exactly the bug this catches. "What are Apple's balance sheet totals:
    assets, ..." produced a seventh element for the heading, and nothing else
    would have shown it.
    """
    items: list[Item] = []
    for index in range(max(len(expected), len(observed))):
        want = expected[index] if index < len(expected) else NOT_EXPECTED
        phrase, got = observed[index] if index < len(observed) else ("(no item)", NOT_PRODUCED)
        items.append(Item(asked=phrase, expected=want, got=got))
    return items


def grade(items: list[Item], *, expected_count: int, stage: Stage) -> Grade:
    """Did the chain do what was written down for every item?

    ``unsafe`` is kept out of ``fail`` because the two are not comparable. A
    miss costs a refusal, which is the outcome this project prefers. Answering
    an item marked ``refused`` or ``asked`` puts a figure in front of someone
    where the honest reply was a decline or a question.
    """
    if stage == "parse" or not expected_count:
        return "ungraded"
    if any(item.expected in {"refused", "asked"} and item.got == "answered" for item in items):
        return "unsafe"
    return "pass" if all(item.ok for item in items) else "fail"


def _mismatch(items: list[Item]) -> str:
    """The failing items, shortest useful form."""
    bad = [item for item in items if not item.ok]
    if not bad:
        return ""
    first = f"{bad[0].asked!r}: want {bad[0].expected}, got {bad[0].got}"
    return first if len(bad) == 1 else f"{first} (+{len(bad) - 1} more)"


def _plan_detail(plan: QueryPlan) -> str:
    parts = [f"unresolved: {u.reason}" for u in plan.unresolved]
    parts += [f"asked {c.element_text!r}: {c.question}" for c in plan.clarifications]
    parts += [
        f"confused by {a.element_text!r}: "
        + ", ".join(f"{c.concept.name}@{c.score:.2f}" for c in a.candidates[:3])
        for a in plan.ambiguous
    ]
    return " | ".join(parts)


def _result_detail(result: ResultSet) -> str:
    verdict = result.verdict
    parts = [f"{verdict.status} {verdict.returned_rows}/{verdict.expected_rows} rows"]
    if verdict.unattributable:
        parts.append(f"{len(verdict.unattributable)} row(s) matched no binding")
    unanticipated = [cell for cell in verdict.missing if not cell.anticipated]
    if unanticipated:
        parts.append(f"{len(unanticipated)} unanticipated missing cell(s)")
    return ", ".join(parts)


async def run_one(entry: dict, *, stage: Stage, parser_model: str | None) -> Run:
    """One question, as far down the chain as ``stage`` allows.

    Never raises. A question that blows up is a data point, not a reason to
    lose the other fifty-five.
    """
    template = bool(entry.get("template", False))
    question = substitute(entry["question"], template=template)
    expected = expected_for(entry)
    started = time.monotonic()

    def done(observed: list[tuple[str, Outcome]], detail: str = "", **fields) -> Run:
        items = pair(expected, observed)
        return Run(
            id=entry["id"],
            question=question,
            grade=grade(items, expected_count=len(expected), stage=stage),
            items=items,
            detail=detail,
            template=template,
            seconds=time.monotonic() - started,
            **fields,
        )

    try:
        kwargs = {"model": parser_model} if parser_model else {}
        query = await parse_question(question, **kwargs)
    except (UnacceptableProposal, ProposalError, ValueError) as exc:
        # Nothing parsed, so there are no elements to line up. Report one
        # `confused` per thing that was asked for: the system did not
        # understand the question, which is what confusion means.
        return done(
            [("(did not parse)", "confused")] * max(len(expected), 1),
            detail=f"{type(exc).__name__}: {exc}",
        )

    if stage == "parse":
        return done([(e.text, "answered") for e in query.elements if e.kind in ITEM_KINDS])

    try:
        plan = await map_query(query)
    except Exception as exc:  # the mapper does not raise by design; record it if it does
        return done(
            [(e.text, "error") for e in query.elements if e.kind in ITEM_KINDS],
            detail=f"map_query {type(exc).__name__}: {exc}",
        )

    shared: dict = {
        "bindings": len(plan.bindings),
        "expected_rows": plan.result.row_count,
        "notes": [note.kind for note in plan.notes],
    }

    # Per part: SQL runs whenever any part can be answered, and the refused
    # and asked-back parts keep their own outcomes alongside it.
    if stage == "map" or not plan.has_answerable_part:
        return done(observe(query, plan, None), detail=_plan_detail(plan), **shared)

    try:
        result = await answer(plan)
    except (GenerationError, InvalidSQL, UnsupportedPlan) as exc:
        return done(crashed(query, plan), detail=f"{type(exc).__name__}: {exc}", **shared)
    except Exception as exc:
        return done(
            crashed(query, plan), detail=f"unexpected {type(exc).__name__}: {exc}", **shared
        )

    shared["notes"] = sorted({*shared["notes"], *(note.kind for note in result.notes)})
    shared["returned_rows"] = result.verdict.returned_rows
    return done(observe(query, plan, result), detail=_result_detail(result), **shared)


async def preflight() -> None:
    """Fail once, clearly, rather than fifty-six times with a stack trace."""
    try:
        async with QueryMapperSessionLocal() as session:
            loaded = await session.scalar(select(func.count()).select_from(Company))
    except Exception as exc:
        sys.exit(f"cannot reach the database ({type(exc).__name__}: {exc}). See BOOTSTRAP.md.")
    if not loaded:
        sys.exit("the store holds no companies. See BOOTSTRAP.md for the load step.")


LABEL = {"pass": "PASS", "fail": "MISS", "unsafe": "UNSAFE", "ungraded": "----"}


def line(index: int, total: int, run: Run) -> str:
    matched = sum(1 for item in run.items if item.ok)
    summary = (
        f"{matched}/{len(run.items)} as expected"
        if run.grade == "pass"
        else _mismatch(run.items) or run.detail[:60]
    )
    return (
        f"[{index:2}/{total}] {run.id}  {LABEL[run.grade]:6} "
        f"{run.seconds:5.1f}s  {summary[:78]}"
    )


def report(runs: list[Run], *, stage: Stage) -> None:
    """The summary. Ordered so the line that matters most is read last."""
    graded = [run for run in runs if run.grade != "ungraded"]
    print(f"\n{len(runs)} question(s), stage={stage}")

    items = [item for run in runs for item in run.items]
    if items:
        print("\nwhat happened to each thing asked for")
        for value in ("answered", "asked", "refused", "confused", "error", NOT_PRODUCED):
            count = sum(1 for item in items if item.got == value)
            if count:
                print(f"  {value:10} {count:4}")

    if stage == "parse":
        print("\nnot graded: a parse-only run has nothing to compare against.")
        return

    passed = [run for run in graded if run.grade == "pass"]
    failed = [run for run in graded if run.grade == "fail"]
    unsafe = [run for run in graded if run.grade == "unsafe"]
    rate = f"{len(passed) / len(graded):.0%}" if graded else "n/a"
    print(f"\npass {len(passed)}/{len(graded)}  ({rate})")

    if failed:
        print(f"\nMISSED ({len(failed)}) -- did not do what was written down:")
        for run in failed:
            print(f"  {run.id}  {run.question[:72]}")
            for item in run.items:
                if not item.ok:
                    print(f"        {item.asked!r}: want {item.expected}, got {item.got}")
            if run.detail:
                print(f"        {run.detail[:110]}")

    print()
    if unsafe:
        print(f"UNSAFE ({len(unsafe)}) -- answered something it should not have:")
        for run in unsafe:
            print(f"  {run.id}  {run.question[:72]}")
            for item in run.items:
                if item.expected in {"refused", "asked"} and item.got == "answered":
                    print(f"        {item.asked!r}: want {item.expected}, got answered")
    else:
        print("UNSAFE: none.")


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


def select_questions(args: argparse.Namespace) -> tuple[list[dict], list[dict]]:
    """``(to run, skipped as known gaps)``.

    A question carrying ``known_gap`` is a measured, understood failure with
    nobody working on it. Running it every time buys nothing and costs a slot
    in the pass rate that reads as a regression, so it is set aside and listed
    instead -- explicitly, by name, with the reason, so setting it aside can
    never be mistaken for it having been fixed.
    """
    document = yaml.safe_load(QUESTIONS.read_text("utf-8"))
    questions = document["questions"]
    if args.id:
        wanted = set(args.id)
        questions = [q for q in questions if q["id"] in wanted]
        missing = wanted - {q["id"] for q in questions}
        if missing:
            sys.exit(f"no such question id(s): {sorted(missing)}")
    if args.limit:
        questions = questions[: args.limit]
    gaps: list[dict] = []
    if not args.include_known_gaps:
        gaps = [q for q in questions if q.get("known_gap")]
        questions = [q for q in questions if not q.get("known_gap")]
    if not questions:
        sys.exit("no questions selected")
    return questions, gaps


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
    parser.add_argument("--limit", type=int, help="run only the first N selected")
    parser.add_argument("--parser-model", help="override the model [C] proposes with")
    parser.add_argument("--no-write", action="store_true", help="do not write the run file")
    parser.add_argument(
        "--include-known-gaps",
        action="store_true",
        help="also run questions marked `known_gap`, which are expected to fail",
    )
    args = parser.parse_args()

    questions, gaps = select_questions(args)
    if args.stage != "parse":
        await preflight()

    print(f"{len(questions)} question(s), stage={args.stage}, templates -> {TEMPLATE_COMPANY}\n")
    runs: list[Run] = []
    for index, entry in enumerate(questions, start=1):
        run = await run_one(entry, stage=args.stage, parser_model=args.parser_model)
        runs.append(run)
        print(line(index, len(questions), run))

    report(runs, stage=args.stage)
    if gaps:
        print("")
        print(f"KNOWN GAPS ({len(gaps)}) -- not run, not counted above:")
        for entry in gaps:
            print(f"  {entry['id']}  {entry['question'][:70]}")
            print(f"        {' '.join(entry['known_gap'].split())[:150]}")
    if not args.no_write:
        print(f"\nwritten: {write_run(runs, stage=args.stage)}")


if __name__ == "__main__":
    asyncio.run(main())

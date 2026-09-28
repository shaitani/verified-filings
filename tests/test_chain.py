"""``app.chain.ask`` over real parser / mapper / executor outputs, replayed.

Each fixture in tests/fixtures/chain/ is one live run of the chain (2026-09-27):
the QueryIn, QueryPlan and ResultSet it produced. The stages are replaced by
stubs returning them, so these run with no model and no database -- and the
failure paths are forced by stubs that raise.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

import app.chain as chain
from app.chain import BUG, EXECUTE_FAILED, PARSE_FAILED, PRESENT_FAILED, ask
from app.parser import UnacceptableProposal
from app.presenter import PresentationError
from app.retrieval import InvalidSQL
from app.schemas.query import QueryIn, QueryPlan, Unresolved
from app.schemas.result import ResultSet

FIXTURES = Path(__file__).parent / "fixtures" / "chain"


def _load(name: str) -> tuple[QueryIn, QueryPlan, ResultSet | None]:
    fixture = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    result = fixture["result_set"]
    return (
        QueryIn.model_validate(fixture["query_in"]),
        QueryPlan.model_validate(fixture["plan"]),
        ResultSet.model_validate(result) if result else None,
    )


def _stub(
    monkeypatch, name: str, *, plan=None, result=None, parse=None, answer=None
) -> list[tuple]:
    """Replace the three stages with the fixture's outputs (or with overrides).
    Returns the list the fake mapper records its inputs in."""
    query, fixture_plan, fixture_result = _load(name)
    mapped: list[tuple] = []

    async def fake_parse(question, *, answers=None):
        return parse(question) if parse else query

    async def fake_map(_query, *, pins=None):
        mapped.append((_query, pins))  # what the mapper was given, for assertions
        return plan or fixture_plan

    async def fake_answer(_plan):
        return answer(_plan) if answer else (result or fixture_result)

    monkeypatch.setattr(chain, "parse_question", fake_parse)
    monkeypatch.setattr(chain, "map_query", fake_map)
    monkeypatch.setattr(chain, "answer", fake_answer)
    return mapped


def _reply(outcome):
    return outcome.reply(uuid.uuid4(), uuid.uuid4())  # Reply's own validators run here


def _raises(exc):
    def raise_it(*_):
        raise exc

    return raise_it


# --------------------------------------------------------------------------- #
# Each kind of reply, from real runs
# --------------------------------------------------------------------------- #


async def test_a_plain_question_is_answered_through_every_stage(monkeypatch) -> None:
    _stub(monkeypatch, "q001")
    stages: list[str] = []
    outcome = await ask("q", on_stage=stages.append)
    reply = _reply(outcome)
    assert reply.status == "answered"
    assert reply.answer.rows[0].display == "$391.04B"
    assert stages == ["parsing", "mapping", "fetching", "presenting"]
    assert set(outcome.timings) == {"parse", "map", "answer", "present"}


async def test_a_refused_part_travels_beside_the_figures(monkeypatch) -> None:
    _stub(monkeypatch, "q052")  # Apple files no goodwill
    reply = _reply(await ask("q"))
    assert reply.status == "partial"
    outcomes = {part.text: part.outcome for part in reply.parts}
    assert outcomes["goodwill"] == "refused" and outcomes["assets"] == "answered"
    goodwill = next(part for part in reply.parts if part.text == "goodwill")
    assert goodwill.reason.startswith("No filed figure for 'goodwill'")  # curated, verbatim
    assert "goodwill" not in {row.metric for row in reply.answer.rows}


async def test_a_refusal_and_a_question_back_travel_together(monkeypatch) -> None:
    _stub(monkeypatch, "q033")  # "Why did Intel's margins fall in 2023?"
    stages: list[str] = []
    outcome = await ask("q", on_stage=stages.append)
    reply = _reply(outcome)
    assert [(p.text, p.outcome) for p in reply.parts] == [("Why", "refused"), ("margins", "asked")]
    assert reply.answer is None and "fetching" not in stages  # nothing bound, nothing fetched
    (record,) = outcome.asks
    assert record.kind == "clarification" and record.element_text == "margins"
    assert [o.metric for o in record.options] == [
        "gross_margin", "operating_margin", "net_margin",
    ]


async def test_a_sinking_refusal_is_shown_alone(monkeypatch) -> None:
    """q028: the period refusal sinks the question; the metric's knock-on
    refusal ("no reporting period in scope") is not shown beside it."""
    _stub(monkeypatch, "q028")
    reply = _reply(await ask("q"))
    assert reply.parts == [] and reply.blocking.stage == "map"
    assert "2015" in reply.blocking.reason
    assert "no reporting period in scope" not in reply.blocking.reason


async def test_a_company_not_loaded_sinks_the_comparison(monkeypatch) -> None:
    _stub(monkeypatch, "q036")
    reply = _reply(await ask("q"))
    assert reply.status == "refused" and "Samsung" in reply.blocking.reason


async def test_an_ambiguity_is_a_real_question_with_its_candidates(monkeypatch) -> None:
    _stub(monkeypatch, "ambiguous")  # "accounts payable"
    outcome = await ask("q")
    (part,) = _reply(outcome).parts
    assert part.outcome == "asked" and part.ask.kind == "ambiguity"
    assert [o.description for o in part.ask.options] == [
        "us-gaap:AccountsPayableCurrent", "us-gaap:IncreaseDecreaseInAccountsPayable",
    ]
    (record,) = outcome.asks
    assert all(option.concept is not None for option in record.options)  # ready for the pin


async def test_an_answered_clarification_is_answered_the_next_round(monkeypatch) -> None:
    _stub(monkeypatch, "q046_round2")  # "profit margin", answered "Gross margin"
    reply = _reply(await ask("q", answers=[("Which profit margin do you mean?", "Gross margin")]))
    assert reply.status == "answered" and reply.answer.rows[0].display == "46.9%"


async def test_an_average_arrives_as_one_figure(monkeypatch) -> None:
    _stub(monkeypatch, "q040")
    reply = _reply(await ask("q"))
    (row,) = reply.answer.rows
    assert row.company == "14 companies"


# --------------------------------------------------------------------------- #
# Failures: a fixed sentence for the reader, the detail for the trace
# --------------------------------------------------------------------------- #


async def test_a_question_the_parser_cannot_read(monkeypatch) -> None:
    _stub(monkeypatch, "q001", parse=_raises(UnacceptableProposal("span not in question")))
    outcome = await ask("q")
    reply = _reply(outcome)
    assert (reply.blocking.stage, reply.blocking.reason) == ("parse", PARSE_FAILED)
    assert outcome.errors[0].message == "span not in question"  # kept, never shown


async def test_a_bug_in_the_parser(monkeypatch) -> None:
    _stub(monkeypatch, "q001", parse=_raises(RuntimeError("boom")))
    outcome = await ask("q")
    assert _reply(outcome).blocking.reason == BUG
    assert outcome.errors[0].type == "RuntimeError" and "boom" in outcome.errors[0].traceback


async def test_a_rejected_statement_refuses_the_figures_but_not_the_mapper_s_parts(
    monkeypatch,
) -> None:
    _stub(monkeypatch, "q052", answer=_raises(InvalidSQL("no LIMIT")))
    reply = _reply(await ask("q"))
    reasons = {part.text: part.reason for part in reply.parts}
    assert reasons["assets"] == EXECUTE_FAILED
    assert reasons["goodwill"].startswith("No filed figure")  # the curated refusal stands
    assert reply.answer is None


async def test_an_unanswerable_verdict_is_not_shown(monkeypatch) -> None:
    _, _, result = _load("q001")
    verdict = result.verdict.model_copy(update={"status": "over", "expected_rows": 0})
    _stub(monkeypatch, "q001", result=result.model_copy(update={"verdict": verdict}))
    outcome = await ask("q")
    assert _reply(outcome).parts[0].reason == EXECUTE_FAILED
    assert outcome.errors[0].stage == "execute"


async def test_a_bug_while_fetching(monkeypatch) -> None:
    _stub(monkeypatch, "q001", answer=_raises(KeyError("b9")))
    assert _reply(await ask("q")).parts[0].reason == BUG


async def test_a_presentation_failure(monkeypatch) -> None:
    _stub(monkeypatch, "q001")
    monkeypatch.setattr(chain, "present", _raises(PresentationError("no display rule")))
    outcome = await ask("q")
    assert _reply(outcome).parts[0].reason == PRESENT_FAILED
    assert outcome.errors[0].stage == "present"


# --------------------------------------------------------------------------- #
# A part is answered with figures, or not at all
# --------------------------------------------------------------------------- #


async def test_rows_of_a_refused_part_are_not_shown(monkeypatch) -> None:
    """Bound for some companies and refused for another reads as refused (the
    evals' precedence) -- and its rows must not reach the answer."""
    query, plan, _ = _load("q052")
    assets = next(e for e in query.elements if e.text == "assets")
    refused = Unresolved(element_id=assets.id, reason="a unit violation", blocks_question=False)
    with_refusal = plan.model_copy(update={"unresolved": [*plan.unresolved, refused]})
    _stub(monkeypatch, "q052", plan=with_refusal)
    reply = _reply(await ask("q"))
    assert next(p for p in reply.parts if p.text == "assets").reason == "a unit violation"
    assert "assets" not in {row.metric for row in reply.answer.rows}


async def test_a_bound_part_with_no_rows_says_so(monkeypatch) -> None:
    _, _, result = _load("q052")
    rows = [row for row in result.rows if row.row.element_id != "e2"]  # drop assets' figure
    _stub(monkeypatch, "q052", result=result.model_copy(update={"rows": rows}))
    reply = _reply(await ask("q"))
    assets = next(p for p in reply.parts if p.text == "assets")
    assert assets.reason == "No figures came back for 'assets' for the periods asked about."


def test_several_sinking_reasons_are_kept_whole() -> None:
    assert chain._joined(["No Samsung.", "No 2015.", "No Samsung."]) == "No Samsung. No 2015."
    long = "x" * 300
    assert chain._joined([long, "y" * 300]) == long  # the second would be cut; it is left out


def test_the_reader_s_sentences_are_the_ones_decided() -> None:
    """app/api/DESIGN.md §12a, as the user worded them."""
    assert PARSE_FAILED.endswith("the company and the period explicitly.")
    assert EXECUTE_FAILED.startswith("The figures for this query came back")
    assert PRESENT_FAILED == "Something went wrong attempting to display the results."
    assert BUG.endswith("jk, time to debug.")


# --------------------------------------------------------------------------- #
# The next round: picks become answers or pins (slice 2)
# --------------------------------------------------------------------------- #


async def _pending(monkeypatch, name: str):
    _stub(monkeypatch, name)
    return (await ask("q")).pending


async def test_only_a_round_that_asked_something_leaves_something_pending(monkeypatch) -> None:
    _stub(monkeypatch, "q001")
    assert (await ask("q")).pending is None
    pending = await _pending(monkeypatch, "ambiguous")
    assert pending.query_in.elements and [a.kind for a in pending.asks] == ["ambiguity"]
    assert pending.model_validate_json(pending.model_dump_json()) == pending  # stored as JSON


async def test_a_pick_becomes_a_pin_or_an_answer(monkeypatch) -> None:
    pending = await _pending(monkeypatch, "ambiguous")
    answers, pins = chain.resolve_choices(pending, [("a1", "o2")])
    assert answers == [] and pins["accounts payable"].name == "IncreaseDecreaseInAccountsPayable"

    pending = await _pending(monkeypatch, "q046")  # "profit margin": a curated question
    answers, pins = chain.resolve_choices(pending, [("a1", "o1")])
    assert pins == {} and answers == [(pending.asks[0].question, "Gross margin")]


@pytest.mark.parametrize("choice", [("a9", "o1"), ("a1", "o9")])
async def test_an_option_never_offered_is_refused(monkeypatch, choice) -> None:
    pending = await _pending(monkeypatch, "ambiguous")
    with pytest.raises(chain.UnknownChoice, match="was offered"):
        chain.resolve_choices(pending, [choice])


async def test_a_round_of_picks_alone_re_maps_without_parsing(monkeypatch) -> None:
    pending = await _pending(monkeypatch, "ambiguous")
    mapped = _stub(monkeypatch, "ambiguous_round2", parse=_raises(AssertionError("parsed")))
    stages: list[str] = []
    outcome, answers, pins = await chain.ask_again(
        "q", pending, [("a1", "o1")], on_stage=stages.append
    )
    assert "parsing" not in stages and "parse" not in outcome.timings
    ((query, given),) = mapped
    assert query is pending.query_in  # the stored parse, unchanged
    assert {eid: c.name for eid, c in given.items()} == {"e2": "AccountsPayableCurrent"}
    assert outcome.reply(uuid.uuid4(), uuid.uuid4()).status == "answered"
    assert outcome.answer.citations["b0"].resolved_by == "pinned"
    assert answers == [] and set(pins) == {"accounts payable"}


async def test_a_new_curated_answer_parses_again_and_keeps_earlier_picks(monkeypatch) -> None:
    pending = await _pending(monkeypatch, "q046")
    concept = _load("ambiguous_round2")[1].bindings[0].concepts[0]
    earlier_pin = {"accounts payable": concept}  # a pick from a round before this one
    seen: list = []
    _stub(monkeypatch, "q046_round2", parse=lambda q: seen.append(q) or _load("q046_round2")[0])
    _, answers, pins = await chain.ask_again(
        "q", pending, [("a1", "o1")], answers=[("earlier?", "Yes")], pins=earlier_pin
    )
    assert seen == ["q"]  # parsed again: the words changed
    assert answers == [("earlier?", "Yes"), (pending.asks[0].question, "Gross margin")]
    assert set(pins) == {"accounts payable"}  # carried forward, whatever this round did


def test_a_pin_finds_its_element_by_phrase_not_by_id() -> None:
    """A re-parse may number elements differently; the phrase is what persists.
    A phrase the re-parse no longer produces finds nothing, and so is asked again."""
    query, _, _ = _load("ambiguous")
    concept = _load("ambiguous_round2")[1].bindings[0].concepts[0]
    found = chain._pins_by_element(query, {"  Accounts   PAYABLE ": concept, "vanished": concept})
    assert found == {"e2": concept}

"""Validates evals/questions.yaml.

The file is hand-edited and its counts drive a design decision, so a typo in a
tag should fail the suite rather than quietly skew a distribution.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from evals.run import (
    EXPECTABLE,
    NOT_EXPECTED,
    TEMPLATE_COMPANY,
    Item,
    expected_for,
    grade,
    pair,
    substitute,
)

QUESTIONS = Path(__file__).resolve().parent.parent / "evals" / "questions.yaml"

#: What a person may write for one item. "confused" and "error" are
#: observation-only -- nobody writes down that they want the system
#: confused -- so they are absent here on purpose.
EXPECT = {"answered", "asked", "refused"}
NEEDS = {
    "retrieval",
    "aggregation",
    "ranking",
    "growth",
    "ratio",
    "cross_company_total",
    "multi_step",
}
BLOCKED_BY = {
    "segment_data",
    "filing_text",
    "out_of_scope_period",
    "not_in_dataset",
    "causal",
    "company_not_loaded",
    "sector_classification",
}
SHAPES = {"scalar", "series", "table", "ranking"}
EXERCISES = {
    "q4_residual",
    "concept_drift",
    "fiscal_calendar",
    "derived_metric",
    "instant_fact",
    "alias_gap",
    "clarification",
    "mixed_granularity",
}


@pytest.fixture(scope="module")
def questions() -> list[dict]:
    document = yaml.safe_load(QUESTIONS.read_text("utf-8"))
    assert document["version"] == 1
    return document["questions"]


def test_ids_are_unique(questions) -> None:
    ids = [q["id"] for q in questions]
    assert len(ids) == len(set(ids))


def test_every_question_has_text(questions) -> None:
    for q in questions:
        assert q.get("question", "").strip(), q["id"]


@pytest.mark.parametrize(
    "field,vocabulary", [("needs", NEEDS), ("blocked_by", BLOCKED_BY), ("exercises", EXERCISES)]
)
def test_tags_come_from_the_vocabulary(questions, field, vocabulary) -> None:
    for q in questions:
        unknown = set(q.get(field, [])) - vocabulary
        assert not unknown, f"{q['id']} has unknown {field}: {sorted(unknown)}"


def test_template_flag_is_boolean(questions) -> None:
    for q in questions:
        if "template" in q:
            assert isinstance(q["template"], bool), q["id"]


def test_shape_values_are_known(questions) -> None:
    for q in questions:
        if "shape" in q:
            assert q["shape"] in SHAPES, q["id"]


def test_expect_is_a_list_of_known_values(questions) -> None:
    """One entry per thing the question asks for.

    A scalar here is the old single-verdict shape, which could not say that
    five of six figures came back.
    """
    for q in questions:
        expect = q.get("expect")
        assert isinstance(expect, list), f"{q['id']}: expect must be a list"
        assert expect, f"{q['id']}: expect is empty"
        unknown = set(expect) - EXPECT
        assert not unknown, f"{q['id']} expects unwritable value(s): {sorted(unknown)}"


def test_a_per_company_expectation_lines_up_with_the_default(questions) -> None:
    """`expect_by_company` replaces `expect` for one template filer, item for
    item -- the same asks, a different answer to one of them. A different
    length would be a different question, and only a template has a filer to
    vary."""
    for q in questions:
        overrides = q.get("expect_by_company")
        if overrides is None:
            continue
        assert q.get("template"), f"{q['id']}: expect_by_company needs template: true"
        assert isinstance(overrides, dict) and overrides, q["id"]
        for company, expect in overrides.items():
            assert isinstance(expect, list), f"{q['id']}/{company}: must be a list"
            assert len(expect) == len(q["expect"]), f"{q['id']}/{company}: length differs"
            unknown = set(expect) - EXPECT
            assert not unknown, f"{q['id']}/{company}: unwritable value(s) {sorted(unknown)}"


def test_the_template_company_s_own_expectation_is_used() -> None:
    entry = {
        "template": True,
        "expect": ["answered", "answered"],
        "expect_by_company": {TEMPLATE_COMPANY: ["answered", "refused"]},
    }
    assert expected_for(entry) == ["answered", "refused"]
    assert expected_for({**entry, "template": False}) == ["answered", "answered"]
    assert expected_for({"expect": ["refused"]}) == ["refused"]


def test_nobody_expects_the_system_to_be_confused(questions) -> None:
    """`confused` and `error` are observation-only.

    Both mean the machinery failed to understand or fell over. Writing one
    down as the desired outcome would make a defect scoreable as a pass.
    """
    assert "confused" not in EXPECT and "error" not in EXPECT
    assert set(EXPECTABLE) == EXPECT


def test_questions_that_should_answer_declare_what_they_need(questions) -> None:
    """A question expecting any answer at all has to say what answering takes
    -- the tally in summarize.py is the whole point of the tag."""
    for q in questions:
        if "answered" in q.get("expect", []) and not q.get("blocked_by"):
            assert "retrieval" in q.get("needs", []), q["id"]


def test_a_known_gap_explains_itself(questions) -> None:
    """A question set aside has to say why, in enough detail to re-open it.

    `known_gap` removes a question from every run, so the one thing that must
    not happen is it quietly staying out after the cause is fixed. The reason
    is what someone reads to decide that.
    """
    for q in questions:
        if "known_gap" in q:
            gap = q["known_gap"]
            assert isinstance(gap, str) and len(gap.split()) >= 10, q["id"]


def test_questions_that_should_refuse_say_why(questions) -> None:
    """A refusal has to name the hazard behind it, so a refusal that starts
    happening for a *new* reason is visible rather than silently still green."""
    for q in questions:
        if set(q.get("expect", [])) == {"refused"}:
            assert q.get("blocked_by"), q["id"]


# --------------------------------------------------------------------------- #
# The runner's scorer
# --------------------------------------------------------------------------- #
#
# evals/run.py itself is not unit-testable -- it is three network calls in a
# loop. Its *scoring rule* is, and that rule is the part worth pinning: it
# decides what a run means, and a silent change to it would move every number
# the set produces without anything failing.


def _items(*triples) -> list[Item]:
    """``("goodwill", "answered", "refused")`` -> one Item."""
    return [Item(asked=a, expected=e, got=g) for a, e, g in triples]


def _grade(*triples, stage="answer"):
    items = _items(*triples)
    return grade(items, expected_count=len(items), stage=stage)


def test_every_item_must_match_for_a_pass() -> None:
    assert _grade(("revenue", "answered", "answered")) == "pass"
    assert _grade(("revenue", "answered", "refused")) == "fail"


def test_one_bad_item_among_many_fails_the_question() -> None:
    """q052 is why. Five of six figures came back; the sixth did not, and a
    green line there would hide it."""
    assert _grade(
        ("assets", "answered", "answered"),
        ("cash", "answered", "answered"),
        ("goodwill", "answered", "refused"),
    ) == "fail"


def test_answering_something_marked_refused_is_unsafe_not_merely_failed() -> None:
    """The asymmetry the whole scorer exists for.

    Every other failure costs a refusal, which this project prefers. This one
    puts a figure in front of someone where the honest reply was a decline.
    """
    assert _grade(("market cap", "refused", "answered")) == "unsafe"
    assert _grade(("market cap", "refused", "confused")) == "fail"
    assert _grade(("market cap", "refused", "refused")) == "pass"


def test_answering_something_marked_asked_is_also_unsafe() -> None:
    """A curated clarification exists where a default would be quietly wrong:
    gross and net profit margin differ by twenty points on one company."""
    assert _grade(("profit margin", "asked", "answered")) == "unsafe"
    assert _grade(("profit margin", "asked", "asked")) == "pass"


def test_confusion_never_passes_whatever_was_expected() -> None:
    """`confused` means the term was not understood -- the candidates are raw
    XBRL names and cosine scores, which is not a question anyone can answer.
    It is never the desired outcome, so it can never match one."""
    for expected in EXPECTABLE:
        assert _grade(("revenue growth", expected, "confused")) != "pass"


def test_a_length_mismatch_cannot_pass() -> None:
    """The parser inventing an item the question never named.

    "What are Apple's balance sheet totals: assets, ..." produced a seventh
    element for the heading. Padding makes it visible instead of letting the
    first six line up and score green.
    """
    items = pair(["answered", "answered"], [("assets", "answered"), ("cash", "answered"),
                                            ("balance sheet totals", "confused")])
    assert len(items) == 3
    assert items[2].expected == NOT_EXPECTED
    assert grade(items, expected_count=2, stage="answer") == "fail"


def test_a_parse_only_run_is_not_graded() -> None:
    assert _grade(("revenue", "answered", "answered"), stage="parse") == "ungraded"


def test_an_unlisted_question_is_not_graded() -> None:
    assert grade([], expected_count=0, stage="answer") == "ungraded"


def test_templates_are_substituted_only_when_flagged() -> None:
    assert substitute("What is <Company>'s profit", template=True) == (
        f"What is {TEMPLATE_COMPANY}'s profit"
    )
    assert substitute("What is <Company>'s profit", template=False) == (
        "What is <Company>'s profit"
    )


def test_every_template_question_carries_the_placeholder(questions) -> None:
    """A question flagged `template` but written with a real company would be
    silently rewritten to Apple; one carrying the placeholder without the flag
    would reach the parser with a literal `<Company>` in it."""
    for q in questions:
        has_placeholder = "<Company>" in q["question"]
        assert has_placeholder == bool(q.get("template", False)), q["id"]

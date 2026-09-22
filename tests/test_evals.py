"""Validates evals/questions.yaml.

The file is hand-edited and its counts drive a design decision, so a typo in a
tag should fail the suite rather than quietly skew a distribution.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from evals.run import TEMPLATE_COMPANY, grade, substitute

QUESTIONS = Path(__file__).resolve().parent.parent / "evals" / "questions.yaml"

#: "unknown", or omitting the field, means a question has been added but not
#: triaged. Allowed on purpose -- making people classify before they can write
#: down a question is how a set like this stops growing.
EXPECT = {"answerable", "partial", "refuse", "unknown"}
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
    "restatement_metadata",
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


def test_expect_values_are_known(questions) -> None:
    for q in questions:
        assert q.get("expect", "unknown") in EXPECT, q["id"]


def test_answerable_questions_declare_what_they_need(questions) -> None:
    """Once a question is classified as answerable, it has to say what that
    takes -- the tally is the whole point. Untriaged entries are exempt."""
    for q in questions:
        if q.get("expect") in {"answerable", "partial"} and not q.get("blocked_by"):
            assert "retrieval" in q.get("needs", []), q["id"]


def test_refused_questions_say_why(questions) -> None:
    for q in questions:
        if q.get("expect") == "refuse":
            assert q.get("blocked_by"), q["id"]


# --------------------------------------------------------------------------- #
# The runner's scorer
# --------------------------------------------------------------------------- #
#
# evals/run.py itself is not unit-testable -- it is three network calls in a
# loop. Its *scoring rule* is, and that rule is the part worth pinning: it
# decides what a run means, and a silent change to it would move every number
# the set produces without anything failing.


def test_answerable_passes_only_when_answered() -> None:
    assert grade("answerable", "answered", stage="answer") == "pass"
    for outcome in ("refused", "clarify", "unanswerable", "parse_failed", "error"):
        assert grade("answerable", outcome, stage="answer") == "fail"


def test_partial_is_scored_like_answerable() -> None:
    """A `partial` question should come back with what resolved plus a caveat.

    Nothing here inspects the caveat -- see the module docstring. What this
    pins is that refusing a partial question counts as a miss, not a pass.
    """
    assert grade("partial", "answered", stage="answer") == "pass"
    assert grade("partial", "refused", stage="answer") == "fail"


def test_answering_a_refuse_question_is_unsafe_not_merely_failed() -> None:
    """The asymmetry the whole scorer exists for.

    Every other failure costs a refusal. This one costs a number somebody
    might act on, so it must never be averaged into the same bucket.
    """
    assert grade("refuse", "answered", stage="answer") == "unsafe"
    for outcome in ("refused", "clarify", "unanswerable", "parse_failed", "error"):
        assert grade("refuse", outcome, stage="answer") == "pass"


def test_earlier_stages_are_not_graded() -> None:
    """[C] deliberately does not judge answerability (app/producer/DESIGN.md
    §7), so scoring a parse-only run would measure the wrong component."""
    for stage in ("parse", "map"):
        assert grade("refuse", "answered", stage=stage) == "ungraded"
        assert grade("answerable", "parsed", stage=stage) == "ungraded"


def test_untriaged_questions_are_not_counted() -> None:
    assert grade("unknown", "answered", stage="answer") == "untriaged"


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

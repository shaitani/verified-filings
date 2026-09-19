"""Validates evals/questions.yaml.

The file is hand-edited and its counts drive a design decision, so a typo in a
tag should fail the suite rather than quietly skew a distribution.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

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

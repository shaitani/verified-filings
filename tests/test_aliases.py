"""Tests for app/schemas/aliases.py and app/semantic/aliases.py.

Pure -- no database. Two jobs here: that the real curated file stays valid (it
is edited by hand, so a typo should fail the suite rather than surface as a
mysteriously unresolvable metric), and that the invariants protecting lookup
from ambiguity actually hold.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.schemas.aliases import AliasFile, MetricAlias, split_concept_ref
from app.semantic.aliases import ALIAS_FILE, AliasIndex, alias_index, load_aliases, normalize

FAKE_ALIASES = Path(__file__).parent / "fixtures" / "aliases_fake.yaml"


def _document(metrics: dict) -> dict:
    return {"version": 1, "metrics": metrics}


# --------------------------------------------------------------------------- #
# The real curated file
# --------------------------------------------------------------------------- #


def test_the_shipped_alias_file_is_valid() -> None:
    """Hand-edited data: a typo should fail here, not at query time."""
    index = alias_index()
    assert len(index) > 0
    assert index.lookup("revenue") is not None


def test_shipped_file_reaches_every_metric_by_its_own_name() -> None:
    """A metric key must itself be a working surface form -- otherwise an entry
    can only be found through synonyms someone remembered to add."""
    document = AliasFile.model_validate(yaml.safe_load(ALIAS_FILE.read_text("utf-8")))
    index = alias_index()
    for metric in document.metrics:
        hit = index.lookup(metric)
        assert hit is not None and hit.metric == metric


def test_shipped_file_only_references_known_taxonomies() -> None:
    document = AliasFile.model_validate(yaml.safe_load(ALIAS_FILE.read_text("utf-8")))
    for alias in document.metrics.values():
        for slot in alias.terms:
            for ref in slot:
                taxonomy, _ = split_concept_ref(ref)
                assert taxonomy in {"dei", "us-gaap", "srt"}


# --------------------------------------------------------------------------- #
# Normalization
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Revenue", "revenue"),
        ("  Net   Sales  ", "net sales"),
        ("free_cash_flow", "free cash flow"),
        ("SG&A", "sga"),
        ("R&D", "rd"),
    ],
)
def test_normalize(raw: str, expected: str) -> None:
    assert normalize(raw) == expected


def test_underscores_fold_so_metric_keys_are_reachable_as_prose() -> None:
    """``free_cash_flow`` should answer to "free cash flow" without anyone
    having to list that as a synonym."""
    assert alias_index().lookup("free cash flow").metric == "free_cash_flow"


# --------------------------------------------------------------------------- #
# Invariants
# --------------------------------------------------------------------------- #


def test_duplicate_surface_form_across_metrics_is_rejected() -> None:
    """Two entries claiming "sales" would make lookup depend on dict order."""
    with pytest.raises(ValidationError, match="claimed by both"):
        AliasFile.model_validate(
            _document(
                {
                    "revenue": {
                        "label": "Revenue",
                        "synonyms": ["sales"],
                        "terms": [["us-gaap:A"]],
                    },
                    "bookings": {
                        "label": "Bookings",
                        "synonyms": ["sales"],
                        "terms": [["us-gaap:B"]],
                    },
                }
            )
        )


def test_forms_colliding_only_after_normalizing_are_rejected() -> None:
    """The metric key "net_income" and the synonym "Net Income" are distinct
    raw strings, so the schema's check passes them -- but they fold to the same
    lookup key, and the index has to be the one that catches it."""
    document = AliasFile.model_validate(
        _document(
            {
                "net_income": {"label": "Net income", "terms": [["us-gaap:A"]]},
                "earnings": {
                    "label": "Earnings",
                    "synonyms": ["Net Income"],
                    "terms": [["us-gaap:B"]],
                },
            }
        )
    )
    with pytest.raises(ValueError, match="already claimed by"):
        AliasIndex(document)


def test_malformed_concept_reference_is_rejected() -> None:
    with pytest.raises(ValidationError, match="not a <taxonomy>:<ConceptName>"):
        MetricAlias(label="X", terms=[["Revenues"]])  # missing taxonomy


def test_unknown_taxonomy_is_rejected() -> None:
    with pytest.raises(ValidationError, match="not a <taxonomy>:<ConceptName>"):
        MetricAlias(label="X", terms=[["ifrs:Revenue"]])


def test_expression_cannot_reference_a_missing_operand_slot() -> None:
    with pytest.raises(ValidationError, match="references c1"):
        MetricAlias(label="X", expression="c0 / c1", terms=[["us-gaap:A"]])


def test_empty_operand_slot_is_rejected() -> None:
    with pytest.raises(ValidationError):
        MetricAlias(label="X", terms=[[]])


def test_expression_defaults_to_the_single_operand() -> None:
    assert MetricAlias(label="X", terms=[["us-gaap:A"]]).expression == "c0"


# --------------------------------------------------------------------------- #
# The test fixture file
# --------------------------------------------------------------------------- #


def test_fake_alias_file_loads() -> None:
    index = load_aliases(FAKE_ALIASES)
    hit = index.lookup("widget sales")
    assert hit is not None
    assert hit.metric == "widget_revenue"
    assert hit.terms[0] == (("us-gaap", "ZzzTestNeverLoaded"), ("us-gaap", "ZzzTestRevenues"))


def test_unknown_phrase_returns_none_so_the_caller_can_fall_through() -> None:
    assert alias_index().lookup("blorptastic synergy index") is None

"""Pydantic v2 schemas for one curated XBRL-data file (``data/xbrl/<TICKER>.json``).

These validate a file on the way IN, before the load step (``app/db/loader.py``,
not written yet) turns it into ORM rows. Inbound validation only -- outbound /
read DTOs are a separate, later concern.

Full rationale for every choice here: ``app/schemas/DESIGN.md``. In brief:

* ``extra="forbid"`` -- an unknown key fails the file (we generate these files;
  a surprise key means a bug or an un-migrated format change).
* ``frozen=True`` -- validated instances are read-only; the loader reads them and
  builds separate ORM objects, never mutates them.
* ``fp`` / ``form`` / taxonomy are ``Literal`` -- only the scope-filtered values
  exist in the data (verified across all 20 files); anything else fails loudly.
* ``val`` mirrors the ``Numeric(30, 6)`` column; string fields mirror their DB
  column lengths. No format regexes on ``accn`` / ``frame``.
* the ``counts`` block is re-derived from the facts tree and a mismatch raises.
* field names mirror the SOURCE JSON (``start`` / ``end`` / ``val``), not the ORM
  (``period_start`` / ``period_end`` / ``value``) -- the loader does the rename.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Taxonomy = Literal["dei", "us-gaap", "srt"]
FiscalPeriod = Literal["FY", "Q1", "Q2", "Q3"]
FilingForm = Literal["10-K", "10-Q"]


class _Base(BaseModel):
    """Shared config for every schema in this module.

    * ``extra="forbid"`` -- reject unknown keys (drift in our own file format).
    * ``frozen=True`` -- immutable after validation.
    * no ``str_strip_whitespace`` -- no silent coercion; a stray-whitespace value
      should fail, not be quietly cleaned.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class FactIn(_Base):
    """One element of ``...units.<unit>[]`` in the source file."""

    end: date
    val: Decimal = Field(max_digits=30, decimal_places=6)  # mirrors Numeric(30, 6)
    accn: str = Field(max_length=20)  # mirrors Filing.accession_number; no regex
    fy: int = Field(ge=2000, le=2100)  # loose sanity bound, NOT the scope check
    fp: FiscalPeriod
    form: FilingForm
    filed: date
    start: date | None = None  # absent => instant fact
    frame: str | None = Field(default=None, max_length=16)  # no regex

    @model_validator(mode="after")
    def _start_not_after_end(self) -> FactIn:
        if self.start is not None and self.start > self.end:
            raise ValueError(f"start {self.start} is after end {self.end}")
        return self


class ConceptIn(_Base):
    """One ``facts.<taxonomy>.<ConceptName>`` entry."""

    label: str | None = Field(default=None, max_length=512)  # mirrors Concept.label
    description: str | None = None  # -> Text column, unbounded

    #: Any unit key is accepted; the load step applies ``ALLOWED_UNITS``.
    #: ``min_length=1``: retrieval prunes empty units, so zero would be a bug.
    units: dict[str, list[FactIn]] = Field(min_length=1)


class ScopeIn(_Base):
    forms: list[FilingForm] = Field(min_length=1)
    fiscal_years: list[int] = Field(min_length=1)


class CountsIn(_Base):
    taxonomies: int = Field(ge=0)
    concepts: int = Field(ge=0)
    facts: int = Field(ge=0)


class CompanyFactsFile(_Base):
    """The whole ``data/xbrl/<TICKER>.json`` document."""

    cik: int = Field(ge=1)
    ticker: str = Field(min_length=1, max_length=10)
    entity_name: str = Field(min_length=1, max_length=150)
    source_url: str = Field(min_length=1, max_length=255)
    retrieved: AwareDatetime  # source always carries a 'Z' offset
    scope: ScopeIn
    counts: CountsIn
    facts: dict[Taxonomy, dict[str, ConceptIn]] = Field(min_length=1)

    @model_validator(mode="after")
    def _counts_match_tree(self) -> CompanyFactsFile:
        n_tax = len(self.facts)
        n_concepts = sum(len(cs) for cs in self.facts.values())
        n_facts = sum(
            len(rows)
            for cs in self.facts.values()
            for c in cs.values()
            for rows in c.units.values()
        )
        bad = {
            name: {"declared": declared, "actual": actual}
            for name, declared, actual in (
                ("taxonomies", self.counts.taxonomies, n_tax),
                ("concepts", self.counts.concepts, n_concepts),
                ("facts", self.counts.facts, n_facts),
            )
            if declared != actual
        }
        if bad:
            raise ValueError(f"counts block disagrees with facts tree: {bad}")
        return self

    def iter_facts(self) -> Iterator[tuple[str, str, ConceptIn, str, FactIn]]:
        """Yield ``(taxonomy, concept_name, concept, unit, fact)`` for every fact.

        The single place the nested walk is written -- the loader consumes this
        rather than re-implementing the four-deep loop.
        """
        for taxonomy, concepts in self.facts.items():
            for concept_name, concept in concepts.items():
                for unit, rows in concept.units.items():
                    for fact in rows:
                        yield taxonomy, concept_name, concept, unit, fact

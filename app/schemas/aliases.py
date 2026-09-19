"""Pydantic v2 schemas for the curated alias file
(``app/semantic/aliases.yaml``).

The file is the accounting judgment the embedding search cannot supply: which
XBRL concepts a business term like "revenue" or "free cash flow" actually maps
to. It is data rather than code so that extending it is an edit, not a deploy.

Full rationale: ``app/schemas/DESIGN.md`` section 9. In brief:

* ``terms`` is a list of **operand slots**; each slot holds *alternative*
  concepts in preference order, not concepts to combine. The resolver takes the
  first alternative with facts covering the requested periods for that company,
  so filer divergence resolves from data instead of per-company curation.
* ``expression`` is arithmetic over the slots (``c0``, ``c1``, ...), defaulting
  to ``"c0"``. It is a string the SQL step reads; nothing here is evaluated.
* synonyms must be unique across the whole file -- two entries claiming the
  same surface form would make lookup order-dependent, so it fails the load.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.xbrl import Taxonomy

#: "us-gaap:Revenues" -- taxonomy and element name, as the taxonomies write it.
_CONCEPT_REF = re.compile(r"^(dei|us-gaap|srt):([A-Za-z][A-Za-z0-9]*)$")

#: Operand reference inside ``expression``.
_OPERAND_REF = re.compile(r"c(\d+)")


class _Base(BaseModel):
    """Same strictness as the other schema modules: unknown keys fail the file,
    instances are read-only once validated."""

    model_config = ConfigDict(extra="forbid", frozen=True)


def split_concept_ref(ref: str) -> tuple[Taxonomy, str]:
    """``"us-gaap:Revenues"`` -> ``("us-gaap", "Revenues")``."""
    match = _CONCEPT_REF.match(ref)
    if match is None:
        raise ValueError(f"{ref!r} is not a <taxonomy>:<ConceptName> reference")
    taxonomy, name = match.groups()
    return taxonomy, name  # type: ignore[return-value]


class MetricAlias(_Base):
    """One curated business term."""

    label: str = Field(min_length=1, max_length=120)
    synonyms: list[str] = Field(default_factory=list)

    #: Arithmetic over the operand slots. "c0" for a plain lookup, "c0 / c1"
    #: for a ratio, "c0 - c1" for a difference.
    expression: str = Field(default="c0", min_length=1, max_length=256)

    #: One entry per operand slot; each entry is alternatives in preference
    #: order. ``min_length=1`` on both: a slot with no candidates, or a metric
    #: with no slots, can never resolve and is a typo rather than a choice.
    terms: list[list[str]] = Field(min_length=1)

    @model_validator(mode="after")
    def _refs_are_wellformed(self) -> MetricAlias:
        for slot in self.terms:
            if not slot:
                raise ValueError("an operand slot must list at least one concept")
            for ref in slot:
                split_concept_ref(ref)  # raises on a malformed reference

        for index in _OPERAND_REF.findall(self.expression):
            if int(index) >= len(self.terms):
                raise ValueError(
                    f"expression {self.expression!r} references c{index}, "
                    f"but only {len(self.terms)} operand slot(s) are defined"
                )
        return self


class AliasFile(_Base):
    """The whole ``aliases.yaml`` document."""

    version: Literal[1]
    metrics: dict[str, MetricAlias] = Field(min_length=1)

    @model_validator(mode="after")
    def _surface_forms_are_unique(self) -> AliasFile:
        """A surface form may only belong to one metric.

        Two entries claiming "sales" would make the answer depend on dict
        order, which is exactly the kind of quiet wrongness this layer exists
        to remove. Compared on the raw strings here; ``app.semantic.aliases``
        normalizes before matching, and its loader re-checks for collisions
        that only appear after normalizing.
        """
        seen: dict[str, str] = {}
        for metric, alias in self.metrics.items():
            for form in (metric, *alias.synonyms):
                if form in seen:
                    raise ValueError(
                        f"surface form {form!r} is claimed by both {seen[form]!r} and {metric!r}"
                    )
                seen[form] = metric
        return self

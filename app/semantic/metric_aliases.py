"""The curated metric alias layer: ``metric_aliases.yaml`` and its lookup.

    from app.semantic.metric_aliases import alias_index
    hit = alias_index().lookup("free cash flow")

Three things in one module because they are one thing: the models that validate
the file, the normalizing that folds surface forms, and the index that answers
lookups. The file ships beside this module and nothing else reads it.

Design notes: ``app/semantic/DESIGN.md``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.xbrl import Taxonomy

ALIAS_FILE = Path(__file__).with_name("metric_aliases.yaml")

#: "us-gaap:Revenues" -- taxonomy and element name, as the taxonomies write it.
_CONCEPT_REF = re.compile(r"^(dei|us-gaap|srt):([A-Za-z][A-Za-z0-9]*)$")

#: Operand reference inside ``expression``.
_OPERAND_REF = re.compile(r"c(\d+)")

#: Anything that isn't a letter, digit or space. Dropped rather than replaced,
#: so "R&D" folds to "rd" while "R and D" stays "r and d" -- the file lists
#: both spellings rather than relying on them colliding.
_PUNCTUATION = re.compile(r"[^\w\s]")
_WHITESPACE = re.compile(r"\s+")


# --------------------------------------------------------------------------- #
# The file format
# --------------------------------------------------------------------------- #


class _Base(BaseModel):
    """Same strictness as the schema modules: unknown keys fail the file,
    instances are read-only once validated."""

    model_config = ConfigDict(extra="forbid", frozen=True)


def split_concept_ref(ref: str) -> tuple[Taxonomy, str]:
    """``"us-gaap:Revenues"`` -> ``("us-gaap", "Revenues")``."""
    match = _CONCEPT_REF.match(ref)
    if match is None:
        raise ValueError(f"{ref!r} is not a <taxonomy>:<ConceptName> reference")
    taxonomy, name = match.groups()
    return taxonomy, name  # type: ignore[return-value]


#: What an operand's sign means, which only matters inside an expression.
#:   "signed"    -- the value's sign is information. Operating cash flow goes
#:                  negative on real cash burn; gross profit goes negative in a
#:                  bad year. Left alone. The default.
#:   "magnitude" -- the concept is a size and the expression's operator carries
#:                  the direction. Capex is tagged positive and subtracted; a
#:                  filer tagging it negative would make `c0 - c1` *add*.
OperandSign = Literal["signed", "magnitude"]


class OperandSlot(_Base):
    """An operand slot that needs more than a bare list of alternatives.

    Only worth writing when ``sign`` is load-bearing -- a plain list is still
    accepted and means ``sign: signed``.
    """

    concepts: list[str] = Field(min_length=1)
    sign: OperandSign = "signed"


class ClarifyOption(_Base):
    """One choice offered back when a term is genuinely several things."""

    #: Another metric key in this file. Validated to exist, so a choice always
    #: leads somewhere resolvable.
    metric: str = Field(min_length=1, max_length=64)

    #: Why someone would pick this one, in business terms.
    description: str = Field(min_length=1, max_length=240)


class ClarifySpec(_Base):
    """What to ask when a term cannot be pinned down without more from the
    person who asked.

    The alternative is picking a convention and being quietly wrong: "profit
    margin" defaults to *net* by convention, but gross and net can differ by
    twenty points on the same company, and someone reading one while meaning
    the other has been given a wrong answer with no signal.
    """

    question: str = Field(min_length=1, max_length=240)
    options: list[ClarifyOption] = Field(min_length=2)


class MetricAlias(_Base):
    """One curated business term.

    Either it resolves (``terms``) or it asks (``clarify``), never both.
    """

    label: str = Field(min_length=1, max_length=120)
    synonyms: list[str] = Field(default_factory=list)

    #: Arithmetic over the operand slots. "c0" for a plain lookup, "c0 / c1"
    #: for a ratio, "c0 - c1" for a difference.
    expression: str = Field(default="c0", min_length=1, max_length=256)

    #: One entry per operand slot; each entry is alternatives in preference
    #: order, either as a bare list or as an ``OperandSlot``. ``min_length=1``
    #: on both: a slot with no candidates, or a metric with no slots, can never
    #: resolve and is a typo rather than a choice.
    terms: list[list[str] | OperandSlot] | None = None

    #: Set instead of ``terms`` when the term is ambiguous by nature.
    clarify: ClarifySpec | None = None

    @property
    def slots(self) -> list[tuple[list[str], OperandSign]]:
        """``terms`` with the two spellings collapsed to one shape."""
        return [
            (slot, "signed") if isinstance(slot, list) else (slot.concepts, slot.sign)
            for slot in self.terms or []
        ]

    @model_validator(mode="after")
    def _resolves_or_asks(self) -> MetricAlias:
        if (self.terms is None) == (self.clarify is None):
            raise ValueError(
                "a metric needs exactly one of `terms` (it resolves) or "
                "`clarify` (it asks); got "
                + ("both" if self.terms else "neither")
            )
        if self.terms is not None and not self.terms:
            raise ValueError("`terms` must list at least one operand slot")
        return self

    @model_validator(mode="after")
    def _refs_are_wellformed(self) -> MetricAlias:
        if self.clarify is not None:
            # A question has no operands, so the default expression has nothing
            # to reference and nothing to check.
            return self
        for concepts, _ in self.slots:
            if not concepts:
                raise ValueError("an operand slot must list at least one concept")
            for ref in concepts:
                split_concept_ref(ref)  # raises on a malformed reference

        slot_count = len(self.slots)
        for index in _OPERAND_REF.findall(self.expression):
            if int(index) >= slot_count:
                raise ValueError(
                    f"expression {self.expression!r} references c{index}, "
                    f"but only {slot_count} operand slot(s) are defined"
                )
        return self


class AliasFile(_Base):
    """The whole ``metric_aliases.yaml`` document."""

    version: Literal[1]
    metrics: dict[str, MetricAlias] = Field(min_length=1)

    @model_validator(mode="after")
    def _surface_forms_are_unique(self) -> AliasFile:
        """A surface form may only belong to one metric -- two entries claiming
        "sales" would make lookup depend on dict order.

        Raw strings only; ``AliasIndex`` catches the pairs that collide once
        normalized.
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

    @model_validator(mode="after")
    def _clarify_options_lead_somewhere(self) -> AliasFile:
        """Every offered choice must name a metric that exists and resolves.

        Offering someone a choice that leads to another question, or to
        nothing, wastes the one round trip you get.
        """
        for metric, alias in self.metrics.items():
            if alias.clarify is None:
                continue
            for option in alias.clarify.options:
                target = self.metrics.get(option.metric)
                if target is None:
                    raise ValueError(
                        f"{metric!r} offers {option.metric!r}, which is not a metric in this file"
                    )
                if target.clarify is not None:
                    raise ValueError(
                        f"{metric!r} offers {option.metric!r}, which is itself a question"
                    )
        return self


# --------------------------------------------------------------------------- #
# Lookup
# --------------------------------------------------------------------------- #


def normalize(text: str) -> str:
    """Fold a surface form to its lookup key: lowercase, no punctuation,
    single-spaced, underscores treated as spaces.

    Underscores fold so a metric named ``free_cash_flow`` is reachable by
    someone typing "free cash flow" without it being listed as a synonym.
    """
    folded = _PUNCTUATION.sub("", text.replace("_", " ").lower())
    return _WHITESPACE.sub(" ", folded).strip()


@dataclass(frozen=True)
class AliasHit:
    """One curated metric, resolved down to concept keys.

    ``terms`` mirrors the file: one tuple per operand slot, each holding
    alternatives in preference order.
    """

    metric: str
    label: str
    expression: str
    terms: tuple[tuple[tuple[Taxonomy, str], ...], ...]

    #: One per entry in ``terms``. See ``OperandSign``.
    signs: tuple[OperandSign, ...]

    #: Set instead of ``terms`` when this term asks rather than resolves.
    clarify: ClarifySpec | None = None

    #: Human label per clarify option, resolved from the target metric so the
    #: question reads in business terms rather than in file keys.
    option_labels: tuple[str, ...] = ()


class AliasIndex:
    """Normalized surface form -> ``AliasHit``."""

    def __init__(self, document: AliasFile) -> None:
        self._by_form: dict[str, AliasHit] = {}

        for metric, alias in document.metrics.items():
            slots = alias.slots
            hit = AliasHit(
                metric=metric,
                label=alias.label,
                expression=alias.expression,
                terms=tuple(
                    tuple(split_concept_ref(ref) for ref in concepts) for concepts, _ in slots
                ),
                signs=tuple(sign for _, sign in slots),
                clarify=alias.clarify,
                option_labels=tuple(
                    document.metrics[o.metric].label for o in alias.clarify.options
                )
                if alias.clarify
                else (),
            )
            for form in (metric, *alias.synonyms):
                key = normalize(form)
                if not key:
                    raise ValueError(f"{metric!r} has a surface form that normalizes to nothing")
                if key in self._by_form and self._by_form[key].metric != metric:
                    raise ValueError(
                        f"surface form {form!r} normalizes to {key!r}, already claimed by "
                        f"{self._by_form[key].metric!r}"
                    )
                self._by_form[key] = hit

    def lookup(self, text: str) -> AliasHit | None:
        """The curated metric for a phrase, or ``None`` to fall through to the
        embedding search."""
        return self._by_form.get(normalize(text))

    def __len__(self) -> int:
        return len(self._by_form)


def load_aliases(path: Path = ALIAS_FILE) -> AliasIndex:
    """Parse and validate one alias file. Raises if it is malformed."""
    document = AliasFile.model_validate(yaml.safe_load(path.read_text("utf-8")))
    return AliasIndex(document)


@lru_cache(maxsize=1)
def alias_index() -> AliasIndex:
    """The process-wide index, parsed once.

    Cached because the file is static for the life of a run. Tests that need a
    different file call ``load_aliases`` directly.
    """
    return load_aliases()

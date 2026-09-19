"""Load ``aliases.yaml`` and look business terms up in it.

    from app.semantic.aliases import alias_index
    hit = alias_index().lookup("free cash flow")

The file itself is the curated accounting knowledge (see its header and
``app/schemas/DESIGN.md`` section 9); this module only parses it, normalizes
surface forms, and answers lookups. Nothing here knows about the database.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from app.schemas.aliases import AliasFile, split_concept_ref
from app.schemas.xbrl import Taxonomy

ALIAS_FILE = Path(__file__).with_name("aliases.yaml")

#: Anything that isn't a letter, digit or space. Dropped rather than replaced
#: so "R&D" and "R and D" do not normalize to the same thing by accident --
#: "r d" and "r and d" stay distinct, and the file lists both spellings.
_PUNCTUATION = re.compile(r"[^\w\s]")
_WHITESPACE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Fold a surface form to its lookup key: lowercase, no punctuation,
    single-spaced, underscores treated as spaces.

    Underscores fold so a metric named ``free_cash_flow`` is reachable by
    someone typing "free cash flow" without needing it listed as a synonym.
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


class AliasIndex:
    """Normalized surface form -> ``AliasHit``."""

    def __init__(self, document: AliasFile) -> None:
        self._by_form: dict[str, AliasHit] = {}

        for metric, alias in document.metrics.items():
            hit = AliasHit(
                metric=metric,
                label=alias.label,
                expression=alias.expression,
                terms=tuple(tuple(split_concept_ref(ref) for ref in slot) for slot in alias.terms),
            )
            for form in (metric, *alias.synonyms):
                key = normalize(form)
                if not key:
                    raise ValueError(f"{metric!r} has a surface form that normalizes to nothing")
                # The schema rejects duplicates among the raw strings; this
                # catches pairs that only collide once folded ("SG&A" / "sg a").
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

    Cached because the file is static for the life of a run and every metric
    element would otherwise re-read it. Tests that need a different file call
    ``load_aliases`` directly.
    """
    return load_aliases()

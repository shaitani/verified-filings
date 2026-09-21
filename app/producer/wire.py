"""The flat JSON shape the producer's model is constrained to emit.

**Why a second schema exists at all.** ``QueryIn`` cannot be used as a
decoding grammar. Its elements are a discriminated union, which
``model_json_schema()`` renders as ``oneOf`` plus an OpenAPI ``discriminator``
key, and Ollama refuses to compile it -- measured 2026-09-20 against
``qwen2.5-coder:7b``::

    ollama._types.ResponseError: Failed to initialize samplers:
    failed to parse grammar  (status code 400)

So the model is constrained to a *flat* shape -- one element type, every
field optional but ``id`` / ``kind`` / ``text`` -- and ``acceptor.accept()``
turns that into the real discriminated union, where Pydantic applies the
rules a grammar cannot express. The grammar buys **well-formedness**;
``QueryIn`` remains the only thing that decides **validity**. Nothing here is
trusted.

**What this shape deliberately omits matters as much as what it carries.**
``CompanyElementIn.ticker`` and ``.name`` are absent, so the model cannot
emit them. ``_lookup_company`` in the mapper tries ``ticker`` before
``text``, so a hallucinated ticker resolves silently to the wrong company --
the one company-level mistake that otherwise-deterministic lookup does not
catch. A field that cannot be filled in cannot be filled in wrongly. The
derived lexicon resolves the span on its own; it already handles "GOOG" for
Alphabet, Facebook for Meta, and the share classes.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.schemas.query import Intent, QueryFiscalPeriod

#: The four ``ElementIn`` kinds, flattened into one enum. Kept in step with
#: ``acceptor._FIELDS_BY_KIND`` by a test, so a kind added here without a
#: field set there fails loudly rather than going unchecked.
WireKind = Literal["metric", "company", "company_group", "period"]


class WireElement(BaseModel):
    """One element as the model is allowed to write it.

    ``extra="forbid"`` is belt and braces: the grammar already prevents an
    unknown key, but the same reply also arrives here on the repair path,
    where it has been through a failure and is worth doubting.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    kind: WireKind
    text: str

    # Period fields. Present on every element because the grammar has one
    # element type; ``accept()`` rejects them on any other kind rather than
    # dropping them, since a field on the wrong kind means the model was
    # confused about the element and silence would hide that.
    fiscal_year: int | None = None
    fiscal_period: QueryFiscalPeriod | None = None
    last_n_years: int | None = None

    # Company-group selectors. Same rule.
    sic_code: str | None = None
    sic_description: str | None = None


class WireQuery(BaseModel):
    """The whole reply. Mirrors ``QueryIn`` minus ``version`` and ``question``,
    both of which the *caller* supplies -- the question because the model must
    not be able to restate it, and the version because it is ours to set.

    ``QueryIn.shape`` is not here, and ``wants_chart`` stands in its place.
    Measured: offered the full ``ResultShape`` as an optional field, the model
    left it null on every question tried, in either field order -- an optional
    field is simply cheaper to skip. Forcing it to choose would be worse than
    useless, because ``_describe_result`` infers shape from what *resolved*
    and does it well: no axes means scalar, a ``rank`` intent means ranking, a
    period axis means series. A 7B guess overriding that is a downgrade.

    What inference cannot recover is presentation, which the docstring there
    says outright: one company over twelve quarters and the same twelve
    quarters *as a chart* need the same rows but not the same answer. So the
    model is asked the one question it can answer and the data cannot -- did
    they ask to see it drawn -- as a required boolean it has no way to skip.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    intent: Intent

    elements: list[WireElement]

    #: Last, and required. Last because the decision is easier once the
    #: elements are written out; required because the grammar then has to
    #: emit a token for it.
    wants_chart: bool


#: Handed to Ollama as ``format=``. Derived, never hand-written: a grammar
#: that has drifted from the model it parses into is a silent source of
#: rejected replies.
WIRE_SCHEMA = WireQuery.model_json_schema()

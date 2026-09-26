"""app/parser/ -- the prompt, the wire shape and the acceptor.

All pure. Nothing here calls the model, for the reason recorded in
`tests/test_retrieval.py`: a test that needs a language model to agree with it
is not a test.

What is worth checking here is mostly the faithfulness gate, because it is the
one thing standing between a paraphrasing parser and a plausible wrong
answer. The prompt gets one test of its own that matters more than it looks:
every worked example must itself obey rule 1, since an example that breaks the
rule teaches the model to break it.
"""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest

from app.parser import (
    MalformedProposal,
    ProposalError,
    UnfaithfulSpan,
    accept,
    build_question_prompt,
    build_repair_prompt,
    check_span,
    extract_json,
    normalize,
    propose,
)
from app.parser.acceptor import _FIELDS_BY_KIND, _OPTIONAL_FIELDS
from app.parser.prompt import _EXAMPLES
from app.parser.proposer import MAX_OUTPUT_TOKENS, REQUEST_TIMEOUT
from app.parser.wire import WireElement, WireQuery

QUESTION = "How much revenue did Apple and Microsoft make in the last 3 years?"


def _reply(**overrides) -> str:
    payload = {
        "intent": "compare",
        "wants_chart": False,
        "elements": [
            {"id": "e1", "kind": "company", "text": "Apple"},
            {"id": "e2", "kind": "company", "text": "Microsoft"},
            {"id": "e3", "kind": "metric", "text": "revenue"},
            {
                "id": "e4",
                "kind": "period",
                "text": "the last 3 years",
                "last_n_years": 3,
            },
        ],
    }
    payload.update(overrides)
    return json.dumps(payload)


def _element(**overrides) -> WireElement:
    return WireElement(**{"id": "e1", "kind": "metric", "text": "revenue", **overrides})


# --------------------------------------------------------------------------- #
# the happy path
# --------------------------------------------------------------------------- #


def test_accept_builds_a_query_the_mapper_can_take():
    query = accept(_reply(), QUESTION)

    assert query.question == QUESTION
    assert query.intent == "compare"
    assert query.shape is None  # wants_chart false; the mapper infers
    assert [element.kind for element in query.elements] == [
        "company",
        "company",
        "metric",
        "period",
    ]
    assert query.elements[3].last_n_years == 3


def test_wants_chart_is_the_only_shape_the_parser_may_assert():
    """False leaves ``shape`` unset so ``_describe_result`` infers it, which it
    does better than a 7B guess. True is the one thing cardinality cannot say:
    the same twelve quarters drawn and not drawn need the same rows and
    different answers."""
    assert accept(_reply(wants_chart=True), QUESTION).shape == "series"
    assert accept(_reply(wants_chart=False), QUESTION).shape is None


def test_wants_chart_is_required():
    """It is the one field the model reliably skipped when it was optional."""
    payload = json.loads(_reply())
    del payload["wants_chart"]
    with pytest.raises(MalformedProposal, match="does not fit the schema"):
        accept(json.dumps(payload), QUESTION)


def test_the_question_is_the_callers_not_the_models():
    """The model never gets to restate the question -- ``accept`` takes it from
    the caller, so a reply that tried to would change nothing."""
    query = accept(_reply(), QUESTION)
    assert query.question == QUESTION


def test_company_elements_carry_no_ticker_or_name_hint():
    """``_lookup_company`` tries ``ticker`` before ``text``, so an invented one
    outranks the span and resolves silently to the wrong filer. The wire shape
    has no such field; this pins that it stays that way."""
    assert "ticker" not in WireElement.model_fields
    assert "name" not in WireElement.model_fields

    company = accept(_reply(), QUESTION).elements[0]
    assert company.ticker is None
    assert company.name is None


# --------------------------------------------------------------------------- #
# gate 2: faithfulness
# --------------------------------------------------------------------------- #


def test_a_substituted_metric_is_refused():
    """Substitution: a phrase that is simply not in the question."""
    reply = _reply(
        elements=[
            {"id": "e1", "kind": "company", "text": "Apple"},
            {"id": "e2", "kind": "metric", "text": "net income"},
        ]
    )
    with pytest.raises(UnfaithfulSpan, match="does not appear in the question"):
        accept(reply, "What was Apple's gross revenue in 2024?")


def test_a_dropped_modifier_is_refused():
    """Omission, which the substring check alone cannot catch -- "revenue" is
    genuinely inside "gross revenue". The most expensive mistake available
    here: it resolves cleanly and returns a real figure under a label it does
    not fit."""
    reply = _reply(
        elements=[
            {"id": "e1", "kind": "company", "text": "Apple"},
            {"id": "e2", "kind": "metric", "text": "revenue"},
        ]
    )
    with pytest.raises(UnfaithfulSpan, match="the question says 'gross revenue'"):
        accept(reply, "What was Apple's gross revenue in 2024?")


@pytest.mark.parametrize(
    ("span", "question"),
    [
        ("income", "Apple's net income in 2024"),
        ("margin", "Apple's gross margin in 2024"),
        ("cash flow", "Apple's free cash flow in 2024"),
        ("debt", "Apple's total debt in 2024"),
        ("expenses", "Apple's operating expenses in 2024"),
    ],
)
def test_dropped_modifiers_are_refused_across_the_curated_list(span, question):
    with pytest.raises(UnfaithfulSpan, match="changes which figure is meant"):
        check_span(_element(kind="metric", text=span), question)


def test_a_modifier_already_carried_is_not_refused_again():
    assert check_span(_element(text="gross revenue"), "Apple's gross revenue in 2024")


def test_the_modifier_rule_applies_to_metrics_only():
    """ "net" before a company name is a coincidence; before a metric it is a
    different line of the accounts."""
    assert check_span(_element(kind="company", text="Systems"), "Net Systems revenue")


# --------------------------------------------------------------------------- #
# colon lists that share a heading
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("span", "question"),
    [
        ("gross revenue", "What is Apple's yearly revenue: gross, net"),
        ("net revenue", "What is Apple's yearly revenue: gross, net"),
        ("gross revenues", "What are Apple's quarterly revenues: gross, net"),
        ("operating cash flow", "What is Apple's cash flow: operating, investing, financing"),
        ("financing cash flow", "What is Apple's cash flow: operating, investing and financing"),
    ],
)
def test_a_list_item_with_its_heading_is_faithful(span, question):
    """The question distributes the heading over the items, so each item plus
    the heading is what it asks for -- q044, q048, q051."""
    assert check_span(_element(text=span), question)


@pytest.mark.parametrize(
    "span",
    [
        "gross yearly revenue",  # the time word belongs to the period
        "revenue gross",  # one reading per item, heading last
        "gross net revenue",
    ],
)
def test_only_one_reading_per_item_is_faithful(span):
    """Measured: "gross yearly revenue" lands on GrossProfit at 0.751. One
    spelling per item, and the refusal names it."""
    with pytest.raises(UnfaithfulSpan, match="'gross revenue', 'net revenue'"):
        check_span(_element(text=span), "What is Apple's yearly revenue: gross, net")


def test_a_reading_is_never_built_from_words_that_merely_occur():
    """No list, no composition: "net income" is not in "net revenue and
    operating income", and it is a real, different figure."""
    with pytest.raises(UnfaithfulSpan):
        check_span(_element(text="net income"), "Apple's net revenue and operating income")


def test_a_reading_is_for_metrics_only():
    with pytest.raises(UnfaithfulSpan):
        check_span(
            _element(kind="company", text="gross revenue"),
            "What is Apple's yearly revenue: gross, net",
        )


def test_the_bare_heading_of_a_modifier_list_is_a_dropped_modifier():
    """ "revenue" alone, for "revenue: gross, net", drops the word that says
    which line is meant -- the colon form of the omission check."""
    with pytest.raises(UnfaithfulSpan, match="changes which figure is meant"):
        check_span(_element(text="revenue"), "What is Apple's revenue: gross, net")


@pytest.mark.parametrize(
    ("span", "question"),
    [
        # No heading at all: every item is copied as written (q052).
        ("goodwill", "What are Apple's: assets, liabilities, goodwill"),
        ("stockholders' equity", "What are Apple's: assets, stockholders' equity"),
        # Items that are metrics themselves (q053).
        ("dividends per share", "What are Apple's returns: buybacks, dividends per share"),
    ],
)
def test_a_list_without_a_shared_heading_is_copied_as_written(span, question):
    assert check_span(_element(text=span), question)


def test_a_list_item_is_one_metric():
    """ "gross" and "gross revenue" are one ask."""
    reply = _reply(
        intent="lookup",
        elements=[
            {"id": "e1", "kind": "company", "text": "Apple"},
            {"id": "e2", "kind": "metric", "text": "gross"},
            {"id": "e3", "kind": "metric", "text": "gross revenue"},
            {"id": "e4", "kind": "metric", "text": "net revenue"},
            {"id": "e5", "kind": "period", "text": "most recent year", "last_n_years": 1},
        ],
    )
    with pytest.raises(MalformedProposal, match="both come from the list item 'gross'"):
        accept(reply, "What is Apple's yearly revenue: gross, net")


def test_a_list_reads_as_one_metric_per_item():
    reply = _reply(
        intent="lookup",
        elements=[
            {"id": "e1", "kind": "company", "text": "Apple"},
            {"id": "e2", "kind": "metric", "text": "gross revenue"},
            {"id": "e3", "kind": "metric", "text": "net revenue"},
            {"id": "e4", "kind": "period", "text": "most recent year", "last_n_years": 1},
        ],
    )
    query = accept(reply, "What is Apple's yearly revenue: gross, net")
    assert [e.text for e in query.elements if e.kind == "metric"] == [
        "gross revenue",
        "net revenue",
    ]


def test_an_invented_company_is_refused():
    reply = _reply(
        elements=[
            {"id": "e1", "kind": "company", "text": "Tesla"},
            {"id": "e2", "kind": "metric", "text": "revenue"},
        ]
    )
    with pytest.raises(UnfaithfulSpan):
        accept(reply, QUESTION)


@pytest.mark.parametrize(
    ("span", "question"),
    [
        ("Apple", "What was Apple's revenue in 2024?"),  # possessive in the question
        ("apple", "What was Apple's revenue in 2024?"),  # case
        ("revenue", "WHAT WAS APPLE REVENUE"),  # case, the other way
        ("gross  margin", "Apple's gross margin in 2024"),  # doubled space
        ("Apple’s", "What was Apple's revenue?"),  # curly apostrophe
        ("2023-2025", "revenue from 2023–2025"),  # en dash
    ],
)
def test_faithful_spans_survive_ordinary_typography(span, question):
    assert check_span(_element(text=span), question)


def test_edge_punctuation_is_trimmed_not_rejected():
    """A trailing comma from "Apple, Microsoft and Nvidia" is careless
    punctuation, not an unfaithful span -- and the trimmed form is what
    reaches the mapper."""
    assert check_span(_element(kind="company", text="Apple,"), QUESTION) == "Apple"


def test_an_empty_span_is_refused():
    with pytest.raises(UnfaithfulSpan, match="empty"):
        check_span(_element(text="  ,  "), QUESTION)


def test_normalize_does_not_collapse_different_words():
    """The gate only works if normalization is conservative. Anything that
    lets two different words compare equal is the hole it exists to close."""
    assert normalize("gross revenue") != normalize("revenue")
    assert normalize("gross profit") != normalize("gross margin")


# --------------------------------------------------------------------------- #
# gate 3: meaning
# --------------------------------------------------------------------------- #


def test_a_period_field_on_a_company_element_is_refused_not_dropped():
    reply = _reply(
        elements=[
            {"id": "e1", "kind": "company", "text": "Apple", "fiscal_year": 2024},
            {"id": "e2", "kind": "metric", "text": "revenue"},
        ]
    )
    with pytest.raises(MalformedProposal, match="fiscal_year"):
        accept(reply, QUESTION)


def test_duplicate_element_ids_are_refused():
    reply = _reply(
        elements=[
            {"id": "e1", "kind": "company", "text": "Apple"},
            {"id": "e1", "kind": "metric", "text": "revenue"},
            {"id": "e2", "kind": "period", "text": "the last 3 years", "last_n_years": 3},
        ]
    )
    with pytest.raises(MalformedProposal, match="duplicate element id"):
        accept(reply, QUESTION)


def test_a_period_cannot_be_absolute_and_relative_at_once():
    reply = _reply(
        elements=[
            {
                "id": "e1",
                "kind": "period",
                "text": "the last 3 years",
                "fiscal_year": 2024,
                "last_n_years": 3,
            }
        ]
    )
    with pytest.raises(MalformedProposal):
        accept(reply, QUESTION)


def test_a_period_naming_no_time_is_refused():
    """Reaches the mapper as ``Unresolved`` otherwise, which costs the reader a
    refusal where one more generation would have done."""
    reply = _reply(elements=[{"id": "e1", "kind": "period", "text": "the last 3 years"}])
    with pytest.raises(MalformedProposal, match="names no time at all"):
        accept(reply, QUESTION)


def test_an_invented_fiscal_year_is_refused():
    """Measured on the first live run: "last year" came back as
    ``fiscal_year: 2023``. The model is told nothing about today's date, so a
    year from a span with no digits in it is a guess -- and it resolves
    cleanly against the wrong year."""
    reply = _reply(
        elements=[{"id": "e1", "kind": "period", "text": "last year", "fiscal_year": 2023}]
    )
    with pytest.raises(MalformedProposal, match="names no year"):
        accept(reply, "What was Microsoft's free cash flow last year?")


def test_a_period_span_need_not_be_in_the_question():
    """A period carries its meaning in its fields; the mapper reads its text
    only in refusal messages. Measured over the eval set, holding periods to
    the substring rule refused 8 answerable questions -- the model composes
    "Q4 last year" out of words from two ends of a sentence, which is a
    perfectly good description and not a span."""
    reply = _reply(
        elements=[
            {"id": "e1", "kind": "metric", "text": "revenue"},
            {
                "id": "e2",
                "kind": "period",
                "text": "Q4 last year",
                "last_n_years": 1,
                "fiscal_period": "Q4",
            },
        ]
    )
    assert accept(reply, "What was the Q4 revenue last year?").elements[1].text


def test_a_range_may_expand_to_years_the_question_never_names():
    """q025: "from 2021 through 2025" has to become five elements, and only
    two of them can quote a year from the question."""
    reply = _reply(
        elements=[
            {"id": "e1", "kind": "metric", "text": "revenue"},
            {"id": "e2", "kind": "period", "text": "from 2021 through 2025", "fiscal_year": 2023},
        ]
    )
    assert accept(reply, "Show revenue from 2021 through 2025.").elements[1].fiscal_year == 2023


def test_a_year_far_earlier_than_any_the_question_names_is_refused():
    """Open-ended forward is legitimate -- "since 2021" reaches years nobody
    typed. Backwards is allowed by one year and no more."""
    reply = _reply(
        elements=[
            {"id": "e1", "kind": "metric", "text": "revenue"},
            {"id": "e2", "kind": "period", "text": "since 2021", "fiscal_year": 2019},
        ]
    )
    with pytest.raises(MalformedProposal, match="more than a year earlier"):
        accept(reply, "Show revenue since 2021.")


def test_the_year_before_the_one_named_is_allowed():
    """A growth question needs the period before the one it names --
    "revenue growth in 2024" is meaningless without 2023."""
    reply = _reply(
        elements=[
            {"id": "e1", "kind": "metric", "text": "revenue growth"},
            {"id": "e2", "kind": "period", "text": "the year before 2024", "fiscal_year": 2023},
            {"id": "e3", "kind": "period", "text": "2024", "fiscal_year": 2024},
        ]
    )
    assert len(accept(reply, "What was revenue growth in 2024?").elements) == 3


def test_a_quarter_and_a_year_may_share_an_element():
    """They are orthogonal, not alternatives. Measured: the model dropped the
    quarter from "Q4 revenue last year" and returned the annual figure."""
    reply = _reply(
        elements=[
            {"id": "e1", "kind": "metric", "text": "revenue"},
            {
                "id": "e2",
                "kind": "period",
                "text": "Q4 last year",
                "fiscal_period": "Q4",
                "last_n_years": 1,
            },
        ]
    )
    period = accept(reply, "What was the Q4 revenue last year?").elements[1]
    assert (period.fiscal_period, period.last_n_years) == ("Q4", 1)


def test_a_clarification_answer_is_a_faithful_source_of_spans():
    """Otherwise the round trip cannot close. Asked "measured by what?" about
    Costco's strongest quarter and answered "Total revenue", the parser has
    to emit a metric of "revenue" -- a word nowhere in the original question.
    The reader's answer is as authoritative as their question."""
    reply = _reply(
        elements=[
            {"id": "e1", "kind": "metric", "text": "revenue"},
            {"id": "e2", "kind": "period", "text": "which quarter", "fiscal_period": "Q1"},
        ]
    )
    question = "Which quarter is Costco's strongest?"
    with pytest.raises(UnfaithfulSpan):
        accept(reply, question)

    answers = [("Measured by what?", "Total revenue")]
    assert accept(reply, question, answers).elements[0].text == "revenue"


def test_an_answer_does_not_license_any_span_at_all():
    """The reader's words widen the haystack; they do not switch the gate off."""
    reply = _reply(
        elements=[
            {"id": "e1", "kind": "metric", "text": "goodwill"},
            {"id": "e2", "kind": "period", "text": "which quarter", "fiscal_period": "Q1"},
        ]
    )
    with pytest.raises(UnfaithfulSpan):
        accept(
            reply,
            "Which quarter is Costco's strongest?",
            [("Measured by what?", "Total revenue")],
        )


def test_a_query_with_no_metric_element_is_refused():
    """Measured: "How did Apple do last year?" came back with a company, a
    period and no metric, and the mapper called that plan ``complete`` --
    nothing was unresolved because nothing had been asked for."""
    reply = _reply(
        elements=[
            {"id": "e1", "kind": "company", "text": "Apple"},
            {"id": "e2", "kind": "period", "text": "the last 3 years", "last_n_years": 3},
        ]
    )
    with pytest.raises(MalformedProposal, match="no metric element"):
        accept(reply, QUESTION)


def test_a_query_with_no_period_element_is_refused():
    """An empty ``PlanFilters.periods`` is unconstrained, not empty. 17 of the
    56 eval questions came back this way."""
    reply = _reply(
        elements=[
            {"id": "e1", "kind": "company", "text": "Apple"},
            {"id": "e2", "kind": "metric", "text": "revenue"},
        ]
    )
    with pytest.raises(MalformedProposal, match="no period element"):
        accept(reply, QUESTION)


def test_a_stated_year_is_allowed():
    reply = _reply(
        elements=[
            {"id": "e1", "kind": "metric", "text": "revenue"},
            {"id": "e2", "kind": "period", "text": "2024", "fiscal_year": 2024},
        ]
    )
    assert accept(reply, "Apple revenue in 2024").elements[1].fiscal_year == 2024


def test_a_year_range_may_name_a_year_inside_it():
    """A quarterly range legitimately repeats one span across several years,
    so the check asks only that the span names *a* year -- not that one."""
    reply = _reply(
        elements=[
            {"id": "e1", "kind": "metric", "text": "revenue"},
            {
                "id": "e2",
                "kind": "period",
                "text": "from 2023 to 2025 by quarter",
                "fiscal_year": 2024,
                "fiscal_period": "Q2",
            },
        ]
    )
    assert accept(reply, "revenue from 2023 to 2025 by quarter").elements[1]


def test_no_elements_is_refused():
    with pytest.raises(MalformedProposal, match="no elements"):
        accept(_reply(elements=[]), QUESTION)


def test_every_optional_wire_field_belongs_to_some_kind():
    """Otherwise a new field is never checked against a kind, and gate 3 goes
    quietly blind to it."""
    optional = set(WireElement.model_fields) - {"id", "kind", "text"}
    assert optional == set(_OPTIONAL_FIELDS)


def test_every_wire_kind_has_a_field_set():
    kinds = set(WireElement.model_fields["kind"].annotation.__args__)
    assert kinds == set(_FIELDS_BY_KIND)


# --------------------------------------------------------------------------- #
# gate 1: shape
# --------------------------------------------------------------------------- #


def test_bad_json_is_refused_with_something_worth_showing_a_model():
    with pytest.raises(MalformedProposal, match="not valid JSON"):
        accept('{"intent": "lookup" "elements": []}', QUESTION)


def test_a_missing_intent_is_refused():
    payload = json.loads(_reply())
    del payload["intent"]
    with pytest.raises(MalformedProposal, match="does not fit the schema"):
        accept(json.dumps(payload), QUESTION)


def test_extract_json_unwraps_a_fence():
    assert json.loads(extract_json(f"Here you go:\n```json\n{_reply()}\n```"))


def test_extract_json_prefers_the_last_block():
    first = json.dumps({"intent": "lookup", "elements": []})
    assert extract_json(f"```{first}```\ntext\n```{_reply()}```") != first


def test_extract_json_finds_a_bare_object():
    assert json.loads(extract_json(f"  {_reply()}  "))


def test_no_object_at_all_is_refused():
    with pytest.raises(MalformedProposal, match="no JSON object"):
        extract_json("I cannot answer that.")


# --------------------------------------------------------------------------- #
# the prompt
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("question", "reply"), _EXAMPLES)
def test_every_worked_example_obeys_the_rule_it_teaches(question, reply):
    """The one prompt test that matters. An example whose ``text`` is not in
    its own question demonstrates the paraphrase the prompt forbids, and the
    model copies what it is shown."""
    accepted = accept(reply, question)
    assert accepted.elements


def test_every_worked_example_parses_as_the_wire_shape():
    for _, reply in _EXAMPLES:
        WireQuery.model_validate(json.loads(reply))


def test_the_prompt_ends_with_the_question():
    prompt = build_question_prompt(QUESTION)
    assert prompt.rstrip().endswith("JSON:")
    assert QUESTION in prompt


def test_clarification_answers_reach_the_prompt():
    prompt = build_question_prompt(
        QUESTION, answers=[("Which margin did you mean?", "net profit margin")]
    )
    assert "net profit margin" in prompt
    assert "Which margin did you mean?" in prompt


def test_the_repair_prompt_quotes_the_failure_and_the_reply():
    repair = build_repair_prompt(QUESTION, _reply(), "element 'e2' has text 'sales'")
    assert "element 'e2' has text 'sales'" in repair
    assert QUESTION in repair


# --------------------------------------------------------------------------- #
# The output ceiling
# --------------------------------------------------------------------------- #
#
# These stub the client rather than the model. What is under test is the branch
# `propose` takes on what came back -- our code -- not whether qwen agrees with
# anything, so the rule at the top of this file still holds.


class _FakeResponse(dict):
    pass


class _FakeClient:
    """Records the options it was called with and replays a canned response."""

    def __init__(self, response=None, raises=None):
        self.response = response or {}
        self.raises = raises
        self.options: dict | None = None

    def __call__(self, *args, **kwargs):  # stands in for AsyncClient(...)
        self.init_kwargs = kwargs
        return self

    async def generate(self, **kwargs):
        self.options = kwargs.get("options")
        if self.raises:
            raise self.raises
        return _FakeResponse(self.response)


async def test_the_ceiling_is_actually_sent(monkeypatch):
    """The regression that would be invisible: a cap nobody passes.

    Without this, dropping `num_predict` from the options dict leaves every
    test green and restores the 4,810-second question.
    """
    client = _FakeClient({"response": _reply(), "done_reason": "stop"})
    monkeypatch.setattr("app.parser.proposer.AsyncClient", client)
    await propose("prompt")
    assert client.options["num_predict"] == MAX_OUTPUT_TOKENS
    assert client.init_kwargs["timeout"] == REQUEST_TIMEOUT


async def test_a_truncated_reply_is_not_offered_for_repair(monkeypatch):
    """`ProposalError`, not `UnacceptableProposal`.

    The distinction is what stops the repair attempt: re-asking a model that
    just ran out of room spends a second full generation to learn the same
    thing. `parse_question` repairs only the latter.
    """
    client = _FakeClient({"response": '{"intent":"lookup","eleme', "done_reason": "length"})
    monkeypatch.setattr("app.parser.proposer.AsyncClient", client)
    with pytest.raises(ProposalError, match="ceiling"):
        await propose("prompt")


async def test_a_timeout_becomes_a_proposal_error(monkeypatch):
    """A wedged server must not escape as httpx's own exception.

    Callers catch this package's vocabulary; a raw `TimeoutException` walks
    past `parse_question` and out of whatever loop is running the questions.
    """
    client = _FakeClient(raises=httpx.ReadTimeout("too slow"))
    monkeypatch.setattr("app.parser.proposer.AsyncClient", client)
    with pytest.raises(ProposalError, match="did not reply"):
        await propose("prompt")


async def test_an_empty_reply_is_still_a_proposal_error(monkeypatch):
    client = _FakeClient({"response": "   ", "done_reason": "stop"})
    monkeypatch.setattr("app.parser.proposer.AsyncClient", client)
    with pytest.raises(ProposalError, match="empty"):
        await propose("prompt")


# --------------------------------------------------------------------------- #
# Thresholds versus qualifiers
# --------------------------------------------------------------------------- #


def test_a_threshold_carries_its_number_as_a_decimal() -> None:
    """The number is typed so the SQL step never reads "100 billion" out of
    English, and so `execute()` can re-check every returned row against it."""
    query = accept(
        _reply(
            intent="rank",
            elements=[
                {"id": "e1", "kind": "metric", "text": "revenue"},
                {
                    "id": "e2",
                    "kind": "metric_threshold",
                    "text": "more than 100 billion dollars",
                    "qualifies": "e1",
                    "comparison": "gt",
                    "threshold": 100000000000,
                },
                {"id": "e3", "kind": "period", "text": "last year", "last_n_years": 1},
            ],
        ),
        "List companies with more than 100 billion dollars in revenue last year.",
    )
    threshold = next(e for e in query.elements if e.kind == "metric_threshold")
    assert threshold.value == Decimal("100000000000")
    assert isinstance(threshold.value, Decimal)
    assert threshold.comparison == "gt"
    assert threshold.qualifies == "e1"


def test_a_threshold_missing_its_number_is_refused() -> None:
    """Dropped, the comparison silently stops narrowing and the reader gets
    every row -- which is the failure the element exists to prevent."""
    with pytest.raises(MalformedProposal, match="needs `threshold`"):
        accept(
            _reply(
                elements=[
                    {"id": "e1", "kind": "metric", "text": "revenue"},
                    {
                        "id": "e2",
                        "kind": "metric_threshold",
                        "text": "more than 100 billion dollars",
                        "qualifies": "e1",
                        "comparison": "gt",
                    },
                    {"id": "e3", "kind": "period", "text": "last year", "last_n_years": 1},
                ]
            ),
            "List companies with more than 100 billion dollars in revenue last year.",
        )


def test_no_worked_example_reads_a_number_as_a_qualifier() -> None:
    """Measured 2026-09-24 on q039: read as a `metric_qualifier`, "more than
    100 billion dollars" earned the dimensional refusal -- "this dataset holds
    company totals only" -- and a perfectly answerable question came back
    unanswerable. The model copies what it is shown, so no example may show it.
    """
    for question, reply in _EXAMPLES:
        for element in json.loads(reply)["elements"]:
            if element["kind"] != "metric_qualifier":
                continue
            assert not any(ch.isdigit() for ch in element["text"]), (
                f"{question!r} shows a qualifier containing a number: "
                f"{element['text']!r}"
            )

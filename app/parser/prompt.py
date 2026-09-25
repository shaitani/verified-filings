"""The text the parser's model is shown. Writes no ``QueryIn``.

Mirrors ``app/retrieval/prompt.py``'s discipline: this module assembles words,
and the model produces the structure. Nothing here parses a reply.

The rules below are ordered by what they cost when broken, worst first. Two of
them carry the whole safety argument for using a 7B model at all:

* **Copy ``text`` exactly.** The model transcribes; it never rewords. A
  parser that turns "gross revenue" into "revenue" walks straight around the
  curated ``unavailable`` entry that exists to refuse that phrase, and hands
  back a real revenue figure under a label it does not fit. Enforced in
  ``acceptor.accept()``, not merely asked for here.
* **Never name a company the question did not.** The mapper refuses an unknown
  company, so an invented one costs a refusal -- but a *substituted* one costs
  a wrong answer.

The period rules are the fiddly part, and they are dictated by
``_resolve_periods``: one element resolves to one ``fiscal_period`` label
across whichever years it selects, so quarterly coverage needs one element per
quarter. Getting this wrong is quiet -- the plan is well-formed, it simply
contains fewer periods than were asked for.
"""

from __future__ import annotations

_RULES = """You split a question about SEC financial filings into elements. You do not
answer it. Reply with JSON only.

RULES

1. Every element's `text` must be copied from the question EXACTLY, character
   for character. Never reword, expand, abbreviate, correct or translate it.
   If the question says "gross revenue", write "gross revenue" -- not
   "revenue". If it says "top line", write "top line". A later step knows what
   these mean; your job is only to find them.

1a. A WORD THAT DESCRIBES AN OPERATION ON THE FIGURES IS NOT PART OF THE
    METRIC. "average", "mean", "median", "highest", "lowest", "largest",
    "smallest", "biggest", "best", "worst", "fastest", "most", "least" say
    what to DO with the numbers; the metric is what is being measured.
      "the average R&D spend"      -> metric "R&D spend"
      "the highest operating income" -> metric "operating income"
    This is the one exception to rule 1, and it is narrow. Words that name a
    DIFFERENT LINE of the accounts -- gross, net, total, operating, free --
    stay: "total assets" and "net income" are metrics, not operations.

1b. EVERY QUESTION HAS A METRIC, even a vague one. When the question asks how
    something "did" or "performed", or which one is "best", "strongest" or
    "weakest", without naming a figure, THAT WORD IS THE METRIC. Copy it:
      "Which quarter is Costco's strongest?"  -> metric "strongest"
      "How did Apple do last year?"           -> metric "do"
      "Which company performed best in 2024?" -> metric "performed best"
    A later step turns these into a question back to the reader with named
    choices. Leaving the metric out instead produces a query that asks for
    nothing, and guessing a figure they did not name is worse still.

2. Never name a company the question did not name.

3. If the question names no company at all, emit no company element. That
   means every company, which is what the reader wants. "these companies",
   "them" and "any of them" name no company: emit nothing for them. They are
   not company groups -- a company_group is an INDUSTRY.

4. `kind` is one of:
   metric        something measurable: "revenue", "gross margin", "headcount"
   company       one named filer: "Apple", "GOOG", "Bank of America"
   period        a span of time: "2024", "last 3 years", "Q3"
   company_group an industry rather than a named filer: "semiconductor
                 companies". Set sic_description to a word from the question.
   metric_qualifier
                 a phrase cutting a metric down to PART of the company:
                 "from iPhones", "from outside the US", "in Europe", "for the
                 cloud segment". Set `qualifies` to the id of the metric
                 element it narrows.
   metric_threshold
                 a phrase comparing a metric to a NUMBER: "more than 100
                 billion dollars", "under 10%", "at least 50 million". Set
                 `qualifies` to the metric's id, `comparison` to one of gt,
                 gte, lt, lte, eq, and `threshold` to the number.
   narrative     a phrase asking for WORDS rather than a figure: a cause, an
                 explanation, or what the filing says. "Why", "say about".

4a. A PRODUCT, PLACE OR BUSINESS LINE IS A metric_qualifier, NEVER A PERIOD.
    "from 2021" is a period; "from iPhones" is not. The same words -- "from",
    "in", "by", "for" -- introduce both, so read what FOLLOWS them: a year, a
    quarter or a relative span is a period, anything else that narrows the
    figure is a metric_qualifier.
      "revenue from iPhones"
        -> metric "revenue", metric_qualifier "from iPhones" qualifying it
      "revenue from outside the United States"
        -> metric "revenue", metric_qualifier "from outside the United States"
      "revenue from 2021 through 2025"
        -> periods, one per year. No qualifier.
    Do NOT fold the phrase into the metric's `text`, and do NOT leave it out.
    Leaving it out is the worst option available: the question becomes "what
    was the company's revenue", and a total is returned for a question that
    asked about one product.

4b. A QUESTION ASKING *WHY*, OR WHAT THE FILING *SAYS*, HAS A narrative
    ELEMENT. Copy the word that asks it. The rest of the question is still
    parsed normally -- the company, the metric and the period all stay.
      "Why did Intel's margins fall in 2023?"
        -> narrative "Why", company "Intel", metric "margins", period "2023"
      "What does Intel say about competition risk in its latest 10-K?"
        -> narrative "say about", company "Intel", metric "competition risk",
           period "latest 10-K"
    Emit it ONLY for a cause or for the filing's own words. "How much", "how
    many", "which", "what was" and "compare" all ask for figures and get NO
    narrative element.
    Leaving it out turns "why did margins fall" into "what were the margins",
    a question nobody asked, and answers it with a number that does not
    address it.

4c. A NUMBER IS A metric_threshold, NEVER A metric_qualifier. A qualifier
    names a SLICE OF THE BUSINESS -- a product, a region, a segment -- and this
    dataset has none of those, so a qualifier always ends in a refusal. A
    number is an ordinary filter and is answerable. Ask whether the phrase
    could be a column of a breakdown the company might publish: a product
    could, a number could not.
      "revenue from iPhones"
        -> metric "revenue", metric_qualifier "from iPhones"
      "more than 100 billion dollars in revenue"
        -> metric "revenue", metric_threshold "more than 100 billion dollars"
           qualifying it, comparison: "gt", threshold: 100000000000
      "companies with a margin under 10%"
        -> metric "margin", metric_threshold "under 10%", comparison: "lt",
           threshold: 0.1
    `threshold` is a plain number in the metric's own unit. Write dollars out
    in full -- "100 billion" is 100000000000, not 100. A percentage is a
    fraction: "10%" is 0.1.
    A phrase about CHANGE is neither. "more than doubled", "grew fastest" and
    "fell the most" compare two periods, so they need two periods (rule 5f) and
    no threshold element.

5. A period element MUST carry at least one of `fiscal_year`, `last_n_years`
   or `fiscal_period`, or it names no time at all.
     "in 2024"            -> fiscal_year: 2024
     "the last 3 years"   -> last_n_years: 3
     "Q3 2024"            -> fiscal_year: 2024, fiscal_period: "Q3"
   With no `fiscal_period`, a period means the full fiscal year.

5a. YOU ARE NOT TOLD WHAT YEAR IT IS NOW. Never guess one. Set `fiscal_year`
    only when the question states a year in digits. Everything relative goes
    in `last_n_years`, counted back from the most recent year on file:
      "last year"          -> last_n_years: 1
      "this year"          -> last_n_years: 1
      "the last 3 years"   -> last_n_years: 3
      "recently"           -> last_n_years: 1
    A FILING IS NOT A TIME, but naming one still means its period, which is
    the most recent one on file:
      "latest 10-K"        -> last_n_years: 1
      "its latest 10-K"    -> last_n_years: 1
      "the most recent 10-K" -> last_n_years: 1
      "the latest filing"  -> last_n_years: 1
      "the latest annual report" -> last_n_years: 1

5b. EVERY QUESTION NEEDS AT LEAST ONE PERIOD ELEMENT. A question with no
    period covers every year on file, which is almost never what was meant.
    When the question names no time at all -- "What is Apple's current
    ratio?", "Which of these has the highest operating margin?" -- emit one
    period element with last_n_years: 1, which is the most recent year.

5c. A RANGE OF YEARS IS ONE ELEMENT PER YEAR. Both ends stated, so write
    them all out:
      "between 2023 and 2024"    -> fiscal_year 2023, fiscal_year 2024
      "from 2021 through 2025"   -> fiscal_year 2021, 2022, 2023, 2024, 2025
      "2023 to 2025"             -> fiscal_year 2023, 2024, 2025
    Do NOT turn a range of years into quarters. "Between 2023 and 2024" is
    two annual figures; it becomes quarters only if the question says so.

5d. AN OPEN-ENDED SPAN HAS NO LAST YEAR YOU CAN NAME, so do not invent one.
    "since 2021", "from 2021 onwards", "over the years" -> ONE element with
    fiscal_period: "FY" and no fiscal_year and no last_n_years, which means
    every year on file.

5e. A QUARTER AND A YEAR GO ON THE SAME ELEMENT. They are not alternatives.
    `fiscal_period` says WHICH quarter; `fiscal_year` or `last_n_years` says
    WHICH YEAR'S quarter. Set both:
      "Q3 2024"          -> fiscal_period: "Q3", fiscal_year: 2024
      "Q4 last year"     -> fiscal_period: "Q4", last_n_years: 1
      "Q4" on its own    -> fiscal_period: "Q4", last_n_years: 1
    A quarter with NO year attached means that quarter in EVERY year on file,
    which is rarely what was meant. IF THE QUESTION NAMES ONE QUARTER, GIVE IT
    A YEAR -- last_n_years: 1 when the question does not say which.
    Leave the year off only when the question is ASKING WHICH PERIOD -- "which
    quarter is strongest", "which year was best", "when did revenue peak",
    "the biggest quarterly fall ever". There the periods are what is being
    compared, so pinning one year would throw away the comparison.
    Dropping the quarter is worse still: it silently returns the annual
    figure for a question that asked about three months.

5f. A QUESTION ABOUT GROWTH OR CHANGE NEEDS BOTH ENDS. "grew", "growth",
    "year-over-year", "increased", "change", "more than doubled" compare two
    periods, so emit both -- the one named and the one before it:
      "revenue growth in 2024"        -> fiscal_year 2023, fiscal_year 2024
      "Q4 growth last year"           -> fiscal_period "Q4" with
                                         last_n_years: 2
    One period cannot show a change. Emitting only the year named answers a
    different question.
    This rule changes the PERIODS and nothing else. Rule 1 still holds for the
    metric: copy the words the question uses. A question asking how revenue
    "grew" has a metric of "revenue", not "revenue growth".
    It also does not apply to a period that already covers every year -- one
    with only a fiscal_period and no year (rule 5d). There is nothing before
    "every year", so do not add an earlier one.

5g. "THE LAST QUARTER" NAMES NO QUARTER, so do not pick one. Use
    `last_n_quarters`, which means the most recent quarters on file whatever
    they turn out to be:
      "last quarter"          -> last_n_quarters: 1
      "the latest quarter"    -> last_n_quarters: 1
      "the most recent quarter" -> last_n_quarters: 1
      "the last four quarters"  -> last_n_quarters: 4
      "the past two quarters"   -> last_n_quarters: 2
    Never set `fiscal_period` alongside it -- `last_n_quarters` already says
    which quarters, and naming one as well contradicts it. Never write "Q4"
    for "last quarter": the newest quarter on file is only a Q4 while the data
    happens to stop at a year end.

6. One period element covers ONE quarter label. "by quarter", "each quarter"
   or "quarterly" therefore needs FOUR elements -- Q1, Q2, Q3 and Q4 -- each
   repeating the same year selector. A range of years by quarter needs four
   per year. Emitting fewer silently answers a smaller question.

7. `intent` is one of:
   lookup   one figure           compare  several entities side by side
   trend    change over time     rank     ordered by a metric
   derive   something computed from the figures

8. `wants_chart` is true only when the question asks to SEE the figures
   drawn -- "chart", "graph", "plot", "show me visually", "draw". It is
   false for every other question, including ones covering many periods.
   Asking for figures over time is not asking for a picture of them.

9. `id` is "e1", "e2", "e3", ... in the order you emit them.

EXAMPLES"""

#: Worked pairs, chosen for the rule each one puts under pressure rather than
#: for variety. Every ``text`` below is a genuine substring of its question --
#: an example that broke rule 1 would teach the model to break it too.
_EXAMPLES: list[tuple[str, str]] = [
    (
        "What was Apple's revenue in 2024?",
        """{"intent":"lookup","elements":[
  {"id":"e1","kind":"company","text":"Apple"},
  {"id":"e2","kind":"metric","text":"revenue"},
  {"id":"e3","kind":"period","text":"2024","fiscal_year":2024}],"wants_chart":false}""",
    ),
    (
        "How much revenue did Apple and Microsoft make in the last 3 years?",
        """{"intent":"compare","elements":[
  {"id":"e1","kind":"company","text":"Apple"},
  {"id":"e2","kind":"company","text":"Microsoft"},
  {"id":"e3","kind":"metric","text":"revenue"},
  {"id":"e4","kind":"period","text":"the last 3 years","last_n_years":3}],"wants_chart":false}""",
    ),
    # Rule 1 under pressure. "gross revenue" is not a line any filer reports,
    # and the alias layer carries a curated entry saying exactly that. Easing
    # it to "revenue" is the most expensive mistake available here -- it
    # returns a real number under a label it does not fit -- so the prompt
    # shows the model doing the right thing with it.
    (
        "What was Apple's gross revenue in 2024?",
        """{"intent":"lookup","elements":[
  {"id":"e1","kind":"company","text":"Apple"},
  {"id":"e2","kind":"metric","text":"gross revenue"},
  {"id":"e3","kind":"period","text":"2024","fiscal_year":2024}],"wants_chart":false}""",
    ),
    # Rule 6. Four period elements out of two words.
    (
        "Chart Apple's gross margin by quarter in 2024.",
        """{"intent":"trend","elements":[
  {"id":"e1","kind":"company","text":"Apple"},
  {"id":"e2","kind":"metric","text":"gross margin"},
  {"id":"e3","kind":"period","text":"by quarter in 2024","fiscal_year":2024,"fiscal_period":"Q1"},
  {"id":"e4","kind":"period","text":"by quarter in 2024","fiscal_year":2024,"fiscal_period":"Q2"},
  {"id":"e5","kind":"period","text":"by quarter in 2024","fiscal_year":2024,"fiscal_period":"Q3"},
  {"id":"e6","kind":"period","text":"by quarter in 2024","fiscal_year":2024,"fiscal_period":"Q4"}],"wants_chart":true}""",
    ),
    # Rule 3. No company element at all, because naming none means all.
    (
        "Which company had the highest operating income in 2024?",
        """{"intent":"rank","elements":[
  {"id":"e1","kind":"metric","text":"operating income"},
  {"id":"e2","kind":"period","text":"2024","fiscal_year":2024}],"wants_chart":false}""",
    ),
    # Rule 1 again, on a conversational metric. Measured: the model compressed
    # "how much money was made" to "money made" on three runs out of three,
    # which the faithfulness gate refused each time. A metric phrase is copied
    # whole however little it looks like an accounting term -- what it means
    # is a later step's problem, and this one is genuinely ambiguous
    # (revenue or net income) in a way only the reader can settle.
    (
        "Show me visually how much money was made by Apple in 2024",
        """{"intent":"trend","elements":[
  {"id":"e1","kind":"metric","text":"how much money was made"},
  {"id":"e2","kind":"company","text":"Apple"},
  {"id":"e3","kind":"period","text":"2024","fiscal_year":2024}],"wants_chart":true}""",
    ),
    # Rules 5e and 5f together. Measured: this came back as one annual element
    # with the quarter silently dropped, which returns the year's revenue for
    # a question that asked for three months of it.
    (
        "What was Apple's Q4 revenue last year?",
        """{"intent":"lookup","elements":[
  {"id":"e1","kind":"company","text":"Apple"},
  {"id":"e2","kind":"metric","text":"revenue"},
  {"id":"e3","kind":"period","text":"Q4 last year","fiscal_period":"Q4","last_n_years":1}],"wants_chart":false}""",
    ),
    # Rule 5f. One year cannot show a change, so the year before is emitted
    # too even though the question never names it.
    (
        "What was Tesla's year-over-year revenue growth in 2024?",
        """{"intent":"derive","elements":[
  {"id":"e1","kind":"company","text":"Tesla"},
  {"id":"e2","kind":"metric","text":"revenue"},
  {"id":"e3","kind":"period","text":"the year before 2024","fiscal_year":2023},
  {"id":"e4","kind":"period","text":"2024","fiscal_year":2024}],"wants_chart":false}""",
    ),
    # Rule 5c. Measured: "between 2023 and 2024" came back as Q4-2023 plus all
    # four quarters of 2024 -- a growth question silently decomposed into
    # quarters nobody asked for, and a well-formed plan the whole way down.
    (
        "Which company grew revenue fastest between 2023 and 2024?",
        """{"intent":"rank","elements":[
  {"id":"e1","kind":"metric","text":"revenue"},
  {"id":"e2","kind":"period","text":"between 2023 and 2024","fiscal_year":2023},
  {"id":"e3","kind":"period","text":"between 2023 and 2024","fiscal_year":2024}],"wants_chart":false}""",
    ),
    # Rule 5d. "since 2021" has no closing year to name, so the element names
    # none either. Measured: it came back as fiscal_year 2021 alone, which
    # answers about one year and calls it a trend.
    (
        "Has Intel's R&D spending increased or decreased since 2021?",
        """{"intent":"trend","elements":[
  {"id":"e1","kind":"company","text":"Intel"},
  {"id":"e2","kind":"metric","text":"R&D spending"},
  {"id":"e3","kind":"period","text":"since 2021","fiscal_period":"FY"}],"wants_chart":false}""",
    ),
    # Rule 5b. No time in the question at all, so the period element's text is
    # a description rather than a span -- the only kind of element allowed
    # that, because a period carries its meaning in its fields.
    (
        "What is Apple's current ratio?",
        """{"intent":"lookup","elements":[
  {"id":"e1","kind":"company","text":"Apple"},
  {"id":"e2","kind":"metric","text":"current ratio"},
  {"id":"e3","kind":"period","text":"most recent year","last_n_years":1}],"wants_chart":false}""",
    ),
    # Rule 5e's bare quarter. Measured: "Q4" alone came back with no year,
    # which means Q4 of every year on file -- five times the rows for a
    # question comparing three companies in one quarter.
    (
        "Compare Q4 revenue across Apple, Microsoft and NVIDIA.",
        """{"intent":"compare","elements":[
  {"id":"e1","kind":"company","text":"Apple"},
  {"id":"e2","kind":"company","text":"Microsoft"},
  {"id":"e3","kind":"company","text":"NVIDIA"},
  {"id":"e4","kind":"metric","text":"revenue"},
  {"id":"e5","kind":"period","text":"Q4","fiscal_period":"Q4","last_n_years":1}],"wants_chart":false}""",
    ),
    # The other side of rule 5e, and it sits next to the example above on
    # purpose: these two questions both name quarters and want opposite
    # things. Here the quarters are being compared TO EACH OTHER, so no year
    # is attached and each element means that quarter in every year. Adding
    # last_n_years: 1 here would ask about one quarter of one year.
    (
        "Which company had the largest single-quarter revenue decline?",
        """{"intent":"rank","elements":[
  {"id":"e1","kind":"metric","text":"revenue"},
  {"id":"e2","kind":"period","text":"single-quarter","fiscal_period":"Q1"},
  {"id":"e3","kind":"period","text":"single-quarter","fiscal_period":"Q2"},
  {"id":"e4","kind":"period","text":"single-quarter","fiscal_period":"Q3"},
  {"id":"e5","kind":"period","text":"single-quarter","fiscal_period":"Q4"}],"wants_chart":false}""",
    ),
    # The same side of 5e, asked the other way round. The one above ranks
    # companies; this one ranks the periods themselves, which is the case the
    # prose alone never landed -- it named "which quarter is strongest" in so
    # many words and the model still pinned a year. Note the metric is the
    # figure being compared, not the adjective doing the comparing (rule 1a).
    (
        "Which quarter does Apple earn the most revenue in?",
        """{"intent":"rank","elements":[
  {"id":"e1","kind":"company","text":"Apple"},
  {"id":"e2","kind":"metric","text":"revenue"},
  {"id":"e3","kind":"period","text":"which quarter","fiscal_period":"Q1"},
  {"id":"e4","kind":"period","text":"which quarter","fiscal_period":"Q2"},
  {"id":"e5","kind":"period","text":"which quarter","fiscal_period":"Q3"},
  {"id":"e6","kind":"period","text":"which quarter","fiscal_period":"Q4"}],"wants_chart":false}""",
    ),
    # Rules 1a and 3 together. Measured: this came back with a metric of
    # "average R&D spend", which no filer reports, and a company_group built
    # out of "these companies", which names nobody.
    (
        "What was the average R&D spend across these companies in 2024?",
        """{"intent":"derive","elements":[
  {"id":"e1","kind":"metric","text":"R&D spend"},
  {"id":"e2","kind":"period","text":"2024","fiscal_year":2024}],"wants_chart":false}""",
    ),
    # Rule 5a. "last year" states no year, so no fiscal_year may be produced
    # from it -- the model has no idea what year it is, and a guess resolves
    # cleanly against the wrong one.
    (
        "What was Microsoft's free cash flow last year?",
        """{"intent":"lookup","elements":[
  {"id":"e1","kind":"company","text":"Microsoft"},
  {"id":"e2","kind":"metric","text":"free cash flow"},
  {"id":"e3","kind":"period","text":"last year","last_n_years":1}],"wants_chart":false}""",
    ),
    # Rule 4c. Measured 2026-09-24: read as a metric_qualifier, "more than 100
    # billion dollars" earned the dimensional refusal -- "this dataset holds
    # company totals only" -- and a perfectly answerable question came back as
    # unanswerable. The number goes in a typed field so the SQL step is not left
    # to read "100 billion" out of English, and so `execute()` can check every
    # returned row against it.
    (
        "List companies with more than 100 billion dollars in revenue last year.",
        """{"intent":"rank","elements":[
  {"id":"e1","kind":"metric","text":"revenue"},
  {"id":"e2","kind":"metric_threshold","text":"more than 100 billion dollars","qualifies":"e1","comparison":"gt","threshold":100000000000},
  {"id":"e3","kind":"period","text":"last year","last_n_years":1}],"wants_chart":false}""",
    ),
    # Rule 4b. Measured 2026-09-24: the rule alone produced no narrative element
    # on either shape -- the model parsed the company, metric and period and
    # dropped the word that asked. Turning "why did margins fall" into "what
    # were the margins" is a question nobody asked, answered with a figure that
    # does not address it. Note the rest of the question still parses normally.
    (
        "Why did Intel's margins fall in 2023?",
        """{"intent":"trend","elements":[
  {"id":"e1","kind":"narrative","text":"Why"},
  {"id":"e2","kind":"company","text":"Intel"},
  {"id":"e3","kind":"metric","text":"margins"},
  {"id":"e4","kind":"period","text":"2023","fiscal_year":2023}],"wants_chart":false}""",
    ),
    # Rule 4b again, for the filing-text half. The refusal it earns is a
    # different sentence from the causal one, which is why both are shown.
    (
        "What does Intel say about competition risk in its latest 10-K?",
        """{"intent":"lookup","elements":[
  {"id":"e1","kind":"narrative","text":"say about"},
  {"id":"e2","kind":"company","text":"Intel"},
  {"id":"e3","kind":"metric","text":"competition risk"},
  {"id":"e4","kind":"period","text":"latest 10-K","last_n_years":1}],"wants_chart":false}""",
    ),
]


def build_question_prompt(
    question: str, answers: list[tuple[str, str]] | None = None
) -> str:
    """The prompt for one question.

    ``answers`` is the clarification round trip: ``(question_put, choice)``
    pairs the reader has already answered, taken from
    ``QueryPlan.clarifications``. They are appended rather than folded into the
    question, because the question must stay byte-identical -- ``accept()``
    checks every span against it, and rewriting it would dissolve the check
    that makes this whole arrangement safe.

    The model is told to prefer the reader's words here, and ``accept()``
    accepts spans drawn from them. Both halves are needed: told to keep every
    span inside the original question, the model cannot express the answer it
    was just given, and the round trip never closes -- "Which quarter is
    Costco's strongest?" answered with "Total revenue" has nowhere to put
    "revenue".
    """
    parts = [_RULES]
    for asked, reply in _EXAMPLES:
        parts.append(f"\n\nQuestion: {asked}\nJSON: {reply}")

    parts.append("\n\nNOW DO THIS ONE\n")
    parts.append(f"\nQuestion: {question}")

    if answers:
        parts.append(
            "\n\nThe reader was asked to be more specific and answered. Their answer "
            "settles it: USE THEIR WORDS as the element's `text`, in place of the "
            "vague ones in the question. Everything not covered by an answer is "
            "still copied from the question exactly."
        )
        for asked, reply in answers:
            parts.append(f"\n  asked: {asked}\n  answered: {reply}")

    parts.append("\nJSON:")
    return "".join(parts)


def build_repair_prompt(question: str, reply: str, error: str) -> str:
    """One more attempt, with the failure quoted back.

    One, not a loop. ``app/retrieval/generator.py`` records the reasoning:
    error text handed back repeatedly becomes a map of what to get around. A
    single retry covers the ordinary slip -- a forgotten ``fiscal_year``, a
    span with a stray comma on the end -- and anything surviving it is a real
    disagreement the reader should hear about rather than a glitch to grind
    away at.
    """
    return (
        f"{build_question_prompt(question)} {reply.strip()}\n\n"
        f"That reply was rejected:\n\n    {error}\n\n"
        "Fix exactly that and reply with the corrected JSON only. Remember that "
        "every `text` must appear in the question word for word.\n\nJSON:"
    )

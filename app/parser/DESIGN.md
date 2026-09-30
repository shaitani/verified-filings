# The parser — question → `QueryIn`

Block **[C]** of the chain, and the head of the pipeline. A string of English
in, a validated `QueryIn` out, or a refusal.

```
build_question_prompt(question)  -> str       rules and examples   no model
propose(prompt)                  -> str       the only model call  no schema
accept(reply, question)          -> QueryIn   judges, or refuses   no model
```

`parse_question()` runs them in order with exactly one repair attempt. The
split mirrors `app/retrieval/`, with different names so nothing collides on
import. **The model proposes; this package decides** — by the time a `QueryIn`
leaves here, nothing downstream can tell a language model was involved.

---

## 1. What the parser can get wrong

Every design decision below comes out of this table. Eight ways to be wrong,
and whether anything catches each one.

| # | Failure | Caught by | Outcome |
|---|---|---|---|
| 1 | Malformed JSON / bad schema | the grammar, then `accept` | loud, repairable |
| 2 | Misspelled or invented company | mapper's deterministic lookup | refusal |
| 3 | Company outside the 20-filer corpus | mapper | refusal |
| 4 | **Substituted metric phrase** | `accept` — faithfulness | refusal |
| 5 | **Dropped modifier** ("gross revenue" → "revenue") | `accept` — `_METRIC_MODIFIERS` | refusal |
| 6 | **Invented `fiscal_year`** | `accept` — `_check_period` | refusal |
| 7 | Period naming no time | `accept` — `_check_period` | repairable |
| 8 | Wrong `intent` | mapper re-derives `ResultSpec` | low stakes |
| 9 | **Question not answerable from facts at all** | *nothing* | **wrong answer** |

1–3 and 8 were safe before this module existed: the schema is a real gate and
the mapper's company lookup is deterministic. 4–7 are what `accept()` was
written for. **9 is the one that remains open**, and it is deliberate — see §7.

The unifying observation: every dangerous parser mistake is a **paraphrase**.
The model restates rather than transcribes, the mapper resolves the
restatement perfectly, coverage proves it, the verdict says `complete`, and a
real figure comes back under a label it does not fit. Nothing further down can
see it, because by then the original words are gone.

So the words are not allowed to go.

## 2. The faithfulness gate

Every element's `text` must appear in the question. Checked in code, not asked
for in the prompt.

This is the whole safety argument for running a 7B model here. It splits into
two halves, because they fail differently:

**Substitution** — a phrase that simply is not in the question. Caught by a
normalized substring check. Normalization is deliberately conservative
(casefold, fold look-alike quotes and dashes, collapse whitespace) and
deliberately not a stemmer: anything that lets two different words compare
equal is the hole this exists to close.

**Omission** — a *shortened* span, which passes a substring check because the
shorter phrase genuinely is in the question. "What was Apple's gross revenue"
with a metric span of "revenue" resolves cleanly and returns 391 billion under
a label that does not fit, walking straight around the curated entry for the
phrase — once a refusal, now an answer carrying a caveat that says which sense
of "gross revenue" was answered (semantic DESIGN §8a). Either way, dropping
the word loses what the curated entry exists to say. Caught by `_METRIC_MODIFIERS` — a
curated list of words that change which line of the accounts is meant, applied
to metric spans only. Accounting judgment as data, the same argument as
`metric_aliases.yaml`: add a word when a filer's numbers differ across it.

Measured end to end: "What was Apple's gross revenue in 2024?" reaches the
mapper with the phrase intact, and the curated entry does its job — at the
time a refusal, now total revenue with a `narrower_than_asked` note naming
the sense answered. Without the gate the same question returns
the figure with no note at all.

## 2a. A colon list that shares its heading

"What is Apple's cash flow: operating, investing, financing"
asks for three figures, and none of their names appears whole in the question.
Each item names one only together with the heading. The substring gate refused
the right reading ("operating cash flow"), so the model found wrong ones that
pass: q051 became one "cash flow" metric with three qualifiers, and q044's
"gross revenue" was refused outright. Bare items are no safer — "gross" alone
reaches embedding search and lands on `GrossProfit` at 0.71.

`_SharedHead` recovers the structure from the grammar, never from vocabulary:
the colon, the heading back to the last possessive, the items after the colon
(split on commas, `;`, `&`, "and", "or"). Each item gets **exactly one**
reading, `"<item> <heading>"`, and those readings join the haystack a *metric*
span may come from — behind the same seam as a clarification answer, so no
span can straddle one.

* **One reading, not every suffix.** "yearly revenue: gross, net" reads as
  "gross revenue", never "gross yearly revenue": the time word belongs to the
  period. Measured, the longer spelling misses the curated alias and embedding
  search puts `GrossProfit` on top at 0.751. `_TIME_WORDS` is a word list, but
  of a small closed class, unlike metric vocabulary.
* **One metric per item.** "gross" and "gross revenue" both pass the span
  check, and would ask for one thing twice; `accept()` refuses the pair.
* **The omission check sees the readings.** A bare "revenue" for
  "revenue: gross, net" drops the word that says which line is meant, exactly as
  it would for "gross revenue" written side by side.
* **No list, no composition.** Composing from words that merely occur would
  accept "net income" from "net revenue and operating income" — a real,
  different figure. The structure is what makes a composed span faithful.

A colon with nothing before it but a possessive ("Apple's: assets, …", q052)
has no heading and gets no readings; its items are copied as written, as
before. Limits, each a refusal rather than a wrong answer: a heading with no
possessive before it ("Show me cash flow: …") gets no readings, and an item
containing "and" ("research and development") is split and so has none.

## 3. Why company elements carry no `ticker` or `name`

`_lookup_company` tries `element.ticker`, then `element.name`, then
`element.text`. A hallucinated ticker therefore **outranks** the span and
resolves silently to the wrong filer — the one company-level mistake the
otherwise-deterministic lookup does not catch.

Neither field exists on `WireElement`, so the model cannot emit one. A field
that cannot be filled in cannot be filled in wrongly. Nothing is lost: the
derived lexicon resolves the span on its own, including "GOOG" for Alphabet,
Facebook for Meta, and the share classes.

## 4. Why there are two schemas

`QueryIn` cannot be used as a decoding grammar. Its elements are a
discriminated union, which renders as `oneOf` plus an OpenAPI `discriminator`
key, and Ollama refuses it — measured 2026-09-20:

```
Failed to initialize samplers: failed to parse grammar  (400)
```

So `wire.py` holds a **flat** shape the grammar compiles, and `accept()` turns
it into the real union. The grammar buys *well-formedness*; `QueryIn` remains
the only thing that decides *validity*. A field on the wrong kind — a
`fiscal_year` on a company element — is **refused, not dropped**: it means the
model was confused about that element, and silence would hide a fault in the
one place where silence is most expensive.

## 5. `wants_chart` instead of `shape`

Offered `QueryIn.shape` as an optional field, the model left it null on every
question tried, in both field orders. An optional field is simply cheaper to
skip.

Forcing it to choose would be worse than useless. `_describe_result` infers
shape from what actually *resolved* — no axes means scalar, a `rank` intent
means ranking, a period axis means series — and a 7B guess overriding that is
a downgrade. What inference cannot recover is **presentation**, which that
function's own docstring says outright: the same twelve quarters drawn and not
drawn need the same rows but not the same answer.

So the model is asked the one question it can answer and the data cannot — did
they ask to see it drawn — as a required boolean the grammar must emit. True
becomes `shape="series"`; false leaves `shape` unset for the mapper to infer.

## 6. The period gates

Periods are the part the model gets wrong most, and the rules here were
rewritten once already after the first full eval run. Four gates:

**A period must name a time.** With no `fiscal_year`, `from_fiscal_year`,
`last_n_years` or `fiscal_period` it reaches the mapper and returns
`Unresolved` — a refusal the reader sees. Caught here it costs one more
generation instead.

**Every year on a period must be one the question names, and not earlier than
the earliest one** — `fiscal_year`, `from_fiscal_year` and `to_fiscal_year`
alike. Checked against the *question*, not the span. The model is told
nothing about today's date; measured on the first live run, "last year" came
back as `fiscal_year: 2023`, which resolves cleanly and answers about the wrong
year. One year below the earliest is allowed, for the growth rule (5f).

**A range's end must be a year the question names.** "From 2020 until today"
has no end the model can know, and a guessed one resolves cleanly — short of
the data or past it. An open range leaves the end to the mapper, which reads
each company's newest year from the data.

**One span is one granularity.** Period elements copied from the same phrase
are all annual or all quarterly (`_refuse_one_span_at_two_granularities`).
Structural, like `_SharedHead`: it reads which elements share a span, not what
the words say.

### A range is one element

"From 2021 through 2025", "2020-2025" and "since 2021" are one period element
with `from_fiscal_year` and, when the question states one, `to_fiscal_year`;
asked quarterly, four elements carrying the same range. The mapper expands it
per company (semantic DESIGN §8c). This took the expansion away from the
model, which used to write one element per year — slow (24 elements for six
years by quarter, 40 s to generate) and unable to say "since 2021" at all:
with no way to keep a first year, it meant every year on file, which is the
same thing only while 2021 is where the store begins.

Measured cold, 2026-09-29, on "Nvidia quarterly gross profits from 2020 until
today / from 2020-2025 / from 2020 through 2025":

| prompt | until today | 2020-2025 | through 2025 |
|---|---|---|---|
| before | one FY2020 element | six annual years, "quarterly" dropped | 24 elements |
| range rules, closed quarterly example | open annual range **plus** four quarters | right | right |
| + a sentence in 5d: "instead of, never as well as" | open annual range, quarters dropped | right | right |
| sentence removed, open quarterly example added | annual range plus Q2–Q4 | right | right |
| + the granularity gate | **right, on repair** | right | right |

The same pattern as §9 and §10a: prose made it worse, an example moved it,
and the structural check closed it. The quarterly range examples sit next to
their annual twins — "between 2023 and 2024" beside "quarterly … from
2021-2023", "since 2021" beside "each quarter from 2022 onwards". With the
final prompt, fourteen questions ran cold with the same grades and row counts
as before — the range and "since" questions (q012, q014, q025, q042, q043),
the colon lists (q044, q045, q048, q051) and the quarter shapes (q016, q017,
q018, q020, q038). The q018 round trip kept Costco on 2 of 2 cold runs.

**Every query needs at least one period element.** An empty
`PlanFilters.periods` is *unconstrained*, not empty — every year on file.
Measured: 17 of 56 eval questions came back with no period at all, so "What is
Apple's current ratio?" asked for five years of rows. The prompt's default is
`last_n_years: 1`.

### Periods are exempt from the faithfulness gate

A period's meaning lives entirely in its typed fields. The mapper reads a
period's `text` in exactly two places, both refusal messages
(when it cannot resolve one). Holding periods to the substring rule refused
correct work: **8 of 56 answerable eval questions failed** because the model
*composes* a period description out of words from different parts of a
sentence — "Q4 last year", "year-over-year in 2024" — which is a perfectly good
description and simply not a contiguous span.

The faithfulness gate still applies in full to metric, company and
company_group elements, which is where a paraphrase actually costs a wrong
number.

## 6a. A qualifier is its own element, because every other home is worse

"How much revenue did Apple make from iPhones?" has a phrase
that cuts the metric down to part of the company, and before `metric_qualifier`
existed the model had three places to put it and all three were wrong:

| where it went | what happened |
|---|---|
| a **period** element | refused for naming no time. True, and tells the reader nothing about iPhones. Measured 5/5 runs |
| folded into the **metric** text | falls to embedding search, comes back as something adjacent |
| **dropped** | the metric binds alone and returns Apple's **total** revenue |

The third is the one that matters. Measured before the fix: $391,035,000,000
returned for a question about one product, verdict `complete`, fully
attributable, answering something nobody asked. A prompt rule that only said
"a product is not a period" produced exactly that, and turned the eval set's
single `unsafe` into two.

So the mapper rule is **an unsatisfiable qualifier takes its metric with it**
(`_apply_qualifiers`), and the refusal names the metric, because the metric is
what the reader will not be getting.

No qualifier is satisfiable today: the XBRL data endpoint carries no
dimensional facts at all (GAPS D3.2). The check is written as
`_qualifier_is_satisfiable` rather than a flat refusal so a later segment
ingest changes one function.

Measured after: q030 and q031 both `refused`, three graded runs each, with
q001, q012, q013, q016 and q025 unchanged -- including "revenue **from** 2021
through 2025", which is the control most at risk from a rule about the word
"from".

## 7. Deliberately not done

**The answerability gate** (failure #9). "Did any of these companies restate
its revenue?" parses into perfectly good elements; nothing here asks whether
the question is about figures at all ([docs/GAPS.md](../../docs/GAPS.md) G1).
"Why" questions are caught — the model tags the causal words as a `narrative`
element, which the mapper refuses — but questions about the *filing* rather
than its figures are not. The gate belongs here, measured against the eval
set rather than guessed at.

**Pinning metric-level ambiguity by substitution.** "How much money was made"
is revenue or net income, and the faithfulness rule means the parser *cannot*
pin it by rewording — that is the paraphrase §2 forbids. It is a curated
`clarify` entry instead (`money_made`), asked of the reader (§10).

## 8. Prompt line breaks are content

`pyproject.toml` ignores `E501` for `prompt.py`. This is not laziness.
Reflowing one worked example to satisfy the line-length rule — moving
`"wants_chart":true}` onto its own line — changed what the model emitted for
an *unrelated* question, turning a passing parse of the smoke-test question
([docs/TESTING.md](../../docs/TESTING.md#verifying-figures-yourself)) into a refusal. At this model size the prompt is whitespace-sensitive.
Formatting rules do not get a vote on prompt content.

The same reasoning is why every worked example is checked by a test: an
example whose `text` is not in its own question demonstrates the paraphrase
the prompt forbids, and the model copies what it is shown.

## 9. What the period rules cost, measured

`qwen2.5-coder:7b`, temperature 0, 3–5 s for an ordinary question. Over the
eval set, no answerable question is refused by the parser; the questions that
parse but should be refused ("market capitalisation", "competition risk") are
the right division of labour — the parser finds the elements, and the mapper
knows what is not a filed fact.

### Rules 5e and 5f, and what they cost

The first pass left one pattern: **the model treated `fiscal_period` and
`last_n_years` as alternatives** when they are orthogonal. "Apple's Q4 revenue
last year" came back annual, with the quarter silently dropped — a wrong
number, not a refusal. Rule 5e says they go on the same element; rule 5f says
a growth question needs the period *before* the one it names, which is also
why `_check_period` allows exactly one year below the earliest year stated.

| id | before 5e/5f | after |
|---|---|---|
| q016 "Q4 revenue last year" | annual, quarter dropped | `Q4` + `last_n_years: 1` |
| q020 "Q3 to Q4 last year" | Q3, Q4 across every year | `Q3`, `Q4` + `last_n_years: 1` |
| q011 "year-over-year growth in 2024" | FY2024 alone | FY2023 + FY2024 |
| q017 "Compare Q4 revenue" | Q4 across every year | *unchanged* |

Adding 5f cost two regressions before it was narrowed, and both are worth
recording because they are the same shape — a rule about periods leaking into
things that are not periods:

* The worked example's metric read "revenue growth", and the model copied that
  phrasing onto q010, whose question says "grown". The faithfulness gate
  refused it, correctly. 5f now states that it changes periods only and that
  rule 1 still governs the metric span.
* "More than doubled since 2021" is both open-ended (5d) and a change question
  (5f), so the model emitted every year *plus* FY2020. The corpus is
  FY2021–FY2025, so that element resolves to nothing and turns an answerable
  question into a refusal. 5f now excludes periods that already cover every
  year — there is nothing before "every year".

### Rule 1a, and the pair of quarter examples

Two later fixes, both about words that look like part of a metric or a period
and are not.

**Rule 1a** — "average", "highest", "largest" and their kin say what to *do*
with the figures; they are not part of what is measured. q040 came back with a
metric of "average R&D spend", which no filer reports. This is the only
exception to rule 1, and it is drawn narrowly: words naming a different *line*
of the accounts — gross, net, total, operating, free — stay, because "total
assets" is a metric and not an operation.

**A bare quarter needs a year** unless the quarters are being compared to each
other. Sharpening 5e fixed q017 ("Compare Q4 revenue across Apple, Microsoft
and NVIDIA" → `Q4` + `last_n_years: 1`, five times fewer rows) and immediately
broke q038, which collapsed from four bare quarters to a single `Q1` of the
latest year — a ranking across all companies and all history narrowed to one
quarter. The two questions look alike and want opposite things, so they sit
**next to each other in the example list**, which is what made the distinction
stick. Measured before and after; nothing else moved.

The recurring lesson, three times over now: a rule sharpened for one case
over-applies to its neighbour, and the way to hold the line is a *contrasting
pair* of examples rather than more prose.

## 10. Vague metrics, and the round trip

q018, "Which quarter is Costco's strongest?", named no figure at all. Three
things were wrong and each needed a different layer.

**The period** narrowed to the latest year. Fixed by generalizing 5e from "the
quarters are compared to each other" to "the question is asking WHICH PERIOD",
and by a second worked example — *"Which quarter does Apple earn the most
revenue in?"* — sitting beside the two above. The prose had named "which
quarter is strongest" in so many words and never landed; the example did.

**The metric** resolved to the adjective "strongest", fell past the alias layer
to the embedding search, and came back `unresolved` — a dead end with nothing
to offer. It is now a curated `clarify` entry, `performance`, covering
*strongest, weakest, best, worst, performed, did well, do, did*. It asks rather
than declines because the question is reasonable: whoever typed it has a figure
in mind and has not said which, and Costco's strongest quarter by revenue and
by margin are not obliged to be the same quarter. Four options: revenue, net
income, operating income, gross margin.

**A query with no metric at all could look finished.** "How did Apple do last
year?" produced a company, a period and nothing else, and the mapper called
that plan `complete` — nothing was unresolved because nothing had been asked
for. `accept()` now requires a metric element, the mirror of the period gate,
and rule 1b tells the model that the vague word *is* the metric.

### 10a. `clarify_as` — the parser names the question

From q043: "how much money was made" is revenue or net
income, the alias file did not list the phrase, and the embedding search found
nothing within reach (best 0.58) — so a question that should have been *asked*
was *refused*. Listing phrases fixes one wording at a time; the vague words
people use are open-ended ("fare", "bring in", "rake in").

**What it is.** An optional field on a metric element — `MetricElementIn.
clarify_as`, `WireElement.clarify_as` — holding the *name* of a curated
`clarify` entry in `metric_aliases.yaml`. The metric's `text` is still copied
exactly; `clarify_as` is extra. The model learns it from three worked
examples, not from a rule — see "Taught by example" below.

**Why the model, and why it is safe.** Judging what a vague phrase is vague
*about* is language understanding, which the model does and similarity does
not — measured, embedding scores for phrases that deserve a question
(0.48–0.68) and phrases that deserve a refusal (0.49–0.61, "price to earnings
ratio" at 0.605) overlap completely. And the field is safe to trust because
its only output is a *question*: the mapper consults it only when the curated
lookup of `text` finds nothing, the reader answers, and the answer binds by
alias on the second pass. A wrong `clarify_as` costs a misdirected question,
never a figure.

**Where it is enforced.**

* The grammar (`wire._wire_schema`) narrows it to the curated names, so the
  model cannot spell one that does not exist.
* `acceptor._check_clarify_as` checks again, naming the valid entries for a
  repair; `_FIELDS_BY_KIND` allows it on metrics only.
* The mapper (`_resolve_metrics`) takes: curated lookup of `text` → then
  `clarify_as` → then the embedding search. A listed phrase outranks it.

**One source of truth.** The grammar's enum and the mapper both read
`AliasIndex.clarify_entries()`, so adding a `clarify` entry to the YAML makes
it a legal `clarify_as` value and an askable question with no code change.
Tests pin both, and that every name an example shows is a real entry.

**Taught by example, not by rule.** The first version added rule 1c: a
numbered rule listing every curated question. Measured cold, it broke q045 —
"profit: net, gross" came back as one vague metric "profit" with
`clarify_as: profit` — and a follow-up sentence meant to prevent that made the
model copy "profit: net, gross" as a span and put `clarify_as` on "stock
price". With the rule removed and only the three examples carrying the field,
the vague questions still asked through it and nothing specific picked it up.
At this model size a rule naming "profit" is a magnet for every "profit" in a
question; an example is not. Do not reintroduce the list without measuring
the colon-list questions (q044, q045, q048, q051) cold.

**The colon lists sit on a knife edge.** Every prompt variant tried flipped at
least one of q044 / q045 / q048 cold — which one depended on the variant. The
structural backstop is in the acceptor: the dropped-modifier check compares
spans with time words removed, so "yearly revenue" for "yearly revenue: gross,
net" is refused with the readings named rather than accepted as a single
metric that silently drops the list.

**Using it when fixing a question that should ask.** Pick the `clarify` entry
whose question fits, or add one (an accounting-judgment edit, collaborative —
[docs/DESIGN.md](../../docs/DESIGN.md#the-alias-layer)). Add the common wordings as synonyms, so the answer does not rest
on the model. Then check, from a cold model
([docs/TESTING.md](../../docs/TESTING.md#the-prompt-cache)), that the question
now comes back `asked` and that questions naming a *specific* figure did not
start carrying `clarify_as` — a stock price or a headcount is specific, not
vague, and should get none. If the model needs a new example to reach a new
entry, add one and re-measure the colon-list questions: every prompt change
here has moved them.

**Examples in the prompt.** "how much money was made" → `money_made`, "margins"
→ `profit_margin`, and "fare" → `performance`, the last a word the file does
not list, so the model sees the field is for unlisted words too.

### The round trip could not close

Found by testing it rather than assuming it. Asked "measured by what?" and
answered "Total revenue", the parser has to emit a metric of **"revenue"** —
a word nowhere in *"Which quarter is Costco's strongest?"*. The faithfulness
gate refused it, so `answers=` was decorative.

The reader's answer is now a faithful source of spans alongside the question:
`_haystack()` joins them with a separator that survives `normalize`, so no
span can straddle the seam. Two details matter. The modifier check still runs
against the **question alone** — it exists to stop the model dropping a
qualifier the reader *typed*, and the curated label "Total revenue" would
otherwise forbid the span "revenue" it names. And the prompt had to change
too: it previously told the model the answer "is not a span", which is the
instruction that made the loop impossible.

Measured end to end: ask → four options → pick "Total revenue" → `complete`,
20 rows (Costco, four quarters, five years), 1 binding.

One retry, not a loop: `app/retrieval/generator.py` records the reasoning —
error text handed back repeatedly becomes a map of what to get around.

### 10b. `over_time` — the metric's movement, not its level

"Revenue growth", "grew fastest", "the largest decline",
"compound annual growth" ask for a metric's movement. The metric element keeps
the metric's words ("revenue") and carries `over_time: change | growth | cagr`;
the grammar allows exactly those values and `accept()` allows the field on
metrics only. Retrieval computes the arithmetic in Python (retrieval DESIGN
§4.8). Taught by example, like `clarify_as`, not by a numbered rule: the Tesla
growth, "grew revenue fastest", the single-quarter decline, and one CAGR
example carry it. "Has Intel's R&D spending increased or decreased since
2021?" deliberately does not — it asks for a series to be judged, which the
model still derives.

### 10c. `rank` — which end of a ranking comes first

A `rank` question said *that* it ordered by a metric and not
*which way*; the direction lived only in the English, and the SQL model read it
from there. The metric element now carries `rank: highest | lowest`, and
`accept()` requires it on some metric of a `rank` question and refuses it on
any other — both repairable. "Largest decline" is `lowest`: the change most
below zero comes first. The mapper copies it onto `ResultSpec.rank` for the
metrics that bound, and from there Python writes the `ORDER BY` (retrieval
DESIGN §4.6) and the Presenter sorts by it without seeing the plan.

**Taught by example, like `clarify_as`**: the five ranking examples carry it,
one of them `lowest`, and a test holds every ranking example to showing it.

**`top_n` — how many.** "The top 3 ..." is a count beside `rank` on the same
metric element (`top_n`, 1–100, refused without `rank`). It is only ever a number
the question states: `accept()` looks for it in the question, in digits or as a
word up to twenty (and 30/40/50/100), the same reason a `fiscal_year` must be
named — a count the reader never wrote would silently cut the answer short. One
ranking example teaches it (`top 5 ... lowest gross profit`); the model leaves
it out otherwise, so "which company had the highest ..." still returns the whole
ranking. Measured 2026-09-29 after adding the example: the colon lists, q009-q018
and q038-q041 unchanged. The mapper copies it onto `ResultSpec.top_n` for the
metrics that bound; the Presenter applies it (presenter DESIGN §2).

**What adding `rank` broke, and how that was found.** The eval set passed cold, 13
of 13 including the colon lists. The clarification round trip is not in the
eval set, and it broke: "Which quarter is Costco's strongest?" answered
"Total revenue" re-parsed with **no company element** on 2 of 2 cold runs —
so the scope widened to every filer and the answer was a confident ranking of
all 20 companies' quarters, verdict `complete`. The code before the change
kept Costco on 2 of 2.

A variant harness found the cause in one pass: `rank` on the rule-3 example
("Which company had the highest operating income in 2024?"), the one ranking
that deliberately names no company. Carrying the same field as the Apple
ranking, it taught "a ranking names no company". Moving it beside the Apple
example — a contrasting pair, as in §9 — kept Costco on 4 of 4. Two things
worth keeping from this:

- **A dropped company is silent.** A question naming no company means every
  filer, so a company the model omits widens the scope rather than failing.
  Nothing structural catches it ([docs/GAPS.md](../../docs/GAPS.md) G4).
- **The round trip needs measuring too.** It runs a different prompt (the
  answers are appended), and a prompt change can move it when the first pass
  is untouched.

# The producer — question → `QueryIn`

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

## 1. What the producer can get wrong

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

The unifying observation: every dangerous producer mistake is a **paraphrase**.
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
a label that does not fit, walking straight around the curated `unavailable`
entry that exists to refuse the phrase. Caught by `_METRIC_MODIFIERS` — a
curated list of words that change which line of the accounts is meant, applied
to metric spans only. Accounting judgment as data, the same argument as
`metric_aliases.yaml`: add a word when a filer's numbers differ across it.

Measured end to end: "What was Apple's gross revenue in 2024?" now reaches the
mapper with the phrase intact and comes back

> US GAAP has no gross-versus-net revenue pair. The revenue a filer tags is
> already net of returns and allowances.

which is the curated refusal doing its job. Without the gate the same question
returns a number.

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
rewritten once already after the first full eval run. Three gates:

**A period must name a time.** With no `fiscal_year`, `last_n_years` or
`fiscal_period` it reaches the mapper and returns `Unresolved` — a refusal the
reader sees. Caught here it costs one more generation instead.

**A `fiscal_year` must be a year the question names, and not earlier than the
earliest one.** Checked against the *question*, not the span. The model is told
nothing about today's date; measured on the first live run, "last year" came
back as `fiscal_year: 2023`, which resolves cleanly and answers about the wrong
year. Open-ended *forward* is allowed, because "since 2021" legitimately
reaches years nobody typed; going below the earliest stated year is not,
because nothing in the question suggests it.

This rule used to read the span, which was wrong: a range like "from 2021
through 2025" has to become five elements and only two of them can quote a
year of their own. q025 was refused for emitting `"2022"`, which is exactly
the right thing to do.

**Every query needs at least one period element.** An empty
`PlanFilters.periods` is *unconstrained*, not empty — every year on file.
Measured: 17 of 56 eval questions came back with no period at all, so "What is
Apple's current ratio?" asked for five years of rows. The prompt's default is
`last_n_years: 1`.

### Periods are exempt from the faithfulness gate

A period's meaning lives entirely in its typed fields. The mapper reads a
period's `text` in exactly two places, both refusal messages
(`query_mapper.py:492`, `:515`). Holding periods to the substring rule refused
correct work: **8 of 56 answerable eval questions failed** because the model
*composes* a period description out of words from different parts of a
sentence — "Q4 last year", "year-over-year in 2024" — which is a perfectly good
description and simply not a contiguous span.

The faithfulness gate still applies in full to metric, company and
company_group elements, which is where a paraphrase actually costs a wrong
number.

## 7. Deliberately not done

**The answerability gate** (failure #9). "Did any of these companies restate
its revenue?" parses into perfectly good elements; nothing here asks whether
the question is about figures at all. This is HANDOFF §6's last
plausible-wrong-answer, and closing it needs the eval set runnable so the gate
can be measured rather than guessed at — and the eval set needs this module.
It is the next thing, not part of this one.

Today that question happens to end in a refusal, because the model tags "these
companies" as a company group that resolves to nobody. That is luck, not a
gate.

**Pinning metric-level ambiguity.** HANDOFF §3 assigns this to the producer:
"how much money was made" is revenue or net income. The faithfulness rule means
the producer *cannot* pin it by substitution — that is exactly the paraphrase
§2 forbids. The right home is a curated `clarify` entry in
`metric_aliases.yaml`, which already has the machinery: a curated question with
named options, surfaced through `QueryPlan.clarifications`, answered by the
reader, and fed back through `parse_question(answers=...)`. That is an
accounting-judgment edit, so it belongs to whoever curates that file.

## 8. Prompt line breaks are content

`pyproject.toml` ignores `E501` for `prompt.py`. This is not laziness.
Reflowing one worked example to satisfy the line-length rule — moving
`"wants_chart":true}` onto its own line — changed what the model emitted for
an *unrelated* question, turning a passing parse of the HANDOFF §8 smoke test
into a refusal. At this model size the prompt is whitespace-sensitive.
Formatting rules do not get a vote on prompt content.

The same reasoning is why every worked example is checked by a test: an
example whose `text` is not in its own question demonstrates the paraphrase
the prompt forbids, and the model copies what it is shown.

## 9. Measured

`qwen2.5-coder:7b`, temperature 0, 3–5 s for an ordinary question. Full eval
set (`evals/questions.yaml`, 56 questions), 2026-09-21:

| | first run | + period fixes | + 5e/5f | + 1a and the quarter pair |
|---|---|---|---|---|
| parsed | 43 | 53 | 53 | **51** |
| refused | 13 | 3 | 3 | **5** |

The raw counts stop being the useful measure at the end, because a refusal is
the right answer to some of these questions. Against what the eval set expects:

| `expect` | parsed | refused |
|---|---|---|
| answerable (37) | **37** | **0** |
| partial (8) | 6 | 2 |
| refuse (11) | 8 | 3 |

**No answerable question is refused by the producer.** The eight `refuse`
questions that parse are the correct division of labour — the producer's job
is to find the elements, and the mapper is what knows that "market
capitalisation" or "competition risk" is not a filed fact.

Spot checks through the mapper: "What was Apple's revenue in 2024?" →
`complete`, 1 binding. "Which company had the highest net income in 2024?" →
`complete`, ranking, 20 bindings. "What was Apple's gross revenue in 2024?" →
refused by the curated entry (§2). HANDOFF §8 smoke test → `series`, axes
`['company','period']`, **36 rows**, `period_misalignment`, metric unresolved
pending a `clarify` entry (§7).

All three refusals are questions the eval set marks `refuse` or `partial`:
q030 (revenue by product line), and q045 / q048, two `<Company>` template
questions whose comma-separated metric lists the model composes into phrases
that are not in the question.

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

### The round trip could not close

Found by testing it rather than assuming it. Asked "measured by what?" and
answered "Total revenue", the producer has to emit a metric of **"revenue"** —
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

# `app/semantic` — the query mapper: design decisions

Companion to [`app/schemas/DESIGN.md`](../schemas/DESIGN.md), which covers the
`QueryIn` / `QueryPlan` models this package produces and consumes (its sections
8 and 8.11-8.12 in particular).

Data hazards and their evidence live in [`PITFALLS.md`](../../PITFALLS.md).

This file covers the **curated metric alias layer** — `metric_aliases.yaml` and
`metric_aliases.py`. The file holds the accounting judgment; the module
validates it, normalizes surface forms and answers lookups. It is **data, not
code**, so extending it is an edit rather than a deploy, and it is the artifact
a non-accountant and a model can sensibly co-author.

Models, loading and the index live in **one** module on purpose: the file ships
beside it, nothing else reads it, and splitting ~200 lines across two packages
under the same name bought nothing but confusion.

---


Added 2026-09-18 with the metric resolver. `metric_aliases.yaml` holds the
accounting judgment; `metric_aliases.py` validates and indexes it. It is **data, not code**, so extending it is an edit
rather than a deploy — and it is the artifact a non-accountant and a model can
sensibly co-author, which is the whole reason it exists.

## 1. `terms` are operand slots, each holding ordered alternatives

`terms[i]` is operand `c{i}` of `expression`; each slot lists *alternative*
concepts in preference order, not concepts to combine. Both axes from `app/schemas/DESIGN.md` §8.4 are
therefore expressible in one shape: `revenue` is one slot with three
alternatives, `free_cash_flow` is two slots of one alternative each with
`expression: c0 - c1`.

## 2. Filer divergence resolves from data, not per-company curation

This is the payoff. `revenue` lists `RevenueFromContractWithCustomerExcluding
AssessedTax` then `Revenues`; the resolver keeps, per company, the first one
whose facts actually cover the requested windows. Measured live: Apple and
Microsoft bind the first, NVIDIA binds the second, from a single entry with no
per-company table to maintain. `app/schemas/DESIGN.md` §8.3's `(element, company)` binding key is what
makes that representable.

The alternative — a curated per-filer mapping — would have needed 20 rows per
metric, gone stale the moment a filer re-tagged, and had no way to notice.

## 3. Preference order *is* the disambiguation, so aliases never go ambiguous

A curated list is already ranked by someone who thought about it. So on the
alias path the first survivor of the coverage filter wins outright and no
`Ambiguity` is ever emitted. The embedding path has no such ranking, so several
survivors there is a real tie and gets reported — deciding it on a hair of
cosine distance is exactly the noise `app/schemas/DESIGN.md` §8.6 argues against.

Observed: "dividends paid" (no alias) returns `PaymentsOfDividends` at 0.757
and `CommonStockDividendsPerShareDeclared` at 0.686, both covered. One is USD
and one is per-share — genuinely different questions, correctly refused rather
than guessed. That pair is also a good candidate for the next alias entry;
that is the intended feedback loop.

**An ambiguity is reported once per element, not once per company.** Each filer
keeps whichever candidates its own facts cover, so the lists differ — but they
raise one question, and asking it twenty times is not twenty questions. The
merge keeps the highest-scoring sighting of each concept. Measured before it
existed: "total debt" over the corpus produced nineteen separate records for
one phrase.

## 3a. A floor under what is worth offering

Below `_MIN_PLAUSIBLE_SIMILARITY` (0.65) the element is `unresolved`, not
`ambiguous`. The two fields mean different things (§8) and an embedding search
always returns *something*, so without a floor every question the store cannot
answer became a menu of nonsense — "competition risk disclosure" came back as a
choice between `AssetsFairValueDisclosure` and `LiabilitiesFairValueDisclosure`.

**Where 0.65 came from.** 221 `(phrase, company)` cases, hand-labelled as
either present in the store with known acceptable concepts, or absent from it.
A floor at 0.65 refuses 104 of the 129 absent cases, and of the 15 answerable
cases it also refuses, **none had the right concept anywhere in the list they
were offering**. The cost is zero because a list scoring that low was never
going to help. At 0.68 the cost stops being zero, which is why the floor sits
where it does.

## 3b. Why the binding bar is not the place to fix wrong bindings

`_MIN_BINDING_SIMILARITY` (0.70) is what a lone surviving candidate must clear
to be bound with nobody having reviewed it. It is tempting to raise it until
the wrong bindings stop. The same 221 cases say the score does not separate
right from wrong well enough for that to work cleanly:

| band | top candidate right | wrong | not in store |
|---|---|---|---|
| 0.70–0.75 | 2 | 19 | 22 |
| 0.75–0.80 | 32 | 7 | 0 |
| 0.80+ | 12 | 2 | 0 |

The band immediately above the old bar was **5% precise**; above 0.75 it is
83%. The bar is therefore 0.75, not the 0.70 it started at — that band is where
"interest income" resolved to pre-tax income (0.718) and "treasury stock" to a
share count (0.701).

But note what the table also says: right answers run from 0.675 to 0.851 and
wrong ones from 0.556 to 0.810, and those ranges overlap across most of their
mass. **No threshold makes this path safe.** 0.75 makes it less bad. The actual
fix for a term people keep asking about is a curated entry — `terms` if the
store has it, `clarify` if it is several things, `unavailable` if it is not
there.

## 4. Surface forms must be unique, at two levels

The schema rejects duplicate raw strings; `AliasIndex` additionally rejects
forms that only collide once normalized (lowercased, punctuation dropped,
underscores folded to spaces). Both exist because lookup resolving by dict
order is precisely the quiet wrongness this layer is meant to remove.

Normalizing underscores means a metric key like `free_cash_flow` answers to
"free cash flow" without anyone remembering to add it as a synonym.

**Punctuation is folded two ways, and both are tried.** This used to be one
rule — punctuation *deleted*, so "R&D" became `rd` — on the reasoning that the
file would list the spelled-out spellings separately. It never listed `rd`, and
the rule quietly broke every hyphenated phrase by running it together:
"long-term debt" became `longtermdebt` and matched nothing. Measured against
the eval set, the commonest spelling of one of the commonest metrics — "R&D" —
missed the file entirely and fell through to an embedding match scoring 0.699,
one thousandth under the binding bar.

One rule cannot serve both cases: "SG&A" wants its ampersand to vanish, and
"long-term" wants its hyphen to become a space. So `normalize` separates
(`r d`, `long term debt`), `compact` deletes (`sga`, `rd`), and both indexing
and lookup use the pair. Collision detection covers both spellings, so an entry
cannot claim a form another entry already owns under either fold.

## 5. Coverage still outranks the file

An alias entry is a hypothesis, not an answer. Every candidate it proposes goes
through the same coverage filter as an embedding candidate, and a curated
concept with no facts for the requested windows loses to a later alternative
that has them. Two entries in the file exist purely to make that testable
against the fixture (`tests/fixtures/metric_aliases_fake.yaml`).

## 6. `unit` on a derived binding

`Binding.unit` is the unit of the *result*, not of the lead operand. It used to
be the latter, so `gross_margin` (`c0 / c1` over two USD concepts) reported
"USD" for a ratio and anything formatting it would have shown 46 cents.

Still not a unit algebra. One rule, matching what the file actually contains —
every expression here divides like-for-like or adds like-for-like:

- one operand → the fact's own unit
- several operands, same unit, expression divides → `"pure"`
- several operands, same unit, no division → that unit
- several operands, **different** units → refuse

The last case is a guard, not a behaviour: no entry does it. It is there so
that a future entry dividing USD by a share count fails loudly instead of
inheriting a unit that describes only its numerator. `"pure"` is XBRL's own
name for a dimensionless quantity and already appears in the store.

## 7. What the resolver deliberately does not do

No fallback arithmetic. `gross_profit` maps only to `us-gaap:GrossProfit` even
though just 9 of 20 filers tag it, rather than quietly computing revenue minus
cost — a computed subtotal is a different number from the filer's own, and
silently substituting one for the other is the class of error this whole layer
exists to prevent. When a filer does not report it, "unresolved" is the honest
answer.

---

## 8. Asking instead of guessing

An alias entry sets exactly one of `terms` (it resolves), `clarify` (it asks)
or `unavailable` (it declines). A clarify entry produces a `Clarification` on
the plan: a question with named choices, each pointing at another metric in the
file that does resolve.

**When to use it.** Where a default would be quietly *wrong*, not merely
imprecise. "Profit margin" reads as net by convention, but gross and net
routinely differ by twenty points on one company, so picking one hands someone
a wrong answer with no signal. Three entries carry it: `profit_margin`,
`profit` and `cash_flow`.

**Only the bare term asks.** "Net profit margin" resolves straight through. If
naming the thing precisely still triggered a question, the clarification would
be a toll gate rather than a service, and a test pins that.

Only the *bare* term asks: `debt` is a question, `total debt` resolves — see
§8b, which is where the interesting part of that entry lives.

Either way it replaced the worst case in the eval set: "total debt" across the
corpus produced nineteen ambiguity records offering, among other things,
`DebtInstrumentCarryingAmount`, a per-instrument footnote line.

## 8b. Answering with a narrower figure, and saying so

`caveats` maps a concept reference to a sentence that becomes a
`narrower_than_asked` note whenever *that* alternative is the one that binds.

**The case it exists for.** "Total debt" is not ambiguous — it means short-term
borrowings plus long-term debt including current maturities, and anyone asking
it knows what they mean. It is simply not a line most filers tag. Three routes
were available and two are wrong:

- *Sum the components.* Measured and rejected: filers double-tag the same
  balance. At FY2025 JNJ reports `DebtCurrent` and `ShortTermBorrowings` as the
  same $8.50B, INTC reports `DebtCurrent` and `LongTermDebtCurrent` as the same
  $2.50B, and CVX's `DebtCurrent` of $10.92B is not the sum of its own parts
  ($7.97B). The addition yields a different wrong answer per filer. This is §7's
  rule arriving with a measurement attached.
- *Bind `LongTermDebt` and say nothing.* It covers 17 filers and omits
  commercial paper — Apple's $8.0B against $90.7B, so the figure runs 8% light
  under a label that promises a total. A quiet 8% is exactly the error class
  this project exists to refuse.
- *Bind it and disclose.* What the file does.

**Why per concept and not per metric.** The alternatives differ in *definition*,
not just in tag, which is unlike every other entry here. `total_debt` prefers a
filer's own combined line (4 filers, needs no caveat), falls back to long-term
debt (omits commercial paper), then to the noncurrent portion (omits current
maturities too). A single message for the metric would be wrong for at least two
of the three, and would put a warning on the four filers whose number is exact.

**What it is not.** Not a way to make a weak binding acceptable. The three
filers with no usable concept — NTGR, which has no debt; CVX, whose
`LongTermDebt` has no fact for the window; JPM, whose only debt total here is
`ShortTermBorrowings` at $69B against a long-term load in the hundreds of
billions — stay unresolved. A caveat discloses a *narrower* answer; it does not
license a wrong one.

**Choices are validated at load.** Every option must name a metric that exists
and that itself resolves — offering a choice which leads to another question,
to a decline, or to nothing, wastes the one round trip you get.

## 8c. Answering over the companies that can answer

Added 2026-09-22, from the first full eval run: five of the sixteen remaining
failures were one filer refusing a question about twenty.

A company that reports nothing for a metric is **ordinary, not an error**.
JPMorgan tags no `OperatingIncomeLoss` and no `GrossProfit`, correctly — a
bank has no gross profit to present. Before this, `_bind_per_company` raised an
`Unresolved` for each such company, which made the plan incomplete, which
refused the whole question. "Which of these companies has the highest operating
margin?" came back as nothing at all, because one filer of twenty could not
take part.

The rule now:

* **some companies cover** → bind them, drop the rest, and attach a plan-level
  `partial_coverage` note naming the dropped filers by name and ticker.
* **no company covers** → one `Unresolved` naming them all. Still a refusal,
  and still the right one: `interest expense` for Apple alone has nowhere to
  go, since Apple stopped tagging it after FY2023.

Three things make this safe rather than a quiet narrowing.

**The note is plan-level, not binding-level.** "JPM is absent" is a statement
about the comparison, and attaching it to one of the surviving sides would be
arbitrary — the same reasoning `period_misalignment` already follows.

**A ranking says so explicitly.** Dropping a company from a lookup costs a row.
Dropping one from a *ranking* can change the answer outright, and no row count
downstream would reveal it, so `_subset_warning` adds a sentence for
`intent="rank"`: the ordering is over the remaining N and is not necessarily
the ordering over all of them.

**`ResultSpec` counts only the companies that bound.** `PlanFilters.ciks`
stays the full resolved scope, because narrowing it would erase the evidence
that a dropped filer was ever asked about. `ResultSpec` has to be honest the
other way: `row_count` is what the reader is promised, and promising a row for
a company with no binding makes the verdict report a shortfall the plan had
already disclosed.

The same split applies to **companies that do not resolve at all**. A name
matching *nothing* is droppable — "revenue for Apple and Samsung" is a real
question about Apple — and produces the same note, unless it leaves a
comparison with one side (§8d). A name matching *several*
loaded companies is not: picking one would be a guess between real
alternatives, so that stays a refusal. And when no named company resolves, the
scope must **not** widen to every loaded filer; `map_query` widens only when
the question names no company element at all.

### What this does not fix

A period that resolves to nothing. "What was Apple's revenue in 2019?" names
one year, no window exists for it, and there is no surviving fraction to
answer — so it stays a refusal. Whether to answer such a question from the
comparative columns a later 10-K carries is a separate decision, still open.

## 8d. A comparison left with one side is refused

Added 2026-09-25, reversing part of §8c at the user's call. "How does Apple
compare to Samsung on revenue?" used to answer with Apple's revenue and a
`partial_coverage` note. Half a comparison is not a smaller version of the
answer, it is a different one — and the SQL step, handed a `compare` plan with
one company, improvised (q036 renamed `value` to `apple_revenue`).

The rule, in `_one_sided`: refuse when **all** of

* the intent is `compare` or `rank`,
* the question **named** its companies (company elements — the implicit
  every-filer scope never refuses here), and
* fewer than two companies can take part.

Two paths reach it, and both refuse the same way:

* **a named company is not loaded** (Samsung) → an `Unresolved` on that company
  element: "There is no data for 'Samsung' at this time …";
* **a named company is loaded but reports nothing for the metric** (JPMorgan and
  gross profit) → an `Unresolved` on the metric element, and that element's
  bindings are dropped with it.

Above the threshold nothing changes: five named companies with one missing
still ranks the other four, with the §8c note and its subset warning.
`lookup`, `trend` and `derive` keep §8c unchanged — "revenue for Apple and
Samsung" is two lookups, and Apple's figure stays true on its own.

Counted per metric, and answered per part. "Compare Apple and JPMorgan on
revenue and gross profit" compares revenue and refuses gross profit, naming
JPMorgan as the missing side: the refusal sits on the gross-profit element, and
a metric's refusal is its own part's answer (`Unresolved.blocks_question`,
§8e). Only a *company* that is not loaded sinks the whole comparison, because
every metric depends on it.

The rule rests on `intent`, which the parser's model sets. A comparison tagged
`lookup` falls back to §8c: half an answer with a note, never a wrong number.

## 8e. A question is answered per part

Added 2026-09-26. "What are Apple's: assets, liabilities, stockholders' equity,
cash, goodwill, inventory" asks six things. Apple files no goodwill, so that
part is refused — and before this, the refusal made the plan incomplete and the
other five went unanswered too. The asker got nothing for five figures the
store holds.

Now every `Unresolved` carries `blocks_question`, set at the end of `map_query`
from the element's kind:

* **metric or narrative element → `False`**, a per-part refusal. The other
  parts are answered, and this one goes back with its reason.
* **anything else → `True`** (the default): a company, a company group or a
  period. Every figure depends on scope, so there is no part left to answer.

`QueryPlan.has_answerable_part` — some binding, and no blocking refusal — is
what the caller checks before running SQL. `is_complete` keeps its meaning,
"everything bound". A clarification or an ambiguity only ever arises for a
metric, so it is always per part: the reply can hold the figures, the refusals
and the clarifying question together.

`ResultSpec` counts only the metrics that bound (`_answering_metrics`), for the
same reason it counts only the companies that did (§8c): `row_count` is a
promise, and six promised against five fetched would report a shortfall the
plan had already explained.

## 8a. Declining, for terms the dataset simply does not hold

`unavailable` carries a sentence of curated reasoning that becomes
`Unresolved.reason` verbatim. It is not a third flavour of "we could not find
it": it is a statement that someone looked, and there is nothing to find.

**Why it has to be in the file rather than left to fail naturally.** An
unlisted term does not fail naturally — it falls into the embedding search,
which always returns *something*. Measured 2026-09-19 across 52 unaliased
business phrases and six filers, that path committed eight bindings with no
human behind them, and half were wrong. "Share price" bound
`dei:EntityListingParValuePerShare` at 0.726, which for Microsoft is
$0.000006 — a plausible wrong number with a rationale reading "verified over
1 period(s)". "Stock price" scored 0.733 against
`CommonStockParOrStatedValuePerShare` and was held back only by a second
candidate happening to survive alongside it, which is luck, not a guard.

So the entries exist for the questions people actually ask that this store
cannot answer, and they reach the resolver first. Four are curated today:

| entry | why it is not here |
|---|---|
| `stock_price` | set by the market, never filed |
| `market_cap` | needs a price; `EntityPublicFloat` is not it |
| `segment_revenue` | the XBRL data endpoint returns only undimensioned consolidated facts (PITFALLS §3.2) |
| `gross_revenue` | US GAAP has no gross-versus-net revenue pair; the tagged revenue line is already net |

`gross_revenue` is the instructive one. It was left *unlisted* for a long time
on the correct reasoning that there was nothing honest to map it to — but
unlisted is not declined. It fell to the embedding net, which offered
`GrossProfit` at 0.812: a different line, and one *smaller* than revenue where
the asker expected something larger.

**The reason text names the plausible wrong answer.** Each one says what the
thing is, why no filing carries it, and which nearby concept an embedding
search would reach for — `EntityPublicFloat` is not market cap, par value is
not a share price. Naming it is what stops the mistake being re-derived by the
next person, or the next model, who notices the concept exists.

**It refuses rather than asks.** `needs_input` stays false: a clarification
offers a choice the asker can make, and here there is none.

**Three ways a plan can fall short, and they are not the same:**

| field | meaning | what to do |
|---|---|---|
| `unresolved` | the data cannot support it — either measured (no coverage) or curated (`unavailable`) | say so; do not ask |
| a `narrower_than_asked` note | it answered, with a figure that is a *subset* of the phrase | show the number **and** the sentence |
| `ambiguous` | the *machine* could not choose; candidates are raw concepts | offer them, imperfectly |
| `clarifications` | a *person* decided the term is several things and wrote the choices | ask properly |

`QueryPlan.needs_input` is true for the latter two. It separates "ask them"
from "tell them it cannot be done", which is the distinction a user-facing
model needs and cannot infer from `is_complete` alone.

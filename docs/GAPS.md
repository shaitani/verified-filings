# Known gaps, defects and data hazards

What the system gets wrong, cannot do, or could get wrong — each with the
evidence behind it. Work to close these is listed in [FUTURE.md](FUTURE.md).

Three parts:

1. [Open gaps](#open-gaps) — weaknesses in the chain, most serious first
2. [Questions that come back wrong](#questions-that-come-back-wrong) — known
   bad replies, collected to fix together
3. [Data hazards](#data-hazards) — every known way XBRL data misleads, handled
   or not, numbered `D1.1` onward and cited by that number in code

---

## Open gaps

### G1. Nothing checks that the answer matches the question

"Did any of these companies restate its revenue?" comes back complete with a
revenue series and no caveat: every element resolved, so by every measure the
mapper has, the plan is perfect. It answers a different question. Seen again
live: a ranking question returned the underlying figures, verdict `complete`,
answerable. The verdict checks cardinality and attribution, not meaning.
Shapes that fail this way: restatement, causality, counts of filings,
anything about the *filing* rather than its figures. The natural home is the
parser, which deliberately does not judge answerability yet
([app/parser/DESIGN.md](../app/parser/DESIGN.md) §7). Today "why" questions are
caught by the `narrative` element; the rest are not.

### G2. What Qwen still writes has no structural check

Python writes every fetch, every multi-operand metric, every threshold, every
change / growth / CAGR and every ranking's order. Qwen still writes the layer
above `figures` for an aggregate (q040's average) or another derivation, and
that statement is checked for shape, not meaning. Measured failure: q040
leaving `unit` out of an average (refused; now a prompt rule).

### G3. The model can switch off the unit guard

`_wrong_unit` is the one structural check on derived arithmetic: a binding
whose result is `pure` cannot have rows in `USD`. It skips any row whose
`derivation` is set — and `derivation` is written by the model. A ratio
question that came back as a subtraction, labelled `USD`, with a plausible
`derivation` name, was graded `complete` and answerable for three days before
anything noticed. **Do not trust a model-supplied flag to gate a check on that
model's own output.** TODO in `app/retrieval/executor.py`.

### G4. A company the parser drops widens the question silently

A question naming no company means every filer, so an omitted company element
is not a refusal — it is an answer about all twenty. Measured 2026-09-27: a
prompt change made the q018 round trip drop "Costco" and rank every filer's
quarters, verdict `complete`. Fixed that time by example order, not by a
check.

### G5. A relationship between two metrics has nowhere to live

"How much of Alphabet's revenue goes to R&D?" is a ratio of two filed figures,
and the chain cannot say so: the parser emits two independent metric
elements, the mapper binds each alone, and the division survives only as
English in `question`. `QueryIn` is a flat list with no field relating one
element to another, so even a perfect parser would have nowhere to put it.
The only route today is a curated alias per *phrase* — "R&D intensity" binds,
"how much of revenue goes to R&D" does not — which cannot scale. q023 is the
eval case. **Probably the highest-value thing not built.**

### G6. Eval grades are not fully reproducible

A question's first attempt depends on Ollama's prompt cache, and the runner
does not unload the model between questions, so a question can inherit cache
state from the ones before it ([TESTING.md](TESTING.md#the-prompt-cache)).
q034 once failed in a full run and passes cold and warm in isolation;
unexplained.

### G7. Retrieval's open edges

- **A ranking's order is not in the verdict.** Python writes it and `BarView`
  checks it where it is drawn; the verdict checks cardinality only.
- **Mixed units are prevented, not detected.** `unit` in the join key stops a
  binding mixing them, but nothing flags a result set whose rows would be
  meaningless summed.
- **The `LIMIT` rule's wording is unpinned.** "End with LIMIT 500 or less"
  once made every ranking return one row ([app/retrieval/DESIGN.md](../app/retrieval/DESIGN.md)
  §5). No test asserts on the rules text, so a tidy-up could restore it.

### G8. Refusal text written for a developer reaches readers

Mapper reasons go to the reader verbatim, and some were written for whoever
was debugging: q049's "no loaded company matches sic_description='a given
sector'", and q028 / q029's knock-on "no reporting period in scope to verify
coverage against" (the Web Server shows only the blocking refusal, which hides
the second).

### G9. Smaller gaps

- **Restatements are never disclosed** (D2.1). `is_latest` picks the right
  value; nothing says a value *was* restated.
- **Segment and geography questions refuse, by design.** The XBRL data
  endpoint carries no dimensional facts (D3.2). An unsatisfiable qualifier
  takes its metric with it, so "revenue from iPhones" is refused rather than
  answered with total revenue.
- **`sic_office` has no source.** `sic_numbers.json` carries code and
  description only; the SEC assigns review offices by SIC *range*, a mapping
  this project does not have. Its refusal says so.
- **Retired tickers do not resolve.** Both SEC feeds give only the current
  symbol, so "FB" does not find Meta. Never a wrong company — the cik never
  changes — only a refusal.
- **A single year with no loaded window refuses.** "Apple's revenue in 2019"
  names a year outside FY2021–FY2025. A range reaching past the store is
  answered over the years it has, with a note naming the rest. Answering a
  missing year from the comparative columns a later 10-K carries is an open
  decision.
- **Alias recall.** Terms people ask that fall to embedding search: interest
  income, treasury stock, deferred revenue, operating expenses, depreciation,
  accounts receivable / payable, retained earnings.
- **An aggregate over one company's periods** ("Apple's average revenue over
  five years") keeps the company but is labelled with a single period.
- **Submissions pagination.** SEC's `filings.recent` holds a year or 1,000
  filings, whichever is more. JPM and BAC file thousands of structured notes a
  year, so their `recent` block holds one 10-K and three 10-Qs; the rest sit in
  overflow files `get-submission` does not fetch. Affects the submissions JSON
  only, not the XBRL data store.
- **The loader does not cross-check** a file's internal `cik` against the
  ticker used to find it.
- **Production gaps before it is used in earnest.** GitHub sign-in is off
  (no production OAuth app); sessions last 30 days; Caddy has no per-IP request
  limits; nothing is backed up; whether Funnel hands Caddy the visitor's real
  address is unconfirmed. See the deployment entry in [FUTURE.md](FUTURE.md).

### Deferred by the user — do not reopen unprompted

**Amended filings (D3.4).** Ingest fetches only `10-K` and `10-Q`, so a
`10-K/A` — the vehicle for a material restatement — is never seen.

---

## Questions that come back wrong

Every question known to get a wrong, odd or badly worded reply, kept in one
place to be addressed together (the user's call). Several grade `pass`,
because the eval set grades the decision, not the figure.

| question | what comes back | where | status |
|---|---|---|---|
| q057 "highest operating income at Costco **in 2024**" | Costco's **FY2025** quarters: the parser drops "in 2024" and emits `last_n_years: 1` | parser | a plausible wrong answer |
| q042 "has anyone's total debt more than **doubled** since 2021?" | year-over-year dollar changes for 15 filers, not growth from 2021 with a >100% filter | parser / mapper | G1 |
| q040 "**average** R&D spend across these companies" | the average on 14 rows, one per company | executor | the Presenter shows it as one figure naming the 14; the real fix is the closed list of operations (FUTURE) |
| q018's round trip | dropped "Costco" and ranked every filer's quarters | parser | fixed by example order, not a check — G4 |
| q023 "how much of Alphabet's revenue goes to R&D?" | one of the two operands, not the ratio | schema | `known_gap`; G5 |
| q015 "gross margin fell three years running" | refused: the 7B model runs out of tokens | executor | `known_gap` |
| q050 "by sic office … quarterly and yearly" | refused before the mapper: the parser invents a year | parser | `known_gap` |
| q048 "quarterly revenues: gross, net" | fails from a cold model, passes warm | parser | G6 |
| q055 "Apple's **interest expense**" | refused: Apple stops tagging it after FY2023 | data | eval expects `answered`; decide whether the expectation or the answer changes |
| q028 / q029 "Apple's revenue in 2015 / 2019" | the right refusal, plus a knock-on one | mapper | G8 |
| q049 "companies in **a given sector**" | a reason written for a developer | mapper | G8 |

---

## Data hazards

The SEC's XBRL data is filed by ~8,000 companies against a taxonomy that
changes every year, with no requirement that two filers describe the same
thing the same way. Most of this project's hard problems are the data being
less uniform than it looks, not code.

Each hazard carries **the measurement that shows it applies to this store**.
Measured over all 20 companies, 5 fiscal years, 174,390 facts on 2026-09-19
unless noted. Several are "true today" rather than "true by construction":
re-measure after loading new filers ([TESTING.md](TESTING.md#verifying-figures-yourself)).
Read D2 and D3 before trusting a number.

### D1. Handled

| # | hazard | handled by |
|---|---|---|
| D1.1 | `Filing.fiscal_year` is provenance, not period | `query_mapper._load_windows`; the view exposes no fiscal year |
| D1.2 | 52/53-week fiscal calendars | windows matched by day range |
| D1.3 | Fiscal years end on different dates per filer | `ResolvedPeriod` is per company |
| D1.4 | No filer ever files a Q4 | the view synthesizes it |
| D1.5 | Q4 arithmetic differs for balances and flows | the view synthesizes flows only |
| D1.6 | Restatements, including stock splits | `Fact.is_latest` |
| D1.7 | Filers tag the same metric differently | ordered alternatives + coverage |
| D1.8 | A filer changes tags mid-range | per-period bindings + a verified `Note` |
| D1.9 | Zero rows reads as "reported nothing" | `Coverage` |
| D1.10 | A derived value missing a component | the view's pairing; `figures` |
| D1.11 | Some metrics are not a single concept | `expression` over operand slots |
| D1.12 | Two equally good embedding matches | `Ambiguity`, never a guess |
| D1.13 | Short query against long description | nomic task prefixes |
| D1.14 | One concept filed in several units | `_gather_evidence` |
| D1.15 | Sign assumptions inside an expression | alias `sign:` + `_sign_violation` |
| D1.16 | One fiscal label, very different dates | `_alignment_notes` |
| D1.17 | `unit` on a derived binding | the result's unit, not the lead operand's |

#### D1.1 `Filing.fiscal_year` is provenance, not the period a number describes

A 10-K carries two years of comparative columns. Combined with `is_latest`
(which keeps the most recently *filed* copy of a period), filtering facts
through `filing.fiscal_year` selects by which filing a number appeared in.

**Measured:** Apple's FY2024-filed 10-K holds the `2021-09-26 → 2022-09-24`
duration — FY2022's window — as its surviving `is_latest` row, because the
FY2025 10-K later superseded Apple's copies of FY2023 and FY2024. A query on
`fiscal_year = 2024` returned FY2022 revenue silently, before this was fixed.

**Handled:** periods resolve to concrete date windows read from the facts;
the plan carries no bare year integers, and `xbrl.reported_fact` has no
`fiscal_year` column at all.

#### D1.2 52/53-week fiscal calendars

A quarter is not 91 days and a year is not 365. **Measured:** quarterly
windows run 83–97 days; J&J's FY2021 runs `2021-01-04 → 2022-01-02`; two of
100 annual filings end in a different calendar year than their `fiscal_year`.

**Handled:** windows are matched by day range, never computed from the label.
A year's *name* comes from the filing, its *dates* from the facts.

#### D1.3 Fiscal years end on different dates per filer

"FY2024" is a different calendar span per company. **Measured:** Microsoft
FY2024 is `2023-07-01 → 2024-06-30`, Apple's `2023-10-01 → 2024-09-28`,
NVIDIA's FY2025 ends `2025-01-26`. Q4 FY2025 is Apr–Jun for MSFT, Jun–Sep for
AAPL and Oct–Jan for NVDA.

**Handled:** `ResolvedPeriod` is per company, so a plan shows three different
windows rather than implying alignment; D1.16 warns when a comparison leans on
a shared label.

#### D1.4 No filer ever files a Q4

US filers file 10-Qs for Q1–Q3 and a 10-K for the year. **Measured:** 100
`FY`, 99 `Q1`, 99 `Q2`, 100 `Q3` and **zero** `Q4` filings.

**Handled:** `xbrl.reported_fact` synthesizes each Q4 as the annual figure
minus the nine-month year-to-date one sharing its start (migration
`a8b5b820cf1a`), marked `is_synthesized`. The pair is found by the gap it
leaves (80–120 days), not by the subtrahend's own length: Costco's quarters
run 12/12/12/16 weeks, so its nine months is 251 days and a fixed bucket
silently lost every Costco Q4. 9,464 synthesized rows, none ambiguous. 39 of
those windows are also filed directly, all by J&J; every one agrees to the
cent, and the filed row wins. Verified: FY2025 Q4 revenue 102.5B (AAPL), 76.4B
(MSFT), 39.3B (NVDA), matching reported figures.

This used to be asked of the SQL-writing model, which returned Apple's whole
FY2024 revenue as its Q4 — 36 of 36 rows, every one attributed, verdict
`complete`.

#### D1.5 Q4 arithmetic differs for balances and flows

Revenue at Q4 is annual minus nine months. Total assets at Q4 is the
fiscal-year-end balance — subtracting anything would be wrong.

**Handled:** the view synthesizes duration facts only. An instant needs no
arithmetic: a Q4 window ends on the fiscal-year end, so the balance filed for
that date is the Q4-end balance.

#### D1.6 Restatements, including stock splits

**Measured:** 1,575 periods carry more than one value. The largest are
retroactive split adjustments — GOOGL `CommonStockSharesAuthorized` FY2021 as
both 15B and 300B (20-for-1), AMZN 5B / 100B, NVDA 8B / 80B — plus genuine
revisions: GOOGL `PropertyPlantAndEquipmentGross` FY2023 moved 166.7B →
201.8B.

**Handled:** the load step keeps every row and marks the most recently filed
one `is_latest` per `(company, concept, unit, window)`. Per-share figures are
comparable only within one filing vintage, and nothing says a value was
restated (D2.1).

#### D1.7 Filers tag the same metric differently

**Measured:** 16 companies report revenue as
`RevenueFromContractWithCustomerExcludingAssessedTax`, 10 as `Revenues`; cost
of revenue splits 12 / 5 between `CostOfGoodsAndServicesSold` and
`CostOfRevenue`.

**Handled:** `metric_aliases.yaml` lists alternatives in preference order and
coverage picks per company — Apple and Microsoft bind the first, NVIDIA the
second, from one entry with no per-filer table.

#### D1.8 A filer changes tags mid-range

**Measured:** Alphabet reports revenue as
`RevenueFromContractWithCustomerExcludingAssessedTax` for FY2021–2024 and as
`Revenues` for FY2021, 2023, 2024 and 2025. Neither covers all five years, so a
five-year question once returned *nothing*.

**Handled:** coverage is decided per period, so Alphabet yields two bindings
and the switch is disclosed as a `Note`. The seam is *verified*: where both
concepts report a period, their values are compared. For Alphabet they agree
to the dollar in all three overlapping years, so the note says the series was
stitched; if they disagreed or never overlapped, it says so instead
(`unverified_switch`). Likely mechanism: taxonomy deprecation (D3.3), not
filer whim.

#### D1.9 Zero rows reads as "the company reported nothing"

A well-formed query over a wrongly bound concept returns nothing, which is
indistinguishable from a genuine absence.

**Handled:** `Coverage` records the facts found; a candidate with no facts for
the requested windows is never bound, whatever its similarity score.

#### D1.10 A derived value missing a component

Dropping a term from a subtraction does not error; it returns a different
number. Omit the nine-month term and "Q4" becomes the whole year.
**Measured:** NVIDIA carries four *annual* facts under the revenue concept
Apple and Microsoft use quarterly, and no nine-month ones.

**Handled:** the view emits a Q4 only where both halves exist, so a missing
component is a missing row — which coverage then refuses — never a year. A
multi-operand metric is computed in `figures`, where a missing operand gives a
NULL, not a partial sum.

#### D1.11 Some metrics are not a single concept

Gross margin is `GrossProfit / Revenues`; free cash flow is operating cash
flow minus capex. No such concept exists, so no search finds them.

**Handled:** an alias entry's `terms` are operand slots with an `expression`
over them.

#### D1.12 Two equally good embedding matches

**Measured:** "dividends paid" (no alias) returns `PaymentsOfDividends` at
0.757 and `CommonStockDividendsPerShareDeclared` at 0.686, both covered — one
USD, one per-share, different questions.

**Handled:** reported as an `Ambiguity`, never decided on cosine distance.
Such pairs are the queue for the next alias entry.

#### D1.13 Short query against long description

nomic-embed-text v1.5 is trained with task prefixes and the Ollama model adds
none, so unprefixed it treats a two-word query and a 1,700-character concept
description as the same kind of text. **Measured:** "net income" ranked
`NetIncomeLoss` fifth unprefixed and first prefixed.

**Handled:** `SEARCH_DOCUMENT_PREFIX` is part of each concept's embedded text
(so changing it re-embeds the corpus via the hash), and `embed_query` adds
`SEARCH_QUERY_PREFIX`.

#### D1.14 One concept filed in several units

**Measured:** `EffectiveIncomeTaxRateContinuingOperations` appears as both
`pure` and `Rate` for all 20 companies; `DebtInstrumentCarryingAmount` as EUR
and USD for 12. Dropping `unit` from the grain makes AMD's FY2024 tax rate two
rows that sum to 0.38.

**Handled:** `_gather_evidence` picks one unit per company and concept and
keeps only that unit's windows, so a binding cannot pass coverage on EUR
facts and be reported as USD; `unit` is in every join key.

#### D1.15 Sign assumptions inside an expression

Capex is a *magnitude* — filers tag it positive and the `-` in `c0 - c1`
supplies the direction — so a filer tagging it negative would make free cash
flow *add*. A blanket "must be positive" rule would be wrong:

```
PaymentsToAcquirePropertyPlantAndEquipment   14 companies +,  0 −
NetCashProvidedByUsedInOperatingActivities   20 companies +,  7 −  (47 facts)
GrossProfit                                   9 companies +,  1 −  (6 facts)
```

Negative operating cash flow is real cash burn; Micron's negative gross profit
is real. Those are signed quantities.

**Handled:** an operand slot may declare `sign: magnitude`, and
`_sign_violation` checks the chosen concept's values at bind time. A violation
**refuses** the binding — refuse when the number would be wrong, note when it
is right but needs context. The default is `signed`; a test asserts no plain
`c0` lookup declares a sign. **Still open:**
`PaymentsForProceedsFromOtherInvestingActivities` is positive for 16 companies
and negative for 15, and nothing stops someone aliasing it into an expression
where neither sign is right.

#### D1.16 One fiscal label, very different dates

**Measured:** one fiscal label covers `period_end` dates up to **343 days**
apart — "FY2025" runs from 2025-01-26 (NVIDIA) to 2025-12-31 (Alphabet). A
line chart puts "Q1 2025" at one x position, and the reader takes the points
as contemporaneous.

**Handled:** `_alignment_notes` emits a plan-level `period_misalignment` note
whenever a result varies by company and a shared label's dates span more than
30 days, reporting the worst label. Silent for one company and for filers that
genuinely align. The Web Client plots series on real dates, not labels.

#### D1.17 `unit` on a derived binding

`Binding.unit` used to be the first operand's, so `gross_margin` (`c0 / c1`
over USD) reported "USD" and a formatter would show a 0.46 margin as 46 cents.

**Handled:** a division of equal units reports `pure` (XBRL's own name for a
dimensionless quantity); other arithmetic reports the shared operand unit;
operands in different units are refused. `operand_unit` carries what the facts
are filed in, for the join.

### D2. Partially handled

#### D2.1 Restatement disclosure

`is_latest` picks the right value, but the plan never says a value *was*
restated, or by how much. The `Note` channel would carry it; the view hides
superseded rows, so it needs them from elsewhere.

#### D2.2 Company name ambiguity

`Ambiguity` is concept-shaped, so a company element matching several filers is
reported through `Unresolved` with the candidates named ("Apple Inc. (AAPL),
ADVANCED MICRO DEVICES INC (AMD) … Name one of them."). Correct; less
convenient than a pick.

### D3. Not handled

#### D3.1 Company extension elements

Filers define their own concepts for anything the taxonomy lacks.
**Measured:** the store holds only `us-gaap` (1,897 concepts), `srt` (4) and
`dei` (3); every extension element was filtered out at ingest. A metric a
company reports only under its own namespace is invisible here, and no alias
will find it.

#### D3.2 Dimensional / segment data

**Measured:** zero collisions on `(company, concept, unit, period_start,
period_end)` among `is_latest` facts — the XBRL data endpoint returns only
undimensioned consolidated facts. "Revenue by segment", "by geography", "by
product" are not answerable from this store at all, and are refused rather
than answered with the consolidated total.

#### D3.3 Deprecated taxonomy elements

The US GAAP taxonomy retires concepts every year — very likely the mechanism
behind D1.8. The store holds no taxonomy version metadata, so a deprecated
concept cannot be told from an unused one, and nothing warns that a preference
order is going stale.

#### D3.4 Amended filings

Ingest is scoped to `10-K` and `10-Q`. A `10-K/A` — the vehicle for a material
restatement — is never fetched, so the most consequential restatements are
exactly the ones not seen. Deferred by the user; do not reopen unprompted.

#### D3.5 Currency

EUR facts exist (T-Mobile and Oracle debt instruments). There is no FX
handling. D1.14 stops a *mixed*-unit binding, but a wholly EUR binding would be
compared against USD downstream without complaint.

#### D3.6 DQC validation rules

XBRL US publishes ~20 filer-side rule sets; none are applied. Deliberately so
for the negative-value rules: DQC 0015 flags negative `GrossProfit`, and the
store's 6 such facts are Micron FY2023, a real downturn. Useful as review
signals, wrong as hard filters.

#### D3.7 Scaling errors

Filers sometimes tag thousands as millions. **Measured:** no annual revenue
spread above 100× within any company, so nothing is present today. Nothing
guards against it.

#### D3.8 A filing's `fy` can be wrong, and ingest scopes by it

The SEC's `fy` on a fact comes from the filing's own fiscal-year tag, and
ingest keeps FY2021–FY2025 by that field. **Measured:** NVIDIA tagged its
first two FY2021 10-Qs (filed 2020-05-21 and 2020-08-19, periods ending
2020-04-26 and 2020-07-26) `fy: 2020`, so both were dropped although their
periods sit inside FY2021. Every other filer has every FY and quarter window
from FY2021 to FY2025; NVIDIA lacks FY2021 Q1 and Q2. A range reaching them
says so in a note (semantic DESIGN §8c); a single `Q1 2021` for NVIDIA is
refused.

### D4. Checked, and not a problem here

- **Negative `GrossProfit`** — all 6 facts are Micron FY2023, genuine.
- **Revenue scaling** — no within-company annual spread above 100×.
- **Capex sign** — positive across all 14 reporting filers.
- **Duplicate facts across filings** — the natural-key index plus `is_latest`
  collapse a 10-K's repetition of 10-Q figures.

### References

- [XBRL US — Approved Validation Rules](https://xbrl.us/home/priorities/data-quality/rules-guidance/)
- [XBRL US — Negative Values (DQC 0015)](https://xbrl.us/data-rule/dqc_0015/)
- [XBRL US — Guiding Principles for Element Selection](https://xbrl.us/home/priorities/data-quality/rules-guidance/principles/)
- [SEC — Staff Observations From Review of Interactive Data](https://www.sec.gov/about/divisions-offices/division-economic-risk-analysis/office-structured-disclosure-staff/staff-observations-review-interactive-data-financial-statements-june-15-2011)
- [SEC — EDGAR APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)

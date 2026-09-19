# XBRL data pitfalls: what bites, what we handle, what we don't

The SEC's XBRL data is filed by ~8,000 companies against a taxonomy that
changes annually, with no requirement that two filers describe the same thing
the same way. Most of the hard parts of this project are not code problems —
they are the data being less uniform than it looks.

This file catalogues every hazard we know about, **with the measurement that
proves it applies to this store**, and says plainly whether it is handled.
Sections 2 and 3 are the ones to read before trusting a number.

Measured against all 20 companies / 5 fiscal years / 174,390 facts on
2026-09-19 unless noted. Re-measure after loading new filers — several of these
are "true today" rather than "true by construction".

---

## 1. Handled

| # | Pitfall | Where |
|---|---|---|
| 1.1 | `Filing.fiscal_year` is provenance, not period | `query_mapper._load_windows` |
| 1.2 | 52/53-week fiscal calendars | `_ANNUAL_SPAN_DAYS` / `_QUARTER_SPAN_DAYS` |
| 1.3 | Fiscal year ends differ by filer | `ResolvedPeriod` is per company |
| 1.4 | No filer ever files a Q4 | `query_mapper._with_derived_q4` |
| 1.5 | Q4 arithmetic differs for balances vs flows | `Binding.period_rule` |
| 1.6 | Restatements, including stock splits | `Fact.is_latest` (load step) |
| 1.7 | Filers tag the same metric differently | ordered alternatives + coverage |
| 1.8 | A filer changes tags mid-range | per-period bindings + `Note` |
| 1.9 | Zero rows reads as "reported nothing" | `Coverage` |
| 1.10 | A derived value missing a component | `Binding` validator |
| 1.11 | Some metrics are not a single concept | `expression` + operand slots |
| 1.12 | Two equally good embedding matches | `Ambiguity`, never a guess |
| 1.13 | Short query vs long description retrieval | nomic task prefixes |
| 1.14 | One concept filed in several units | `_gather_evidence` |
| 1.15 | Sign assumptions inside an expression | alias `sign:` + `_sign_violation` |

### 1.1 `Filing.fiscal_year` is provenance, not the period a number describes

A 10-K carries two years of comparative columns. Combined with `is_latest`
(which keeps the most recently *filed* copy of a period), filtering facts
through `filing.fiscal_year` selects by which filing a number appeared in.

**Measured:** Apple's FY2024-filed 10-K holds the `2021-09-26 → 2022-09-24`
duration — FY2022's window — as its surviving `is_latest` row, because the
FY2025 10-K later superseded Apple's copies of FY2023 and FY2024. A query
filtered on `fiscal_year = 2024` would have returned FY2022 revenue silently.

**Handled:** periods resolve to concrete date windows read from the facts.
`PlanFilters.periods` carries no bare year integers. See `app/schemas/DESIGN.md`
§8.11.

### 1.2 52/53-week fiscal calendars

A "quarter" is not 91 days and a "year" is not 365.

**Measured:** quarterly windows run 83–97 days across the store; JNJ's FY2021
runs `2021-01-04 → 2022-01-02`, so its `period_end` falls in the *next*
calendar year. Two of 100 annual filings end in a different calendar year than
their `fiscal_year`.

**Handled:** windows are matched by day-range, never computed from the label.
The fiscal year's *name* comes from `Filing.fiscal_year`; its *dates* come from
the facts.

### 1.3 Fiscal year ends differ by filer

"FY2024" is a different calendar span for different companies, so a
cross-company comparison of the same label compares different windows.

**Measured:** Microsoft FY2024 is `2023-07-01 → 2024-06-30`; Apple's is
`2023-10-01 → 2024-09-28`; NVIDIA's FY2025 ends `2025-01-26`. Q4 FY2025 is
Apr–Jun for MSFT, Jun–Sep for AAPL and Oct–Jan for NVDA.

**Handled:** `ResolvedPeriod` is per company, so the plan shows the three
different windows rather than implying alignment. **Not** handled: nothing
warns that comparing them is apples-to-oranges — see §3.6.

### 1.4 No filer ever files a Q4

US filers file 10-Qs for Q1–Q3 and a 10-K covering the year; the fourth
quarter is only ever implicit.

**Measured:** the store holds 100 `FY`, 99 `Q1`, 99 `Q2`, 100 `Q3` and **zero**
`Q4` filings.

**Handled:** `_with_derived_q4` synthesizes it from the annual and Q3 windows
it already resolved, and `PeriodResidual` carries the two windows the SQL step
subtracts. Verified end to end: FY2025 Q4 revenue of 102.5B (AAPL), 76.4B
(MSFT), 39.3B (NVDA), all matching reported figures.

### 1.5 Q4 arithmetic differs for balances vs flows

Revenue at Q4 is `annual − nine-month`. Total assets at Q4 is just the
fiscal-year-end instant — subtracting anything would be wrong, not merely
unnecessary.

**Handled:** `Binding.period_rule` sits on the binding rather than the period,
because the answer depends on the concept. A `residual` rule on an instant
concept fails validation.

### 1.6 Restatements, including stock splits

The same concept and window can carry different values in different filings.

**Measured:** 1,575 periods have more than one value. The largest are
retroactive split adjustments — GOOGL `CommonStockSharesAuthorized` FY2021
appears as both 15B and 300B (20-for-1), AMZN 5B/100B (20-for-1), NVDA 8B/80B
(10-for-1). Also genuine revisions: GOOGL `PropertyPlantAndEquipmentGross`
FY2023 moved 166.7B → 201.8B.

**Handled:** the load step maintains `Fact.is_latest` per
`(company, concept, unit, window)`, so analytics see the most recent figure.
**Caveat:** per-share figures are only comparable within one filing vintage,
and nothing surfaces *that* a value was restated — see §2.2.

### 1.7 Filers tag the same metric differently

**Measured:** 16 companies report revenue under
`RevenueFromContractWithCustomerExcludingAssessedTax`, 10 under `Revenues`.
Cost of revenue splits 12 / 5 between `CostOfGoodsAndServicesSold` and
`CostOfRevenue`.

**Handled:** `metric_aliases.yaml` lists alternatives in preference order and
the coverage filter picks per company. Apple and Microsoft bind the first,
NVIDIA the second, from one entry with no per-filer table.

### 1.8 A filer changes tags mid-range

The one that motivated this document.

**Measured:** Alphabet reports revenue under
`RevenueFromContractWithCustomerExcludingAssessedTax` for FY2021–2024 and
`Revenues` for FY2021, 2023, 2024, 2025. Neither covers all five years, so
before the fix a five-year question returned *nothing*.

**Handled:** coverage is decided per period and periods sharing a concept are
grouped, so Alphabet yields two bindings. The switch is disclosed as a `Note`,
and the seam is *verified*: where both concepts report a period their values
are compared. For Alphabet they agree to the dollar in all three overlapping
years, so the note says the series was stitched. If they disagreed, or there
were no overlap, the note says so instead — `unverified_switch`.

### 1.9 Zero rows reads as "the company reported nothing"

A well-formed query over a wrongly-bound concept returns an empty result, which
is indistinguishable from a genuine zero.

**Handled:** `Coverage` records the facts found. A candidate with no facts for
the requested windows is never bound, whatever its similarity score.

### 1.10 A derived value missing a component

Dropping a term from a subtraction does not error; it returns a different
number. Omit the nine-month term and "Q4" silently becomes the whole year.

**Measured:** NVIDIA carries four *annual* facts under the revenue concept
Apple and Microsoft use quarterly, and zero nine-month ones. A Q4 binding to it
passes a concept-level count and then cannot be computed.

**Handled:** `Binding` refuses to validate a `residual` binding whose
per-component coverage is not each proven non-empty. The bug is
unrepresentable, not merely discouraged.

### 1.11 Some metrics are not a single concept

Gross margin is `GrossProfit / Revenues`; free cash flow is operating cash flow
minus capex. No embedding search finds these because no such concept exists.

**Handled:** `terms` is a list of operand slots with an `expression` over them.

### 1.12 Two equally good embedding matches

**Measured:** "dividends paid" (no curated alias) returns `PaymentsOfDividends`
at 0.757 and `CommonStockDividendsPerShareDeclared` at 0.686, both with
coverage. One is USD, one is per-share — different questions.

**Handled:** reported as `Ambiguity`, never decided on cosine distance. Such
pairs are the queue for the next alias entry.

### 1.13 Short query against long description

nomic-embed-text-v1.5 is trained with task prefixes and the Ollama model adds
none, so an unprefixed setup treats a two-word query and a 1.7k-character
concept description as the same kind of text.

**Measured:** "net income" ranked `NetIncomeLoss` #5 unprefixed and #1
prefixed; "operating income" went from `InterestIncomeOperating` to
`OperatingIncomeLoss` at the top.

**Handled:** `SEARCH_DOCUMENT_PREFIX` is baked into
`Concept.embedding_source_text` (so changing it re-embeds the corpus via the
hash), and `embed_query` applies `SEARCH_QUERY_PREFIX`.

### 1.14 One concept filed in several units

**Measured:** `EffectiveIncomeTaxRateContinuingOperations` appears under both
`pure` and `Rate` across all 20 companies; `DebtInstrumentCarryingAmount` under
EUR and USD across 12.

**Handled:** `_gather_evidence` picks one unit per `(company, concept)` and
keeps only that unit's windows, so a binding cannot pass coverage on EUR facts
and be reported as USD. No currently-aliased concept is multi-unit, so this is
a guard rather than a live fix.

### 1.15 Sign assumptions inside an expression

Arithmetic assumes a sign for each operand. Capex is a *magnitude* — filers tag
it positive and the `-` in `c0 - c1` supplies the direction — so a filer
tagging it negative would make free cash flow *add* and come out inflated, with
nothing downstream able to tell.

A blunt "must be positive" rule would be wrong, which is why this took a
declaration rather than a global check:

```
PaymentsToAcquirePropertyPlantAndEquipment   14 companies +,  0 −
NetCashProvidedByUsedInOperatingActivities   20 companies +,  7 −  (47 facts)
GrossProfit                                   9 companies +,  1 −  (6 facts)
```

Operating cash flow going negative is real cash burn — the element is literally
`ProvidedByUsedIn`. Micron's negative gross profit is real. Those are *signed
quantities* and must be left alone.

**Handled:** an operand slot may declare `sign: magnitude`, and
`_sign_violation` checks the chosen concept's actual values over the requested
windows at bind time. A violation **refuses** the binding rather than noting
it — the same line drawn for a missing residual component: refuse when the
number would be wrong, note when it is right but needs context. The default is
`signed`, so only the two arithmetic entries in `metric_aliases.yaml` carry an
annotation, and a test asserts no plain `c0` lookup declares one.

**Still open:** `PaymentsForProceedsFromOtherInvestingActivities` is positive
for 16 companies and negative for 15 — genuinely bidirectional, and nothing
stops someone aliasing it into an expression where neither sign is right. The
declaration is per slot, not per filer.

---

## 2. Partially handled — know the edges

### 2.1 `unit` on a derived binding

`Binding.unit` is read from the first operand. For `expression: "c0"` that is
exact; for `gross_margin` (`c0 / c1`) the result is dimensionless and "USD"
describes the operands, not the answer.

**Gap:** a display layer formatting these would render "0.46 USD". Needs either
a unit algebra or an explicit "derived/dimensionless" marker before anything
renders values.

### 2.2 Restatement disclosure

`is_latest` picks the right value, but the plan never says a value *was*
restated, or by how much.

**Gap:** the `Note` channel now exists and this would fit it. Not wired up.

### 2.3 Company name ambiguity

`Ambiguity` is concept-shaped, so a company element matching several filers is
reported through `Unresolved` with the colliding ciks named — correct, but it
reads worse than it should. See `app/schemas/DESIGN.md` §8.5.

---

## 3. Not handled at all

### 3.1 Company extension elements

Filers define concepts in their own namespace for anything the taxonomy does
not cover, and they do so heavily.

**Measured:** the store contains only `us-gaap` (1,897 concepts), `srt` (4) and
`dei` (3). Every extension element was filtered out at ingest.

**Consequence:** any metric a company reports only under its own namespace is
invisible here, and no amount of alias curation will find it. This is an ingest
scope decision (`app/ingest/`), not a mapper limitation.

### 3.2 Dimensional / segment data

**Measured:** zero collisions on
`(company, concept, unit, period_start, period_end)` among `is_latest` facts,
confirming the companyfacts endpoint returns only undimensioned consolidated
facts.

**Consequence:** "revenue by segment", "revenue by geography", "headcount by
division" are not answerable from this store at all — not badly, at all. A
question of that shape should be refused rather than answered with the
consolidated total.

### 3.3 Deprecated taxonomy elements

The US GAAP taxonomy retires concepts every year. A concept valid in 2021 may
be deprecated by 2025, which is very likely the *mechanism* behind the
Alphabet drift in §1.8 — not filer whim, a taxonomy change.

**Gap:** we hold no taxonomy version metadata, so we cannot tell a deprecated
concept from an unused one, and cannot warn that a preference order is about to
go stale.

### 3.4 Amended filings

Ingest is scoped to `10-K` and `10-Q`. A `10-K/A` — the vehicle for a material
restatement — is never fetched.

**Consequence:** the most consequential restatements are exactly the ones we
cannot see.

### 3.5 Currency

EUR-denominated facts exist in the store (`DebtInstrumentCarryingAmount`).
There is no FX handling anywhere, and no conversion to a reporting currency.

**Gap:** §1.14 stops a *mixed*-unit binding, but a wholly-EUR binding would be
reported as EUR and compared against USD downstream with no complaint.

### 3.6 Cross-company period comparability

§1.3 makes the different windows visible; nothing warns that comparing
Microsoft's Apr–Jun quarter against NVIDIA's Oct–Jan one is only sometimes
meaningful.

**Gap:** a plan-level note when the compared windows do not overlap
substantially would cover it. The `Note` channel would carry it.

### 3.7 DQC validation rules

XBRL US publishes ~20 rule sets for filer-side validation. None are applied
here.

**Deliberate for the negative-value rules at least:** DQC 0015 flags negative
`GrossProfit`, and the store's 6 such facts are all Micron FY2023 — a real
memory-market downturn, not an error. Those rules are useful as review signals
and wrong as hard filters.

### 3.8 Scaling errors

Filers sometimes tag thousands as millions.

**Measured:** no annual-revenue spread above 100x within any company, so
nothing obvious is present today. Nothing guards against it.

---

## 4. Checked, and not a problem here

- **Negative `GrossProfit`** — all 6 facts are Micron FY2023, genuine.
- **Revenue scaling** — no within-company annual spread above 100x.
- **Capex sign** — uniformly positive across all 14 reporting filers.
- **Duplicate facts across filings** — the natural-key index plus `is_latest`
  collapse the 10-K's repetition of 10-Q figures.

---

## 5. How to re-measure

The probes behind every number above are ad-hoc SQL over `xbrl.fact` joined to
`xbrl.concept` / `xbrl.filing`. The patterns worth keeping:

- **windows:** `DISTINCT ON (company, fiscal_year, fiscal_period)` ordered by
  `period_end DESC, (period_end - period_start) ASC` — the filing's own window,
  shortest span breaking ties so a discrete quarter beats the year-to-date.
- **drift:** per company, per alias slot, the set of fiscal years each
  alternative covers. Any slot where no single alternative covers the union is
  a drift case.
- **restatements:** group by `(company, concept, unit, period_start,
  period_end)` and count distinct values. Grouping without `period_start` is a
  trap — it compares 3-month against 9-month windows sharing an end date.
- **signs / units:** group by concept, count companies with positive vs
  negative values, or count distinct units.

## References

- [XBRL US — Approved Validation Rules](https://xbrl.us/home/priorities/data-quality/rules-guidance/)
- [XBRL US — Negative Values (DQC 0015)](https://xbrl.us/data-rule/dqc_0015/)
- [XBRL US — Guiding Principles for Element Selection](https://xbrl.us/home/priorities/data-quality/rules-guidance/principles/)
- [SEC — Staff Observations From Review of Interactive Data](https://www.sec.gov/about/divisions-offices/division-economic-risk-analysis/office-structured-disclosure-staff/staff-observations-review-interactive-data-financial-statements-june-15-2011)
- [SEC — EDGAR APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)

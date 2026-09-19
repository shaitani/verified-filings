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

## 4. Surface forms must be unique, at two levels

The schema rejects duplicate raw strings; `AliasIndex` additionally rejects
forms that only collide once normalized (lowercased, punctuation dropped,
underscores folded to spaces). Both exist because lookup resolving by dict
order is precisely the quiet wrongness this layer is meant to remove.

Normalizing underscores means a metric key like `free_cash_flow` answers to
"free cash flow" without anyone remembering to add it as a synonym. Punctuation
is dropped rather than replaced, so "R&D" folds to `rd` and not to `r d` — the
file lists the spelled-out forms separately.

## 5. Coverage still outranks the file

An alias entry is a hypothesis, not an answer. Every candidate it proposes goes
through the same coverage filter as an embedding candidate, and a curated
concept with no facts for the requested windows loses to a later alternative
that has them. Two entries in the file exist purely to make that testable
against the fixture (`tests/fixtures/metric_aliases_fake.yaml`).

## 6. Known rough edge: `unit` on a derived binding

`Binding.unit` is read from the first operand's facts. For `expression: "c0"`
that is exactly right; for `gross_margin` (`c0 / c1`) the result is
dimensionless and "USD" describes the operands, not the answer. Left as is
rather than inventing a unit algebra: `expression` is right there for a
consumer to notice, and a real unit system should wait until something needs
one. Worth fixing before any display layer formats these values.

## 7. What the resolver deliberately does not do

No fallback arithmetic. `gross_profit` maps only to `us-gaap:GrossProfit` even
though just 9 of 20 filers tag it, rather than quietly computing revenue minus
cost — a computed subtotal is a different number from the filer's own, and
silently substituting one for the other is the class of error this whole layer
exists to prevent. When a filer does not report it, "unresolved" is the honest
answer.

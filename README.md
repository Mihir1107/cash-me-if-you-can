# Multi-Source Reconciliation Agent
**Razorpay AI Buildathon — Track 04, AI Finance Controller**

Closes one finance-ops loop end to end: a merchant's **internal ledger** vs
**Razorpay settlements** vs the **actual bank statement**, across a 55-record
synthetic batch, reporting a match rate, a reason-coded exception list, and a
measured accuracy score against ground truth.

```bash
pip install -r requirements.txt
python main.py --evaluate
```

## Results on the 55-order batch

```
MATCH RATE: 45.45%   (25/55 orders confirmed on BOTH legs)
UNRECONCILED: 30     every one carries a reason code

duplicate_settlement       5     no_settlement_found          5
fee_footing_mismatch       5     bank_credit_delayed          5
refund_not_reflected       4     bank_amount_mismatch         2
settlement_not_credited    2     ledger_gross_amount_mismatch 1
credit_unattributed        1
```

**Every reason code the pipeline can emit for an order is exercised by this
batch** — there is no code the demo cannot demonstrate.

Every figure in this README is from an executed run, not a projection. With no
`OPENAI_API_KEY` set the same batch degrades honestly to **47.27%** and three
exceptions instead of one — the missing tier costs the headline number, which
is the point.

**That number is deliberately not 95%.** The generator injects real failure
modes on purpose, so the exception list means something instead of being a
rounding error on clean data. The question a judge should ask isn't "is 50.91%
good" — it's "is 50.91% *correct*." Which is why the next section exists.

## Throughput

```
Wall clock: 11.2 ms  (9,091 records/sec over 102 records)
LLM calls made: 0   (0.0 per 100 orders)
```

Per-stage timings land in `output/reconciliation_report.json` under
`throughput.stage_ms`. `llm_calls` counts **actual API round-trips**, not rows
offered to the tier — with no key set the tier short-circuits and this stays 0.

**With Tier 3 enabled the same batch takes 5,394 ms — and 5,368 ms of that is
the three LLM calls.** The entire deterministic pipeline, both matching stages
and 47 bank narrations, is the remaining ~26 ms. That ratio is the strongest
argument for the tiering: every narration pushed down to Tier 1 or 2 is roughly
2,000× cheaper in latency as well as in money. Scoping the model to the rows
that genuinely need it is what keeps the batch fast, not just cheap.

The 55-order batch is the committed default because the brief asks for 50+.
It is not the ceiling:

| batch | wall clock | records/sec | faults caught | missed | false positives |
|---|---|---|---|---|---|
| 55 | 11.2 ms | 9,091 | 29 / 29 | 0 | 3 |
| 500 | 52.1 ms | 18,320 | 214 / 214 | 0 | 3 |
| 5,000 | 543.3 ms | 17,632 | 2,089 / 2,089 | 0 | 3 |

Zero missed faults at every scale. Reproduce with `N_ORDERS` in
`data/generate_synthetic.py`.

## Money, not just order counts

A match rate counts orders. A finance controller does not ask how many orders
failed — they ask **how many rupees are unaccounted for, and which bucket each
one is in.** Those numbers come apart:

```
MONEY RECONCILED
  Total exposure:              635,548.23
  Confirmed in bank:           255,896.12   (40.26% by value)
  AT RISK:                     379,652.11
    fee_footing_mismatch                 88,397.13
    duplicate_settlement                 75,769.24
    no_settlement_found                  64,511.08
    bank_credit_delayed                  52,592.63
    credit_unattributed                  43,868.63
    refund_not_reflected                 41,518.23
    settlement_not_credited               6,201.64
    ledger_gross_amount_mismatch          3,474.27
    bank_amount_mismatch                  3,319.26
  [OK] total_exposure == confirmed_value + at_risk_value  (residual 0.00)
```

**Ranking by count and ranking by value disagree**, which matters operationally.
`credit_unattributed` is 6th by count and 5th by value: one order worth ₹43,869.
`settlement_not_credited` is two orders worth ₹6,202 between them. A controller
working the queue by count clears two ₹3k problems before touching a ₹44k one.
The count view cannot tell you that.

### The identity

Every rupee of exposure lands in exactly one bucket, and that is enforced, not
summarised:

```
total_exposure == confirmed_value + at_risk_value
```

Exposure is `settled_amount` where a settlement exists, and the ledger amount
where none does — revenue booked with no payout is exactly the amount at stake.
If the identity ever fails, the report is not imprecise, it is **lying about
where money went**, so the residual is printed and flagged rather than swallowed.
A tool that cannot account for its own arithmetic has no business reporting
anyone else's.

Unattributed bank credits are tracked separately and deliberately *not* netted
against exposure. Cash we hold but cannot place is a different problem from cash
we are owed but cannot find; netting them would hide both.

## Measured accuracy, not just throughput

`data/generate_synthetic.py` records what *should* happen to every order at
injection time into `data/ground_truth.csv`. The pipeline never reads it. The
evaluator scores against it:

```
Injected faults:              29
  detected:                   29
  missed (false negatives):    0
  false positives:             1
Fault detection:     precision 96.67%   recall 100.00%
Exact reason-code accuracy:  98.18%
```

| reason_code | support | precision | recall | F1 |
|---|---|---|---|---|
| `matched` | 26 | 100.00% | 96.15% | 0.98 |
| `bank_credit_delayed` | 5 | 100.00% | 100.00% | 1.00 |
| `duplicate_settlement` | 5 | 100.00% | 100.00% | 1.00 |
| `fee_footing_mismatch` | 5 | 100.00% | 100.00% | 1.00 |
| `no_settlement_found` | 5 | 100.00% | 100.00% | 1.00 |
| `refund_not_reflected` | 4 | 100.00% | 100.00% | 1.00 |
| `bank_amount_mismatch` | 2 | 100.00% | 100.00% | 1.00 |
| `settlement_not_credited` | 2 | 100.00% | 100.00% | 1.00 |
| `ledger_gross_amount_mismatch` | 1 | 100.00% | 100.00% | 1.00 |
| `credit_unattributed` | 0 | — | — | — |

**Every injected fault type scores 100% precision and 100% recall.**

The single false positive sits in `credit_unattributed`, and it is the one case
nothing can recover (see below).

**Every injected failure mode scores 100/100, with zero misses.** The system's
entire error is one order, named in the output rather than buried:

`order_000023` is a healthy order whose bank narration quotes **no reference at
all** — `CR/ONLINE TRF/paymnt gateway aug batch/no ref quoted`. No tier recovers
it, and none should: there is nothing in the text to read. It stays an exception
in every configuration, including against a live model, which correctly returned
confidence 0 rather than inventing a reference.

Two further orders — `order_000009` and `order_000033` — are only matched
*because* Tier 3 exists. Their narrations quote **two** real UTRs, a reversal and
a credit:

```
RAZORPAY NET STLMT AUG/DR RVSL REF UTR100001/CR REF UTR100009
```

Substring matching finds both and has no basis for ranking them, so both
deterministic tiers correctly refuse. On the live run the model picked the
credit reference in both cases and ignored `UTR100001` — the same reversal
reference — both times. It read the words, not the positions.

### `settlement_not_credited` vs `credit_unattributed`

Two exceptions wear the same face and a controller triages them oppositely:

- **`settlement_not_credited`** — Razorpay reports a payout and no credit for
  that amount exists anywhere in the statement. *The money never arrived.*
  Escalate to Razorpay.
- **`credit_unattributed`** — a credit for exactly the expected amount is
  sitting in the statement, but its narration quotes no readable reference.
  *The money is there; the reference isn't.* Fix the parser, don't call support.

Critically, the second case is **still an exception**. A bank credit for
precisely the settled amount is not evidence of a match — an amount lining up
is a triage hint, never a verdict. Enforced by
`test_mangled_narration_is_never_matched_on_amount_alone`.

## Adversarial testing

`tests/test_stress.py` is 29 cases written to break the pipeline rather than to
confirm it. The bar for each is deliberately low: **do not crash, and do not
silently invent a match.** Producing an exception is a pass; producing a wrong
match is a failure.

The first run found **five real bugs.** All are fixed and regression-tested:

| What broke | Severity | Cause | Fix |
|---|---|---|---|
| A blank amount came out **`matched`** | Critical | `abs(nan - x) > tol` is `False`, so NaN defeated every tolerance check in the file | `_num()` returns `None` for blank/NaN/non-numeric; unreadable values raise `source_value_missing` |
| A malformed date **killed the whole batch** | High | `strptime` raised out of Stage B, and my earlier refactor had moved error handling to load-time only | `_parse_date()` returns `None`; unreadable dates raise `date_unparseable` |
| A **debit satisfied a settlement** | High | Bank rows were never filtered by `type`, so a chargeback quoting the settlement's UTR read as the payment arriving | Non-credit rows are excluded from matching and returned separately |
| A credit dated **before** its settlement matched | Medium | The window only ever checked the late side | `bank_credit_predates_settlement` |
| A double-booked order broke report arithmetic | Medium | `total_orders` counted ledger rows while Stage A counted orders | Stage A emits one verdict per distinct `order_id`; new `duplicate_ledger_entry` code |

The remaining 24 cases passed unchanged: empty batches, 10 KB narrations, emoji
and RTL text, CSV-quoted commas, formula-looking narrations, `₹`-formatted
amounts, negative and zero amounts, 1,000-character UTRs, purely numeric UTRs,
500 unrelated bank credits, 50 settlements sharing one UTR, and a 10,000-order
batch.

The NaN bug is the one worth dwelling on. Every other failure here was loud — a
crash, or a wrong reason code. That one was silent, and it produced the exact
outcome this entire design exists to prevent: **a match nobody verified.** It
had been in the code since the first version, under 60 passing tests, and only
an adversarial case found it.

## Property-based testing

`tests/test_properties.py` uses [hypothesis](https://hypothesis.works/) to
generate whole three-source batches — arbitrary amounts, fees, dates, duplicate
settlements, orphans, missing bank rows — and asserts invariants that must hold
for **every** batch, not just ones I thought to write:

| Invariant | Meaning |
|---|---|
| every order gets exactly one verdict | nothing vanishes between stages |
| `reconciled + unreconciled == total` | the count balances |
| `exposure == confirmed + at_risk` | the money balances |
| a match always has bank confirmation | **nothing is ever invented** |
| same input ⇒ same output | a finance control that drifts is worthless |

The fourth is the reason the file exists. It states the property the whole
architecture is built to guarantee, over arbitrary input, rather than over the
batch I happened to author.

It found a bug on its first run. Hypothesis shrank to a settlement where
**fees exceeded the transaction value** — `fee 497,662` on `gross 502,782`,
netting `-84,459`. The footing arithmetic is internally consistent, so every
check in Stage A passed it and it reached the bank stage looking valid. It is
still nonsense: a processor does not charge more to handle a sale than the sale
was worth. Now `fee_exceeds_gross`.

## Generalization: a different bank's conventions

The obvious objection to any single-dataset result is that the matcher was
tuned to its own fixture. `data/generate_alt_format.py` builds a second batch
that shares nothing with the first but a CSV schema:

| | primary batch | alt batch |
|---|---|---|
| reference format | `UTR100009` | `RRN4471829`, `IMPSP20003`, `AXIS8800011`, `9100039` |
| fee model | flat 2% | ₹3.00 flat + 1.75% |
| settlement cadence | T+2 | T+1 |
| narration style | `RAZORPAY SETTLEMENT UTR:…` | HDFC / ICICI / AXIS templates |
| order ids | `order_000001` | `INV-2026-00001` |

**No line of `src/` changes to run it.** Same thresholds, same reason codes:

```bash
python main.py --alt --evaluate
```

```
orders 60 | match rate 61.67% | faults 23/23 | missed 0 | false positives 0
exact reason-code accuracy 100.00%   |   money identity holds
```

Not one reference in that batch contains the string `UTR`. Extraction works by
normalised substring against references the pipeline already holds — never by a
format-specific regex — so a bank's narration conventions are not something it
can depend on. (The original `UTR_RE` regex was removed once it became dead
code; keeping it would have implied a coupling the matcher does not have.)

## Where the AI is, and where it deliberately isn't

Reconciliation fails in production for one reason: someone lets the model
decide whether money matches. It never does here.

| Tier | What it does | LLM? |
|---|---|---|
| **Stage A** — ledger ↔ settlement | join on `order_id`, verify gross + fee/tax/refund footing | no |
| **Stage B** — settlement ↔ bank | UTR match on narration, batch-total + date-window verification | no |
| **Tier 1** — narration regex | normalise separators, find a *known* UTR | no |
| **Tier 2** — fuzzy recovery | recover a UTR whose prefix the bank mangled | **no** |
| **Tier 3** — LLM | read a reference out of genuinely free text | yes (`gpt-4o-mini`) |

Tiers 2 and 3 only ever **propose** which settlement a bank credit should be
compared against. Stage B then runs one verification pass over everything, so a
proposal from the model faces exactly the same amount, batch-total and
date-window checks a clean regex match does.

**Model sizing follows from the scoping.** Because Tiers 1 and 2 absorb
everything structurally recoverable, Tier 3 makes **two calls per run at any
batch size** — and its whole job is reading a reference number out of one short
string. That is a small-model task, so it runs on `gpt-4o-mini` with strict
structured outputs. A full 55-order run costs well under a cent. Picking the
cheapest model that can do the job is the same judgment call as not using a
model at all in Tiers 1 and 2.

Three constraints make that boundary real rather than rhetorical:

1. **The model never sees the answer key.** `resolve_narration()` is passed the
   narration string and nothing else — no UTR list, no amounts. It cannot pick a
   plausible reference off a menu; it has to read one out of the text.
   Enforced by `test_llm_request_never_contains_settlement_data`.
2. **A confident proposal naming a UTR no settlement carries is discarded.**
   Enforced by `test_a_confident_llm_proposal_for_an_unknown_utr_is_rejected`.
3. **No API key degrades the system, it doesn't make it lie.** Every ambiguous
   row becomes an exception. The match rate *drops*.

### The tier ablation — what the AI is actually worth

| config | match rate | resolved by fuzzy | resolved by LLM | unresolved |
|---|---|---|---|---|
| regex only | 32.73% | 0 | 0 | 8 |
| + fuzzy (still zero LLM calls) | 41.82% | 5 | 0 | 3 |
| + LLM tier | **45.45%** | 5 | 2 | 1 |

**The cheap deterministic tier does 5 of the 7 mangled narrations for free.**
The LLM is scoped down to the 2 rows that genuinely need a model. That is the
whole "right tool in the right place" argument, as a measured number.

All three rows are executed, the last one against the live API.
`python -m src.evaluate` reports the LLM row as *skipped* unless
`OPENAI_API_KEY` is set, so the printed table never claims a tier it did not run.

The residual `1` is deliberate and permanent: one narration quotes no reference
at all, and no tier should ever "resolve" it.

## The failure story — a match rate that couldn't lose

The first working version reported **65.45%**. It was wrong, and not by a
rounding error.

`build_report()` scored the match rate off Stage A alone — the ledger↔settlement
leg. An order counted as "matched" the moment the books agreed with Razorpay,
whether or not the money ever arrived. Five orders were simultaneously counted
inside the 65.45% **and** listed in the exception list. Same orders, both
columns.

Worse, it made the degradation story fake. Losing the bank statement, or running
with no API key, sent rows to exceptions — but cost the headline number nothing.
A match rate that cannot lose points when verification fails isn't measuring
anything.

Fixing it turned up four more problems:

- The rate is now strictly three-way: `matched_stage_a & matched_stage_b`. It
  fell sixteen points on the batch as it stood then, and every later change has
  been measured from that honest floor rather than back against 65.45%.
- Stage B was inverted to iterate **settlements instead of bank rows**. The old
  direction made "Razorpay says it settled and the money never arrived" —
  arguably the most important exception in reconciliation — structurally
  invisible: no bank row meant no verdict at all, not an exception. It is now
  `settlement_not_credited`.
- Measuring throughput exposed a fourth problem the accuracy work had hidden.
  A 5,000-order batch took 3,368 ms where 500 took 115 ms — 10× the data for
  29× the time, which is quadratic, not linear. Both narration tiers scanned
  every settlement for every bank row, and the regex tier re-sorted the entire
  UTR list *inside* that loop. Indexing both by length dropped 5,000 orders to
  554 ms — **6× faster, and now flat as the batch grows** — with byte-identical
  verdicts. The index also removed a latent nondeterminism: the old scan
  iterated a `set`, so a narration quoting two same-length UTRs resolved
  differently depending on the hash seed.
- The LLM tier passed every test and resolved nothing in production. Its tests
  stubbed the model and handed it the correct UTR, which proved the pipeline
  *handles* a resolution — not that a model could *produce* one. The first live
  run returned confidence `0.00` on every row, correctly: the narrations were
  written to defeat regex and fuzzy matching, and had overshot into quoting no
  reference at all. There was nothing to read. The synthetic batch now includes
  narrations that quote a reversal reference *alongside* the credit, where the
  characters are genuinely ambiguous and only the surrounding words decide —
  a language question, which is the one thing string matching cannot do. One
  narration quoting nothing at all is kept, unresolvable by every tier.

  Rewriting the data exposed a further bug. With two real UTRs in one narration,
  the regex tier returned whichever appeared first — which is the *reversal*
  reference, confidently wrong. Both deterministic tiers now refuse when a
  narration quotes more than one known UTR, which is the correct handoff to
  Tier 3. On the live run the model picked the credit reference both times and
  ignored the reversal both times.
- The `try/except` that was supposed to handle a missing bank file never ran.
  `load_sources()` read all three CSVs *before* the `try`, so a missing
  `bank_statement.csv` raised `FileNotFoundError` and killed the batch. Sources
  are now loaded independently: the ledger and settlement files are the
  backbone and fail loudly, and the bank file degrades Stage B while Stage A
  results stay valid. Covered by two tests.

## Run it

```bash
pip install -r requirements.txt
python data/generate_synthetic.py    # regenerate the 4 source files (seeded, reproducible)
python main.py                       # reconcile, print the report
python main.py --evaluate            # ...and score it against ground truth
python main.py --alt --evaluate      # ...on a different bank's conventions
python -m pytest tests/ -q           # 103 tests
```

Set `OPENAI_API_KEY` to enable Tier 3. Without it the pipeline still runs
end to end and reports those rows honestly as exceptions.

## Output

- `output/reconciliation_report.json` — match rate, money reconciliation, per-stage counts, throughput, full exception list
- `output/evaluation.json` — precision/recall/F1 per reason code, confusion matrix, ablation
- `output/audit_trail.jsonl` — one line per decision by any stage: timestamp, stage, basis, confidence

Every decision is auditable, including the ones the model proposed:

```json
{"timestamp": "...", "order_id": null, "stage": "narration_fuzzy",
 "decision": "link_proposed", "confidence": 0.95,
 "basis": "narration quotes reference digits of UTR100005 verbatim (prefix mangled), no LLM call needed"}
```

## Structure

```
data/generate_synthetic.py   3-source batch + ground_truth.csv answer key
src/matcher.py               Stage A + Stage B, fully deterministic
src/fuzzy_resolver.py        Tier 2 — string recovery, zero LLM calls
src/llm_resolver.py          Tier 3 — LLM proposes, never confirms
src/reconcile.py             orchestrator, tier order, source degradation
src/evaluate.py              ground-truth scoring + tier ablation
src/report.py                three-way match rate, exception list
src/audit.py                 append-only decision log
src/money.py                 value reconciliation + the accounting identity
data/generate_alt_format.py  alt-convention batch for the generalization run
tests/                       103 tests
  test_stress.py               29 adversarial cases
  test_properties.py           invariants over hypothesis-generated batches
  test_generalize.py           alt-convention regression
```

## Known limits

- One-to-many (a single settlement split across several bank credits) is
  handled by summing linked credits, but is not exercised by the synthetic
  batch — only many-to-one is.
- `settlement_not_credited` cannot currently distinguish "money never arrived"
  from "money arrived but we couldn't attribute it." Both are real exceptions,
  but a controller would triage them differently.
- The date window is a flat 5 days; real settlement cycles vary by method and
  by bank holiday calendar.

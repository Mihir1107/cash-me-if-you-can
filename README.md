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
MATCH RATE: 49.09%   (27/55 orders confirmed on BOTH legs)
UNRECONCILED: 28     every one carries a reason code

duplicate_settlement       5     no_settlement_found        5
fee_footing_mismatch       5     bank_credit_delayed        5
refund_not_reflected       4     settlement_not_credited    2
credit_unattributed        2
```

**That number is deliberately not 95%.** The generator injects real failure
modes on purpose, so the exception list means something instead of being a
rounding error on clean data. The question a judge should ask isn't "is 49%
good" — it's "is 49% *correct*." Which is why the next section exists.

## Throughput

```
Wall clock: 7.5 ms  (13,369 records/sec over 100 records)
LLM calls made: 0   (0.0 per 100 orders)
```

Per-stage timings land in `output/reconciliation_report.json` under
`throughput.stage_ms`. `llm_calls` counts **actual API round-trips**, not rows
offered to the tier — with no key set the tier short-circuits and this stays 0.

The 55-order batch is the committed default because the brief asks for 50+.
It is not the ceiling:

| batch | wall clock | records/sec | faults caught | missed | false positives |
|---|---|---|---|---|---|
| 55 | 7.5 ms | 13,369 | 26 / 26 | 0 | 2 |
| 500 | 72.4 ms | 13,158 | 211 / 211 | 0 | 2 |
| 5,000 | 554.5 ms | 17,272 | 2,086 / 2,086 | 0 | 2 |

Zero missed faults at every scale. Reproduce with `N_ORDERS` in
`data/generate_synthetic.py`.

## Measured accuracy, not just throughput

`data/generate_synthetic.py` records what *should* happen to every order at
injection time into `data/ground_truth.csv`. The pipeline never reads it. The
evaluator scores against it:

```
Injected faults:              26
  detected:                   26
  missed (false negatives):    0
  false positives:             2
Fault detection:     precision 92.86%   recall 100.00%
Exact reason-code accuracy:  96.36%
```

| reason_code | support | precision | recall | F1 |
|---|---|---|---|---|
| `matched` | 29 | 100.00% | 93.10% | 0.96 |
| `bank_credit_delayed` | 5 | 100.00% | 100.00% | 1.00 |
| `duplicate_settlement` | 5 | 100.00% | 100.00% | 1.00 |
| `fee_footing_mismatch` | 5 | 100.00% | 100.00% | 1.00 |
| `no_settlement_found` | 5 | 100.00% | 100.00% | 1.00 |
| `refund_not_reflected` | 4 | 100.00% | 100.00% | 1.00 |
| `settlement_not_credited` | 2 | 100.00% | 100.00% | 1.00 |
| `credit_unattributed` | 0 | — | — | — |

**Every injected failure mode scores 100/100.** The system's entire error is
localised to one code, and both false positives are named in the output rather
than buried: `order_000009` and `order_000033` are healthy orders whose bank
narration quotes no reference at all. Without an `ANTHROPIC_API_KEY` the
pipeline cannot attribute their credits, so it reports them as
`credit_unattributed`. That is the honest cost of running the deterministic
tiers alone — and it is exactly what the LLM tier is for.

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

## Where the AI is, and where it deliberately isn't

Reconciliation fails in production for one reason: someone lets the model
decide whether money matches. It never does here.

| Tier | What it does | LLM? |
|---|---|---|
| **Stage A** — ledger ↔ settlement | join on `order_id`, verify gross + fee/tax/refund footing | no |
| **Stage B** — settlement ↔ bank | UTR match on narration, batch-total + date-window verification | no |
| **Tier 1** — narration regex | normalise separators, find a *known* UTR | no |
| **Tier 2** — fuzzy recovery | recover a UTR whose prefix the bank mangled | **no** |
| **Tier 3** — LLM | read a reference out of genuinely free text | yes |

Tiers 2 and 3 only ever **propose** which settlement a bank credit should be
compared against. Stage B then runs one verification pass over everything, so a
proposal from the model faces exactly the same amount, batch-total and
date-window checks a clean regex match does.

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
| regex only | 40.00% | 0 | 0 | 7 |
| + fuzzy (still zero LLM calls) | **49.09%** | 5 | 0 | 2 |
| + LLM tier | 52.73% ¹ | 5 | 2 | 0 |

**The cheap deterministic tier does 5 of the 7 mangled narrations for free.**
The LLM is scoped down to the 2 rows that genuinely need a model. That is the
whole "right tool in the right place" argument, as a measured number.

¹ measured with a stubbed model in `test_llm_tier_recovers_the_free_text_narrations`.
The live API call is not exercised in this environment (no key available), so
the row is reported as skipped by `python -m src.evaluate` unless you set one.

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

Three things came out of fixing it:

- The rate is now strictly three-way: `matched_stage_a & matched_stage_b`. It
  fell to **49.09%**, and that is the honest number.
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
python -m pytest tests/ -q           # 55 tests
```

Set `ANTHROPIC_API_KEY` to enable Tier 3. Without it the pipeline still runs
end to end and reports those rows honestly as exceptions.

## Output

- `output/reconciliation_report.json` — match rate, per-stage counts, throughput, full exception list
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
tests/                       55 tests
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

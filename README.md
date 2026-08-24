# Multi-Source Reconciliation Agent

**Razorpay AI Buildathon, Track 04 (AI Finance Controller)**

Closes one finance-ops loop across a 55-record batch: a merchant's internal
ledger against Razorpay settlements against the actual bank statement. Reports a
match rate, the money at risk, and a reason-coded exception list.

```bash
pip install -r requirements.txt
python main.py --evaluate        # reconcile, then score against ground truth
python main.py --alt --evaluate  # same code, a different bank's conventions
python -m pytest tests/ -q       # 103 tests
```

Set `OPENAI_API_KEY` to enable the LLM tier. Without it the pipeline still runs
end to end and reports the affected rows honestly as exceptions.

## Results

| | no API key | with API key |
|---|---|---|
| Match rate (3-way confirmed) | 41.82% | **45.45%** |
| Injected faults detected | 29 / 29 | 29 / 29 |
| Missed faults | **0** | **0** |
| False positives | 3 | 1 |
| Reason-code accuracy | 94.55% | 98.18% |

The match rate is deliberately not 95%. The generator injects real failure modes
on purpose, so the exception list means something. The number that measures the
*agent* is 29/29 with zero misses; the match rate measures the *data*.

Verified against `data/ground_truth.csv`, which the generator writes at
injection time and the pipeline never reads. Every injected fault type, all
eight of them, scores 100% precision and 100% recall. The single false positive
is a healthy order whose bank narration quotes no reference at all, so no tier
can attribute it, and none should.

## Money, not just order counts

A controller does not ask how many orders failed. They ask how many rupees are
unaccounted for, and where. Figures below are the no-key run; total exposure is
the same either way, only the confirmed split moves.

```
Total exposure:        635,548.23
Confirmed in bank:     255,896.12   (40.26% by value)
At risk:               379,652.11
  fee_footing_mismatch          88,397.13
  duplicate_settlement          75,769.24
  no_settlement_found           64,511.08
  bank_credit_delayed           52,592.63
  credit_unattributed           43,868.63
  ... 4 more codes
[OK] total_exposure == confirmed_value + at_risk_value  (residual 0.00)
```

Ranking by count and ranking by value disagree. One `credit_unattributed` order
is worth ₹43,869; two `settlement_not_credited` orders are worth ₹6,202 between
them. Work the queue by count and you clear two small problems before touching a
large one.

The identity is enforced, not summarised. Exposure is `settled_amount` where a
settlement exists, and the ledger amount where none does, since booked revenue
with no payout is exactly the amount at stake. A non-zero residual means the
report is lying about where money went, so it prints as a failure.

## Where the AI is, and where it is not

Reconciliation fails in production when someone lets a model decide whether
money matches. It never does here.

| Stage | Job | LLM |
|---|---|---|
| A: ledger to settlement | join on `order_id`, verify gross and fee/tax/refund footing | no |
| B: settlement to bank | reference match, batch totals, date window | no |
| Tier 1: narration regex | normalise separators, find a known reference | no |
| Tier 2: fuzzy recovery | recover a reference the bank mangled | **no** |
| Tier 3: LLM (`gpt-4o-mini`) | read a reference out of genuinely free text | yes |

Tiers 2 and 3 only *propose* which settlement a credit should be compared
against. Stage B then verifies amount, batch total and date window itself, so a
model proposal faces the same checks a clean regex match does.

Three constraints make that boundary real:

1. **The model never sees the answer key.** It gets the narration string and
   nothing else: no reference list, no amounts. It cannot pick a plausible
   answer off a menu, it has to read one out of the text.
2. **A confident proposal naming a reference no settlement carries is
   discarded**, however certain the model is.
3. **No API key degrades the system, it does not make it lie.** The match rate
   drops by 3.63 points.

### What each tier is worth

| Configuration | Match rate | Resolved by fuzzy | Resolved by LLM |
|---|---|---|---|
| regex only | 32.73% | 0 | 0 |
| + fuzzy (zero LLM calls) | 41.82% | 5 | 0 |
| + LLM tier | **45.45%** | 5 | 2 |

The free deterministic tier does 5 of 7 mangled narrations. The model is scoped
to the 2 that genuinely need language: narrations quoting *two* real references,
a reversal and a credit, where only the surrounding words decide which is which.
On the live run it picked the credit reference both times and ignored the same
reversal reference both times.

Sizing follows from scoping. Tier 3 makes two calls per run at any batch size,
and its job is reading a reference out of one short string, so it runs on the
cheapest model that supports strict structured outputs. A full run costs well
under a cent.

## Throughput

```
11.2 ms for 102 records (9,091 records/sec), 0 LLM calls
```

| Batch | Wall clock | Records/sec | Faults caught | Missed |
|---|---|---|---|---|
| 55 | 11.2 ms | 9,091 | 29 / 29 | 0 |
| 500 | 52.1 ms | 18,320 | 214 / 214 | 0 |
| 5,000 | 543.3 ms | 17,632 | 2,089 / 2,089 | 0 |

With Tier 3 on, the same batch takes 5,394 ms, of which 5,368 ms is three API
calls. The entire deterministic pipeline is the remaining 26 ms. Every narration
absorbed by Tiers 1 and 2 is roughly 2,000x cheaper in latency, not just in
money.

## Proof it is not tuned to its own fixture

`data/generate_alt_format.py` builds a second batch sharing nothing with the
first but a CSV schema: `RRN`/`IMPS`/`AXIS` references with no `UTR` prefix
anywhere and some purely numeric, a flat-plus-percentage fee model, T+1 cadence
instead of T+2, four unfamiliar narration templates, different order ids.

No line of `src/` changes to run it:

```
60 orders | match 61.67% | faults 23/23 | missed 0 | false positives 0
exact reason-code accuracy 100.00% | money identity holds
```

## How it is tested

103 tests, three kinds:

- **`test_stress.py`**, 29 adversarial cases written to break the pipeline. The
  bar for each: do not crash, do not silently invent a match.
- **`test_properties.py`**, invariants asserted over hypothesis-generated
  batches. The load-bearing one: *a match always has bank confirmation*, stated
  over arbitrary input rather than over the batch I happened to write.
- **`test_generalize.py`**, the alt-convention run above, as a regression.

## What broke

The first version reported **65.45%**. It was wrong, and the fixes cost points.

1. **The match rate double-counted.** It scored Stage A alone, so five orders
   were counted as matched *and* listed as exceptions. Strictly three-way now,
   and the number fell.
2. **"Razorpay settled, money never arrived" was invisible.** Stage B iterated
   bank rows, so a settlement with no bank row produced no verdict at all rather
   than an exception. It now iterates settlements.
3. **A blank amount came out `matched`.** `abs(nan - x) > tol` is `False`, so
   NaN defeated every tolerance check in the file. Silent, present since the
   first version, under 60 passing tests, and it produced exactly what this
   design exists to prevent: a match nobody verified. Found by an adversarial
   case, not by a green suite.
4. **A test asserted a capability the model does not have.** The LLM tier was
   stubbed with the correct reference looked up from the settlement table, the
   answer key it is deliberately never shown. It passed. The first live call
   resolved zero, correctly, because the narration quoted no reference at all.
   The code was right; the test and the data were wrong.

Also fixed from the same passes: a malformed date killed the batch, a debit
satisfied a settlement, a credit dated before its settlement matched, a
double-booked order broke the report arithmetic, and a settlement whose fees
exceeded the sale value passed every check because its footing was internally
consistent.

## Layout

```
src/matcher.py               Stage A + Stage B, fully deterministic
src/fuzzy_resolver.py        Tier 2, string recovery, zero LLM calls
src/llm_resolver.py          Tier 3, the model proposes, never confirms
src/money.py                 value reconciliation + the accounting identity
src/reconcile.py             orchestrator, tier order, source degradation
src/evaluate.py              ground-truth scoring + tier ablation
src/report.py                three-way match rate, exception list
src/audit.py                 append-only decision log, one run_id per run
data/generate_synthetic.py   primary batch + ground_truth.csv answer key
data/generate_alt_format.py  alt-convention batch
tests/                       103 tests
```

Outputs land in `output/`: the report, the evaluation, and `audit_trail.jsonl`,
one append-only line per decision with timestamp, stage, basis and confidence.

## Limits

- Fault density is roughly 50% of orders, far above any real merchant. Nine
  reason codes cannot be exercised across 55 orders at realistic rates. The
  match rate is a property of that choice; the 29/29 detection rate is not.
- `credit_unattributed` cannot distinguish "money never arrived" from "money
  arrived and we could not attribute it" beyond an amount heuristic.
- The settlement window is a flat 5 days. Real cadences vary by method and by
  bank holiday calendar.

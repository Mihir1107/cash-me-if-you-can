# Multi-Source Reconciliation Agent

**Razorpay AI Buildathon, Track 04 (AI Finance Controller)**

Reconciles a merchant's internal ledger against Razorpay settlements against the
actual bank statement, across 57 orders. Reports a match rate, the money at
risk, a routed work queue, and whether the period can be closed.

```bash
pip install -r requirements.txt
python main.py --evaluate        # reconcile, then score against ground truth
python main.py --prove           # 30 seconds: watch a wrong proposal get refused
python main.py --alt --evaluate  # same code, a different bank's conventions
python -m pytest tests/ -q       # 175 tests
```

## The thing worth reading first

Reconciliation fails in production when a confident guess becomes a match. So
the interesting question is not whether this agent gets things right, it is what
happens when a component of it is **wrong**.

`python main.py --prove` corrupts a bank narration one character toward the
wrong settlement, live, and shows what follows:

```
Two settlements outstanding:
   order_1   UTR100005   expects       976.40
   order_2   UTR100006   expects     4,882.00

A credit arrives for 4,882.00. That is order_2's money.
Its narration has lost a character:  "NEFT RZPY UTR10005 CR"

TIER 2 proposes:  UTR100005 at 94% confidence
                  ...which belongs to order_1.   THE PROPOSAL IS WRONG.

STAGE B verifies the credit against the settlement it was pointed at:
   settlement expects         976.40
   bank credited            4,882.00
   -> REFUSED

MATCHES CREATED BY THE WRONG PROPOSAL: 0
   order_1: exception  bank_amount_mismatch
   order_2: exception  settlement_not_credited
```

The proposal was well formed, highly confident, and named a settlement that
genuinely exists. It still could not become a match, because **a proposal only
chooses what a credit is compared against. Stage B does the comparing, and
confidence buys nothing there.** The same holds for a model proposal at 100%
confidence, and at every confidence level in between
(`tests/test_wrong_proposals.py`).

## What broke

The first version reported **65.45%**. Every fix since has cost points.

**A blank amount came out `matched`.** `abs(nan - x) > tol` is `False` in
Python, so a NaN defeated every tolerance check in the file. It had been there
since the first version, under 60 passing tests, and it produced exactly what
this design exists to prevent: a match nobody verified. A blank cell in a bank
export is the most common defect in real finance data. Found by an adversarial
case, not by a green suite.

**A settlement for an order the merchant never booked vanished entirely.** Stage
A iterated the ledger, so an order Razorpay settled and the ledger never
recorded got no verdict at all. A batch containing one read as 100% reconciled
with zero exceptions while real money sat unexplained. Silent, and flattering.

**A test asserted a capability the model does not have.** The LLM tier was
stubbed with the correct reference looked up from the settlement table, the
answer key it is deliberately never shown. It passed. The first live call
resolved zero, correctly, because that narration quoted no reference at all.

Also: the match rate double-counted five orders as matched *and* excepted; a
chargeback reversal was invisible; a malformed date killed the batch; a debit
satisfied a settlement; a credit dated before its settlement matched; and a
settlement whose fees exceeded the sale passed every check because its footing
was internally consistent.

Three of those came from one cause: **logic that should have been shared having
a second copy that drifted.** Each fix was a deletion, not a patch.

## Results

| | no API key | with API key |
|---|---|---|
| Match rate (3-way confirmed) | 38.60% | **42.11%** |
| Injected faults detected | **32 / 32** | **32 / 32** |
| Missed faults | **0** | **0** |
| False positives | 3 | 1 |
| Reason-code accuracy | 94.74% | **98.25%** |

Recall is **100%, 95% CI [89.3%, 100%]**. That interval is the honest reading of
a perfect score on 32 injected faults: consistent with a true detection rate
above about 89%, not proof of 100%.

**The match rate carries no interval, deliberately.** Every order in the batch
was checked, so it is a census. A confidence band would imply sampling error
that does not exist. Recall and precision estimate behaviour on faults the agent
has not seen, so they get one.

Scored against `data/ground_truth.csv`, which the generator writes at injection
time and the pipeline never reads. All ten injected fault types score 100%
precision and 100% recall. The single false positive is a healthy order whose
narration quotes no reference at all, so no tier can attribute it, and none
should.

## Can the period be closed?

That is the decision reconciliation exists to support, so the agent answers it:

```
PERIOD CLOSE: BLOCKED
4 of 7 conditions fail. 504,718.32 is unresolved.

1. revenue_recorded              28,876.92
   2 settlements were received for orders the ledger never booked,
   so revenue for the period is understated.
2. reversals_booked              13,381.50
3. cash_attributable             43,868.63
4. material_exceptions_resolved 418,591.27

Passing: audit_trail_intact, sources_verifiable, books_balance
```

Two things deliberately do not block. **Immaterial exceptions**, because that is
what materiality means. **Late credits that arrived**, because a payment outside
the normal window is a timing observation, not a misstatement.

## An audit trail that can prove it was not edited

Every decision is logged with a timestamp, basis and confidence. Each entry also
carries the SHA-256 of the entry before it, so the file is a hash chain:

```
edited one word        -> BROKEN at line 13: contents no longer hash to their digest
exception -> matched   -> BROKEN at line 1
deleted a line         -> BROKEN at line 21: an entry was deleted or reordered
reordered two lines    -> BROKEN at line 9
```

This is the difference between claiming the exception list is honest and being
able to show nobody adjusted it between the run and the review. A broken chain
blocks the period close outright, because a figure whose derivation cannot be
verified is not evidence.

## Money, not just order counts

A controller does not ask how many orders failed. They ask how many rupees are
unaccounted for, and where.

```
Total exposure:        664,425.15
Confirmed in bank:     264,713.45   (39.84% by value)
At risk:               399,711.70
  fee_footing_mismatch          88,397.13
  duplicate_settlement          75,769.24
  no_settlement_found           64,511.08
  ... 8 more codes
[OK] total_exposure == confirmed_value + at_risk_value  (residual 0.00)
```

The identity is enforced, not summarised. Exposure is `settled_amount` where a
settlement exists and the ledger amount where none does, since booked revenue
with no payout is exactly the amount at stake. A non-zero residual means the
report is lying about where money went, so it prints as a failure.

## Triage: who fixes what

Thirty-three exception rows is a list, not a plan.

```
33 exception rows cluster into 11 incidents, 10 above the materiality
threshold of 3,322.13 (0.5% of total exposure, floored at 1,000.00)

owner                incidents  orders   value at risk
razorpay_support             5      19      238,198.35
merchant_finance             4       8       95,539.22
bank_ops                     1       5       52,592.63
chargeback_ops               1       1       13,381.50
```

Clustering finds patterns rather than manufacturing them. Two payouts short by
exactly 250.00 are one deduction applied twice, so they become a single incident
compared against the threshold once. Split per order they would have been two
pieces of small change, below the threshold twice, and the pattern invisible.

Consequence outranks size: unrecorded revenue ranks above a larger delayed
credit. **No model is involved.** Routing is a lookup, clustering is a
signature, priority is arithmetic on money already computed.

## Where the AI is, and where it is not

| Stage | Job | LLM |
|---|---|---|
| A: ledger to settlement | join on `order_id`, verify gross and fee/tax/refund footing | no |
| B: settlement to bank | reference match, batch totals, date window | no |
| Tier 1: narration regex | normalise separators, find a known reference | no |
| Tier 2: fuzzy recovery | recover a reference the bank mangled | **no** |
| Tier 3: LLM (`gpt-4o-mini`) | read a reference out of genuinely free text | yes |
| Triage and close gate | cluster, route, decide | **no** |

Three constraints make the boundary real:

1. **The model never sees the answer key.** It gets the narration string and
   nothing else: no reference list, no amounts. It cannot pick a plausible
   answer off a menu, it has to read one out of the text.
2. **A confident proposal naming a reference no settlement carries is
   discarded**, however certain the model is.
3. **No API key degrades the system, it does not make it lie.** The match rate
   drops 3.51 points.

### The one constructive use

`python main.py --brief` has the model write a plain-English brief for each top
incident, so a merchant's finance lead reads two sentences instead of parsing a
reason code. It is asked to **phrase** facts, never to establish them: every
figure was decided by deterministic code before the model saw it.

The same discipline applies. `verify_brief()` extracts every number from the
draft and checks each against the facts the model was given. One invented figure
and the brief is discarded in favour of the deterministic action line:

```
"...2 orders totalling 28,876.92, about 3.4% of monthly revenue."
   -> REJECTED, invented: [3.4]
```

Rounding is rejected too, because in a finance document a rounded figure is a
different figure.

### What each tier is worth

| Configuration | Match rate | Resolved by fuzzy | Resolved by LLM |
|---|---|---|---|
| regex only | 29.82% | 0 | 0 |
| + fuzzy (zero LLM calls) | 38.60% | 5 | 0 |
| + LLM tier | **42.11%** | 5 | 2 |

The free deterministic tier does 5 of 7 mangled narrations. The model is scoped
to the 2 that genuinely need language: narrations quoting *two* real references,
a reversal and a credit, where only the surrounding words decide which is which.
On the live run it picked the credit reference both times and ignored the same
reversal reference both times.

A default run makes **two model calls at any batch size**.

## Not tuned to its own fixture

`data/generate_alt_format.py` builds a second batch sharing nothing with the
first but a CSV schema: `RRN`/`IMPS`/`AXIS` references with no `UTR` prefix
anywhere and some purely numeric, a flat-plus-percentage fee model, T+1 cadence,
unfamiliar narration templates.

No line of `src/` changes to run it:

```
60 orders | match 61.67% | faults 23/23 | missed 0 | false positives 0
exact reason-code accuracy 100.00%   |   money identity holds
```

## Throughput

```
7.7 ms for 107 records (13,878 records/sec), 0 LLM calls
```

| Orders | Wall clock | Records/sec | Faults caught | Missed |
|---|---|---|---|---|
| 57 | 6.3 ms | 17,038 | 32 / 32 | **0** |
| 502 | 51.3 ms | 18,713 | 217 / 217 | **0** |
| 5,000 | 544.9 ms | 17,585 | 2,090 / 2,090 | **0** |

Best of three. With Tier 3 on, the same batch takes 8,124 ms, of which 8,101 ms
is three API calls; the entire deterministic pipeline is the remaining 23 ms.

## How it is tested

175 tests at 98% line coverage:

- **`test_wrong_proposals.py`** upstream tiers being confidently wrong
- **`test_stress.py`** 29 adversarial cases written to break the pipeline
- **`test_properties.py`** invariants over hypothesis-generated batches, the
  load-bearing one being *a match always has bank confirmation*
- **`test_generalize.py`** the alt-convention run, as a regression
- **`test_brief.py`** the fabrication guard
- **`test_output.py`** the printed summary, including degraded shapes

## Layout

```
src/matcher.py               Stage A + Stage B, fully deterministic
src/fuzzy_resolver.py        Tier 2, string recovery, zero LLM calls
src/llm_resolver.py          Tier 3, the model proposes, never confirms
src/money.py                 value reconciliation + the accounting identity
src/triage.py                incident clustering, routing, materiality
src/close_gate.py            the period close decision
src/brief.py                 model phrases, deterministic code verifies
src/audit.py                 append-only, hash-chained decision log
src/prove.py                 the live boundary demonstration
src/evaluate.py              ground-truth scoring, Wilson intervals, ablation
src/reconcile.py             orchestrator, tier order, source degradation
src/report.py                three-way match rate, exception list
data/generate_synthetic.py   primary batch + ground_truth.csv answer key
data/generate_alt_format.py  alt-convention batch
tests/                       175 tests, 98% line coverage
```

A committed sample run is in [`docs/`](docs/), generated by
`docs/capture_sample_run.py` rather than pasted.

## Limits

- Fault density is roughly half the orders, far above any real merchant. Ten
  reason codes cannot be exercised across 57 orders at realistic rates. The
  match rate is a property of that choice; the 32/32 detection rate is not.
- The hash chain is tamper-evident, not tamper-proof. Anyone who can rewrite the
  file can recompute the chain. It proves the log was not quietly adjusted
  between the run and the review, which is what an auditor asks.
- `credit_unattributed` cannot distinguish "money never arrived" from "money
  arrived and we could not attribute it" beyond an amount heuristic.

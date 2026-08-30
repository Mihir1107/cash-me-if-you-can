# Multi-Source Reconciliation Agent

**Razorpay AI Buildathon, Track 04 (AI Finance Controller)**

Reconciles a merchant's ledger against Razorpay settlements against the bank
statement, across 57 orders. Reports a match rate, the money at risk, a routed
work queue, and whether the period can be closed.

```bash
pip install -r requirements.txt
python main.py --evaluate        # reconcile, then score against ground truth
python main.py --prove           # 30 seconds: watch a wrong proposal get refused
python main.py --alt --evaluate  # same code, a different bank's conventions
python -m pytest tests/ -q       # 175 tests
```

## Read this part first

Reconciliation fails in production when a confident guess becomes a match. So
the question worth asking is not whether this agent gets things right. It is
what happens when a component of it is **wrong**.

`python main.py --prove` corrupts a narration one character toward the wrong
settlement, live, and shows what follows:

```
order_1  UTR100005  expects     976.40
order_2  UTR100006  expects   4,882.00

A credit arrives for 4,882.00. That is order_2's money.
Its narration lost a character:  "NEFT RZPY UTR10005 CR"

TIER 2 proposes:  UTR100005 at 94% confidence   <- WRONG, that is order_1
STAGE B:          expects 976.40, credited 4,882.00   -> REFUSED

MATCHES CREATED BY THE WRONG PROPOSAL: 0
   order_1: exception  bank_amount_mismatch
   order_2: exception  settlement_not_credited
```

The proposal was well formed, highly confident, and named a real settlement. It
still could not become a match, because **a proposal only chooses what a credit
is compared against. Stage B does the comparing, and confidence buys nothing
there.** Same for a model proposal at 100% confidence, and every level between
(`tests/test_wrong_proposals.py`).

## What broke

The first version reported **65.45%**. Every fix since has cost points.

**A confidently wrong proposal could still become a match, if the amounts
collided.** Stage B verifies amount and date, so it catches a wrong proposal
only when the settlement it names expects a *different* amount. Two settlements
expecting the same amount on the same day defeated it completely: the credit
passed every check against whichever one it was pointed at, and the other was
reported as never credited. A confident, fully verified, wrong match, sitting
directly under the claim this project leads with. Found by an external review,
not by my 183 tests. Proposals from the fuzzy and LLM tiers are now refused when
more than one uncredited settlement could claim the credit (`attribution_
ambiguous`); a reference read straight out of the narration is evidence rather
than a guess and still survives a collision.

**A blank amount came out `matched`.** `abs(nan - x) > tol` is `False` in
Python, so NaN defeated every tolerance check in the file. Present since the
first version, under 60 passing tests, producing exactly what this design exists
to prevent: a match nobody verified. A blank cell is the most common defect in
real bank exports. Found by an adversarial case, not a green suite.

**A settlement for an unbooked order vanished entirely.** Stage A iterated the
ledger, so an order Razorpay settled and the ledger never recorded got no
verdict. A batch containing one read as 100% reconciled, zero exceptions, while
real money sat unexplained. Silent, and flattering.

**A test asserted a capability the model does not have.** The LLM tier was
stubbed with the answer looked up from the settlement table, the key it is
deliberately never shown. It passed. The first live call resolved zero,
correctly, because that narration quoted no reference at all.

That pattern repeated. Every feature here that touches the model has shipped a
bug only a real API call could find: the brief guard checked numbers and passed
`$13381.5` for an amount in rupees, because the digits were right. Stubs test
the code you wrote against the behaviour you imagined.

Also fixed: the match rate double-counted five orders; a chargeback reversal was
invisible; a malformed date killed the batch; a debit satisfied a settlement; a
credit dated before its settlement matched; a settlement whose fees exceeded the
sale passed every check. Three came from one cause, **shared logic with a second
copy that drifted**, and each fix was a deletion rather than a patch.

## Results

| | no API key | with API key |
|---|---|---|
| Match rate (3-way confirmed) | 38.60% | **42.11%** |
| Injected faults detected | **32 / 32** | **32 / 32** |
| Missed faults | **0** | **0** |
| False positives | 3 | 1 |
| Reason-code accuracy | 94.74% | **98.25%** |

Recall is 100%, **95% CI [89.3%, 100%]**: the honest reading of a perfect score
on 32 faults is a true rate above about 89%, not proof of 100%.

**The match rate carries no interval, deliberately.** Every order was checked,
so it is a census; a confidence band would imply sampling error that is not
there. Recall and precision estimate behaviour on unseen faults, so they get one.

Scored against `data/ground_truth.csv`, written by the generator at injection
time and never read by the pipeline. All ten injected fault types score 100%
precision and 100% recall. The one false positive is a healthy order whose
narration quotes no reference at all, which no tier should resolve.

## Can the period be closed?

The decision reconciliation exists to support, so the agent answers it:

```
PERIOD CLOSE: BLOCKED
4 of 7 conditions fail. 504,718.32 is unresolved.

1. revenue_recorded               28,876.92   2 settlements received for orders
                                              the ledger never booked
2. reversals_booked               13,381.50
3. cash_attributable              43,868.63
4. material_exceptions_resolved  418,591.27

Passing: audit_trail_intact, sources_verifiable, books_balance
```

Immaterial exceptions and late-but-arrived credits deliberately do not block.

## An audit trail that proves it was not edited

Every decision is logged with timestamp, basis and confidence, and each entry
carries the SHA-256 of the one before it:

```
edited one word        -> BROKEN at line 13: contents no longer hash to their digest
exception -> matched   -> BROKEN at line 1
deleted a line         -> BROKEN at line 21: an entry was deleted or reordered
```

A broken chain blocks the close outright, because a figure whose derivation
cannot be verified is not evidence.

## Money, not order counts

```
Total exposure:        664,425.15
Confirmed in bank:     264,713.45   (39.84% by value)
At risk:               399,711.70
  fee_footing_mismatch          88,397.13
  duplicate_settlement          75,769.24
  no_settlement_found           64,511.08     ... 8 more codes
[OK] total_exposure == confirmed_value + at_risk_value  (residual 0.00)
```

The identity is enforced, not summarised. A non-zero residual means the report
is lying about where money went, so it prints as a failure.

Exposure is `settled_amount` where a settlement exists and the ledger amount
where none does. That basis deliberately changes, and it is worth naming: it
mixes cash risk (a payout that did not arrive) with receivable risk (revenue
booked that has not settled). A controller tracks those separately. This is an
operational figure for ranking exceptions, not a cash-flow or accounting
exposure.

## Triage: who fixes what

```
33 exception rows cluster into 11 incidents, 10 above the materiality
threshold of 3,322.13 (0.5% of exposure, floored at 1,000.00)

owner                incidents  orders   value at risk
razorpay_support             5      19      238,198.35
merchant_finance             4       8       95,539.22
bank_ops                     1       5       52,592.63
chargeback_ops               1       1       13,381.50
```

Clustering finds patterns rather than manufacturing them: two payouts short by
exactly 250.00 are one deduction applied twice, so they become one incident
compared against the threshold once. Split per order they would have been small
change, below threshold twice, and the pattern invisible.

**The threshold is an operational triage heuristic, not audit materiality.**
Materiality under SA 320 / ISA 320 is a judgement set against an entity
benchmark (profit before tax, revenue, total assets) and carries qualitative
overrides, so a small item can still be material by its nature. This is 0.5% of
the run's own exposure, floored, chosen because it produces a sensible queue at
this batch size. A real deployment would replace it with a threshold agreed with
the controller.

Consequence outranks size. **No model is involved**: routing is a lookup,
clustering is a signature, priority is arithmetic on money already computed.

## Where the AI is, and where it is not

| Stage | Job | LLM |
|---|---|---|
| A: ledger to settlement | join on `order_id`, verify fee/tax/refund footing | no |
| B: settlement to bank | reference match, settlement totals, working-day window | no |
| Tier 1: narration regex | find a known reference | no |
| Tier 2: fuzzy recovery | recover a reference the bank mangled | **no** |
| Tier 3: LLM (`gpt-4o-mini`) | read a reference out of free text | yes |
| Triage, close gate | cluster, route, decide | **no** |

1. **The model never sees the answer key.** It gets the narration and nothing
   else: no reference list, no amounts. It cannot pick a plausible answer off a
   menu, it has to read one out of the text.
2. **A confident proposal naming a reference no settlement carries is
   discarded**, however certain the model is.
3. **No API key degrades the system, it does not make it lie.** The match rate
   drops 3.51 points.

### The one constructive use

`python main.py --brief` has the model write a plain-English brief per incident,
so a finance lead reads two sentences instead of a reason code. It **phrases**
facts, never establishes them. Then `verify_brief()` extracts every number from
the draft and checks each against the facts the model was given:

```
"...2 orders totalling 28,876.92, about 3.4% of monthly revenue."
   -> REJECTED, invented: [3.4]
"...1 order putting $13381.5 at risk."
   -> REJECTED, invented: ['$']
```

Rounding is rejected too: in a finance document a rounded figure is a different
figure. So is a currency. The first live run of this feature had the model write
`$13381.5` for an amount in rupees, and the number guard passed it because the
digits were right. A brief wrong by an exchange rate is worse than a dull one,
so the facts state amounts as bare numbers and a brief that names a currency is
discarded.

### What each tier is worth

| Configuration | Match rate | Fuzzy | LLM |
|---|---|---|---|
| regex only | 29.82% | 0 | 0 |
| + fuzzy (zero LLM calls) | 38.60% | 5 | 0 |
| + LLM tier | **42.11%** | 5 | 2 |

The free tier does 5 of 7 mangled narrations. The model gets the 2 needing
language: narrations quoting *two* real references, a reversal and a credit,
where only the surrounding words decide which is which. Live, it picked the
credit reference both times and ignored the same reversal reference both times.

A default run makes **two model calls at any batch size**.

## Not tuned to its own fixture

`data/generate_alt_format.py` builds a second batch sharing nothing with the
first but a CSV schema: `RRN`/`IMPS`/`AXIS` references with no `UTR` prefix and
some purely numeric, a flat-plus-percentage fee model, T+1 cadence, unfamiliar
narrations. No line of `src/` changes:

```
60 orders | match 61.67% | faults 23/23 | missed 0 | false positives 0
exact reason-code accuracy 100.00%   |   money identity holds
```

## Throughput

| Orders | Wall clock | Records/sec | Faults caught | Missed |
|---|---|---|---|---|
| 57 | 6.3 ms | 17,038 | 32 / 32 | **0** |
| 502 | 51.3 ms | 18,713 | 217 / 217 | **0** |
| 5,000 | 544.9 ms | 17,585 | 2,090 / 2,090 | **0** |

Best of three. With Tier 3 on the batch takes 8,124 ms, of which 8,101 ms is
three API calls; the deterministic pipeline is the remaining 23 ms.

## Tests

175 tests, 98% line coverage.

| File | What it holds |
|---|---|
| `test_wrong_proposals.py` | upstream tiers being confidently wrong |
| `test_stress.py` | 29 adversarial cases written to break the pipeline |
| `test_properties.py` | invariants over hypothesis-generated batches |
| `test_generalize.py` | the alt-convention run, as a regression |
| `test_brief.py` | the fabrication guard |
| `test_output.py` | the printed summary, including degraded shapes |

The load-bearing property: *a match always has bank confirmation*, asserted over
arbitrary generated input rather than the batch I happened to write.

## Layout

```
src/matcher.py           Stage A + Stage B, fully deterministic
src/fuzzy_resolver.py    Tier 2, string recovery, zero LLM calls
src/llm_resolver.py      Tier 3, the model proposes, never confirms
src/money.py             value reconciliation + the accounting identity
src/triage.py            incident clustering, routing, materiality
src/close_gate.py        the period close decision
src/brief.py             model phrases, deterministic code verifies
src/audit.py             append-only, hash-chained decision log
src/prove.py             the live boundary demonstration
src/evaluate.py          ground-truth scoring, Wilson intervals, ablation
src/reconcile.py         orchestrator, tier order, source degradation
src/report.py            three-way match rate, exception list
data/                    both batches + the ground-truth answer key
tests/                   175 tests
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
  arrived and we could not attribute it" beyond an amount heuristic. Amount
  equality is weak evidence of identity; neither state becomes a match, but the
  two exceptions can be labelled the wrong way round.
- The settlement window counts **bank working days** (excluding Sundays and the
  second and fourth Saturday, per Razorpay's documented T+2 working-day cycle)
  but does **not** model public holidays, so a credit delayed only by a holiday
  can still be flagged.
- The settlement arithmetic is `gross − fee − tax − refund`. Real Razorpay
  settlements also carry adjustments, transfers and disputes. This models a
  simplified gateway settlement, not Razorpay's full accounting.

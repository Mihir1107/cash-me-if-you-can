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
python -m pytest tests/ -q       # 314 tests
python app.py                    # the app: drop in three CSVs, get a close decision
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

**The answer key was wrong at scale, and the matcher took the blame.** The two
orders that exist only on Razorpay's side are pinned at order numbers 901 and
902, clear of the 57-order fixture. Raise the batch past them — 2,000 orders for
the realistic month, 5,000 for the throughput table — and those orders are in
the ledger, so "settle an order the merchant never booked" injects a *second*
settlement for an order that was booked. The pipeline read that correctly as
`duplicate_settlement`; the answer key still claimed `no_ledger_entry`, and
scored the pipeline wrong for being right. It also wrote the same `order_id`
twice, which `dict(zip(...))` silently resolved to whichever row came last. Both
halves are now caught: the unbooked numbers move clear of whatever the book is,
and an answer key naming one order twice raises instead of scoring. Fixing it
moved the realistic month's close gate from 2 failing conditions to 3 — the
missing revenue was real and the fixture had been hiding it.

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
4 of 7 conditions fail. 460,320.66 is unresolved.

1. revenue_recorded               28,876.92   2 settlements received for orders
                                              the ledger never booked
2. reversals_booked               13,381.50
3. cash_attributable              21,669.80
4. material_exceptions_resolved  396,392.44

Passing: audit_trail_intact, sources_verifiable, books_balance
```

Sub-threshold exceptions and late-but-arrived credits deliberately do not block.
Nor do qualitative overrides exist here: a real close can be held open by a small
item whose *nature* matters, and this gate does not model that.

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
33 exception rows cluster into 11 incidents, 10 above the triage
threshold of 3,322.13 (0.5% of this run's exposure, floored at 1,000.00.
Operational triage heuristic, not audit materiality under SA 320 / ISA 320.)

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
| Schema tier (`gpt-4o-mini`) | read a column header, then get verified against the file | opt-in |
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

A default run makes **three model calls, two of which resolve a reference**. The
count is the same at 57, 502 and 5,002 orders — but that is a property of this
fixture, not a guarantee: the generator injects exactly three narrations the
deterministic tiers cannot recover (two quoting two references, one quoting
none). Everything that *does* scale, the fuzzy tier absorbs for free — 5
recoveries at 57 orders, 42 at 502, 417 at 5,002, still zero API calls. On real
data, Tier 3 volume would track how many narrations are genuinely unrecoverable,
not how many orders there are.

## Not tuned to its own fixture

`data/generate_alt_format.py` builds a second batch sharing nothing with the
first but a CSV schema: `RRN`/`IMPS`/`AXIS` references with no `UTR` prefix and
some purely numeric, a flat-plus-percentage fee model, T+1 cadence, unfamiliar
narrations. No line of `src/` changes:

```
60 orders | match 61.67% | faults 23/23 | missed 0 | false positives 0
exact reason-code accuracy 100.00%   |   money identity holds
```

## The app

A controller does not have Python. So the pipeline has a front end:

```bash
python app.py        # then open http://127.0.0.1:5051
```

Drag in three CSVs — your ledger, the Razorpay settlement report, the bank
statement — and get back the close decision, the money at risk by reason, and a
work queue routed to whoever has to fix each thing. Download the report and the
audit trail from the same screen. There is a **Try it with sample data** button
if you just want to see it work.

**Your columns do not have to be named ours.** Nobody's export says `ledger_id`.
A Tally ledger says *Voucher No* and *Party Name*, a settlement report says
*Settled Amount* and *UTR Number*, an HDFC statement says *Particulars* and
*Withdrawal Amt.*. `src/schema.py` maps them: names are normalised to lowercase
alphanumerics and looked up in a curated alias table, so *Order ID*, *order_id*
and *OrderID* are one thing. A statement that splits money across *Withdrawal*
and *Deposit* columns is a different **shape**, not a different spelling — those
are folded into one amount with the direction kept, because direction is how a
chargeback is told apart from a payout.

**What it cannot place, it asks about.** There is no fuzzy matching in the
accepted path, and that is deliberate: point `settled_amount` at the gross column
and every downstream check still passes — the footing foots, the identity
balances, and every figure in the report is wrong with nothing anywhere to show
for it. That is the confidently-wrong failure this whole architecture exists to
prevent, and it is not worth saving two clicks. So unmatched columns go to a
mapping screen with a ranked suggestion pre-selected, which a person confirms.
The guess is offered; it is never applied.

### Tier 3, for schemas

The alias table only knows the headers I thought to write down, and there are
thousands of accounting packages. Reading *"Remitted To Bank"* and knowing it
means the net payout **is** a language problem — so it gets the same treatment
the mangled-narration problem gets, with the same boundary.

The model proposes; `verify_mapping()` then checks that proposal against your
actual file. Do the amount columns hold numbers, do the dates parse, do the
identifiers identify, and — the strong one — **does the settlement foot against
its own parts**. A proposal that fails is discarded whole, because a mapping is a
single claim about what a file means: if the model confused gross and settled,
nothing it said about that file has earned any trust.

```
gross ↔ settled swapped    ->  0% of rows foot            REFUSED
fee ↔ gross swapped        ->  median fee is 5000% of gross   REFUSED
settlement_date ← utr      ->  0% parse as a date         REFUSED
fee ↔ tax swapped          ->  tax is larger than the fee REFUSED
```

A settlement report is the easy case: it has an arithmetic identity to test
against. The ledger and the bank statement do **not**, and single-file checks are
correspondingly weak there — `narration` pointed at a branch-name column and
`order_id` swapped with `ledger_id` pass every check a single file can make.

**So the three files also get checked against each other**, because they are
three views of the same money and that is itself a testable claim:

```
ledger and settlements share order ids   ->  a swapped join key gives 0%
narrations quote references the          ->  a wrong text column gives 0%
  settlements carry
```

Plus two guards for mis-maps that look perfectly plausible in isolation:

```
amount -> Balance      values are 73x their own step size    REFUSED
                       (a real amount column is ~2x — a running
                        balance is large and moves in small steps)
status -> Party Name   100% of values are distinct           REFUSED
                       (a status repeats itself; a name does not)
```

Thresholds sit low on purpose. They exist to catch a column aimed at the wrong
thing, not to grade anyone's bookkeeping — a genuinely messy month clears them,
a mis-mapped join key does not.

Back to the settlements case, because it hides the subtlest failure of the lot:
the footing identity **cannot see** a fee/tax swap —
`settled = gross − fee − tax` is symmetric in fee and tax, so every row still
foots perfectly. What separates them is domain, not arithmetic: GST is charged
*on* the commission, so tax is a fraction of fee and always the smaller. Stated
as the assumption it is; a gateway that taxed differently would trip this
honestly and go to a human.

Measured on headers the alias table places **0 of 10** of (`Payout Batch Ref`,
`Captured Value`, `Remitted To Bank`, `Levy On Charge`…), six live runs:

```
3 accepted — every column correct
3 refused  — fell back to the mapping screen
0 accepted but wrong
```

Non-deterministic in **coverage**, never in **correctness**. The same file can
need the mapping screen on one run and not the next, which is a real wart; what
it cannot do is quietly get a column wrong.

**It is off by default**, and it is the one thing on that page that leaves your
machine. Only **column headers** are sent — no rows, no amounts, no customer
names, no narrations — because a header is the one part of a finance export that
carries no financial data. The verification runs locally. With no key, or the box
unticked, the alias table and the mapping screen carry it exactly as before.

Two things about it are load-bearing:

**It computes nothing.** Every route saves the uploads to a temp directory and
calls the same `run_reconciliation()` that `main.py` calls. There is no second
implementation of the matching logic in JavaScript and there must never be one —
three bugs here came from shared logic having a copy that drifted, and the copy
that must never drift is the one deciding whether money matched.

**Nothing leaves the machine.** It binds to localhost, the uploads are deleted
when the request finishes, and the only outbound call the process can make is
Tier 3's narration lookup — one bank narration string, no amounts, and only if a
key is configured. There is a checkbox to turn it off. A merchant's ledger is
not something to be casual about.

Errors are written for the person holding the file, not the person who wrote the
parser: a near-miss export comes back as *"your ledger is missing `amount`,
re-export with that column"*, naming which of the three files and highlighting
it, rather than a stack trace.

The scorecard is deliberately **absent** on your own data. There is no answer key
for a real merchant's month — if they knew which rows were wrong they would not
need this — and a blank scorecard reads as "checked, found nothing". Run a
sample and it fills in, because the generator recorded what it broke.

### A captured run, for people who won't install anything

```bash
python data/make_realistic.py      # a 2,000-order month at ~3% faults
python docs/build_dashboard.py     # -> docs/close-desk.html, self-contained
```

Same design, same render code, no server — a single HTML file with three runs
baked in, for a README link or a judge who is not going to clone a repo. It
carries three runs of the same code and switches between them:

| | orders | fault density | match rate | incidents | close gate |
|---|---|---|---|---|---|
| Real month | 2,002 | ~3% | 97.20% | 11, of which 1 material | 3 of 7 fail |
| Test fixture | 57 | ~56% | 42.11% | 11, of which 10 material | 4 of 7 fail |
| At volume | 5,002 | ~56% | 58.08% | 15, of which 5 material | 3 of 7 fail |

That switch is the honest answer to "your match rate is only 42%". At a density
a real merchant would recognise, 2,002 orders becomes eleven incidents and one
thing worth doing today — and the batch at volume is the only one where the
ambiguity filter actually refuses anything, so the mechanism this project is
built around is visible rather than asserted.

## Throughput

| Orders | Wall clock | Records/sec | Faults caught | Missed | False positives |
|---|---|---|---|---|---|
| 57 | 10.4 ms | 10,288 | 32 / 32 | **0** | 3 |
| 502 | 78.7 ms | 12,201 | 217 / 217 | **0** | 3 |
| 5,002 | 1,420.1 ms | 6,750 | 2,092 / 2,092 | **0** | 7 |

Best of three, no API key, regenerated by `python docs/benchmark.py`. The
generator is seeded, so every column but the timings reproduces exactly.

With Tier 3 on, the same batch takes seconds rather than milliseconds: 4,394.9 ms
in the committed sample run, of which 4,364.9 ms is three API calls and 25 ms is
the deterministic pipeline. The deterministic part is the stable number. API
latency is not — it moved between 3.8 and 5.3 seconds across runs of the same
three calls, which is the honest reason cost and latency here scale with
unrecoverable narrations rather than with orders.

**Detection holds at every scale. Precision drifts, and the reason is the
ambiguity filter.** At 5,002 orders, 4 of those 7 false positives are
`attribution_ambiguous` on healthy orders: the more settlements are outstanding,
the more often two of them expect the same amount, and the filter refuses rather
than guesses.

Refusing on amount alone was too blunt: it cost 20 healthy orders at 5,002, and
16 of those had only one settlement the credit could actually have come from. So
a rival has to be a *real* rival — the credit must be date-feasible against it
too, which is the same window Stage B enforces anyway, asked earlier. A payout
made three weeks ago was never competing for today's credit. That is narrowing
*who is competing*, never picking between competitors: two settlements that are
both feasible are still refused, at any confidence. 20 false refusals became 4.

It fires zero times at 57 and 502 orders. That is the cost of the
guarantee, and it is a real cost — it turns clean orders into manual work as the
book grows. At this fault density it stays small, 4 orders in 5,002, but it is
the number to watch on a book with more outstanding settlements than this one.

## Tests

314 tests, 96% line coverage of `src/`.

| File | What it holds |
|---|---|
| `test_wrong_proposals.py` | upstream tiers being confidently wrong, including the equal-amount collision and its control case |
| `test_stress.py` | 31 adversarial cases written to break the pipeline |
| `test_properties.py` | invariants over hypothesis-generated batches |
| `test_generalize.py` | the alt-convention run, as a regression |
| `test_brief.py` | the fabrication guard, numbers and currency |
| `test_output.py` | the printed summary, including degraded shapes |
| `test_audit.py` | the hash chain under tampering, attacked directly rather than sideways — including two attacks it does **not** catch |
| `test_close_gate.py` | each of the seven conditions, in both the blocking and the passing state, and what must deliberately not block |
| `test_realistic_density.py` | the same code at ~3% fault density, not the fixture's ~56% |
| `test_webapp.py` | the app's own edges: real-world headers, uploads not outliving the request, a holiday calendar not leaking into the next run |
| `test_schema.py` | column resolution, and what it refuses to place |
| `test_schema_llm.py` | a confidently wrong schema proposal, caught by verifying it against the file |
| `test_evaluate.py` | the scorer itself, including an answer key that names one order twice |
| `test_matcher.py`, `test_reconcile.py`, `test_resolvers.py`, `test_triage.py` | the stage, orchestration, tier and routing logic underneath all of it |

The load-bearing property: *a match always has bank confirmation*, asserted over
arbitrary generated input rather than the batch I happened to write.

Coverage is close to even: `close_gate` 100%, `fuzzy_resolver` 100%, `audit`
99%, `matcher` 98%. The thinnest are `schema_llm` at 81% and `reconcile` at 91%,
and what is uncovered in them is the live API-call path, the `except
ImportError` fallback for the openai import, and source-degradation branches —
not logic that decides a verdict. The two files carrying the project's honesty
claims, `audit` and `close_gate`, were the weakest here until they got test
modules of their own.

## Layout

```
src/matcher.py           Stage A + Stage B, fully deterministic
src/fuzzy_resolver.py    Tier 2, string recovery, zero LLM calls
src/llm_resolver.py      Tier 3, the model proposes, never confirms
src/money.py             value reconciliation + the accounting identity
src/triage.py            incident clustering, routing, triage threshold
src/close_gate.py        the period close decision
src/brief.py             model phrases, deterministic code verifies
src/audit.py             append-only, hash-chained decision log
src/prove.py             the live boundary demonstration
src/evaluate.py          ground-truth scoring, Wilson intervals, ablation
src/reconcile.py         orchestrator, tier order, source degradation
src/report.py            three-way match rate, exception list
src/schema.py            their column names onto ours, by table not by guess
src/schema_llm.py        tier 3 for schemas: propose a mapping, then verify it
data/                    both batches + the ground-truth answer key
app.py                   the local web app: three CSVs in, a close decision out
webapp/static/desk.css   the close desk design system, one copy
webapp/static/desk.js    the render logic, one copy, shared by app and artifact
webapp/static/upload.js  the intake screen: files in, report to Desk.render
docs/build_dashboard.py  bakes a captured run into one self-contained HTML file
data/make_realistic.py   a 2,000-order month at a density a merchant would know
tests/                   314 tests
```

A committed sample run is in [`docs/`](docs/), generated by
`docs/capture_sample_run.py` rather than pasted, and the throughput table above
by `docs/benchmark.py`. Both write the numbers this README quotes, so a figure
here that disagrees with the code is a script away from being caught.

`docs/audit_trail_sample.jsonl` is an excerpt of one run's trail, sampled for a
spread of decision shapes. It deliberately does not verify as a hash chain —
non-consecutive entries are exactly what `verify_chain()` rejects. The full
trail it came from verifies, which is what `audit_trail_intact` reports in the
close gate above.

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
  two exceptions can be labelled the wrong way round. This is the only source of
  false positives on the 57-order batch, at both key states.
- **The ambiguity filter's cost grows with the book.** It never creates a false
  match, but it refuses healthy attributions at a rate that rises with how many
  settlements are outstanding — zero at 57 and 502 orders, 4 at 5,002 — and
  nothing currently bounds that rate. See Throughput.
- **One column mis-map still gets through**, and it is date-shaped rather than
  arithmetic: `date` pointed at a second date column (`Posting Date` instead of
  `Value Date`). Both parse, both are dates, and nothing inside the file
  separates them — only knowing that bank's semantics would. It misclassifies
  the settlement window rather than any total.
- `attribution_ambiguous` clusters `per_order`, so every collision becomes its
  own incident for `merchant_finance` rather than one incident naming the credit
  that two settlements both claim. Right owner, right action, more queue items
  than the underlying problem deserves.
- The settlement window counts **bank working days** (excluding Sundays and the
  second and fourth Saturday, per Razorpay's documented T+2 working-day cycle).
  Public holidays are supported but **not populated**: `matcher.BANK_HOLIDAYS` is
  empty and `load_bank_holidays()` reads a `date,name` CSV, so the calendar is
  the deployer's to supply from the RBI notification for the period. Shipping a
  half-remembered holiday list would be worse than shipping none — a wrong date
  silently changes whether a real payout reads as late, where a missing one is at
  least this paragraph. Until one is supplied, a credit delayed only by a holiday
  can still be flagged.
- The settlement arithmetic is `gross − fee − tax − refund`. Real Razorpay
  settlements also carry adjustments, transfers and disputes. This models a
  simplified gateway settlement, not Razorpay's full accounting.

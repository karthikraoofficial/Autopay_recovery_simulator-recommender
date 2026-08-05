# rebound — user guide

## What this is, in one paragraph

`rebound` simulates a book of Indian recurring-payment mandates, makes some of the debits
fail for realistic reasons, and then measures how much money different retry strategies
would have recovered. You give it six facts about a merchant. It gives you two rupee
figures with confidence intervals, one chart, and the full list of every assumption it
used to get there.

**It is not a measurement of any merchant.** No real payment data has ever gone into it.
148 of its 160 numbers are marked `estimate`, meaning "our guess, no source read". The
guide below is as much about what the output *doesn't* mean as what it does.

---

## Starting it

Two processes. From the repo root:

```bash
# terminal 1 — the simulation API
.venv/Scripts/python.exe -m uvicorn rebound.api.app:app --port 8000

# terminal 2 — the dashboard
cd web && npm run dev
```

Then open **http://localhost:5173**.

> Use `localhost`, not `127.0.0.1`. Vite binds IPv6 here and `127.0.0.1:5173` is refused.

The assumptions section loads immediately, as does an estimate of what each run would cost.
Nothing is simulated until you press a Run button, and both runs then execute in the
background with a progress bar — a publication run on a real book takes minutes, so it does
not hold the page hostage.

---

## The six inputs

Six fields, and every one of them changes the answer. If a field existed that didn't, it
would be the first thing a sceptical reader found, so there isn't one.

### 1. Book size — how many active mandates

The number of customers on autopay. Type your real figure.

The two run modes treat it differently, and this matters:

- **The publication run simulates your actual book.** 8,000 mandates means 8,000 simulated
  mandates. The rupee figures it produces are genuinely yours.
- **The fast run caps the book at 200 mandates**, because simulating your real book takes
  minutes and the point of the fast run is to move a control and see the answer move. Its
  rupee figures are therefore **per simulated book of 200**, not yours. The result panel
  says so in an amber block whenever the cap has bitten.

The cap is a ceiling, not a replacement — if you have 150 mandates, the fast run simulates
all 150.

**Nothing multiplies a capped result up to your book size**, and this is deliberate rather
than an omission. See "Things this cannot tell you" at the end.

### 2. Average ticket (₹) — the typical monthly charge

A ₹149 music subscription, a ₹499 OTT bundle, a ₹2,999 gym membership, a ₹15,000 SIP.

This matters more than it looks. A small ticket clears a thin bank balance easily, so
insufficient-funds failures are rare and timing barely helps. A large ticket fails
constantly on the same customers, so timing is worth a lot — right up until it exceeds
the mandate cap, at which point you get hard declines that no retry can fix.

### 3–4. UPI Autopay share and eNACH share

Two numbers between 0 and 1. **Card e-mandate takes whatever is left**, shown live under
the field. Enter `0.55` and `0.30` and cards get `0.15`.

The three rails behave genuinely differently and this is the most interesting lever:

| Rail | Behaviour | Retry cap |
|---|---|---|
| **UPI Autopay** | Real-time. Succeeds only if the money is there *at that instant*. Highest failure rate — and the biggest timing opportunity. | 3 |
| **eNACH** | Batch-cleared. More stable, but only presents on weekdays before the bank's cutoff, and **each bounce can carry a bank charge**. | 2 |
| **Card e-mandate** | Bank pre-authorised. Lowest failure rate, least to gain. | 3 |

A UPI-heavy book has more to gain from retry timing than a card-heavy one. A UPI-heavy
book also has more to lose from retrying badly, because there is more to get wrong.

### 5. Failure mix — the most consequential field on the page

This is *why* your debits fail, and it swings the answer more than anything else here.
Three options:

| Setting | What it describes | What actually fails |
|---|---|---|
| **As shipped** | The config's own settings | 42% insufficient funds, 29% technical decline |
| **Insufficient-funds dominant** | Thin balances, reliable banks — a price-sensitive consumer book | **82% insufficient funds** |
| **Technical dominant** | Healthy balances, unreliable rails — an affluent book on flaky infrastructure | **76% technical decline** |

Those percentages are measured from the simulation, not asserted. A preset that claimed
one shape and produced another would be a lie in a dropdown.

**Why it matters:** insufficient funds is a *timing* problem — the money arrives on payday,
so a retry placed well recovers it. A technical decline is the *bank's* problem — no
amount of clever scheduling against payroll helps, because payroll was never the issue.
So retry intelligence is worth roughly seventeen times more on an insufficient-funds book
than on a technical one. Picking the wrong preset here will give you a confidently wrong
number.

**Which do you pick?** Honestly: you probably don't know yet, and that is the point. Your
payment gateway can give you a reason-code breakdown of last quarter's failures. Until
someone pulls it, run all three and look at the range. That range is your real uncertainty.

### 6. Performance fee — the commercial term

Default 15%. Taken off gross recovery to produce the "net to merchant" figure, which is
the only number a merchant actually cares about. Change it to whatever you're proposing to
charge.

---

## Fast run vs publication run

| | Fast (interactive) | Publication |
|---|---|---|
| Seeds | 4 | 12 |
| Book | capped at 200 mandates | **your actual book** |
| Time | ~35 seconds, always | linear in book size — see below |
| Figures are | per simulated book of 200 | yours |
| Use it for | moving a control and watching the answer move | the number you put in front of someone |

Both now run **in the background with a progress bar**, showing steps completed, elapsed
time, and the expected duration. Before you press anything, the cost of both runs is shown
under the input form and re-prices as you type — so you never commit to a run without
knowing what it costs.

Publication runtime, at the measured ~45 ms per mandate per seed:

| Your book | Publication run takes about |
|---|---|
| 400 | 3.5 minutes |
| 2,000 (the default) | 18 minutes |
| 8,000 | 72 minutes |
| 25,000 (the hard cap) | 3.75 hours |

Above 25,000 the app refuses and tells you how long it would have taken. That is a typo
guard, not a modelling limit — someone entering an extra zero should not queue a six-hour
run by accident.

> The expected duration is measured from the development machine and will be wrong on
> yours. Elapsed time is shown next to it precisely so a bad estimate is visible as one.

These are the **same model measured at different precision**, not two models. Fewer seeds
and a smaller book mean wider intervals, and the fast run's intervals are wide enough that
a lift can look real when it isn't — or vanish when it is real.

A worked case, on the technical-dominant preset. A fast-sized run (120 mandates, 4 seeds)
returned a strategy lift of **−₹848 to +₹1,060**. That interval contains zero, so the
correct reading is "this run cannot tell whether the strategy helps at all". A 400-mandate,
12-seed run narrowed the same figure to **₹71 to ₹1,803** — a real but tiny effect. Same
model. The fast run simply could not see it.

**Never quote a fast-run number.**

---

## Reading the result

### The two line items — and why they are never added together

This is the single most important thing on the page.

The dashboard shows **two separate rupee figures** and deliberately provides no total. It
is not an oversight and the two must not be added:

**1. Rescheduling lift** — worth having, but it is *plumbing.*

> When a compliance rule blocks a retry — the eNACH batch cutoff has passed, it's a
> Saturday, the 24-hour notice hasn't elapsed — you can either abandon that retry or move
> it to the next legal slot. Moving it is worth money. It involves no reason codes, no
> prediction, no model. It is a scheduler that doesn't give up.

**2. Strategy lift** — this is the *intelligence.*

> What branching on reason codes, avoiding bank downtime, and timing against inferred
> payroll adds **on top of** a scheduler that already re-presents blocked retries.

Measured over 12 seeds on a 400-mandate book, 12 months, ₹499 ticket. Your own publication
run will simulate your book size instead, so the absolute rupees will differ — it is the
*ratio between the two columns* that carries the lesson:

| Failure mix | Rescheduling | Strategy |
|---|---|---|
| As shipped | ₹6,998 – ₹10,144 | ₹990 – ₹5,903 |
| Insufficient-funds dominant | ₹11,486 – ₹15,375 | ₹2,862 – ₹12,123 |
| Technical dominant | ₹11,982 – ₹14,704 | **₹71 – ₹1,803** |

Look at the ratio. **Rescheduling is several times larger than strategy in every case, and
about 170× larger under a technical-dominant book.** If you summed these into one headline
you would be selling a scheduler fix as an intelligence product. Someone technical will
work that out, and everything else you said becomes suspect.

The honest pitch is: *"Most of this is a scheduling fix you could arguably build yourself.
Here is that number. Here, separately and smaller, is what our retry logic adds."*

### Intervals come before point estimates

Every figure reads `₹2,862 to ₹12,123` first, with the point estimate in smaller text
below. That ordering is deliberate: a point estimate read first anchors you, and these
intervals are wide enough that the anchoring would mislead. The honest summary of that row
is "somewhere between three and twelve thousand", not "seven thousand".

### When an interval contains zero

The dashboard says so in plain words and draws no bar:

> **Not significant — this interval contains zero. On this run the effect is
> indistinguishable from no effect. Do not read it as a gain.**

Take that literally. It does not mean "small positive effect". It means the run cannot
distinguish the strategy from doing nothing. Reporting the point estimate from a
zero-spanning interval is the most common way a simulation result becomes dishonest.

### The precision strip

Seeds, simulated book size, confidence level, seed and output hash — displayed across the
top of the result, not tucked in a corner. The cost of a cheap run should be visible in the
same glance as the number it produced.

If the simulated book is smaller than the one you typed, an amber block says so explicitly
and states that the figures are per simulated book. Take that at face value: those rupees
are not your book's.

The **output hash** is a reproducibility check. Same inputs and same seed always produce
the same hash. If someone re-runs your figures and gets a different hash, the inputs
differed — this is how you prove the number wasn't cherry-picked from a lucky run.

---

## Reading the chart

One line per strategy: the **baseline** (`FixedSchedule` — T+1, T+3, T+7, what most
merchants do today) against **`Blended`**, the best strategy. ₹ recovered per book in each
billing month, gross of fees.

Two things people misread:

- **It is not cumulative.** Month 12 shows what month 12 recovered, not the running total.
- **The gap between the lines is the strategy line item only.** The rescheduling line item
  is a comparison against a scheduler that isn't drawn on this chart. Do not eyeball the
  gap and call it the whole opportunity.

The lines usually drift downward across the year. That is the model working: retrying
annoys people, some of them cancel, and a cancelled mandate never bills again.

---

## The strategy table

Every strategy in the comparison, run against **the identical simulated population** — the
same customers, the same banks, the same seeded bad luck. Differences between rows are
timing, never sampling.

| Column | What it means |
|---|---|
| **Recovery rate** | Share of failed episodes that ended recovered |
| **Net to merchant / book** | Gross recovered less your performance fee |
| **Attempts / episode** | The parity check — read this before recovery rate |
| **Induced revocations** | Customers who cancelled *because* you retried. The cost. |
| **Compliance blocks** | Retries a rule forbade outright. Revenue forgone for compliance. |
| **Rescheduled** | Retries a timing rule moved rather than killed. Revenue collected later. |

**Read "attempts per episode" before "recovery rate."** Two strategies are only comparable
on recovery if they were allowed comparable numbers of shots at it. A strategy that
recovers less while also attempting less has not been shown to time worse — it has been
shown to have been stopped earlier.

The rows you should look at:

- **`NoRetry`** — the floor. Some debits succeed later with no intervention at all. Any
  strategy must beat this or it is doing nothing.
- **`NoReschedule`** — what a merchant without a re-presenting scheduler runs today.
- **`FixedSchedule`** — the baseline. T+1/T+3/T+7 *with* rescheduling.
- **`SalaryAware`** — see the warning below.
- **`Blended`** — the headline strategy.

### A specific warning about `SalaryAware`

It is in the table, and it appears to work. **Do not sell it.**

Phase 7 measured this directly: a customer's payroll date cannot be recovered from payment
history. Every mandate bills on one day of the month, so the history is that same day
repeated, and the estimator simply lands on the mandate's own billing day 93.7% of the
time. Against the true salary day it is right within two days only 14.6% of the time —
**worse than guessing uniformly at random (17.9%)**.

A control strategy that skips the inference entirely and just anchors on the billing day
scores exactly zero lift. So what `SalaryAware` mostly does is reproduce the baseline.

The consequence is a product decision, not a bug: **salary timing needs an external data
source** — account-aggregator consent, a payroll date collected at signup, an issuer
signal. It cannot be mined out of payment telemetry, and more months of history do not
help, because they supply more copies of the same uninformative day.

---

## The segment report — where in the book the money is

The headline tells you how big the opportunity is. The segment report tells you **where**
it sits. It has no dashboard yet; it is an API call and a text report:

After a run completes, the **Take away** section offers *Download segment report* as a
text file. It re-runs the simulation rather than reading a cached result, so it costs about
what the original run did. By script:

```bash
curl "http://localhost:8000/segments?book_size=400&format=text" > segments.txt

# JSON instead, if you want to process it:
curl "http://localhost:8000/segments?book_size=400" > segments.json

# or standalone, with the control's own summary printed last:
python notebooks/phase85_segments.py 400 8
```

It cuts your book four ways, using only things **you can already see in your own data**
without this system:

| Dimension | Segments |
|---|---|
| **Rail** | UPI Autopay / eNACH / card e-mandate |
| **Dominant reason code** | What each mandate mostly fails with |
| **Failure frequency** | 1 failure, 2–3, 4+, and mandates that never failed |
| **Mandate cap band** | Quartiles — **a negative control, see below** |

Each segment reports its size, its recovery rate, the two lift line items kept separate as
always, and **net ₹ per mandate per year**, so a small segment and a large one can be
compared directly.

### It is not a customer list, and will not become one

There is no row-per-customer view and there should never be one. These customers are
synthetic. A per-customer report would read as an operational action list for people who
do not exist, and someone would eventually work it.

### Read the negative control first

One axis — mandate cap band — is **known to contain nothing.** Caps in this model are
generated independently of income, balance and salary day, and the cap almost never
affects the debit, so there is no way for it to influence recovery. It is in the report on
purpose, labelled *expected null*, as a check on the statistics.

**If anything wins on that axis, the report's significance testing is broken and nothing
else in it should be believed.**

This is not theoretical. On a real 400-mandate run, two cap bands cleared zero *before*
correction:

```
Q3  strategy lift [Rs 4, Rs 21]  ReasonAware   -> killed by correction
Q4  strategy lift [Rs 1, Rs 17]  SalaryAware   -> killed by correction
```

Two apparent findings, on an axis containing no signal at all. That is what happens when
you test many segments and read the winners, and it is why the next section exists.

### Only corrected verdicts are published

With 72 tests in a typical report, some segments clear zero by chance. So:

- The **test count and corrected threshold are printed at the top**, not in a footnote.
- Every verdict is corrected across **all** segments and all dimensions at once — because a
  reader scanning the page for a winner is running every test simultaneously.
- **There is no uncorrected column.** If one existed, it is what would end up in a deck.
- Where a segment clears zero raw but fails correction, the report says so in words:
  *"would win uncorrected, does not survive correction — treat it as untested rather than
  as a small win."*
- Segments below 60 episodes report **`insufficient data`**, never a wide interval.

### What a real run found

From the 400-mandate, 8-seed run:

| Segment | Result |
|---|---|
| `TECHNICAL_DECLINE` (34% of episodes) | **Blended wins**, survives correction |
| `BANK_UNAVAILABLE` (16%) | **Blended wins**, survives correction |
| `UPI_AUTOPAY` | **ReasonAware wins** |
| `ENACH` | **BankAware wins** |
| `INSUFFICIENT_FUNDS` (**40% of episodes**) | **No strategy beats the baseline** |

Two things worth taking from that.

**The biggest segment is the one nothing wins in.** Insufficient funds is 40% of episodes
and 40% of the money at risk, and after correction no retry strategy beats the plain
T+1/T+3/T+7 baseline there. The reason is the salary-inference problem described earlier:
the one strategy designed for this segment has nothing reliable to time against. That gap
does not close with better retry logic — it closes with an external data source, or not at
all. Do not let anyone plan a roadmap that attacks this segment with a better scheduler.

**Different rails want different strategies.** UPI favours reason-code branching; eNACH
favours bank-timing awareness. That makes mechanical sense — UPI is real-time so a reason
branch acts immediately, while eNACH is dominated by presentation windows. Whether a
rail-routed strategy actually beats a single global one is untested and is logged in SPEC
§11 as a post-v0.1 question. It is not a safe assumption: routing adds a degree of freedom,
and therefore a way to overfit.

## The trace export — what actually happened, row by row

If you want to know *why* a number came out the way it did, this is the tool. It exports
one seeded book at row level: every attempt, every retry, every verdict the compliance
guard handed down.

### Start here: four rows that explain the whole rescheduling line item

Same synthetic mandate, same billing cycle, two strategies side by side. The opening debit
failed on a Saturday, so T+1 lands on a Sunday — and eNACH does not clear at weekends:

```
strategy       att  retry  scheduled     verdict         from -> to              outcome
FixedSchedule   1   False  2026-11-07    not_reviewed                            FAILURE (BANK_UNAVAILABLE)
FixedSchedule   2   True   2026-11-09    rescheduled     11-08 -> 11-09          SUCCESS
NoReschedule    1   False  2026-11-07    not_reviewed                            FAILURE (BANK_UNAVAILABLE)
NoReschedule   —    True   2026-11-08    blocked         presentation_window     —
```

Identical mandate. Identical failure. The only difference is what happens when a
compliance rule refuses the retry:

- **`FixedSchedule`** moves it to Monday the 9th and **collects ₹499**.
- **`NoReschedule`** abandons the cycle and **collects nothing**.

That is the rescheduling line item, in four rows. No reason codes were consulted, no
prediction was made, no model was involved — a scheduler simply declined to give up when
the calendar got in the way. It is also why the headline is never a single number: the gap
between those two rows is not intelligence, and quoting it as such would be a lie you could
be caught in with this export.

Note the blocked row has **no attempt number**. It never became an attempt; numbering it
would claim a debit was made that never was.

### Getting it

**From the dashboard.** After a run completes, a **Take away** section appears below the
result with two downloads: *Download segment report* and, for the trace, a seed picker plus
*Attempts (.csv)* and *Mandate attributes (.csv)*. Nothing is rendered on the page —
these are files you take away. Filenames carry the seed and the output hash, so a
download can always be traced back to the run that produced it.

Two things to know before you click:

- **Both re-run the simulation.** They are not reading a cached result. A segment report
  costs about what the original run cost — roughly 45 seconds for a 200-mandate fast run,
  and as long as the publication run itself if you started from one. The trace is cheaper:
  one seed rather than all of them.
- **The trace is unavailable above 2,000 mandates.** The endpoint caps there, and a
  smaller trace would be a *different population* from the result above it, not a subset
  of it. The buttons disable themselves and say so rather than handing you a mismatched
  file.

**Or by script**, which is the better route if you want them repeatedly or in a pipeline:

```bash
# the segment report, as the rendered text
curl "http://localhost:8000/segments?book_size=200&format=text" > segments.txt

# JSON, structured
curl "http://localhost:8000/trace?seed=20260801&book_size=200" > trace.json

# CSV, one row per attempt
curl "http://localhost:8000/trace?seed=20260801&format=csv&table=attempts" > attempts.csv

# CSV, the mandate attributes, joined on sim_id
curl "http://localhost:8000/trace?seed=20260801&format=csv&table=mandates" > mandates.csv
```

Two CSVs, one join key. `attempts.csv` is long format — one row per attempt, every column
you need to filter on already present. `mandates.csv` carries the generated attributes
(rail, cap, bank, income band, salary day, billing day) once per mandate instead of
repeating them on every row.

### It is a trace, not a to-do list

Every id is prefixed `SIM-` (`SIM-000005`), and seed plus output hash repeat on **every
row**, so a row pasted into a ticket can always be traced back — and can never be mistaken
for a production identifier. Every file opens with a header block stating in plain words
that these mandates are synthetic and will not exist on any other run.

**There is no "recommended action" column, and there never will be.** A test fails if
anyone adds one. What each strategy *did* is a fact about a run. What you *should* do
generalises across books and belongs elsewhere. Advice sitting next to a customer id — even
a fake one — is something people work.

### One seed. Always.

`?seed=1&seed=2` is refused with a 422, and so is `?seed=1,2`. A mandate id means nothing
across seeds: `SIM-00247` in one run and `SIM-00247` in another are unrelated customers, and
a merged export would imply a continuity that does not exist.

This is not merely validated — `build_trace` takes a single integer and there is no
argument shape that could express a multi-seed request.

### It reconciles, and that is tested

Per strategy, the ₹ recovered summed across every mandate in the trace equals the headline
figure for that seed, exactly. On a 200-mandate, 12-month run:

```
NoReschedule    trace Rs 50,898    headline Rs 50,898
FixedSchedule   trace Rs 53,892    headline Rs 53,892
Blended         trace Rs 59,381    headline Rs 59,381
```

Also asserted: every reschedule in the guard's log appears in the trace with matching
from/to times, and no attempt lacks a verdict. If the trace and the headline ever disagree,
one of them is wrong — and a debugging tool that lies about the thing you are debugging is
worse than no tool at all.

## The recommendation service — a real failure, a recommended time

Everything above is a simulation of a book. `POST /recommend` is the other direction: you
send **one failure that actually happened** and get back a time to retry it, the rule that
produced that time, and the lift measured in the segment that failure belongs to.

It runs no simulation. It is the same strategy classes, driven by your fields instead of
generated ones.

```bash
curl -s localhost:8000/recommend -H 'content-type: application/json' -d '{
  "mandate_ref": "your-own-id",
  "rail": "UPI_AUTOPAY",
  "reason_code": "TECHNICAL_DECLINE",
  "failed_at": "2026-03-10T11:00:00+00:00",
  "amount_paise": 49900,
  "mandate_cap_paise": 150000,
  "attempt_number": 1,
  "notified_at": "2026-03-08T11:00:00+00:00",
  "prior_failure_count": 0
}'
```

### It recommends a time. It does not predict an outcome.

There is no success probability in the response and there is no field to put one in. Inside
the simulator P(success) is knowable because the model generated the answer; out here
nothing in this project has ever seen a real payment succeed. A number like that would be
invented, and it would be the first thing quoted back at you.

### Two tiers of input, and it tells you which one you sent

The **minimum set** is the nine fields above (plus `original_attempt_at` once
`attempt_number` is 2 or more, and `bank_batch_cutoff_time` on eNACH). It serves
`FixedSchedule` and `ReasonAware`.

The **extended set** adds `bank_id` and `attempt_history` — this cycle's attempts with
their times, amounts, outcomes and reason codes. It additionally serves `BankAware`,
`SalaryAware` and `Blended`.

A minimum-set request is answered, not rejected. The `availability` block names every
strategy that could not run and the field that would unlock it, and `selection_notes` says
when the *measured winner* for your segment was one of them:

```
BankAware is the measured winner in the rail segment 'ENACH' and cannot be run on
this input. Supplying bank_id, attempt_history would unlock it.
```

That is deliberate. A quiet downgrade to the baseline would read as "the system recommends
what you already do", which is a different and false claim.

### A missing field is refused by name, never defaulted

`notified_at` is the clearest case. Under the shipped reading a retry inherits the original
charge's notice — but only if there was one. Defaulting the field would launder an
un-notified debit into a compliant-looking retry, so an absent one is refused instead.

Where a field is required and absent, the response is a 422 that names it. Where an
*extended* field is absent, you get an answer plus the availability note above.

### The guard applies here, harder than it does in the simulator

Every recommendation passes the compliance guard before it is returned. A simulated illegal
retry costs a wrong number; a recommended illegal retry gets executed against a real
customer.

- A **hard decline** returns no time at all — and no lift figure beside it.
- A **rescheduled** proposal returns the moved time, with the rule that moved it and the
  original in `compliance.moved_from`. The original is never returned in its place.
- A **blocked** proposal returns no time, with the blocking rule and its `assumptions.yaml`
  key so you can read the rule we applied.

### What the quoted lift is, and is not

It is the lift measured for a **segment** — your failure's reason code, rail or
failure-frequency band — from the phase 8.5 segment report. It is **not** the effect of the
rule that produced your time: no experiment in this project isolates one branch of one
strategy, so the response names both the rule and the segment and does not conflate them.

Where nothing won, it says so in words: *no strategy beats FixedSchedule here.*

Three caveats travel in every response rather than living in this guide, because the
response is what gets forwarded: it is a simulated book, the balance process it rests on is
the least grounded part of the model, and `Blended`'s weights have no corrected sensitivity
sweep. **Mandate cap is accepted for the amount check and is never segmented on** — SPEC
§12.5 keeps that axis empty on purpose, as an instrument for catching a broken significance
procedure.

If `assumptions.yaml` changes, the recorded measurement no longer describes the rules being
applied, and the service returns 503 until the evidence is regenerated with
`python notebooks/phase85_segments.py 400 8`. That is the same fail-closed rule the segment
and trace exports use.

### A CSV of failures

```bash
curl -s "localhost:8000/recommend/template" -o failures.csv   # or ?extended=true
# fill it in, then:
curl -s localhost:8000/recommend/batch --data-binary @failures.csv
```

One answer per row. A bad row is refused on its own and the good rows still answer;
`refusals_by_field` counts them by column, ahead of the rows, so a partial refusal is not
something you discover on line 4,000.

**Batch answers are minimum-tier, always.** A CSV row cannot carry an attempt history, so
`BankAware`, `SalaryAware` and `Blended` can never be selected from an upload. A batch that
shows only `FixedSchedule` and `ReasonAware` is not telling you those two won — the other
three were never eligible. Where one of them is the measured winner for a row's segment,
that row says so and names the fields that would unlock it.

---

## Uploading your own export

The dashboard's upload section takes the CSV your system already produces, in your own
column names and your own failure codes, and answers it row by row.

### Your file is checked before any row is read

A problem with the *file* is reported once, naming it — not four thousand times, once per
row. In order: encoding, delimiter, whether there is a header, whether the columns match,
row count, and whether there are any rows at all.

```
the file looks semicolon-delimited, not comma-delimited: the header splits into
11 fields on a semicolon and 1 on a comma. Nothing was read.
```

An empty file, a header with no rows, and a file whose header was stripped all **refuse**.
Answering "0 rows" to a file that should have had 4,000 in it is a failure dressed as a
success.

Everything that varies row to row — a bad date, an unknown code, a missing conditional
field — is still answered row by row.

### Mapping profiles: your vocabulary, stored

A profile in `config/merchants/<name>.yaml` maps your codes, rails, column names and date
format onto the canonical ones. Name it in the request:

```bash
curl -s "localhost:8000/recommend/batch?mapping=example" --data-binary @failures.csv
```

See `config/merchants/example.yaml` for a worked one.

**Profiles are files, not something you attach to an upload, and the UI will not let you
edit one.** A mapping that can change per upload is a mapping you cannot compare between
uploads: the same file answers differently on two days and nothing records why.

**An unmapped value refuses its row.** No case-folding, no nearest match. `U31` does not
become whatever `U30` meant, because a recommendation built on that guess looks exactly
like a real one.

### Two fingerprints, and being told when one moves

Every answer carries `evidence_fingerprint` (the measurement it quotes) and
`mapping_fingerprint` (the vocabulary it was read through). Those are the two things that
decide the answer, so a file answered differently on two days is traceable to whichever
moved.

Pass `expect_mapping` and a change is refused rather than absorbed:

```bash
curl -s "localhost:8000/recommend/batch?mapping=example&expect_mapping=375fd14397dd..." \
  --data-binary @failures.csv
# 409: the mapping profile 'example' is not the one this file was expected to be read
# through. Nothing was answered.
```

Same shape as `expect_config` on `/segments` and `/trace`, and for the same reason: a
fingerprint nobody compares is decoration.

### Getting the answers back out

```bash
# the answers, as CSV, with both fingerprints in the # header block
curl -s "localhost:8000/recommend/batch?mapping=example&format=csv&table=answers" \
  --data-binary @failures.csv

# the refusals, carrying your own values back under the canonical column names
curl -s "localhost:8000/recommend/batch?mapping=example&format=csv&table=refusals" \
  --data-binary @failures.csv
```

The refusals table is a valid upload once you have corrected the offending cells — fix and
resubmit it directly, rather than going back to your original file to work out which lines
they were.

---

## The assumptions section

The entire config file, always visible, never behind a click, with a source and a
confidence level on every number. Filter by keyword, or tick "estimates only".

| Confidence | Meaning | Count |
|---|---|---|
| `primary` | From an RBI/NPCI circular or published data file | 8 |
| `practitioner` | Stated by someone operating real mandate volume | 4 |
| `estimate` | **Our guess. No source read.** | **148** |

Every `estimate` carries the open question that must be answered before it is defensible.
This section exists to be read by the person trying to catch you out. Show it to them
first.

The three that matter most, if you only check three:

1. **`population.balance.cushion_lognormal_sigma`** — how many of your customers run a
   near-zero balance. This one parameter alone sets the insufficient-funds rate, and
   therefore most of the result. Set it to zero and the model has no insufficient-funds
   failures at all.
2. **`compliance.pre_debit_notification.retry_inherits_original_notice`** — the largest
   *regulatory* risk in the whole model. We assume a retry inherits the original charge's
   24-hour notice. **No circular has been read.** If the strict reading is correct, every
   sub-24-hour retry is illegal, the fast technical retry disappears, and the strategy line
   item falls. The dashboard states which setting the run used.
3. **`engine.reaction.revocation_base_hazard`** — how often an extra retry makes a customer
   cancel. Set it to zero and "retry forever" becomes optimal, which is wrong and would
   wreck a real merchant's customer relationships.

---

## Three worked examples

### A. Regional OTT service — 8,000 subscribers at ₹199, UPI-heavy

`book_size 8000` · `avg_ticket 199` · `upi 0.75` · `enach 0.05` · `mix insufficient-funds
dominant` · `fee 0.15`

**Why these settings:** low ticket, price-sensitive audience, UPI-first signup. Their
customers genuinely run out of money before payday.

**What you'd see:** the largest strategy line item of the three examples. Insufficient
funds is a timing problem and this book is almost entirely timing problems.

**What it means:** this is the best-case customer for retry intelligence, and the pitch
survives scrutiny — but *only* if their real reason-code mix looks like the preset. Get
that report from their gateway before quoting anything. If their failures turn out to be
technical, the number collapses by an order of magnitude.

**Note the runtime.** A publication run on 8,000 mandates takes about 72 minutes. Explore
with the fast run, then start the publication run and go and do something else — it runs in
the background and the progress bar will tell you where it is.

### B. Insurance premiums — 3,000 mandates at ₹4,500, eNACH-heavy

`book_size 3000` · `avg_ticket 4500` · `upi 0.15` · `enach 0.75` · `mix as shipped` ·
`fee 0.15`

**Why these settings:** larger ticket, older demographic, eNACH is standard for insurance.

**What you'd see:** high compliance-block and rescheduled counts. eNACH only presents on
weekdays before the batch cutoff, so a large share of proposed retries hit a timing rule.
The **rescheduling** line item dominates heavily.

**What it means:** most of this merchant's opportunity is a scheduler that respects the
clearing calendar instead of dropping retries. That is a real and sellable fix, but it is
plumbing — say so. Also check the induced-revocations column carefully: eNACH is capped at
2 retries partly because **every bounce can carry a bank return charge**, so each extra
attempt costs real money, not just an API call. A high recovery rate bought with extra
eNACH attempts may not be profitable at all.

### C. B2B SaaS — 400 mandates at ₹12,000, card-heavy

`book_size 400` · `avg_ticket 12000` · `upi 0.10` · `enach 0.20` · `mix technical
dominant` · `fee 0.15`

**Why these settings:** business customers on corporate cards. They are not short of money;
when a debit fails it is usually infrastructure.

**What you'd see:** a strategy line item hovering near zero, quite possibly spanning zero
on a fast run.

**What it means:** **walk away from the retry-intelligence pitch for this merchant.** The
model is telling you their failures aren't a timing problem, so timing them better recovers
almost nothing. The rescheduling fix still pays, and that is what you should offer. A
result near zero is a finding, not a failure of the tool — and a simulator that only ever
produced encouraging numbers would be worthless.

---

## Things this cannot tell you

Be direct about these. They are what a competent CFO will ask.

- **It is not your book.** Even when the publication run simulates your exact book *size*,
  the customers in it are synthetic and built from guesses. Until a pilot merchant supplies
  real reason-code counts, the number is a hypothesis about a merchant like you, not a
  measurement of you.
- **A capped fast run is not scaled up, and never will be.** Multiplying a 200-mandate
  result by `your_size / 200` is a one-line change that is deliberately absent from the
  code, forbidden in SPEC §10, and guarded by a test that fails if anyone adds it. It would
  assume mandates are independent and identically distributed — untested here, and false in
  reality, where books cluster on signup dates, verticals and a handful of banks. If you
  want your book's number, run the publication run, which simulates it.
- **No bank holiday calendar is modelled.** Only weekends. So eNACH compliance blocks are
  undercounted and recoverable revenue on that rail is overstated.
- **The regulatory reading is unverified.** No RBI circular has been read. See
  `retry_inherits_original_notice` above.
- **Hard declines always stop the chain.** Revoked, expired, frozen — no retry, ever, no
  configuration flag to change it. This is deliberate: naive simulators that retry after a
  hard decline are the single biggest source of fake lift, and this one refuses at the type
  level.
- **Inferred salary timing does not work.** Covered above. Do not let it back into a
  pitch.

---

## Quick reference

| Task | Where |
|---|---|
| Change what's simulated | `config/assumptions.yaml` — every number lives here, none in code |
| Re-measure the failure-mix presets | `python notebooks/phase8_presets.py 12` |
| Print the segment report | `python notebooks/phase85_segments.py 400 8` |
| Download either artifact | the **Take away** section, after a run completes |
| Row-level trace of one seed | `GET /trace?seed=N` (JSON) or `&format=csv&table=attempts` |
| Check nothing broke | `pytest -q` |
| API docs | http://localhost:8000/docs |
| Price a run without running it | `POST /simulate/estimate` |
| Start a run / poll it | `POST /simulate/jobs` → `GET /simulate/jobs/{id}` |
| Run synchronously (small books only) | `POST /simulate` |
| Per-segment breakdown | `GET /segments` |
| Recommend a retry for one real failure | `POST /recommend` |
| The same for a CSV of failures | `GET /recommend/template` → `POST /recommend/batch` |
| Upload your own export, in your vocabulary | `POST /recommend/batch?mapping=<name>` |
| List stored mapping profiles | `GET /recommend/mappings` |
| Be told if a mapping changed | add `&expect_mapping=<fingerprint>` |
| Row-level trace, one seed only | `GET /trace?seed=N` |
| The rest | `GET /assumptions`, `GET /strategies` |

Jobs live in the API process's memory and are lost on restart. That is not an oversight —
the simulation is stateless, and a job that survived a restart would be the only thing here
that wasn't. Re-running the same inputs reproduces the same result and the same hash.

Same seed plus same config always produces byte-identical output. If it doesn't, that is a
bug and the number should not be trusted.

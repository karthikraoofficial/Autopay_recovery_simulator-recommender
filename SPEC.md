# SPEC.md — Recurring Payment Recovery Simulator (India)

**Codename:** `rebound`
**Purpose:** A discrete-event simulator that models Indian recurring-payment mandate books, generates realistic failures, and measures the incremental revenue recovered by competing retry strategies against a fixed-schedule baseline.

**This is not a production payment system.** It is (a) a design sandbox for retry logic and (b) a sales instrument that produces a defensible, merchant-specific ROI number.

---

## 0. Non-negotiable principles

These are constraints on the build, not suggestions. Violating any of them makes the output commercially worthless.

1. **Every numeric assumption lives in `config/assumptions.yaml`** with a `source` field and a `confidence` field (`primary` | `practitioner` | `estimate`). Nothing is hardcoded in Python. The dashboard renders this file.
2. **The evaluation harness is built and tested before any retry strategy.** If the measurement is not trustworthy, no strategy result means anything.
3. **The compliance guard is a hard gate, not a warning.** A strategy that would violate RBI/NPCI rules must be *unable* to execute, not flagged after the fact. Simulated lift that cannot be legally realised is fraud in a sales deck.
4. **Failures carry reason codes, never booleans.** The entire thesis rests on treating an insufficient-balance failure differently from a revoked mandate.
5. **Deterministic given a seed.** Same seed + same config = byte-identical results. Non-reproducible numbers are not evidence.
6. **Strategies are compared on identical populations.** Paired comparison on the same seeded book, not independent runs.

---

## 1. Domain model

### 1.1 Rails

| Rail | Notes |
|---|---|
| `UPI_AUTOPAY` | Real-time, stateless. Debit succeeds only if balance sufficient *at that instant*. Highest failure rate. Mandate cap ₹1L for select categories (insurance, SIP, credit card bills), ₹15k default. |
| `ENACH` | Batch-cleared via NPCI. Higher execution stability, higher setup abandonment. Caps up to ₹1 crore. Bank levies return charges on bounce. |
| `CARD_EMANDATE` | Bank-side pre-authorised. Lowest execution failure. AFA required above ₹15,000 (post-2021 rules; verify current threshold). |

Rail behaviour differences must be modelled explicitly — they drive different optimal strategies, which is the interesting finding.

### 1.2 Entities

```
Merchant
  id, name, vertical, book_size, avg_ticket, billing_day_policy, rails_enabled

Mandate
  id, merchant_id, customer_id, rail, max_amount, created_at,
  status: ACTIVE | REVOKED | EXPIRED | PAUSED

Customer
  id, bank_id, salary_credit_day, income_band, balance_process,
  intent_score (latent 0-1: probability they still want the service),
  upi_app, has_alt_rail

Bank
  id, name, td_rate (technical decline %), uptime_profile,
  downtime_windows[], batch_cutoff_time, return_charge

DebitAttempt
  id, mandate_id, scheduled_at, executed_at, amount,
  outcome: SUCCESS | FAILURE, reason_code, attempt_number, is_retry

RecoveryEpisode
  mandate_id, cycle_id, original_attempt, retry_attempts[],
  outcome: RECOVERED | LAPSED, days_to_recovery, amount_recovered
```

### 1.3 Reason codes

Model these as an enum with recovery characteristics attached. Approximate NPCI/PG semantics:

| Code | Category | Soft/Hard | Recoverable | Notes |
|---|---|---|---|---|
| `INSUFFICIENT_FUNDS` | Business Decline | Soft | **High** | The core opportunity. Purely a timing problem. |
| `LIMIT_EXCEEDED` | Business Decline | Soft | Medium | Daily/per-txn cap hit. Retry next day or split. |
| `MANDATE_AMOUNT_EXCEEDED` | Business Decline | Hard | Low | Debit > mandate cap. Needs new mandate. |
| `TECHNICAL_DECLINE` | Technical Decline | Soft | **Very high** | Bank/NPCI side. Retry shortly. |
| `BANK_UNAVAILABLE` | Technical Decline | Soft | **Very high** | Downtime window. Retry after window closes. |
| `MANDATE_REVOKED` | Business Decline | Hard | None | Customer cancelled. **Stop immediately.** |
| `MANDATE_EXPIRED` | Business Decline | Hard | None | Requires re-auth. Route to dunning, not retry. |
| `ACCOUNT_FROZEN` | Business Decline | Hard | None | Stop. |
| `INVALID_PIN` | Business Decline | Soft | Low | Not applicable to autopay execution; include for completeness. |

**Hard declines must terminate the retry chain.** This is both a compliance requirement and the single biggest source of fake lift in naive simulators.

---

## 2. Failure engine

The failure engine is the credibility centre of the project. Build it first, after the harness.

### 2.1 Balance process

Each customer has a stochastic daily balance. Suggested model:

- Salary credit of `income` on `salary_credit_day` (with 0–2 day jitter, and shift for weekends/holidays)
- Exponential-ish decay through the month (higher spend velocity in the first week)
- Log-normal noise
- A floor at zero

The key emergent property: **probability of insufficient funds is strongly dependent on days-since-salary-credit.** If your simulator does not reproduce this, the Salary-Aware strategy will show no lift and you will have learned nothing.

### 2.2 Failure generation per attempt

```
1. Check mandate status → REVOKED/EXPIRED → hard decline
2. Roll bank availability (downtime windows + td_rate) → BANK_UNAVAILABLE / TECHNICAL_DECLINE
3. Check rail-specific caps → MANDATE_AMOUNT_EXCEEDED
4. Check daily limit consumption → LIMIT_EXCEEDED
5. Check balance ≥ amount → INSUFFICIENT_FUNDS
6. Else SUCCESS
```

Order matters — it determines the reason-code mix, which must be tunable to match observed merchant data during pilots.

### 2.3 Customer reaction model

Between attempts, customers react:
- Probability of voluntary revocation increases with number of failed attempts (retry aggression has a **cost**)
- Probability of top-up increases if a pre-debit notification was sent
- `intent_score` decays over the episode

Without this, every strategy converges on "retry infinitely," which is wrong and would destroy a real merchant's relationship with its customers. Model the downside.

---

## 3. Compliance guard

Implemented as a decorator/middleware that every strategy's proposed retry must pass. Rules to encode (each with a `source` in the assumptions file — **verify current RBI/NPCI circulars before relying on any of these**):

- **Pre-debit notification:** customer must be notified at least 24 hours before a recurring charge, with amount and cancellation option. A retry scheduled inside 24 hours of notification is **blocked** unless the rail/scenario exempts it.
- **Attempt caps:** maximum retry attempts per billing cycle per rail. Configurable; default conservative.
- **Hard-decline stop:** no retry after any hard decline code. Absolute.
- **Revocation respect:** no attempt against a `REVOKED` mandate under any circumstance.
- **Presentation windows:** eNACH respects batch cutoff times and clearing calendars (no debit on non-clearing days).
- **Amount integrity:** retry amount must equal original; no partial debits unless explicitly modelled as a separate feature with its own compliance analysis.

Blocked retries are logged with the rule that blocked them and surfaced in the dashboard as "opportunity forgone for compliance." That transparency is a selling point, not an embarrassment.

---

## 4. Strategy layer

Interface:

```python
class RetryStrategy(Protocol):
    name: str

    def propose_retries(
        self,
        failed_attempt: DebitAttempt,
        mandate: Mandate,
        customer_view: CustomerObservable,  # only what a real merchant could know
        history: list[DebitAttempt],
        clock: datetime,
    ) -> list[ProposedRetry]: ...
```

**Critical:** `CustomerObservable` must expose only information a real system would have — past attempt outcomes, reason codes, bank identity, mandate metadata, and *inferred* salary date. It must **not** expose the true balance or true salary day. A strategy that reads ground truth produces lift you can never deliver. Enforce this with a separate type, not discipline.

### Strategies to implement, in order

1. **`FixedSchedule`** (baseline) — T+1, T+3, T+7. This is what most merchants do today. Everything is measured against this. **Built in phase 2**, because the harness cannot be tested without a strategy to measure. It must never acquire reason-code, bank, or salary logic: if the baseline moves, every previously reported lift number silently changes meaning.
2. **`NoRetry`** (floor) — establishes natural recovery rate. **Built in phase 2**, same reason.
3. **`ReasonAware`** — branch on reason code. Technical decline → retry in 2 hours. Insufficient funds → wait. Hard decline → stop.
4. **`SalaryAware`** — infer salary credit day from historical success timestamps, schedule the retry to land 0–2 days after predicted credit.
5. **`BankAware`** — avoid each bank's known downtime windows and low-uptime hours; prefer high-success time-of-day slots.
6. **`Blended`** — combines 3–5 with a scoring function.
7. **`MLRanked`** — gradient-boosted model predicting P(success | retry at time t, features). Trained on simulated history; evaluated out-of-sample. **Only build this after 1–6 work.** It is the least important and most seductive component.

---

## 5. Evaluation harness

Build this **second**, immediately after the domain model and before the failure engine's tuning.

### 5.1 Design

- Generate a seeded population once; run every strategy against a deep copy. Paired comparison.
- Report per strategy:
  - Recovery rate (% of failed episodes ending RECOVERED)
  - Gross ₹ recovered
  - **Net ₹ to merchant after a configurable performance fee** (default 15%) — this is the number the merchant cares about
  - Median and p90 days-to-recovery
  - Attempts per recovery (cost proxy)
  - Induced revocations (the downside)
  - Compliance blocks
- Bootstrap confidence intervals over N seeds (default 50). **Never report a point estimate without an interval.**
- Sensitivity analysis: sweep each assumption ±50% and report which ones the conclusion is fragile to. This is the section that survives a CFO.

### 5.2 Guardrail tests

Write these as failing tests before the strategies exist:

- A strategy that retries after a hard decline → harness raises
- A strategy that reads true balance → type error / raises
- Two runs with the same seed → identical output hash
- `NoRetry` ≤ `FixedSchedule` ≤ `Blended` on recovery rate (sanity ordering)
- Total recovered ≤ total failed (no money created)

---

## 6. Interface

Single-page dashboard. Three sections:

1. **Input** — six live fields, every one of which moves the number: book size, avg ticket, UPI share, eNACH share (card = remainder), reason-code mix (current / IF-dominant / technical-dominant), performance fee rate. Horizon fixed at 12 months. `vertical` and `billing_day_policy` were dropped in phase 8 — neither drove any generator, and an inert control is worse than an absent one.
2. **Result** — the decomposition as **two separate line items, never summed**: rescheduling lift (`NoReschedule` → `FixedSchedule`) and strategy lift (`FixedSchedule` → `Blended`), each in ₹ with its confidence interval. Seed count and interval width displayed alongside. Where an interval spans zero, say so in words — a non-significant result must not be rendered as a bar that reads as positive.
3. **Assumptions** — rendered `assumptions.yaml` with sources and confidence levels, always visible, never behind a click.

Resist adding more. The dashboard's job is to survive scrutiny, not to impress.

---

## 7. Stack

- **Python 3.11+**, `pydantic` v2 for the domain model, `numpy` for the stochastic processes, `pandas` for the harness output, `pytest` + `hypothesis` for tests.
- **Simulation:** hand-rolled discrete-event loop. Do not use SimPy — the added abstraction costs more than it saves here.
- **API:** FastAPI, three endpoints (`POST /simulate`, `GET /assumptions`, `GET /strategies`). The sim is stateless — no persistence layer, no caching. Interval width is the honest signal of what a cheap run bought; a cache hides that cost.
- **Front end:** React + Vite + Recharts. Single page. No component library.
- **Persistence:** SQLite via SQLModel, only for caching simulation runs. The sim itself is stateless.
- **ML (phase 7 only):** scikit-learn `HistGradientBoostingClassifier`. Not XGBoost, not PyTorch.

No Docker until it runs locally. No cloud until a real merchant asks.

---

## 8. Repo layout

```
rebound/
  SPEC.md
  CLAUDE.md                  # working agreement for the coding agent
  config/
    assumptions.yaml         # every number, with source + confidence
    merchants/               # saved merchant profiles
  src/rebound/
    domain/                  # entities, enums, reason codes
    population/              # customer/bank/mandate generators
    engine/                  # balance process, failure engine, clock
    compliance/              # guard rules
    strategies/              # one file per strategy
    harness/                 # runner, metrics, bootstrap, sensitivity
    api/
  tests/
    test_harness_guardrails.py
    test_determinism.py
    test_compliance.py
    test_strategies/
  web/
  notebooks/                 # exploration only, never imported
```

---

## 9. Build order (do not reorder)

| Phase | Deliverable | Done when |
|---|---|---|
| 0 | Repo, config loader, `assumptions.yaml` skeleton | `pytest` runs, config loads |
| 1 | Domain model + reason codes | Types are complete, guardrail tests written and **failing** |
| 2 | Evaluation harness + `NoRetry` and `FixedSchedule` baselines | Determinism + no-money-created tests pass |
| 3 | Population + bank generators | Salary-date distribution reproduces expected shape |
| 4 | Failure engine | Reason-code mix is tunable to a target distribution |
| 5 | Compliance guard | Violating strategies are blocked, logged, tested |
| 6 | Strategy 3 (`ReasonAware`); 1 and 2 already exist from phase 2 | `ReasonAware` beats `FixedSchedule` with non-overlapping CIs |
| 7 | Strategies 4–6 + `NoReschedule` reference | Sensitivity sweep run **at powered sizing (400 mandates, 12 seeds)** and documented. A sweep run where the base effect is not significant produces sign-flips of noise, not fragility — the harness raises `UnderpoweredSweepError` rather than emitting that list. |
| 8 | API + dashboard | End-to-end from six inputs to one chart |
| 8.5 | Segment report (§12) | Segments reconcile to the headline; every verdict corrected for multiplicity |
| 9 | `MLRanked` | Only if 4–6 plateau. **Phase 7 evidence says skip it:** rescheduling (plumbing, no model) is worth ~4× the best retry logic and is significant under every reason-code mix. Do not build this without a specific reason that survives that finding. |
| 10 | Recommendation service (§14) | A merchant-supplied failure returns a retry time, the rule that produced it, and the measured lift for its segment. A **sibling of the simulator, not a phase of it** — it adds no simulation and reorders nothing above. Phase 9 stays skipped on phase 7's evidence; phase 10 does not depend on it. |

---

## 10. Out of scope

Explicitly not building: real PG integration, webhook ingestion, customer messaging, KYC, merchant auth, multi-tenancy, billing. This is a simulator. When it convinces someone, then build the product.

**Engine performance optimisation is out of scope.** The simulation costs roughly 45 ms per mandate per seed for the full seven-strategy comparison (measured phase 8, flat across book sizes). That is slow, and it is deliberately not being fixed: the cost sits in the measurement core, and any change to it requires re-verifying every phase 4–7 result against its recorded output hash. The runtime is managed at the edges instead — the interactive run caps its book, the publication run goes to a background job with a progress indicator. Revisit only if a real user complains about it, never preemptively.

**Extrapolating a small run to a larger book is out of scope, permanently.** Multiplying a 400-mandate result by `book_size / 400` is a one-line convenience and it must not be added. It asserts that mandates are independent and identically distributed — untested here, and false in real books, which concentrate on signup dates, verticals and a handful of banks. A fabricated figure that looks like the merchant's own is worse than an honest one that does not. The publication run simulates the merchant's actual book; that is the supported way to get their number. See the note on `Sizing` in `api/inputs.py`.

---

## 11. Open questions to resolve with primary sources

Flag these in `assumptions.yaml` as `confidence: estimate` until verified:

- Current RBI e-mandate AFA threshold and pre-debit notification rules (check the latest circular, not blog summaries)
- **Does a retry of an already-notified charge require fresh notice? — largest single regulatory risk in the model. Unverified.** The simulator ships assuming it does not (`compliance.pre_debit_notification.retry_inherits_original_notice: true`). If it does, every sub-24h retry is illegal, the fast technical retry disappears, and reported lift falls. Phase 7 must show the headline number under both settings side by side, permanently.
- NPCI per-bank TD% and uptime — pull the actual monthly published file
- Retry attempt caps per rail — is there a hard NPCI limit or is it PG policy?
- eNACH clearing calendar and presentation cutoffs
- Realistic reason-code mix by vertical — **unknown until a pilot merchant shares data. This is the largest uncertainty in the model and must be labelled as such on the dashboard.**

### Known limitation, measured phase 8.5 — the population has no ticket dispersion

**Per-mandate ticket is constant at `book.avg_ticket_paise`.** `generate_mandates` draws a lognormal ticket per mandate, but the harness debits `min(book.merchant.avg_ticket_paise, mandate.max_amount_paise)`, and the drawn ticket survives only in the mandate cap. Measured on a 2,000-mandate book: **99.85% of mandates debit exactly the same amount**, and only 4 distinct debit amounts exist in the whole book.

Two consequences, neither of them cosmetic:

- **Real books have ticket dispersion and this one does not.** A high-ticket mandate plausibly fails more often on insufficient funds, since it is a larger claim on the same balance. If that is right, the real opportunity is *more concentrated* in a minority of mandates than this model can show, and the current output understates that concentration.
- **`mandate.max_amount_paise` is not a usable proxy for it, and carries no signal at all.** Caps are drawn in a different RNG stream from every customer attribute and are independent of all of them (measured on 4,000 mandates: ρ = −0.012 income, +0.000 spend decay, +0.000 balance volatility, −0.010 salary day, −0.035 intent). The median cap is 3× the debit amount, so the cap binds on **0.12%** of mandates and has essentially no causal path to any outcome. First-attempt failure rate across cap quartiles varies by 0.57pp, non-monotonically (Q3 > Q4) — noise.

  **This is a property of the current generator, not a finding about merchants.** In a real book a mandate cap is set by the merchant with the customer's income and ticket in view, so it would correlate with both. Here it correlates with nothing. Any segmentation on cap band is therefore a *negative control* — an axis known to be empty, useful for checking that a significance procedure does not manufacture winners, and not to be read as evidence about real books.

  Both this and the constant-ticket limitation above are candidates for the same post-v0.1 population fix, and should be fixed together: giving tickets dispersion without also linking caps to income would leave the cap axis just as empty.

**Candidate for the first post-v0.1 engine change**, in its own commit after tagging: it changes every phase 4–7 number and each would need re-verifying against its recorded output hash. Until then, any segmentation on ticket or cap is a negative control, not a finding.


### Known limitations of the batch endpoint, deferred past phase 10.5

Logged rather than built. None of them is a finding about the model.

- **No streaming or memory bound.** `run_batch` reads the whole body into a string and parses every row before the `recommend.batch_max_rows` cap is applied, so the cap bounds processing rather than memory. Fine at the current cap; it is the first thing to revisit if the cap is raised.
- **No duplicate detection.** The same `mandate_ref` and `failed_at` twice in one file are answered twice, with no note that they were the same failure.
- **Batch is minimum-tier by construction, and this is a format limitation, not a finding.** A CSV row cannot express `attempt_history` — a per-attempt structure does not fit one row — so `Tier.EXTENDED` is unreachable from an upload however many columns are filled in, including `bank_id`. **`BankAware`, `SalaryAware` and `Blended` can therefore never be selected from a CSV upload.**

  Anyone reading a batch of answers will see `FixedSchedule` and `ReasonAware` and nothing else, and must not read that as those three strategies having lost. They were never eligible. Phase 8.5 measured `BankAware` as the winner on eNACH and `Blended` on two other segments, so the strategy a batch answer *omits* may be the one the evidence favours — which is exactly why every response names the unavailable winner and the field that would unlock it (§14.4).

  Closing it needs a second uploaded file of attempts keyed on `mandate_ref`, in the shape §13.5 uses for the trace export: two files, one join key. Not in 10.5.

### Sensitivity sweep, phase 7 (run and valid; two rows outstanding)

Run at 400 mandates x 12 months x 24 seeds, subject `Blended` vs `FixedSchedule`, 16 shortlisted keys. Output: `notebooks/phase7_sweep_output.txt`, `notebooks/phase7_sweep.json`.

**Sequence note.** This sweep is phase 7's last deliverable but it post-dates the `v0.1-sim` tag (`94c5554`) that phase 7 otherwise closes: the first attempt was void (see below), and the corrected run landed eleven commits later, during phase 8.7. `v0.1-sim` was left where it is rather than moved, so the sweep result ships in **`v0.3-exports`**. Anyone reading `v0.1-sim` as "phase 7 complete" should know its sensitivity analysis is not in that tree.

**Base effect: [₹1,007, ₹4,666] per book, point ₹2,863 — excludes zero, so the ranking is meaningful.**

Fragile keys (the conclusion's sign flips or its significance is lost when the assumption moves ±50%):

| key | max relative swing |
|---|---|
| `population.balance.spend_decay_rate.low` | 282.1% |
| `population.balance.cushion_lognormal_sigma` | 85.2% |
| `compliance.reschedule_horizon_days` | 51.9% |

**Two of the three are the balance process — the part of the model with no empirical grounding at all.** The conclusion is most fragile to the assumptions there is least basis for. `book.avg_ticket_paise` swings 194.4% without being flagged: "fragile" means the sign flips or significance is lost, not that the magnitude moves, and that distinction has to be stated to anyone reading the table.

**Two rows are unreliable and must not be quoted.** `strategy.blended.weight_reason` (14.2%) and `strategy.blended.weight_salary` (6.2%) were clamped during the run by the substring-matching defect in `_is_probability`: `"rate"` is inside `"st-rate-gy"`, so every `strategy.*` key with unit `ratio` was treated as a probability and clamped at 0.999. `weight_reason` (1.0) was swept 0.90 to 0.999 rather than 0.5 to 1.5 — a much smaller question, reported beside keys swept properly.

The matcher is now fixed (tokens, not substrings) and the three blend weights sweep correctly. **The other 14 keys and every fragility verdict are unaffected** — verified key by key; neither clamped key was flagged fragile, so the fragility conclusion stands as written.

**Outstanding: a corrected sweep of the three `strategy.blended.weight_*` keys, ~3.5 hours at the measured rate** (2,071s per experiment; note the first estimate of 124s was wrong by a factor of ~17, because cost does not scale linearly with strategy count). Not a blocker for v0.1-sim; the headline and the fragility ranking do not depend on those two figures.

### Measured phase 8.5 — segment findings

Both measured on a 400-mandate book over 12 months, 8 seeds, 90% CI, Bonferroni-corrected across all 72 tests. See `notebooks/phase85_segments.py`.

- **`INSUFFICIENT_FUNDS` is the largest pocket of unaddressed opportunity, and it is blocked on data rather than on logic.** It is the biggest episode segment in the book — **39.9% of episodes and 39.9% of ₹ at risk** — and **no strategy beats `FixedSchedule` in it after correction**. `SalaryAware` leads it on the uncorrected interval and does not survive the adjustment.

  The reason is already recorded above: payroll timing is unidentifiable from payment telemetry, so the one strategy whose whole purpose is to time a retry against a salary credit has nothing reliable to time against. This is the largest identified gap in the model and **it does not close with better retry logic.** It closes with an external data source — account-aggregator consent, a payroll date declared at signup, or an issuer signal — or not at all. Any roadmap that proposes to attack this segment by improving the scheduler is attacking the wrong constraint.

- **The winning strategy differs by rail, which no single global strategy captures.** `UPI_AUTOPAY` favours `ReasonAware`; `ENACH` favours `BankAware`. Both survive correction. That is a plausible mechanism rather than a surprise — UPI is real-time so a reason-code branch acts immediately, whereas eNACH is batch-cleared and dominated by presentation windows and bank timing, which is exactly what `BankAware` reads.

  **Worth investigating post-v0.1: a rail-routed strategy that dispatches to a different policy per rail, measured against the best single global strategy.** It is not obviously a win — routing adds a degree of freedom and therefore a way to overfit the simulated book — so it needs its own paired comparison against `Blended`, not an assumption that combining the two rail winners must beat both.

### Resolved by measurement, phase 7

- **Per-mandate attempt history cannot identify a customer's payroll date.** Not a tuning problem and not fixable by a better estimator: it is unidentifiable from this data. Every mandate bills on one calendar day, so the observable history is one day-of-month repeated. The likelihood is then maximised by placing payroll immediately before that day — because a debit is likeliest to succeed just after a credit — regardless of when payroll actually is. Measured over 1,191 mandates: the estimate lands on the mandate's own **billing** day 93.7% of the time, and against the true salary day is within 2 days only 14.6% of the time, *worse than the 17.9% a uniform guess over 28 candidate days achieves*. On the 6.3% of mandates whose attempt history is varied enough to pull the estimate off the billing day, accuracy rises to 29.3% within 2 days — weak signal, not none, and confined to a minority.

  **Consequence: salary-timing requires an external data source** (bank statement / account-aggregator consent, payroll-date declaration at signup, or an issuer signal). It cannot be recovered from payment telemetry alone, and no amount of history fixes it — more cycles supply more copies of the same uninformative day.

  `SalaryAware` and `strategies/salary_inference.py` are **kept deliberately**, along with `test_a_single_billing_day_collapses_the_estimate_onto_that_day`, as executable documentation of why. Do not delete them to tidy up, and do not report `SalaryAware`'s measured lift as evidence that inferred salary timing works — a `BillingDayAnchor` control that skips inference entirely scores exactly zero lift, so what the estimator mostly does is reproduce the baseline.

---

## 12. Segment report (phase 8.5)

A per-segment breakdown of the headline, so a merchant can see **where** in their book the opportunity sits rather than only its total size.

**It is not a per-customer listing, and must never become one.** These customers are synthetic. A row-per-customer report reads as an operational action list for people who do not exist, and would be acted on as if it were one.

### 12.1 Segment dimensions

Cut only on things a real merchant can observe in their own book *without our system*:

| Dimension | Definition |
|---|---|
| **Rail** | `UPI_AUTOPAY` / `ENACH` / `CARD_EMANDATE`. |
| **Mandate cap band** | Quantiles of `mandate.max_amount_paise` over that seed's book; fractions in `assumptions.yaml`, realised paise edges reported. **A negative control — see §12.5.** |
| **Dominant reason code** | Modal reason code over the mandate's failed *opening* attempts. Ties broken by `ReasonCode` enum order. A hard decline does not override the mode; it is one observation among others. |
| **Failure frequency band** | Episodes per mandate over the horizon, banded on edges from `assumptions.yaml`. Mandates with no failed cycle form an explicit `no failures` segment. |

**Segment assignment is computed once, from the `harness.scheduler_reference_strategy` run, and reused for every strategy.** If each strategy segmented on its own history, retries would move mandates between segments and a baseline-versus-strategy comparison would no longer be within-segment. It is also the honest definition: the reference run is what the merchant is doing today, so it is what they can actually observe.

### 12.2 Reported per segment

Across the full seed set, with intervals before point estimates:

- Segment size: share of mandates, share of episodes, share of ₹ at risk
- Baseline recovery rate under `NoReschedule` and under `FixedSchedule`
- **Rescheduling lift and strategy lift, kept separate, never summed** — as §6.2
- Winning strategy, or explicitly *no strategy beats `FixedSchedule`*
- Net ₹ per mandate per year, so segments of different sizes are comparable

### 12.3 Cut from the same runs, never re-simulated

Segments are aggregated from the episodes of the *same* seeded paired runs that produce the headline. Re-simulating would silently allow the segment numbers and the headline to disagree. A test asserts that, per dimension, segment episode counts and ₹ at risk sum to the run totals.

### 12.4 Multiple comparisons

With N segments some will clear zero by chance, and a report that presents only the winners is a machine for finding them.

- The family is **every segment × every candidate strategy, across all dimensions at once** — not per dimension. A reader scanning the page for a winner is running every test simultaneously, whatever the dimensions are nominally called.
- The **total test count is stated next to the results**, not in a footnote.
- Only the **corrected verdict is published.** There is no uncorrected column: if both are available, the uncorrected one is what ends up in a deck.
- Where a raw interval clears zero but does not survive correction, the report **says so in words** rather than presenting it as a near-miss.
- Any segment below `segment.min_episodes` reports **`insufficient data`**, not an interval.

### 12.5 The negative control

Mandate cap band is reported in its own section, labelled **expected null**, and never among the findings. §11 records why: caps are generated independently of income, balance and salary day (|ρ| < 0.04) and bind on 0.12% of mandates, so the axis carries no signal.

This makes it an instrument. **A known-empty axis that produces winners proves the significance procedure is broken**, so a test asserts that no cap band survives correction. That test failing means either the generator changed or the statistics did, and both need investigating before any segment result is believed.

### 12.6 Recommendation text

Derived from the measured result, never a template with numbers substituted in. Each verdict produces a materially different sentence, and where nothing wins it says so plainly rather than reaching for the least-bad option.

---

## 13. Per-mandate trace export (phase 8.7)

Row-level export of one seeded book, for inspection and debugging.

**This is a TRACE of what happened, not a recommendation list.** It records what each strategy did with each simulated mandate. That is a fact about a run. What a merchant *should* do is a different kind of claim — it generalises across books, it belongs in a decision table, and it must never appear beside a synthetic id.

### 13.1 One seed, structurally

A trace covers **exactly one seed**. A mandate id is meaningful only within the seed that generated it; a merged export would imply a continuity between `SIM-00247` in seed 1 and `SIM-00247` in seed 2 that does not exist.

This is enforced by construction rather than by validation: `build_trace` takes a single `int` and calls `run_paired` on one generated book — `run_experiment`, which loops over seeds, is not on the path. There is no argument shape that expresses a multi-seed trace.

`GET /trace` additionally **refuses a repeated `seed` parameter**. FastAPI binds the last value of a repeated query parameter, so `?seed=1&seed=2` would otherwise answer with seed 2's trace and give no sign the request was not honoured — the same quiet merge, arriving through the transport layer.

### 13.2 Header block, on every export

Both formats carry seed, output hash, export timestamp, strategy list, book size, and a plain sentence stating the mandates are synthetic, were generated by this seed, and will not exist on any other run. In CSV it is `#`-prefixed, so the file still parses with `comment='#'` and is unmissable to a human opening it.

Seed and output hash **also repeat on every row**, so a row separated from its header is still traceable to the run that produced it.

`exported_at` is the only non-deterministic field in the export. It is provenance, not result: it never enters the output hash and nothing reconciles against it.

### 13.3 No recommendation column

There is no "recommended tactic" column and none may be added. A test asserts the row models carry no field named for advice. Advice next to a synthetic id invites someone to work it as an action list.

### 13.4 The trace must reconcile

Per strategy, the per-mandate ₹ recovered must equal the headline `gross_recovered_paise` for that seed, and episode and attempt counts must equal the harness counters. Compared against the harness's own `StrategyMetrics` object from the same run — not a recomputation, which could reproduce the trace's own mistake and agree with it.

Two further checks, because the failure mode is silent under-reporting rather than a visible error:

- **Every `ComplianceReschedule` in the guard log appears in the trace** with matching from/to times.
- **No attempt lacks a guard verdict.** Opening debits carry an explicit `not_reviewed_opening_debit` — the guard is never consulted for them — so a blank verdict always means a defect rather than "not applicable".

Guard verdicts are attributed by slicing the guard's append-only log at each cycle boundary as the run proceeds, not by joining on `(mandate_id, scheduled_at)` afterwards. A post-hoc join would have to guess which cycle a block belonged to and could drop rows.

### 13.5 Shape

Long format: **one row per attempt**, plus a row for each proposal the guard refused. A blocked proposal carries no attempt number — it never became an attempt, and numbering it would imply the merchant made one.

Generated mandate attributes (rail, cap, bank, income band, salary day, billing day) live in a **second table keyed on `sim_id`**, rather than being repeated on every attempt row. Two files, one join key.

`GET /trace?seed=N` returns JSON; `&format=csv&table=attempts|mandates|outcomes` returns that table as CSV. No new dependencies — `csv` is in the standard library.

---

## 14. Recommendation service (phase 10)

`POST /recommend` takes one **observed** failure from a real merchant — rail, reason code, failure time, amount, mandate cap, attempt number, prior failure count — and returns a recommended retry time, the rule that produced it, and the measured lift for the segment that failure falls in.

**It is a sibling of the simulator, not an extension of it.** It runs no simulation, generates no population, and adds no retry logic. It is a second entry point into the strategy classes of §4, which are used **unchanged**: an adapter builds a `CustomerObservable` from merchant-supplied fields and calls `propose_retries`. Nothing under `engine/`, `harness/`, `population/` or `strategies/` may be modified to make this work. If a strategy cannot be driven from merchant-supplied data, that is a finding to report (§14.4), not a reason to change the strategy.

### 14.1 No outcome prediction, ever

The service recommends a **time**. It does not claim a success probability, an expected recovery, or a per-failure ₹ figure.

**There is no oracle outside the simulator.** Inside it, P(success) is knowable because the balance process generated the answer. Outside it, nothing in this repo has ever seen a real payment succeed. A probability attached to a real merchant's real failure would be a number with no measurement behind it, and it would be the first thing quoted back to us.

A test asserts the response models carry **no field named for a probability, likelihood, score, or expected value** — the same shape of test as §13.3's ban on an advice column, and for the same reason: the field would be filled eventually if it existed.

### 14.2 Two input tiers

Fields are split into a **minimum set** that serves `FixedSchedule` and `ReasonAware`, and an **extended set** that additionally serves `BankAware`, `SalaryAware` and `Blended`. The split is not a convenience — it is the honest statement of what each strategy actually reads.

**Minimum set**

| Field | Why it is needed |
|---|---|
| `mandate_ref` | The merchant's own reference, echoed back. Never generated by us, never a synthetic id (§13). |
| `rail` | Selects rail-specific compliance rules and the rail segment. |
| `reason_code` | The failure's own code. Hard codes terminate (§14.5). |
| `failed_at` | The time being reacted to. Aware datetime; a naive one is refused. |
| `amount_paise` | The retry amount. Compliance requires it equal the original (§3, amount integrity). |
| `notified_at` | When the pre-debit notice for this charge was sent. Under the shipped `retry_inherits_original_notice: true`, a retry inherits the original notice **only if there was one** — the guard blocks otherwise, so that the exemption cannot launder an un-notified debit. Defaulting this field would do exactly that laundering on the merchant's behalf, which is why it is required rather than assumed. |
| `mandate_cap_paise` | Amount-integrity check only. **Never a segment dimension — see §14.8.** |
| `attempt_number` | Which attempt in this cycle. Drives the baseline offset and the attempt cap. |
| `original_attempt_at` | Required when `attempt_number > 1`; every strategy schedules from the *original* attempt, not the last one. At `attempt_number == 1` it is `failed_at`. |
| `prior_failure_count` | Failed cycles for this mandate in the **preceding 12 months, excluding this one**. Used only to place the failure in a failure-frequency segment (§14.6). The horizon must match §12's or the band is wrong, so the field is defined by that horizon rather than by "how many you remember". |
| `bank_batch_cutoff_time` | **eNACH only.** The presentation-window rule needs it. The minimum set is rail-dependent in exactly this one place. |

**Extended set** — everything above, plus:

| Field | Unlocks | Why |
|---|---|---|
| `bank_id` | `BankAware`, `Blended` | Both tally success by `(bank, hour)`. Without it there is nothing to tally against. Any merchant-stable identifier works; it is a grouping key, not a lookup into our data. |
| `attempt_history` | `BankAware`, `SalaryAware`, `Blended` | This cycle's attempts, each with scheduled time, amount, outcome and reason code. `BankAware` learns its hour tally from outcomes; without them its tally is empty and it degenerates to a fixed hour. |

**`attempt_history` deliberately stops at this cycle.** Extending it across cycles would not help: §11 measured that a mandate's attempt history cannot identify its payroll date, because every mandate bills on one calendar day and more cycles supply more copies of the same uninformative day. Asking a merchant for a year of history to feed an estimator that is *worse than a uniform guess* would be extracting data under a false promise.

### 14.3 Refusal, never a default

**Any value a strategy or the guard actually reads, and the input does not supply, causes an explicit refusal naming the field.** No defaults, no zero-filling.

This is the same principle as §4's observable boundary, applied at a different edge. There the danger was a strategy reading ground truth it could never have; here it is a strategy reading a value we invented on the merchant's behalf. A fabricated `bank_id` makes `BankAware` run and return a time that is a function of our placeholder, not of their book. It would look identical to a real recommendation.

Refusals name the field and the strategies it would unlock. A refusal is a complete, successful response — it is the answer, not an error — except where **no** strategy can be served, which is a 422. Minimum-set fields are exactly the ones whose absence leaves nothing servable.

**The placeholder rule.** The strategy and guard interfaces demand whole objects where the merchant supplies facts: `RetryContext` wants a `Bank` but reads only `batch_cutoff_time`, and `propose_retries` wants a list of `DebitAttempt` where all that is read is the original attempt's time and amount and the number of retries so far — three things the minimum set does supply. The adapter fills the remaining fields with placeholders, under one condition that makes the difference from fabrication checkable rather than asserted:

> **Every placeholder field is covered by a test that varies it and asserts the recommendation is unchanged.**

The bank's `td_rate`, `uptime_profile` and `downtime_windows`; the reason codes and intermediate times of prior attempts the merchant did not itemise. If any strategy or rule ever begins reading one of them, the test fails and the field has to move into the input tiers. What is forbidden is inventing a value a consumer *reads* — that is what makes a recommendation a function of our guess. Placeholder reason codes are drawn from the observed failure's own code, which is soft by construction: a hard decline stops the chain before any of this is built (§14.5).

Under the minimum set, `CustomerObservable.past_attempts` is **empty**, not placeholder-filled. It is the one place a placeholder would be read — `BankAware` tallies outcomes from it — so nothing goes in it that the merchant did not attest to.

**Mandate status is not an input.** `RetryContext` needs one, and the adapter always sets `ACTIVE`. A merchant cannot report a status we could trust — the observable signal that a mandate is dead is the reason code, and `MANDATE_REVOKED` and `MANDATE_EXPIRED` are hard declines that stop the chain before the guard is reached. Accepting a status field would add a second, weaker path to the same decision, and a mandate reported `ACTIVE` alongside a revocation code would put the two in conflict.

### 14.4 Availability is reported, not silently downgraded

On a minimum-set input the service answers with the best available strategy **and states plainly which strategies were unavailable and which fields would unlock them.**

A merchant who sends less data should learn what better data would buy them. A quiet downgrade to `FixedSchedule` reads as "the system recommends the baseline", which is a different and false claim.

This applies to `SalaryAware` in both directions. Where the extended set is absent, the response says `SalaryAware` was unavailable for want of `attempt_history`. Where it is present, the response says `SalaryAware` ran and that §11 measured its inference as unidentifiable from payment telemetry — the service never selects it as a winner on that basis. Reporting only the second case would let the first read as an endorsement by silence.

### 14.5 The compliance guard applies here too

Every recommendation passes through `ComplianceGuard` before it is returned. A hard gate here as in §3, and more consequential: a simulated illegal retry costs a wrong number, a recommended illegal retry gets executed against a real customer.

- A hard decline returns **no time at all**, with the code and the reason. There is no config flag (§1.3).
- A proposal the guard reschedules returns the **rescheduled** time, with the rule that moved it, never the original.
- A proposal the guard blocks returns no time, with the blocking rule and its `source_key` from `assumptions.yaml`, so the merchant can read the rule we applied.

The unresolved regulatory question of §11 — whether a retry of an already-notified charge needs fresh notice — reaches further here than in the simulator. The service ships under the same `retry_inherits_original_notice: true` setting and **says so in every response that recommends a sub-24-hour retry**, naming the assumption key. If the answer turns out to be the other one, those are the recommendations that were wrong.

### 14.6 Where the evidence comes from

**Correction, recorded so the wrong artefact is not reached for again:** the per-segment lift quoted by this service comes from the **phase 8.5 segment report** (§12, `notebooks/phase85_result.json`), *not* from the phase 7 **sensitivity sweep** (§11). The sweep measures fragility of the headline to ±50% moves in an assumption; it produces no per-segment figure and cannot answer "what is this worth here". The two were conflated once during phase 10 planning. They are different measurements with different units.

**The quoted number is the lift measured in the segment this failure belongs to — never the rule's own effect.** No experiment in this repo isolates the contribution of a single branch of a strategy. `ReasonAware`'s technical-decline branch has no measured lift; what was measured is the lift of a whole strategy within a segment. Every response therefore names **both** the rule that produced the time and the segment the lift was measured in, phrased as *measured lift in the segment this failure belongs to*.

Segment assignment for a single observed failure, by dimension:

| Dimension | Assignment from the input |
|---|---|
| Rail | Directly. |
| Dominant reason code | The cycle's **opening** reason code — from `attempt_history` where supplied, otherwise this failure's own code when it is the opening attempt. |
| Failure frequency | `prior_failure_count + 1`, banded on `segment.failure_frequency_band_edges`. |
| Mandate cap band | **Not assigned. §14.8.** |

§12.1 defines the dominant reason as the mode over a mandate's opening attempts across the horizon. One failure is not a mode. The assignment above is a **one-cycle approximation** of that definition, and every response says so rather than implying the merchant's mandate was segmented the way the report's mandates were.

Selection precedence, fixed and stated: **dominant reason code, then rail, then failure frequency.** The first dimension whose segment has a winner surviving correction selects the strategy. Reason code leads because it is the failure's own attribute and §0.4 makes it the thesis; frequency band trails because it describes the mandate's past rather than this failure. Where no dimension has a surviving winner, the recommendation is `FixedSchedule` and the response says **"no strategy beats FixedSchedule here"** in those words. Where a segment is below `segment.min_episodes`, it reports `insufficient data` and is not used for selection.

Intervals precede point estimates, as everywhere else in this project.

**Evidence must carry a matching `config_fingerprint`.** The service refuses an evidence artefact whose fingerprint differs from the running configuration, and refuses one that carries no fingerprint at all — the same fail-closed rule as `/segments` and `/trace`. Lift measured under different assumptions than the rules being applied is not evidence about the recommendation being made.

The comparison is against the shipped assumptions with the evidence run's own sizing applied (`recommend.evidence.book_size`, `recommend.evidence.seeds`), because the report was produced at a sizing rather than at the shipped book. Sizing changes how precisely the effect was measured; every key that governs what a strategy or the guard *does* is compared exactly. The recorded `notebooks/phase85_result.json` predates the fingerprint field entirely and was regenerated for this phase — its figures were confirmed unchanged first, so the regeneration adds provenance without moving any number.

### 14.7 Caveats that travel with every number

Three, carried in the response rather than in documentation, because the response is what gets forwarded:

1. **It is a simulated book.** Every figure is measured on synthetic mandates. It is not a measurement of the merchant's book and does not become one by being quoted at them.
2. **Balance-process fragility (§11).** Two of the three keys the headline is most fragile to are `population.balance.*` — the part of the model with no empirical grounding at all. Every lift figure this service quotes inherits that fragility.
3. **Unswept blend weights (§11).** Wherever `Blended` is named, the response states that its three `strategy.blended.weight_*` keys have no corrected sensitivity sweep. The first sweep of them was void, and the corrected run is outstanding.

### 14.8 Mandate cap is accepted, never segmented on

The cap is used for the amount-integrity check and nothing else. **It must not become a segment dimension here, however obvious a cut it looks.**

§11 and §12.5 record why: caps are drawn independently of income, spend, balance volatility, salary day and intent (|ρ| < 0.04) and bind on 0.12% of mandates. The axis is a known-empty negative control, kept precisely so that a winner appearing on it proves the significance procedure is broken. Quoting it to a merchant as a finding would convert an instrument into a claim, and the claim would be about our random number generator.

This is worth restating because the cap is the most natural-looking cut a reader will reach for — *high-value mandates behave differently* is a plausible sentence, it is simply not something this model has measured. It becomes measurable only after the §11 population fix links caps to income and gives tickets dispersion, and not before.

### 14.9 Batch endpoint

`POST /recommend/batch` takes a CSV of failures and returns the same per row. Columns are the §14.2 fields; a file carrying only the minimum-set columns is valid and every row answers at minimum-set availability. **The tiers are defined here and nowhere else** — any template offered to merchants derives its columns from this section rather than restating them, so the two cannot drift.

- **Per-row refusal, not per-file.** A row missing a field is answered with that row's refusal; the rest are answered normally. Fail-closed at file level would discard good rows for one bad one.
- **Refusals are counted in a summary block**, by field, ahead of the rows. A partial refusal must not be discoverable only by reading 4,000 rows.
- Row count is capped by `recommend.batch_max_rows`; over it, the file is refused whole, before any row is processed.
- No new dependencies — `csv` is in the standard library, as in §13.

### 14.10 Determinism

Same input, same configuration, same output. The strategies' `clock` argument is bound to `failed_at`, never to the wall clock; no field of the response is drawn from the current time. The evidence artefact's `config_fingerprint` is echoed so a response can be traced to the measurement that justified it.

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

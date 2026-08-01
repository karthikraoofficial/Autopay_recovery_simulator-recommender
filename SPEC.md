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
| 9 | `MLRanked` | Only if 4–6 plateau. **Phase 7 evidence says skip it:** rescheduling (plumbing, no model) is worth ~4× the best retry logic and is significant under every reason-code mix. Do not build this without a specific reason that survives that finding. |

---

## 10. Out of scope

Explicitly not building: real PG integration, webhook ingestion, customer messaging, KYC, merchant auth, multi-tenancy, billing. This is a simulator. When it convinces someone, then build the product.

---

## 11. Open questions to resolve with primary sources

Flag these in `assumptions.yaml` as `confidence: estimate` until verified:

- Current RBI e-mandate AFA threshold and pre-debit notification rules (check the latest circular, not blog summaries)
- **Does a retry of an already-notified charge require fresh notice? — largest single regulatory risk in the model. Unverified.** The simulator ships assuming it does not (`compliance.pre_debit_notification.retry_inherits_original_notice: true`). If it does, every sub-24h retry is illegal, the fast technical retry disappears, and reported lift falls. Phase 7 must show the headline number under both settings side by side, permanently.
- NPCI per-bank TD% and uptime — pull the actual monthly published file
- Retry attempt caps per rail — is there a hard NPCI limit or is it PG policy?
- eNACH clearing calendar and presentation cutoffs
- Realistic reason-code mix by vertical — **unknown until a pilot merchant shares data. This is the largest uncertainty in the model and must be labelled as such on the dashboard.**

### Resolved by measurement, phase 7

- **Per-mandate attempt history cannot identify a customer's payroll date.** Not a tuning problem and not fixable by a better estimator: it is unidentifiable from this data. Every mandate bills on one calendar day, so the observable history is one day-of-month repeated. The likelihood is then maximised by placing payroll immediately before that day — because a debit is likeliest to succeed just after a credit — regardless of when payroll actually is. Measured over 1,191 mandates: the estimate lands on the mandate's own **billing** day 93.7% of the time, and against the true salary day is within 2 days only 14.6% of the time, *worse than the 17.9% a uniform guess over 28 candidate days achieves*. On the 6.3% of mandates whose attempt history is varied enough to pull the estimate off the billing day, accuracy rises to 29.3% within 2 days — weak signal, not none, and confined to a minority.

  **Consequence: salary-timing requires an external data source** (bank statement / account-aggregator consent, payroll-date declaration at signup, or an issuer signal). It cannot be recovered from payment telemetry alone, and no amount of history fixes it — more cycles supply more copies of the same uninformative day.

  `SalaryAware` and `strategies/salary_inference.py` are **kept deliberately**, along with `test_a_single_billing_day_collapses_the_estimate_onto_that_day`, as executable documentation of why. Do not delete them to tidy up, and do not report `SalaryAware`'s measured lift as evidence that inferred salary timing works — a `BillingDayAnchor` control that skips inference entirely scores exactly zero lift, so what the estimator mostly does is reproduce the baseline.

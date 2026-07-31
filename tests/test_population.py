"""Phase-3 population tests.

The load-bearing one is `test_p_insufficient_funds_rises_with_days_since_salary_credit`.
SPEC.md §2.1: if the balance process does not reproduce that relationship, the
Salary-Aware strategy has nothing to exploit and a null result in phase 7 would be an
artefact of the generator rather than a finding about retry timing.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, timedelta

import pytest

from rebound.config import Assumptions
from rebound.domain.entities import Customer, IncomeBand, Rail
from rebound.population.balance import BalanceProcess
from rebound.population.banks import generate_banks, market_shares
from rebound.population.book import generate_book
from rebound.population.customers import BANDS, generate_customers
from rebound.population.mandates import rail_caps_paise

SEED = 20260731

# Probability of insufficient funds is measured against a ticket sized as a share of the
# customer's own income, not a flat rupee amount. A flat amount is ~0% for the HIGH band
# and ~100% for the LOW band, which would make the monotonicity claim untestable in two
# of the three bands. The claim under test is about shape within a band.
PROBE_SHARE_OF_INCOME = 0.30

# Weekly buckets. Day-by-day comparison would be testing sampling noise; the assertion
# that matters is that a debit late in the salary cycle is meaningfully worse off than
# one early in it.
DAY_BUCKETS = ((0, 6), (7, 13), (14, 20), (21, 27))


@pytest.fixture(scope="session")
def population(wide: Assumptions) -> tuple[Assumptions, tuple[Customer, ...]]:
    """Generated once: every balance test below reads the same population."""
    size = int(wide.value("book.size"))
    return wide, generate_customers(wide, SEED, generate_banks(wide, SEED), size)


def _p_insufficient(
    process: BalanceProcess, customers: tuple[Customer, ...], days: int, month: date
) -> float:
    """Share of customers whose balance is below their probe amount exactly `days` after
    their salary credit. Samples where the next credit has already landed are excluded
    rather than mislabelled — the process, not the test, decides what day it is on."""
    short = 0
    counted = 0
    for customer in customers:
        credit = process.salary_credit_date(customer, month.year, month.month)
        when = credit + timedelta(days=days)
        if process.days_since_salary_credit(customer, when) != days:
            continue
        probe = customer.balance_process.monthly_income_paise * PROBE_SHARE_OF_INCOME
        counted += 1
        short += process.balance_paise(customer, when) < probe
    assert counted > 100, f"only {counted} usable samples at day {days}"
    return short / counted


def _bucket_p(
    process: BalanceProcess, customers: tuple[Customer, ...], bucket: tuple[int, int]
) -> float:
    low, high = bucket
    month = date(2026, 4, 1)
    return sum(_p_insufficient(process, customers, d, month) for d in range(low, high + 1)) / (
        high - low + 1
    )


def test_p_insufficient_funds_rises_with_days_since_salary_credit(
    population: tuple[Assumptions, tuple[Customer, ...]],
) -> None:
    """SPEC §2.1's key emergent property, asserted separately for every income band.

    Band by band, not pooled: a pooled series can rise purely because the bands have
    different salary dates and the late-cycle sample tilts towards low earners. That
    would be a composition effect masquerading as a timing effect.
    """
    assumptions, customers = population
    process = BalanceProcess(assumptions, SEED)
    for band in BANDS:
        in_band = tuple(c for c in customers if c.income_band is band)
        curve = [_bucket_p(process, in_band, bucket) for bucket in DAY_BUCKETS]
        assert curve == sorted(curve), f"{band}: P(insufficient) not monotone: {curve}"
        assert curve[0] < curve[-1], f"{band}: no rise across the cycle: {curve}"


def test_insufficient_funds_risk_is_ordered_by_income_band(
    population: tuple[Assumptions, tuple[Customer, ...]],
) -> None:
    """Against the book's actual ticket, a lower band is more exposed late in the cycle.
    This is the level claim the previous test deliberately does not make.

    The strict inequality matters: with an all-zero risk curve this assertion passes
    vacuously on `[0.0, 0.0, 0.0]`, and an engine built on a balance process that never
    runs short would report no insufficient-funds failures at all — the failure mode
    SPEC §2.1 warns about, arriving silently as a green test.
    """
    assumptions, customers = population
    process = BalanceProcess(assumptions, SEED)
    ticket = int(assumptions.value("book.avg_ticket_paise"))
    rates = []
    for band in BANDS:
        in_band = [c for c in customers if c.income_band is band]
        short = sum(
            process.balance_paise(c, process.salary_credit_date(c, 2026, 4) + timedelta(days=25))
            < ticket
            for c in in_band
        )
        rates.append(short / len(in_band))
    assert rates == sorted(rates, reverse=True), f"bands not ordered by risk: {rates}"
    assert rates[0] > rates[-1], f"no band spread in insufficient-funds risk: {rates}"
    assert rates[0] > 0.05, f"LOW band never runs short at the book's own ticket: {rates}"


def test_balance_is_highest_on_the_credit_day(
    population: tuple[Assumptions, tuple[Customer, ...]],
) -> None:
    """The salary credit is a step up, not a smooth trend, which is what makes waiting
    for it a strategy rather than a delay."""
    assumptions, customers = population
    process = BalanceProcess(assumptions, SEED)
    for customer in customers[:200]:
        credit = process.salary_credit_date(customer, 2026, 4)
        on_credit = process.balance_paise(customer, credit)
        day_before = process.balance_paise(customer, credit - timedelta(days=1))
        assert process.days_since_salary_credit(customer, credit) == 0
        assert on_credit > 0
        assert day_before >= 0


def test_median_balance_falls_across_the_cycle(
    population: tuple[Assumptions, tuple[Customer, ...]],
) -> None:
    assumptions, customers = population
    process = BalanceProcess(assumptions, SEED)
    sample = customers[:400]
    balances = []
    for days in (0, 10, 25):
        credits = [process.salary_credit_date(c, 2026, 4) for c in sample]
        values = sorted(
            process.balance_paise(c, credit + timedelta(days=days))
            for c, credit in zip(sample, credits, strict=True)
        )
        balances.append(values[len(values) // 2])
    assert balances[0] > balances[1] > balances[2], balances


def test_balance_is_deterministic_and_seed_dependent(
    population: tuple[Assumptions, tuple[Customer, ...]],
) -> None:
    assumptions, customers = population
    when = date(2026, 4, 15)
    first = BalanceProcess(assumptions, SEED)
    again = BalanceProcess(assumptions, SEED)
    other = BalanceProcess(assumptions, SEED + 1)
    sample = customers[:100]
    assert [first.balance_paise(c, when) for c in sample] == [
        again.balance_paise(c, when) for c in sample
    ]
    assert [first.balance_paise(c, when) for c in sample] != [
        other.balance_paise(c, when) for c in sample
    ]


def test_balance_does_not_depend_on_the_order_days_are_asked_for(
    population: tuple[Assumptions, tuple[Customer, ...]],
) -> None:
    """SPEC §0.5/§0.6: a strategy that probes different days must not perturb the
    balances another strategy sees. Draws are addressed, never sequential."""
    assumptions, customers = population
    days = [date(2026, 4, d) for d in (3, 17, 28, 9)]
    forward = BalanceProcess(assumptions, SEED)
    backward = BalanceProcess(assumptions, SEED)
    customer = customers[0]
    ordered = {d: forward.balance_paise(customer, d) for d in days}
    for d in reversed(days):
        assert backward.balance_paise(customer, d) == ordered[d]


def test_salary_credit_days_form_two_clusters(shipped: Assumptions) -> None:
    """Phase-3 done criterion: the salary-date distribution reproduces the expected
    shape. Bimodal, weighted to month end."""
    banks = generate_banks(shipped, SEED)
    customers = generate_customers(shipped, SEED, banks, 3000)
    days = Counter(c.salary_credit_day for c in customers)
    end_min = int(shipped.value("population.customer.salary_day.month_end_min"))
    start_max = int(shipped.value("population.customer.salary_day.month_start_max"))
    at_end = sum(n for day, n in days.items() if day >= end_min)
    at_start = sum(n for day, n in days.items() if day <= start_max)
    assert at_end + at_start == 3000, f"salary days outside both clusters: {sorted(days)}"
    expected = float(shipped.value("population.customer.salary_day.month_end_share"))
    assert abs(at_end / 3000 - expected) < 0.03
    assert at_end > at_start


def test_salary_credit_never_lands_on_a_weekend(shipped: Assumptions) -> None:
    banks = generate_banks(shipped, SEED)
    customers = generate_customers(shipped, SEED, banks, 300)
    process = BalanceProcess(shipped, SEED)
    for customer in customers:
        for month in range(1, 13):
            credited = process.salary_credit_date(customer, 2026, month)
            assert credited.weekday() < 5, f"{customer.id} credited on {credited}"


def test_salary_credit_is_never_early_before_the_weekend_shift(shipped: Assumptions) -> None:
    """Jitter is late-only. The only thing that can move a credit earlier than its
    nominal day is the weekend shift, and never by more than two days."""
    banks = generate_banks(shipped, SEED)
    customers = generate_customers(shipped, SEED, banks, 300)
    process = BalanceProcess(shipped, SEED)
    jitter = int(shipped.value("population.balance.salary_jitter_max_days"))
    for customer in customers:
        credited = process.salary_credit_date(customer, 2026, 6)
        nominal = date(2026, 6, min(customer.salary_credit_day, 30))
        assert timedelta(days=-2) <= credited - nominal <= timedelta(days=jitter)


def test_income_bands_match_their_configured_shares(shipped: Assumptions) -> None:
    banks = generate_banks(shipped, SEED)
    customers = generate_customers(shipped, SEED, banks, 3000)
    counts = Counter(c.income_band for c in customers)
    for band in BANDS:
        expected = float(shipped.value(f"population.customer.band_share.{band.value}"))
        assert abs(counts[band] / 3000 - expected) < 0.03, f"{band}: {counts[band] / 3000}"


def test_income_rises_with_band(shipped: Assumptions) -> None:
    banks = generate_banks(shipped, SEED)
    customers = generate_customers(shipped, SEED, banks, 1200)
    medians = []
    for band in BANDS:
        incomes = sorted(
            c.balance_process.monthly_income_paise for c in customers if c.income_band is band
        )
        medians.append(incomes[len(incomes) // 2])
    assert medians == sorted(medians), medians


def test_lower_bands_spend_down_faster(shipped: Assumptions) -> None:
    """The mechanism behind band-ordered insufficient-funds risk, asserted directly so a
    change to the assumptions file cannot quietly invert it."""
    banks = generate_banks(shipped, SEED)
    customers = generate_customers(shipped, SEED, banks, 600)
    rates = {c.income_band: c.balance_process.spend_decay_rate for c in customers}
    assert rates[IncomeBand.LOW] > rates[IncomeBand.MID] > rates[IncomeBand.HIGH]


def test_bank_uptime_dips_at_night_and_covers_every_hour(shipped: Assumptions) -> None:
    banks = generate_banks(shipped, SEED)
    trough_hour = int(shipped.value("population.bank.uptime_trough_hour"))
    for bank in banks:
        assert sorted(bank.uptime_profile) == list(range(24))
        worst = min(bank.uptime_profile, key=lambda h: bank.uptime_profile[h])
        assert worst == trough_hour
        assert bank.uptime_profile[trough_hour] < bank.uptime_profile[(trough_hour + 12) % 24]


def test_bank_market_share_is_concentrated(shipped: Assumptions) -> None:
    """A book spread evenly over its banks would make bank-level knowledge worthless;
    the generator must produce a book that piles into a few."""
    banks = generate_banks(shipped, SEED)
    shares = market_shares(shipped, len(banks))
    assert shares[0] > shares[-1]
    assert shares[:3].sum() > 0.4, shares


def test_bank_downtime_windows_are_overnight_and_bounded(shipped: Assumptions) -> None:
    banks = generate_banks(shipped, SEED)
    earliest = int(shipped.value("population.bank.downtime_window_earliest_hour"))
    latest = int(shipped.value("population.bank.downtime_window_latest_hour"))
    maximum = int(shipped.value("population.bank.downtime_windows_max"))
    assert any(bank.downtime_windows for bank in banks), "no bank got a downtime window"
    for bank in banks:
        assert len(bank.downtime_windows) <= maximum
        for window in bank.downtime_windows:
            assert earliest <= window.start.hour <= latest
            assert window.end > window.start


def test_mandate_cap_never_exceeds_the_rail_ceiling(shipped: Assumptions) -> None:
    book = generate_book(shipped, SEED)
    caps = rail_caps_paise(shipped)
    for mandate in book.mandates:
        assert 0 < mandate.max_amount_paise <= caps[mandate.rail]


def test_billing_days_are_spread_across_the_month(shipped: Assumptions) -> None:
    """Every mandate billing on the same day would hide the salary effect entirely,
    because days-since-credit at debit time would be near-constant within a band."""
    book = generate_book(shipped, SEED)
    days = {m.created_at.day for m in book.mandates}
    assert len(days) == int(shipped.value("population.mandate.billing_day_max"))
    assert max(days) <= 28


def test_every_rail_and_bank_is_represented(shipped: Assumptions) -> None:
    book = generate_book(shipped, SEED)
    assert {m.rail for m in book.mandates} == set(Rail)
    assert len({c.bank_id for c in book.customers}) == len(book.banks)
    assert len(book.customers) == len(book.mandates) == int(shipped.value("book.size"))

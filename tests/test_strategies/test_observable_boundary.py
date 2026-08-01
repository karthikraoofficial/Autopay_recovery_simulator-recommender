"""SPEC §4: the ground-truth boundary, enforced by type rather than by discipline.

A strategy that reads the true balance or the true salary day produces lift that can
never be delivered to a merchant. Reviewing for that by reading code fails silently, so
the boundary is a distinct type in the strategy signature and these tests assert the
type stays distinct.

The project has no static type checker in its stack (SPEC §7 fixes the stack, and adding
one is not ours to decide), so `get_type_hints` on the Protocol is what stands in for
one: if someone widens the signature to accept a Customer, this fails.
"""

from __future__ import annotations

import inspect
import typing
from datetime import UTC, datetime

import pytest

from rebound.domain.entities import (
    BalanceProcessParams,
    Customer,
    IncomeBand,
    Mandate,
    Rail,
)
from rebound.strategies.base import CustomerObservable, RetryStrategy
from rebound.strategies.fixed_schedule import FixedSchedule
from rebound.strategies.no_retry import NoRetry
from rebound.strategies.reason_aware import ReasonAware

GROUND_TRUTH_FIELDS = (
    "balance",
    "balance_paise",
    "balance_process",
    "salary_credit_day",
    "income_band",
    "intent_score",
    "monthly_income_paise",
)

STRATEGY_CLASSES = (NoRetry, FixedSchedule, ReasonAware)


def _customer() -> Customer:
    return Customer(
        id="cust-000000",
        bank_id="bank-000",
        salary_credit_day=28,
        income_band=IncomeBand.LOW,
        balance_process=BalanceProcessParams(
            monthly_income_paise=2_500_000, spend_decay_rate=0.075, lognormal_sigma=0.55
        ),
        intent_score=0.9,
    )


def _mandate() -> Mandate:
    return Mandate(
        id="mandate-000000",
        merchant_id="merchant-000",
        customer_id="cust-000000",
        rail=Rail.UPI_AUTOPAY,
        max_amount_paise=1_000_000,
        created_at=datetime(2026, 1, 15, tzinfo=UTC),
    )


def test_the_protocol_signature_names_customer_observable_not_customer() -> None:
    """The boundary is the signature. Widening it to Customer would hand every strategy
    the true balance, and no other test in this file would notice."""
    hints = typing.get_type_hints(RetryStrategy.propose_retries)
    assert hints["customer_view"] is CustomerObservable
    assert Customer not in hints.values()


@pytest.mark.parametrize("strategy_class", STRATEGY_CLASSES)
def test_every_strategy_annotates_the_observable_type(strategy_class: type) -> None:
    hints = typing.get_type_hints(strategy_class.propose_retries)
    assert hints["customer_view"] is CustomerObservable, (
        f"{strategy_class.__name__} does not take the observable type"
    )


@pytest.mark.parametrize("field", GROUND_TRUTH_FIELDS)
def test_the_observable_has_no_ground_truth_field(field: str) -> None:
    assert field not in CustomerObservable.model_fields


@pytest.mark.parametrize("field", GROUND_TRUTH_FIELDS)
def test_ground_truth_cannot_be_smuggled_onto_the_observable(field: str) -> None:
    """`extra='forbid'` and `frozen=True` together mean a strategy cannot be handed a
    view with an extra attribute bolted on, and cannot bolt one on itself."""
    view = CustomerObservable.from_customer(_customer(), _mandate())
    with pytest.raises(ValueError):
        CustomerObservable(**{**view.model_dump(), field: 1})
    with pytest.raises((ValueError, AttributeError)):
        setattr(view, field, 1)


def test_reading_ground_truth_off_the_view_raises() -> None:
    view = CustomerObservable.from_customer(_customer(), _mandate())
    for field in GROUND_TRUTH_FIELDS:
        with pytest.raises(AttributeError):
            getattr(view, field)


def test_from_customer_forwards_only_observable_fields() -> None:
    """The single place the boundary is crossed. Everything it emits must be something a
    real merchant's system could already know."""
    customer = _customer()
    view = CustomerObservable.from_customer(customer, _mandate())
    assert view.customer_id == customer.id
    assert view.bank_id == customer.bank_id
    assert view.rail is Rail.UPI_AUTOPAY
    assert view.inferred_salary_day is None
    emitted = set(view.model_dump())
    assert emitted.isdisjoint(GROUND_TRUTH_FIELDS)


def test_inferred_salary_day_is_not_the_true_salary_day() -> None:
    """SPEC §4: an inferred salary day must come from a strategy's own reasoning over
    past attempts. `from_customer` sees the true day and must not pass it through, or
    SalaryAware in phase 7 would score against ground truth it could never have."""
    customer = _customer()
    view = CustomerObservable.from_customer(customer, _mandate())
    assert view.inferred_salary_day is None
    # Comment lines stripped: the method documents at length that it reads the true
    # salary day and deliberately does not forward it, and that prose must not be what
    # satisfies the check.
    code_lines = [
        line
        for line in inspect.getsource(CustomerObservable.from_customer).splitlines()
        if not line.strip().startswith("#")
    ]
    assert "customer.salary_credit_day" not in "\n".join(code_lines)


@pytest.mark.parametrize("strategy_class", STRATEGY_CLASSES)
def test_no_strategy_module_imports_ground_truth_types(strategy_class: type) -> None:
    """A strategy that never receives a Customer could still import the balance process
    and reconstruct one. Nothing in the package should let it."""
    module = inspect.getmodule(strategy_class)
    assert module is not None
    source = inspect.getsource(module)
    for forbidden in ("BalanceProcess", "from rebound.population", "from rebound.engine"):
        assert forbidden not in source, f"{strategy_class.__name__} reaches for {forbidden}"

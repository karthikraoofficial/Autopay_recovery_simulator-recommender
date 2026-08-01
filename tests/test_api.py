"""Phase 8. SPEC §7's three endpoints and §6's three sections.

The tests that matter here are not about HTTP. They are that the six inputs actually
reach the simulation, that the headline cannot be served as one number, and that a run is
still reproducible once it has been through a web form.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from rebound.api.app import create_app
from rebound.api.inputs import FailureMix, MerchantProfile, Sizing, sizing_plan
from rebound.api.service import SimulationResult, assumptions_view, simulate, strategy_descriptions
from rebound.config import Assumptions

MASTER_SEED = 20260801


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(create_app())


@pytest.fixture
def profile() -> MerchantProfile:
    return MerchantProfile(
        book_size=40,
        avg_ticket_inr=499.0,
        upi_autopay_share=0.55,
        enach_share=0.30,
        failure_mix=FailureMix.CURRENT,
        performance_fee_rate=0.15,
    )


@pytest.fixture(scope="module")
def tiny(shipped: Assumptions) -> Assumptions:
    """Small enough to run the whole seven-strategy comparison a few times in a suite."""
    return shipped.with_values(
        {"book.months": 4, "population.bank.count": 4, "api.interactive_seeds": 2}
    )


@pytest.fixture(scope="module")
def result(tiny: Assumptions) -> SimulationResult:
    small = MerchantProfile(
        book_size=40,
        avg_ticket_inr=499.0,
        upi_autopay_share=0.55,
        enach_share=0.30,
        performance_fee_rate=0.15,
    )
    return simulate(small, MASTER_SEED, Sizing.INTERACTIVE, tiny)


# --- the six inputs reach the simulation --------------------------------------


def test_every_field_of_the_profile_overrides_a_real_assumption(
    profile: MerchantProfile, shipped: Assumptions
) -> None:
    """A field that maps to no key would be a control on the dashboard that moves no
    number, which is exactly what phase 8 removed `vertical` for."""
    configured = shipped.with_values(profile.overrides(shipped))
    assert configured.value("book.size") == 40
    assert configured.value("book.avg_ticket_paise") == 49900
    assert configured.value("harness.performance_fee_rate") == 0.15
    assert configured.value("population.mandate.rail_mix.upi_autopay") == 0.55


def test_the_card_share_absorbs_the_remainder_so_the_mix_always_sums_to_one(
    shipped: Assumptions,
) -> None:
    profile = MerchantProfile(
        book_size=100,
        avg_ticket_inr=100.0,
        upi_autopay_share=0.7,
        enach_share=0.1,
        performance_fee_rate=0.1,
    )
    assert profile.card_emandate_share == pytest.approx(0.2)
    values = profile.overrides(shipped)
    rails = ("upi_autopay", "enach", "card_emandate")
    total = sum(values[f"population.mandate.rail_mix.{r}"] for r in rails)
    assert total == pytest.approx(1.0)


def test_rail_shares_over_one_are_rejected() -> None:
    with pytest.raises(ValueError, match="must not exceed"):
        MerchantProfile(
            book_size=100,
            avg_ticket_inr=100.0,
            upi_autopay_share=0.8,
            enach_share=0.5,
            performance_fee_rate=0.1,
        )


def test_each_failure_mix_preset_changes_the_engine(shipped: Assumptions) -> None:
    """A preset that overrode nothing would be a selector with three identical settings."""
    base = MerchantProfile(
        book_size=100,
        avg_ticket_inr=499.0,
        upi_autopay_share=0.5,
        enach_share=0.3,
        performance_fee_rate=0.15,
    )
    configured = {
        mix: base.model_copy(update={"failure_mix": mix}).configure(
            shipped, sizing_plan(shipped, Sizing.INTERACTIVE, 100)
        )
        for mix in FailureMix
    }
    sigmas = {
        mix: a.value("population.balance.cushion_lognormal_sigma") for mix, a in configured.items()
    }
    assert len(set(sigmas.values())) == len(FailureMix)
    # Thin balances in one direction, reliable banks in the other. If these ever invert,
    # the presets are mislabelled and the dashboard's mix selector lies.
    assert sigmas[FailureMix.INSUFFICIENT_FUNDS_DOMINANT] > sigmas[FailureMix.CURRENT]
    assert sigmas[FailureMix.TECHNICAL_DOMINANT] < sigmas[FailureMix.CURRENT]
    technical = configured[FailureMix.TECHNICAL_DOMINANT]
    assert technical.value("population.bank.td_rate_max") > shipped.value(
        "population.bank.td_rate_max"
    )


def test_an_unknown_assumption_key_is_refused(shipped: Assumptions) -> None:
    """Silently accepting it would report the run as if the input had been applied."""
    with pytest.raises(KeyError, match="no assumption"):
        shipped.with_values({"book.szie": 10})


def test_overriding_a_value_keeps_its_source_and_confidence(shipped: Assumptions) -> None:
    """The dashboard renders the note beside a number the caller supplied, so provenance
    must survive the override."""
    before = shipped.assumptions["book.size"]
    after = shipped.with_values({"book.size": 999}).assumptions["book.size"]
    assert after.value == 999
    assert (after.source, after.confidence, after.notes) == (
        before.source,
        before.confidence,
        before.notes,
    )


# --- sizing is a compute choice, not a modelling one --------------------------


def test_publication_sizing_runs_more_seeds_than_interactive(shipped: Assumptions) -> None:
    interactive = sizing_plan(shipped, Sizing.INTERACTIVE, 2000)
    publication = sizing_plan(shipped, Sizing.PUBLICATION, 2000)
    assert publication.n_seeds > interactive.n_seeds
    assert interactive.book_size == 2000


def test_the_simulated_book_size_is_reported_not_the_requested_one(
    result: SimulationResult,
) -> None:
    """Publication sizing measures a different book from the one the merchant typed. The
    response says which, so the chart cannot be read as their book without noticing."""
    assert result.book_size_simulated == 40
    assert result.n_seeds >= 2


# --- the headline cannot be served as one number ------------------------------


def test_the_result_carries_two_line_items_and_no_total(result: SimulationResult) -> None:
    """The structural guarantee, asserted rather than trusted to review. Phase 7 measured
    the rescheduling half to be several times the strategy half, so a combined field would
    sell scheduler plumbing as intelligence."""
    fields = set(SimulationResult.model_fields)
    assert {"rescheduling", "strategy"} <= fields
    assert not {"total", "headline_lift", "combined", "total_lift_inr"} & fields
    assert result.rescheduling.label != result.strategy.label


def test_every_reported_figure_carries_an_interval(result: SimulationResult) -> None:
    """CLAUDE.md: never a point estimate without an interval."""
    for line in (result.rescheduling, result.strategy):
        assert line.net_low_inr <= line.net_point_inr <= line.net_high_inr
        assert line.gross_low_inr <= line.gross_point_inr <= line.gross_high_inr
        assert 0.0 < line.level < 1.0
    for strategy in result.strategies:
        assert strategy.recovery_rate_low <= strategy.recovery_rate_point
        assert strategy.recovery_rate_point <= strategy.recovery_rate_high


def test_significance_is_false_when_the_interval_spans_zero(result: SimulationResult) -> None:
    """What the dashboard reads to decide whether to render a bar as a positive result.
    Under the technical-dominant mix this is expected to be false for the strategy line,
    and the UI must say so rather than draw a bar."""
    line = result.strategy.model_copy(update={"net_low_inr": -100.0, "net_high_inr": 200.0})
    assert not line.significant
    assert line.model_copy(update={"net_low_inr": 10.0}).significant


# --- the chart ----------------------------------------------------------------


def test_the_monthly_series_covers_the_whole_horizon(
    result: SimulationResult, tiny: Assumptions
) -> None:
    months = int(tiny.value("book.months"))
    assert len(result.months) == months
    assert [m.month for m in result.months] == list(range(1, months + 1))


def test_the_monthly_series_sums_to_the_strategy_total(
    result: SimulationResult, tiny: Assumptions
) -> None:
    """The chart and the table must be the same measurement. If these drift, one of them
    is decoration."""
    headline_total = sum(m.headline_inr for m in result.months)
    headline = next(s for s in result.strategies if s.name == result.headline)
    fee = float(tiny.value("harness.performance_fee_rate"))
    assert headline_total * (1.0 - fee) == pytest.approx(headline.net_point_inr, rel=0.01)


# --- reproducibility through the web form -------------------------------------


def test_the_same_request_twice_gives_the_same_output_hash(
    profile: MerchantProfile, tiny: Assumptions
) -> None:
    """SPEC §0.5. A number a merchant cannot reproduce is not evidence, and going through
    an API is exactly where a wall-clock seed would creep in."""
    first = simulate(profile, MASTER_SEED, Sizing.INTERACTIVE, tiny)
    second = simulate(profile, MASTER_SEED, Sizing.INTERACTIVE, tiny)
    assert first.output_hash == second.output_hash
    assert first.model_dump() == second.model_dump()


def test_a_different_seed_gives_a_different_run(
    profile: MerchantProfile, tiny: Assumptions
) -> None:
    first = simulate(profile, MASTER_SEED, Sizing.INTERACTIVE, tiny)
    second = simulate(profile, MASTER_SEED + 1000, Sizing.INTERACTIVE, tiny)
    assert first.output_hash != second.output_hash


# --- endpoints ----------------------------------------------------------------


def test_simulate_endpoint_returns_both_line_items(client: TestClient) -> None:
    response = client.post(
        "/simulate",
        json={
            "profile": {
                "book_size": 30,
                "avg_ticket_inr": 499.0,
                "upi_autopay_share": 0.55,
                "enach_share": 0.30,
                "failure_mix": "current",
                "performance_fee_rate": 0.15,
            },
            "sizing": "interactive",
            "master_seed": MASTER_SEED,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert set(body["rescheduling"]) >= {"net_low_inr", "net_high_inr", "net_point_inr"}
    assert body["rescheduling"]["label"] != body["strategy"]["label"]
    assert body["n_seeds"] >= 1


def test_simulate_rejects_an_impossible_rail_mix(client: TestClient) -> None:
    response = client.post(
        "/simulate",
        json={
            "profile": {
                "book_size": 30,
                "avg_ticket_inr": 499.0,
                "upi_autopay_share": 0.9,
                "enach_share": 0.9,
                "performance_fee_rate": 0.15,
            }
        },
    )
    assert response.status_code == 422


def test_assumptions_endpoint_renders_source_and_confidence_for_every_key(
    client: TestClient, shipped: Assumptions
) -> None:
    """SPEC §6.3: always visible, never behind a click. Every key, not a curated subset."""
    body = client.get("/assumptions").json()
    assert len(body["assumptions"]) == len(shipped.assumptions)
    for line in body["assumptions"]:
        assert line["source"] and line["unit"]
        assert line["confidence"] in {"primary", "practitioner", "estimate"}


def test_every_estimate_reaches_the_dashboard_with_its_open_question() -> None:
    """SPEC §0.1 requires an unsourced number to be admitted to. The admission is only
    worth anything if it is rendered next to the number."""
    view = assumptions_view()
    unadmitted = [
        a.key for a in view.assumptions if a.confidence == "estimate" and not a.open_question
    ]
    assert unadmitted == []
    assert view.estimate_count > 0


def test_unkeyed_open_questions_are_not_dropped() -> None:
    """SPEC §11 lists questions with no assumption key of their own. A view that only
    walked the assumptions would silently lose them."""
    assert assumptions_view().unkeyed_open_questions


def test_strategies_endpoint_lists_the_references_as_well_as_the_strategies(
    client: TestClient,
) -> None:
    """The three reference points are not decoration: without NoReschedule there is no
    rescheduling line item, and the headline collapses back into one number."""
    names = {s["name"] for s in client.get("/strategies").json()}
    assert {"NoRetry", "NoReschedule", "FixedSchedule", "Blended"} <= names
    assert len(strategy_descriptions()) == len(names)

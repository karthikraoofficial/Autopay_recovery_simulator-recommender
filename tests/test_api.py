"""Phase 8. SPEC §7's three endpoints and §6's three sections.

The tests that matter here are not about HTTP. They are that the six inputs actually
reach the simulation, that the headline cannot be served as one number, and that a run is
still reproducible once it has been through a web form.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from rebound.api.app import create_app
from rebound.api.inputs import (
    BookSizeTooLargeError,
    FailureMix,
    MerchantProfile,
    Sizing,
    sizing_plan,
)
from rebound.api.jobs import JobStatus
from rebound.api.service import (
    AssumptionsView,
    LiftLine,
    MonthPoint,
    SimulationResult,
    StrategyLine,
    assumptions_view,
    simulate,
    strategy_descriptions,
)
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


def test_the_fast_run_is_actually_faster_than_the_publication_run(
    shipped: Assumptions,
) -> None:
    """The bug this cap was added for. Interactive cut seeds but kept the merchant's book,
    so at the default 2000 mandates the 'fast' run was slower than the 'slow' one and the
    buttons lied about which was which."""
    interactive = sizing_plan(shipped, Sizing.INTERACTIVE, 2000)
    publication = sizing_plan(shipped, Sizing.PUBLICATION, 2000)
    assert interactive.book_size < publication.book_size
    assert interactive.estimated_seconds < publication.estimated_seconds


def test_the_interactive_cap_is_a_ceiling_not_a_replacement(shipped: Assumptions) -> None:
    """A merchant smaller than the cap is simulated whole, so their result is not
    needlessly a fraction of a book they could have had measured exactly."""
    cap = int(shipped.value("api.interactive_book_size"))
    small = sizing_plan(shipped, Sizing.INTERACTIVE, cap // 2)
    assert small.book_size == cap // 2
    assert not small.is_capped
    assert sizing_plan(shipped, Sizing.INTERACTIVE, cap * 10).is_capped


def test_the_publication_run_simulates_the_merchants_actual_book(
    shipped: Assumptions,
) -> None:
    """The whole point of the slow run: the quoted rupee figure is theirs, not an
    extrapolation from a smaller one."""
    plan = sizing_plan(shipped, Sizing.PUBLICATION, 7_500)
    assert plan.book_size == 7_500
    assert not plan.is_capped


def test_an_absurd_publication_book_is_refused_with_the_duration(
    shipped: Assumptions,
) -> None:
    """A typo must not queue a multi-hour run. The message says how long it would take,
    because 'too large' without a number invites simply raising the cap."""
    cap = int(shipped.value("api.publication_book_size_cap"))
    with pytest.raises(BookSizeTooLargeError, match="hours"):
        sizing_plan(shipped, Sizing.PUBLICATION, cap + 1)


def test_nothing_scales_a_capped_result_up_to_the_requested_book(
    result: SimulationResult,
) -> None:
    """SPEC §10 forbids this permanently. Asserted here so a future 'convenience' has to
    delete a test that explains why, rather than quietly adding a multiplication."""
    assert result.book_size_simulated == 40
    assert result.book_size_requested == 40
    assert not result.book_size_capped
    assert "scaled" not in SimulationResult.model_fields
    assert not {"book_size_scaled", "projected_annual_inr"} & set(SimulationResult.model_fields)


# --- the headline cannot be served as one number ------------------------------


def test_the_result_carries_two_line_items_and_no_total(result: SimulationResult) -> None:
    """The structural guarantee, asserted rather than trusted to review. Phase 7 measured
    the rescheduling half to be several times the strategy half, so a combined field would
    sell scheduler plumbing as intelligence."""
    # Computed fields are included: a `@computed_field` summing the two would reach the
    # dashboard exactly like a declared one, and checking only model_fields would miss it.
    fields = set(SimulationResult.model_fields) | set(SimulationResult.model_computed_fields)
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


def _line(low: float, high: float) -> LiftLine:
    """A line item with a chosen net interval. Gross is held strictly positive throughout,
    so a test that passes only because it read the gross interval would still fail on the
    zero-spanning case below."""
    return LiftLine(
        label="Retry strategy (FixedSchedule to Blended)",
        explanation="whatever the retry logic adds",
        gross_low_inr=300.0,
        gross_high_inr=700.0,
        gross_point_inr=500.0,
        net_low_inr=low,
        net_high_inr=high,
        net_point_inr=(low + high) / 2,
        level=0.9,
    )


def test_a_strictly_positive_interval_is_significant_in_the_response_body() -> None:
    """The regression. `significant` was a bare `property`, which Pydantic omits from
    `model_dump`, so the key was missing from the JSON entirely. The dashboard read
    `undefined`, took it as falsy, and rendered 'this interval contains zero' on every
    result — including publication runs like [261, 437] and [207, 583], where it does not.

    Asserted on the serialised body, not the Python object. The old test checked the
    object, passed, and the UI was wrong anyway.
    """
    body = _line(261.0, 437.0).model_dump()
    assert "significant" in body, "significant must be in the payload, not just on the object"
    assert body["significant"] is True
    assert _line(207.0, 583.0).model_dump()["significant"] is True


def test_a_zero_spanning_interval_is_not_significant_in_the_response_body() -> None:
    """The other direction, and the one that must never regress quietly: a lift
    indistinguishable from zero has to be reported as such."""
    body = _line(-100.0, 200.0).model_dump()
    assert body["significant"] is False
    # Touching zero from either side still counts as containing it.
    assert _line(0.0, 400.0).model_dump()["significant"] is False
    assert _line(-400.0, 0.0).model_dump()["significant"] is False
    # A wholly negative interval is significant — the effect is real and it is a loss.
    assert _line(-500.0, -100.0).model_dump()["significant"] is True


def test_significance_reads_the_net_interval_not_the_gross_one() -> None:
    """Net is what the merchant receives, and the fee can be what pushes a positive gross
    result onto zero. Reading the wrong interval would overstate every marginal case."""
    line = LiftLine(
        label="x",
        explanation="y",
        gross_low_inr=100.0,
        gross_high_inr=900.0,
        gross_point_inr=500.0,
        net_low_inr=-50.0,
        net_high_inr=120.0,
        net_point_inr=35.0,
        level=0.9,
    )
    assert line.model_dump()["significant"] is False


def test_significance_survives_a_round_trip_through_http(client: TestClient) -> None:
    """End to end, because the failure was in serialisation rather than in the predicate.
    Whatever the run produces, the flag in the body must agree with its own interval."""
    response = client.post(
        "/simulate",
        json={
            "profile": {
                "book_size": 30,
                "avg_ticket_inr": 499.0,
                "upi_autopay_share": 0.55,
                "enach_share": 0.30,
                "performance_fee_rate": 0.15,
            },
            "master_seed": MASTER_SEED,
        },
    )
    assert response.status_code == 200
    for key in ("rescheduling", "strategy"):
        line = response.json()[key]
        assert "significant" in line
        expected = line["net_low_inr"] > 0 or line["net_high_inr"] < 0
        assert line["significant"] is expected


def test_no_response_model_hides_a_predicate_behind_a_bare_property() -> None:
    """The class of bug, not just this instance.

    A plain `@property` on a Pydantic v2 model is invisible to `model_dump`, so any UI
    reading it gets `undefined`. Every derived value on a model the API returns must be a
    `computed_field`. This is the check that would have caught the significance bug on the
    day it was written.
    """
    returned = (
        LiftLine,
        StrategyLine,
        MonthPoint,
        SimulationResult,
        AssumptionsView,
        JobStatus,
    )
    offenders = [
        f"{model.__name__}.{name}"
        for model in returned
        for name, attribute in vars(model).items()
        if isinstance(attribute, property) and name not in model.model_computed_fields
    ]
    assert offenders == []


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


def test_estimate_costs_a_run_without_running_it(client: TestClient) -> None:
    """Shown before the user commits. It must be cheap, or it defeats its own purpose."""
    body = {
        "profile": {
            "book_size": 20_000,
            "avg_ticket_inr": 499.0,
            "upi_autopay_share": 0.55,
            "enach_share": 0.30,
            "performance_fee_rate": 0.15,
        },
        "sizing": "publication",
    }
    started = time.monotonic()
    response = client.post("/simulate/estimate", json=body)
    assert time.monotonic() - started < 5.0
    assert response.status_code == 200
    estimate = response.json()
    assert estimate["book_size"] == 20_000
    assert estimate["estimated_seconds"] > 60


def test_estimating_an_oversized_publication_run_is_refused(client: TestClient) -> None:
    """Between the profile's own ceiling and the publication cap, so it is the cap that
    rejects this and the message names the duration."""
    response = client.post(
        "/simulate/estimate",
        json={
            "profile": {
                "book_size": 60_000,
                "avg_ticket_inr": 499.0,
                "upi_autopay_share": 0.55,
                "enach_share": 0.30,
                "performance_fee_rate": 0.15,
            },
            "sizing": "publication",
        },
    )
    assert response.status_code == 422
    assert "hours" in response.json()["detail"]


def test_a_job_runs_in_the_background_and_reports_progress(client: TestClient) -> None:
    response = client.post(
        "/simulate/jobs",
        json={
            "profile": {
                "book_size": 30,
                "avg_ticket_inr": 499.0,
                "upi_autopay_share": 0.55,
                "enach_share": 0.30,
                "performance_fee_rate": 0.15,
            },
            "sizing": "interactive",
            "master_seed": MASTER_SEED,
        },
    )
    assert response.status_code == 202
    job_id = response.json()["id"]

    deadline = time.monotonic() + 180
    status = response.json()
    while status["state"] == "running" and time.monotonic() < deadline:
        time.sleep(0.5)
        status = client.get(f"/simulate/jobs/{job_id}").json()

    assert status["state"] == "done", status.get("error")
    assert status["completed_steps"] == status["total_steps"] > 0
    assert status["elapsed_seconds"] > 0
    assert status["result"]["rescheduling"]["label"] != status["result"]["strategy"]["label"]


def test_elapsed_is_measured_and_stops_when_the_run_does(client: TestClient) -> None:
    """Elapsed is wall-clock, not the estimate. The dashboard showed the estimate under an
    'Elapsed' label, so the two are asserted to be different quantities here: elapsed must
    keep its value once the job is finished, and must not simply echo estimated_seconds.
    """
    response = client.post(
        "/simulate/jobs",
        json={
            "profile": {
                "book_size": 30,
                "avg_ticket_inr": 499.0,
                "upi_autopay_share": 0.55,
                "enach_share": 0.30,
                "performance_fee_rate": 0.15,
            },
            "sizing": "interactive",
            "master_seed": MASTER_SEED,
        },
    )
    job_id = response.json()["id"]
    deadline = time.monotonic() + 180
    status = response.json()
    while status["state"] == "running" and time.monotonic() < deadline:
        time.sleep(0.5)
        status = client.get(f"/simulate/jobs/{job_id}").json()
    assert status["state"] == "done", status.get("error")

    finished = status["elapsed_seconds"]
    assert finished > 0
    # A finished job's clock is stopped. If this kept climbing, the panel would show a
    # duration that grew while the user read it.
    time.sleep(1.5)
    assert client.get(f"/simulate/jobs/{job_id}").json()["elapsed_seconds"] == finished


def test_the_result_payload_carries_no_wall_clock_time(result: SimulationResult) -> None:
    """Timing lives on the job, not on the result. SPEC §0.5 promises a byte-identical
    payload for a given seed and config, and a wall-clock field would break that."""
    fields = set(SimulationResult.model_fields) | set(SimulationResult.model_computed_fields)
    assert not {"elapsed_seconds", "started_at", "finished_at", "duration_seconds"} & fields
    # The estimate is fine: it is a function of the config, not of when the run happened.
    assert result.estimated_seconds > 0


def test_an_unknown_job_is_a_404(client: TestClient) -> None:
    assert client.get("/simulate/jobs/nope").status_code == 404


def test_progress_reporting_does_not_change_the_result(
    profile: MerchantProfile, tiny: Assumptions
) -> None:
    """The callback receives counts and returns nothing, so it cannot reach the RNG. If
    observing a run could change it, the output hash would stop meaning anything."""
    seen: list[tuple[int, int]] = []
    watched = simulate(
        profile, MASTER_SEED, Sizing.INTERACTIVE, tiny, progress=lambda d, t: seen.append((d, t))
    )
    silent = simulate(profile, MASTER_SEED, Sizing.INTERACTIVE, tiny)
    assert watched.output_hash == silent.output_hash
    assert seen[-1][0] == seen[-1][1] == len(watched.strategies) * watched.n_seeds
    assert [d for d, _ in seen] == list(range(1, len(seen) + 1))


def test_strategies_endpoint_lists_the_references_as_well_as_the_strategies(
    client: TestClient,
) -> None:
    """The three reference points are not decoration: without NoReschedule there is no
    rescheduling line item, and the headline collapses back into one number."""
    names = {s["name"] for s in client.get("/strategies").json()}
    assert {"NoRetry", "NoReschedule", "FixedSchedule", "Blended"} <= names
    assert len(strategy_descriptions()) == len(names)


# --- downloadable artifacts (phase 8.9) ---------------------------------------


def test_the_result_reports_the_seeds_actually_run(result: SimulationResult) -> None:
    """The dashboard's trace picker offers exactly these. Derived client-side as
    master_seed + i it would drift from `run_experiment`, and /trace would then answer
    with a valid trace of a population the headline never simulated."""
    assert len(result.seeds) == result.n_seeds
    assert result.seeds[0] == result.master_seed
    assert list(result.seeds) == sorted(set(result.seeds))


def test_segments_can_be_served_as_the_rendered_text_report(client: TestClient) -> None:
    """The banner and the plain-language verdicts are the point of the text form: they are
    what stop a segment number being read as a finding it has not earned."""
    response = client.get("/segments", params={"book_size": 40, "format": "text"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "attachment" in response.headers["content-disposition"]
    assert "TESTS PERFORMED" in response.text
    assert "NEGATIVE CONTROL - EXPECTED NULL" in response.text


def test_the_text_report_is_the_same_run_as_the_json(client: TestClient) -> None:
    """One renderer, not two. If the text form ever came from a separate code path it
    could disagree with the JSON while both looked right."""
    params = {"book_size": 40, "master_seed": MASTER_SEED}
    data = client.get("/segments", params=params).json()
    text = client.get("/segments", params={**params, "format": "text"}).text
    assert f"{data['tests_performed']} TESTS PERFORMED" in text
    assert f"total episodes {data['total_episodes']:,}" in text


def test_an_unknown_segment_format_is_refused(client: TestClient) -> None:
    assert client.get("/segments", params={"book_size": 40, "format": "pdf"}).status_code == 422


# --- every derived export must be checkable against its headline --------------

# Routes that RUN the simulation and produce a headline. These emit a fingerprint for
# derived exports to be compared against; they do not consume one.
HEADLINE_ROUTES = {("POST", "/simulate"), ("POST", "/simulate/jobs")}

# Routes that do not run the simulation at all, so there is nothing to diverge.
NON_SIMULATING_ROUTES = {
    ("GET", "/assumptions"),
    ("GET", "/strategies"),
    ("GET", "/simulate/jobs/{job_id}"),
    ("POST", "/simulate/estimate"),
    # SPEC §14. These quote a recorded measurement rather than deriving an artifact from a
    # live run, so there is no headline to carry an expect_config against. The equivalent
    # check is inside the service: `load_evidence` refuses an artefact whose
    # config_fingerprint differs from the running configuration, and 503s rather than
    # quoting it. `test_recommend.py` covers that path.
    ("POST", "/recommend"),
    ("POST", "/recommend/batch"),
    ("GET", "/recommend/template"),
}

# FastAPI's own.
BUILT_IN_ROUTES = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}


def _routes(app) -> list[tuple[str, str, set[str]]]:
    found = []
    for route in app.routes:
        path = getattr(route, "path", None)
        dependant = getattr(route, "dependant", None)
        if path is None or dependant is None or path in BUILT_IN_ROUTES:
            continue
        params = {p.name for p in dependant.query_params}
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            found.append((method, path, params))
    return found


def test_every_export_endpoint_compares_its_config_against_the_headline() -> None:
    """Fails closed. A new endpoint that re-runs the simulation must either declare itself
    above or accept `expect_config`, and until it does this test fails.

    The rule it enforces: any endpoint deriving an artifact from a headline run can diverge
    from it on ANY config dimension, and that divergence is invisible — the seed fixes the
    mandate ids, and a re-run's own output hash differs from the headline's by construction
    anyway. /trace shipped without this and silently traced a population generated at the
    default ticket. Comparing the input config is the only check that catches the general
    case rather than one field at a time.
    """
    undeclared, unguarded = [], []
    for method, path, params in _routes(create_app()):
        if (method, path) in HEADLINE_ROUTES or (method, path) in NON_SIMULATING_ROUTES:
            continue
        if "expect_config" not in params:
            # Two different failures: a route nobody classified, versus one classified as
            # an export and missing the guard. Both fail; the message says which.
            (undeclared if not path.startswith(("/segments", "/trace")) else unguarded).append(
                f"{method} {path}"
            )
    assert unguarded == [], (
        f"export endpoints without an expect_config guard: {unguarded}. "
        "An export derived from a headline run must be refusable when the config differs."
    )
    assert undeclared == [], (
        f"unclassified routes: {undeclared}. Add each to HEADLINE_ROUTES (it produces a "
        "headline), NON_SIMULATING_ROUTES (it does not run the simulation), or give it an "
        "expect_config guard (it derives an artifact from a headline run)."
    )


def test_the_headline_routes_emit_a_fingerprint_to_compare_against() -> None:
    """The other half. A guard on the exports is useless if the headline never publishes
    what they should match."""
    fields = set(SimulationResult.model_fields) | set(SimulationResult.model_computed_fields)
    assert "config_fingerprint" in fields
    # A background run reaches the dashboard as JobStatus.result, so the fingerprint has
    # to survive that hop too — the publication path is exactly where a stale download is
    # most likely, because minutes pass between running and downloading.
    assert JobStatus.model_fields["result"].annotation == SimulationResult | None


def test_a_mismatched_config_is_refused_by_segments_too(client: TestClient) -> None:
    """Symmetric with the /trace guard. A mismatch is a refusal, never a warning printed
    on top of an export someone will use anyway."""
    response = client.get(
        "/segments",
        params={"book_size": 40, "format": "text", "expect_config": "0" * 64},
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "different configuration" in detail
    assert "Nothing is exported" in detail


def test_a_matching_config_is_exported_by_segments(client: TestClient) -> None:
    """The guard must not refuse the good case, or it gets routed around."""
    params = {"book_size": 40, "master_seed": MASTER_SEED}
    fingerprint = client.get("/segments", params=params).json()["config_fingerprint"]
    ok = client.get("/segments", params={**params, "format": "text", "expect_config": fingerprint})
    assert ok.status_code == 200
    assert f"config_fingerprint: {fingerprint}" in ok.text


# --- SPEC §14: the recommendation service -----------------------------------------------
# A sibling of the simulator. These routes run no simulation, so the tests here are about
# what reaches the merchant: a time, a refusal that names its field, and never a prediction.


def _failure_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "mandate_ref": "M-9001",
        "rail": "UPI_AUTOPAY",
        "reason_code": "TECHNICAL_DECLINE",
        "failed_at": "2026-03-10T11:00:00+00:00",
        "amount_paise": 49900,
        "mandate_cap_paise": 150000,
        "attempt_number": 1,
        "notified_at": "2026-02-28T11:00:00+00:00",
        "prior_failure_count": 0,
    }
    payload.update(overrides)
    return payload


def test_recommend_returns_a_time_and_the_rule_behind_it(client: TestClient) -> None:
    response = client.post("/recommend", json=_failure_payload())
    assert response.status_code == 200
    body = response.json()
    assert body["retry_at"] > _failure_payload()["failed_at"]
    assert body["strategy"] and body["rule"]
    assert body["evidence"]["segment_label"]


def test_recommend_refuses_a_missing_minimum_field_by_name(client: TestClient) -> None:
    """SPEC §14.3: a minimum-set field leaves nothing servable, so it is a 422."""
    payload = _failure_payload(rail="ENACH")
    response = client.post("/recommend", json=payload)
    assert response.status_code == 422
    assert "bank_batch_cutoff_time" in response.text


def test_recommend_answers_an_extended_gap_rather_than_erroring(client: TestClient) -> None:
    """SPEC §14.4: a missing extended field is answered, with what it would unlock."""
    body = client.post("/recommend", json=_failure_payload()).json()
    unavailable = [row for row in body["availability"] if not row["available"]]
    assert unavailable and all(row["missing_fields"] for row in unavailable)


def test_recommend_never_returns_a_success_probability(client: TestClient) -> None:
    body = client.post("/recommend", json=_failure_payload()).json()
    assert not [k for k in body if "prob" in k or "score" in k or "expected" in k]


def test_a_hard_decline_is_refused_a_time_over_http(client: TestClient) -> None:
    body = client.post("/recommend", json=_failure_payload(reason_code="MANDATE_REVOKED")).json()
    assert body["retry_at"] is None
    assert body["status"] == "stop, hard decline"


def test_the_batch_template_is_offered_in_both_tiers(client: TestClient) -> None:
    assert client.get("/recommend/template").text.startswith("mandate_ref,rail")
    assert "bank_id" in client.get("/recommend/template", params={"extended": True}).text


def test_batch_answers_per_row_and_counts_its_refusals(client: TestClient) -> None:
    header = client.get("/recommend/template").text.strip()
    tail = (
        "TECHNICAL_DECLINE,2026-03-10T11:00:00+00:00,49900,150000,1,"
        "2026-02-28T11:00:00+00:00,0,,"
    )
    good = f"M-1,UPI_AUTOPAY,{tail}"
    bad = f"M-2,ENACH,{tail}"  # a batch-cleared rail with no cutoff column filled in
    response = client.post("/recommend/batch", content=f"{header}\n{good}\n{bad}\n")
    assert response.status_code == 200
    body = response.json()
    assert body["rows_answered"] == 1
    assert body["rows_refused"] == 1
    assert body["refusals_by_field"] == {"bank_batch_cutoff_time": 1}

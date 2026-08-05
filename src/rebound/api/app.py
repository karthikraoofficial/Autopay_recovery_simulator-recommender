"""SPEC §7's three endpoints. All logic lives in `service.py`; this is transport only.

There is no persistence layer. SPEC §7 offers SQLite via SQLModel for caching runs; the
simulation is stateless and a cache would be the only stateful thing in the project, so
interactive runs are made cheap instead (fewer seeds, wider intervals, both reported).
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from rebound.api.health import Health, health
from rebound.api.inputs import (
    BookSizeTooLargeError,
    FailureMix,
    MerchantProfile,
    Sizing,
    sizing_plan,
)
from rebound.api.jobs import JobRegistry, JobStatus
from rebound.api.service import (
    AssumptionsView,
    SimulationResult,
    StrategyDescription,
    assumptions_view,
    segment_report,
    simulate,
    strategy_descriptions,
)
from rebound.config import load_assumptions
from rebound.harness.segments import SegmentReport, render
from rebound.harness.trace import MultipleSeedsError, Table, Trace, build_trace, to_csv
from rebound.recommend.batch import BatchResult, BatchTooLargeError, run_batch, template

# Aliased: `Table` is already the trace's table selector (SPEC §13.5), and two
# different meanings under one name in one router is how the wrong one gets used.
from rebound.recommend.csv_out import Table as BatchTable
from rebound.recommend.csv_out import to_csv as batch_to_csv
from rebound.recommend.evidence import EvidenceUnavailableError, load_evidence
from rebound.recommend.ingest import FileRefusalError
from rebound.recommend.inputs import ObservedFailure
from rebound.recommend.mapping import (
    MappingError,
    MerchantMapping,
    available_mappings,
    load_mapping,
)
from rebound.recommend.service import Recommendation, recommend

# The Vite dev server. A simulator that runs locally and talks to nothing else does not
# need a configurable origin list, and SPEC §7 says no cloud until a merchant asks.
DEV_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")


class RunEstimate(BaseModel):
    """The cost of a run, without running it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    book_size: int
    requested_book_size: int
    book_size_capped: bool
    n_seeds: int
    estimated_seconds: float


class MappingSummary(BaseModel):
    """A stored mapping profile as the UI needs to see it: what it is and what it hashes to."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    description: str | None = None
    fingerprint: str
    reason_codes: int
    rails: int
    renamed_columns: int


def _mapping_or_422(name: str | None) -> MerchantMapping | None:
    if name is None:
        return None
    try:
        return load_mapping(name)
    except MappingError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _check_expected_mapping(profile: MerchantMapping | None, expected: str | None) -> None:
    """SPEC §15.4, the same shape as `expect_config` on /segments and /trace.

    A fingerprint only helps if something compares it. A merchant re-running a file against
    a mapping they believe unchanged is told that it changed, rather than being left to
    notice a hash. Nothing is answered on a mismatch: a partial answer under an unexpected
    mapping is the outcome the parameter exists to prevent.
    """
    if expected is None:
        return
    if profile is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "expect_mapping was given without a mapping. There is no profile to "
                "compare it against; name one with ?mapping="
            ),
        )
    actual = profile.fingerprint()
    if actual != expected:
        raise HTTPException(
            status_code=409,
            detail=(
                f"the mapping profile '{profile.name}' is not the one this file was "
                f"expected to be read through. Expected {expected[:12]}, the stored "
                f"profile is {actual[:12]}. Nothing was answered. The same file read "
                "through a different vocabulary can produce different recommendations, so "
                "the difference is reported rather than absorbed."
            ),
        )


class SimulateRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    profile: MerchantProfile
    sizing: Sizing = Sizing.INTERACTIVE
    # Explicit and defaulted, never drawn from the clock. Two requests with the same body
    # must return the same output_hash, which is the only claim the dashboard makes that
    # a reader can check for themselves.
    master_seed: int = Field(default=20260801, ge=0)


def create_app() -> FastAPI:
    app = FastAPI(title="rebound", version="0.1.0")
    app.state.jobs = JobRegistry()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(DEV_ORIGINS),
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    @app.post("/simulate/estimate", response_model=RunEstimate)
    def post_estimate(request: SimulateRequest) -> RunEstimate:
        """What a run would cost, before anyone commits to it. Cheap: no simulation."""
        try:
            plan = sizing_plan(load_assumptions(), request.sizing, request.profile.book_size)
        except BookSizeTooLargeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return RunEstimate(
            book_size=plan.book_size,
            requested_book_size=plan.requested_book_size,
            book_size_capped=plan.is_capped,
            n_seeds=plan.n_seeds,
            estimated_seconds=plan.estimated_seconds,
        )

    @app.post("/simulate", response_model=SimulationResult)
    def post_simulate(request: SimulateRequest) -> SimulationResult:
        """Synchronous. Suitable for interactive sizing, which is capped to seconds; a
        publication run on a real book takes minutes and belongs on /simulate/jobs."""
        try:
            return simulate(request.profile, request.master_seed, request.sizing)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/simulate/jobs", response_model=JobStatus, status_code=202)
    def post_job(request: SimulateRequest) -> JobStatus:
        try:
            return app.state.jobs.submit(request.profile, request.master_seed, request.sizing)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/simulate/jobs/{job_id}", response_model=JobStatus)
    def get_job(job_id: str) -> JobStatus:
        status = app.state.jobs.status(job_id)
        if status is None:
            raise HTTPException(status_code=404, detail=f"no job {job_id}")
        return status

    # response_model=None: this route returns either a SegmentReport or a text
    # response, and FastAPI cannot build one response model from that union.
    @app.get("/segments", response_model=None)
    def get_segments(
        book_size: int = 2000,
        avg_ticket_inr: float = 499.0,
        upi_autopay_share: float = 0.55,
        enach_share: float = 0.30,
        failure_mix: FailureMix = FailureMix.CURRENT,
        performance_fee_rate: float = 0.15,
        sizing: Sizing = Sizing.INTERACTIVE,
        master_seed: int = 20260801,
        fmt: str = Query(default="json", pattern="^(json|text)$", alias="format"),
        expect_config: str | None = Query(
            default=None,
            description=(
                "Config fingerprint of the run this report should match. Supplied by the "
                "dashboard; a mismatch is refused rather than exported."
            ),
        ),
    ) -> SegmentReport | PlainTextResponse:
        """SPEC §12. Defaults to interactive sizing: a publication-sized segment run takes
        as long as a publication headline run, and a GET should not hold that open."""
        try:
            profile = MerchantProfile(
                book_size=book_size,
                avg_ticket_inr=avg_ticket_inr,
                upi_autopay_share=upi_autopay_share,
                enach_share=enach_share,
                failure_mix=failure_mix,
                performance_fee_rate=performance_fee_rate,
            )
            report = segment_report(profile, master_seed, sizing)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        # Same guard as /trace. This endpoint re-runs the simulation, so it can diverge
        # from the headline on any config dimension -- not only on the profile, which it
        # does receive, but on anything edited in assumptions.yaml in between.
        if expect_config is not None and expect_config != report.config_fingerprint:
            raise HTTPException(
                status_code=409,
                detail=(
                    "this segment report would describe a different configuration from "
                    f"the run it is meant to explain. Expected config {expect_config[:12]}, "
                    f"this report is {report.config_fingerprint[:12]}. Nothing is exported. "
                    "Re-run the simulation and download again; if assumptions.yaml changed "
                    "in between, the earlier result no longer describes this configuration "
                    "either."
                ),
            )
        if fmt == "text":
            # The rendered report, not a second formatter: the banner and the
            # plain-language verdicts are what stop the numbers being misread.
            return PlainTextResponse(
                render(report),
                media_type="text/plain",
                headers={
                    "Content-Disposition": (
                        f'attachment; filename="rebound-segments-seed{master_seed}.txt"'
                    )
                },
            )
        return report

    # response_model=None because this route returns either a Trace or a CSV response,
    # and FastAPI cannot build one response model from that union.
    @app.get("/trace", response_model=None)
    def get_trace(
        request: Request,
        seed: int = Query(description="Exactly one seed. A trace never spans seeds."),
        table: Table = Table.ATTEMPTS,
        fmt: str = Query(default="json", pattern="^(json|csv)$", alias="format"),
        book_size: int = Query(default=200, gt=0, le=2000),
        # The same six merchant inputs /segments takes. Without them the trace is
        # generated from the shipped defaults and describes a different population from
        # the run it is meant to explain -- invisibly, because the seed fixes the mandate
        # ids whatever the config.
        avg_ticket_inr: float = 499.0,
        upi_autopay_share: float = 0.55,
        enach_share: float = 0.30,
        failure_mix: FailureMix = FailureMix.CURRENT,
        performance_fee_rate: float = 0.15,
        expect_config: str | None = Query(
            default=None,
            description=(
                "Config fingerprint of the run this trace should match. Supplied by the "
                "dashboard; a mismatch is refused rather than exported."
            ),
        ),
    ) -> Trace | PlainTextResponse:
        """SPEC §13. Row-level trace of one seeded book, for inspection and debugging.

        `seed` is a single int by signature, so a comma-joined value fails to parse. A
        *repeated* parameter does not: FastAPI silently binds the last one, which would
        answer `?seed=1&seed=2` with seed 2's trace and no indication the request was not
        honoured. That is the quiet merge SPEC §13.1 forbids, so it is refused explicitly.
        """
        supplied = request.query_params.getlist("seed")
        if len(supplied) != 1:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"a trace covers exactly one seed; {len(supplied)} were given "
                    f"({', '.join(supplied) or 'none'}). A mandate id means nothing "
                    "across seeds, so a merged export would imply a continuity that does "
                    "not exist. Request one trace per seed."
                ),
            )
        try:
            profile = MerchantProfile(
                book_size=book_size,
                avg_ticket_inr=avg_ticket_inr,
                upi_autopay_share=upi_autopay_share,
                enach_share=enach_share,
                failure_mix=failure_mix,
                performance_fee_rate=performance_fee_rate,
            )
            base = load_assumptions()
            configured = base.with_values(profile.overrides(base))
            trace = build_trace(seed, configured, book_size=book_size)
        except (MultipleSeedsError, KeyError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        # Structural check, not a warning. A trace of the wrong population is invisible:
        # the seed fixes the mandate ids, so only the config distinguishes them. Refusing
        # is the only response that cannot be missed.
        if expect_config is not None and expect_config != trace.header.config_fingerprint:
            raise HTTPException(
                status_code=409,
                detail=(
                    "this trace would describe a different population from the run it is "
                    f"meant to explain. Expected config {expect_config[:12]}, this trace "
                    f"is {trace.header.config_fingerprint[:12]}. Nothing is exported. "
                    "Re-run the simulation and download the trace again; if assumptions."
                    "yaml changed in between, the earlier result no longer describes this "
                    "configuration either."
                ),
            )
        if fmt == "csv":
            return PlainTextResponse(
                to_csv(trace, table),
                media_type="text/csv",
                headers={
                    "Content-Disposition": (
                        f'attachment; filename="rebound-trace-{table.value}-seed{seed}.csv"'
                    )
                },
            )
        return trace

    # SPEC §14. A sibling of the simulator: these two routes run no simulation at all.
    # The evidence artefact is loaded per request rather than cached on app state, so a
    # regenerated report is picked up without a restart and a divergent one is refused at
    # the first request rather than at boot.
    @app.post("/recommend", response_model=Recommendation)
    def post_recommend(failure: ObservedFailure) -> Recommendation:
        """SPEC §14: an observed failure in, a retry time and the rule behind it out.

        A missing minimum-set field arrives as a 422 from the input model, because it
        leaves nothing servable. A missing *extended* field is not an error: it is
        answered, with the strategies it would have unlocked named in `availability`.
        """
        try:
            report = load_evidence(load_assumptions())
        except EvidenceUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        try:
            return recommend(failure, report)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/recommend/template", response_class=PlainTextResponse)
    def get_template(extended: bool = False) -> PlainTextResponse:
        """The batch header row, derived from the tier definition in `recommend/inputs.py`."""
        name = "extended" if extended else "minimum"
        return PlainTextResponse(
            template(extended=extended),
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="rebound-recommend-{name}.csv"'
            },
        )

    @app.get("/recommend/mappings", response_model=list[MappingSummary])
    def get_mappings() -> list[MappingSummary]:
        """SPEC §15.3: the stored profiles, with their fingerprints. Never an editor."""
        summaries = []
        for name in available_mappings():
            profile = load_mapping(name)
            summaries.append(
                MappingSummary(
                    name=name,
                    description=profile.description,
                    fingerprint=profile.fingerprint(),
                    reason_codes=len(profile.reason_codes),
                    rails=len(profile.rails),
                    renamed_columns=len(profile.columns),
                )
            )
        return summaries

    # response_model=None: this route returns either a BatchResult or a CSV response, and
    # FastAPI cannot build one response model from that union.
    @app.post("/recommend/batch", response_model=None)
    async def post_recommend_batch(
        request: Request,
        mapping: str | None = Query(
            default=None,
            description=(
                "Name of a stored profile in config/merchants/. Never uploaded with the "
                "file: an unversioned mapping answers the same file differently on two "
                "days with nothing recording why (SPEC §15.3)."
            ),
        ),
        expect_mapping: str | None = Query(
            default=None,
            description=(
                "Fingerprint the named profile is expected to have. A mismatch is refused "
                "rather than answered under a mapping the caller did not expect."
            ),
        ),
        fmt: str = Query(default="json", pattern="^(json|csv)$", alias="format"),
        table: BatchTable = BatchTable.ANSWERS,
    ) -> BatchResult | PlainTextResponse:
        """SPEC §14.9/§15. The body is the CSV itself: a file upload would need a new
        dependency, and the stack is fixed in SPEC §7.

        File-level defects (encoding, delimiter, header) refuse the whole file with one
        message; row-level defects are answered per row.
        """
        body = await request.body()
        try:
            report = load_evidence(load_assumptions())
        except EvidenceUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        profile = _mapping_or_422(mapping)
        _check_expected_mapping(profile, expect_mapping)
        try:
            result = run_batch(body, report, mapping=profile)
        except BatchTooLargeError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        except (FileRefusalError, KeyError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if fmt == "csv":
            return PlainTextResponse(
                batch_to_csv(result, table),
                media_type="text/csv",
                headers={
                    "Content-Disposition": (
                        f'attachment; filename="rebound-recommend-{table.value}.csv"'
                    )
                },
            )
        return result

    @app.get("/health", response_model=Health)
    def get_health() -> Health:
        """What this process is: the commit it was started from, and the config it would
        use next. Exists so a stale process announces itself rather than being inferred
        from which paths return 404, which has pointed at the wrong thing twice."""
        return health()

    @app.get("/assumptions", response_model=AssumptionsView)
    def get_assumptions() -> AssumptionsView:
        return assumptions_view()

    @app.get("/strategies", response_model=list[StrategyDescription])
    def get_strategies() -> list[StrategyDescription]:
        return list(strategy_descriptions())

    return app


app = create_app()

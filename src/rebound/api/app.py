"""SPEC §7's three endpoints. All logic lives in `service.py`; this is transport only.

There is no persistence layer. SPEC §7 offers SQLite via SQLModel for caching runs; the
simulation is stateless and a cache would be the only stateful thing in the project, so
interactive runs are made cheap instead (fewer seeds, wider intervals, both reported).
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field

from rebound.api.inputs import MerchantProfile, Sizing
from rebound.api.service import (
    AssumptionsView,
    SimulationResult,
    StrategyDescription,
    assumptions_view,
    simulate,
    strategy_descriptions,
)

# The Vite dev server. A simulator that runs locally and talks to nothing else does not
# need a configurable origin list, and SPEC §7 says no cloud until a merchant asks.
DEV_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")


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
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(DEV_ORIGINS),
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    @app.post("/simulate", response_model=SimulationResult)
    def post_simulate(request: SimulateRequest) -> SimulationResult:
        try:
            return simulate(request.profile, request.master_seed, request.sizing)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/assumptions", response_model=AssumptionsView)
    def get_assumptions() -> AssumptionsView:
        return assumptions_view()

    @app.get("/strategies", response_model=list[StrategyDescription])
    def get_strategies() -> list[StrategyDescription]:
        return list(strategy_descriptions())

    return app


app = create_app()

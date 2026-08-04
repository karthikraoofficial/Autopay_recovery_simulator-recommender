# CLAUDE.md — Working agreement for `rebound`

Read `SPEC.md` once at session start. Do not re-read it unless I say the spec changed.

**Current phase: 10 (complete, untagged) — the recommendation service of SPEC §14, a sibling of the simulator. 8.7 is tagged v0.3-exports (v0.1-sim and v0.2-demo remain at phases 7 and 8); phase 9 stays skipped on phase 7's evidence. `notebooks/phase85_result.json` was regenerated during phase 10 to carry a `config_fingerprint`; every figure in it was verified unchanged. Next: the post-v0.1 population fix (SPEC §11: ticket dispersion + caps correlated with income, together), and the outstanding corrected sweep of the three strategy.blended.weight_* keys (~3.5h).** Update this line when a phase completes.

## Ground rules

- **Build order in SPEC.md §9 is binding.** Do not skip ahead. Do not build a strategy before the harness.
- **No number is hardcoded.** Every constant goes in `config/assumptions.yaml` with `source` and `confidence`. If I have not given you a source, use `confidence: estimate` and add it to the open-questions list — do not invent a citation.
- **Tests before implementation** for anything in `compliance/` or `harness/`. Elsewhere, tests in the same commit.
- **Strategies receive `CustomerObservable`, never the true state.** If you find yourself needing ground truth to make a strategy work, stop and tell me — that is a finding, not an obstacle.
- **Hard declines terminate retry chains.** No exceptions, no config flag to disable.
- **Deterministic given a seed.** Same seed + same config = byte-identical output. Never seed from wall clock, `hash()`, set/dict iteration order, or an unseeded global RNG. Thread the seed explicitly.
- **Strategies are compared on identical populations.** Generate the seeded book once, deep-copy it per strategy, compare paired. Never compare independent runs.

## Style

- Python 3.11+, full type hints, `pydantic` v2 models, `ruff` clean.
- Prefer boring, explicit code. No metaclasses, no dynamic dispatch, no clever comprehensions.
- Functions under 40 lines. If it does not fit, it does two things.
- Docstrings only where the *why* is non-obvious. No docstrings restating the signature.
- No new dependency without asking. Stack is fixed in SPEC.md §7.

## Working method

- Before writing code for a phase, state the plan in 5 lines or fewer and wait if the phase is new.
- Change one thing at a time. Do not refactor adjacent code while implementing a feature.
- After each phase, run `pytest -q` and report only failures.
- When you finish a phase, say so and stop. Do not continue into the next phase unprompted.

## What to tell me

- If an assumption in `assumptions.yaml` materially drives a result, say which one.
- Never give me a point estimate without an interval. Interval first, then the point estimate. This applies to every reported metric, not just lift.
- If something in SPEC.md is ambiguous or wrong, say so rather than guessing.

## What not to do

- Do not add logging frameworks, config frameworks, DI containers, or abstractions "for later."
- Do not create README files, CI configs, or Dockerfiles unless asked.
- Do not write summary markdown files describing what you just did.
- Do not add features that are in SPEC.md §10 (out of scope).

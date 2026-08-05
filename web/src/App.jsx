import React, { useEffect, useState } from "react";
import Chart from "./Chart.jsx";
import Assumptions from "./Assumptions.jsx";
import Downloads from "./Downloads.jsx";
import Ingest from "./Ingest.jsx";
import ErrorBoundary from "./ErrorBoundary.jsx";
import { getJson } from "./api.js";
import { duration, inr, pct } from "./format.js";

const API = "/api";

// SPEC §6.1: six fields, no more. `vertical` and `billing_day_policy` are deliberately
// absent — see the module docstring in src/rebound/api/inputs.py. A control that moves no
// number is the first thing a sceptical reader finds.
const DEFAULT_PROFILE = {
  book_size: 2000,
  avg_ticket_inr: 499,
  upi_autopay_share: 0.55,
  enach_share: 0.3,
  failure_mix: "current",
  performance_fee_rate: 0.15,
};

const FAILURE_MIXES = [
  ["current", "As shipped", "The config's own balance and bank settings."],
  [
    "insufficient_funds_dominant",
    "Insufficient-funds dominant",
    "Thin balances, reliable banks. Failures are a timing problem, which is the shape retry timing is worth the most in.",
  ],
  [
    "technical_dominant",
    "Technical dominant",
    "Healthy balances, unreliable rails. Failures are the bank's fault, so timing against payroll has little to work with.",
  ],
];

function Field({ label, hint, children }) {
  return (
    <div>
      <label>{label}</label>
      {children}
      {hint ? <div className="hint">{hint}</div> : null}
    </div>
  );
}

function Inputs({ profile, setProfile, onRun, busy, estimates }) {
  const set = (key) => (event) => {
    const raw = event.target.value;
    setProfile({ ...profile, [key]: key === "failure_mix" ? raw : Number(raw) });
  };
  const cardShare = Math.max(0, 1 - profile.upi_autopay_share - profile.enach_share);
  const mixNote = FAILURE_MIXES.find(([v]) => v === profile.failure_mix)?.[2];

  return (
    <section>
      <h2>Merchant profile</h2>
      <div className="fields">
        <Field label="Book size (mandates)">
          <input type="number" min="1" step="100" value={profile.book_size} onChange={set("book_size")} />
        </Field>
        <Field label="Average ticket (₹)">
          <input type="number" min="1" step="50" value={profile.avg_ticket_inr} onChange={set("avg_ticket_inr")} />
        </Field>
        <Field label="UPI Autopay share" hint={`Card e-mandate takes the remainder: ${pct(cardShare)}`}>
          <input
            type="number"
            min="0"
            max="1"
            step="0.05"
            value={profile.upi_autopay_share}
            onChange={set("upi_autopay_share")}
          />
        </Field>
        <Field label="eNACH share">
          <input type="number" min="0" max="1" step="0.05" value={profile.enach_share} onChange={set("enach_share")} />
        </Field>
        <Field label="Failure mix" hint={mixNote}>
          <select value={profile.failure_mix} onChange={set("failure_mix")}>
            {FAILURE_MIXES.map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Performance fee" hint="Taken off gross recovery to give the net figure.">
          <input
            type="number"
            min="0"
            max="0.99"
            step="0.01"
            value={profile.performance_fee_rate}
            onChange={set("performance_fee_rate")}
          />
        </Field>
      </div>

      <div className="controls">
        <button className="primary" onClick={() => onRun("interactive")} disabled={busy}>
          Run (fast)
        </button>
        <button onClick={() => onRun("publication")} disabled={busy}>
          Publication run
        </button>
        <span className="hint" style={{ marginTop: 0, maxWidth: "52ch" }}>
          The fast run caps the book at {estimates.interactive?.book_size ?? "…"} mandates and uses fewer seeds, so its
          rupee figures are <strong>per simulated book of that size, not yours</strong>. The publication run simulates
          your actual book and is the number to quote.
        </span>
      </div>

      {/* The cost of each run, before anyone commits to one. */}
      <div className="estimates">
        {["interactive", "publication"].map((which) => {
          const e = estimates[which];
          const label = which === "interactive" ? "Fast run" : "Publication run";
          if (!e) return null;
          if (e.error) {
            return (
              <div key={which} className="estimate error">
                {label}: {e.error}
              </div>
            );
          }
          return (
            <div key={which} className="estimate">
              <strong>{label}</strong> — {e.book_size.toLocaleString("en-IN")} mandates × {e.n_seeds} seeds ≈{" "}
              {duration(e.estimated_seconds)}
              {e.book_size_capped ? (
                <span className="capped">
                  {" "}
                  capped from {e.requested_book_size.toLocaleString("en-IN")}
                </span>
              ) : null}
            </div>
          );
        })}
      </div>
    </section>
  );
}

function Progress({ job, sizing }) {
  const pct = job?.total_steps ? job.completed_steps / job.total_steps : 0;
  return (
    <section>
      <h2>{sizing === "publication" ? "Publication run" : "Fast run"} in progress</h2>
      <div className="bar">
        <div className="bar-fill" style={{ width: `${Math.max(2, pct * 100)}%` }} />
      </div>
      <div className="precision" style={{ marginTop: 14, marginBottom: 0 }}>
        <div>
          <span className="k">Elapsed</span>
          <span className="v">{duration(job?.elapsed_seconds ?? 0)}</span>
        </div>
        <div>
          <span className="k">Expected</span>
          <span className="v">{duration(job?.estimated_seconds ?? 0)}</span>
        </div>
        <div>
          <span className="k">Progress</span>
          <span className="v">
            {job?.completed_steps ?? 0} / {job?.total_steps ?? 0} strategy-seeds
          </span>
        </div>
        <div>
          <span className="k">Book</span>
          <span className="v">
            {(job?.book_size ?? 0).toLocaleString("en-IN")} × {job?.n_seeds ?? 0} seeds
          </span>
        </div>
      </div>
      <p className="hint">
        The expected duration is measured from this machine and is only a guide. Elapsed time is shown beside it so a
        bad estimate is visible as one.
      </p>
    </section>
  );
}

function LineItem({ line }) {
  return (
    <div className="line-item">
      <h3>{line.label}</h3>
      {/* Interval first, then the point estimate. A point read first anchors the reader,
          and these intervals are wide enough that the anchoring would mislead. */}
      <div className="interval">
        {inr(line.net_low_inr)} to {inr(line.net_high_inr)}
      </div>
      <div className="point">
        net of fees · point estimate {inr(line.net_point_inr)} · gross {inr(line.gross_low_inr)} to{" "}
        {inr(line.gross_high_inr)}
      </div>
      {!line.significant ? (
        <div className="not-significant">
          Not significant — this interval contains zero. On this run the effect is indistinguishable from no effect.
          Do not read it as a gain.
        </div>
      ) : null}
      <div className="explanation">{line.explanation}</div>
    </div>
  );
}

function StrategyTable({ result }) {
  return (
    <div className="scroll">
      <table>
        <thead>
          <tr>
            <th>Strategy</th>
            <th>Recovery rate</th>
            <th>Net to merchant / book</th>
            <th>Attempts / episode</th>
            <th>Induced revocations</th>
            <th>Compliance blocks</th>
            <th>Rescheduled</th>
          </tr>
        </thead>
        <tbody>
          {result.strategies.map((s) => (
            <tr key={s.name}>
              <td>
                <div>{s.name}</div>
                <div className="role">{s.role}</div>
              </td>
              <td>
                {pct(s.recovery_rate_low)} – {pct(s.recovery_rate_high)}
                <div className="role">point {pct(s.recovery_rate_point)}</div>
              </td>
              <td>
                {inr(s.net_low_inr)} – {inr(s.net_high_inr)}
                <div className="role">point {inr(s.net_point_inr)}</div>
              </td>
              <td>{s.attempts_per_episode_point.toFixed(2)}</td>
              <td>{s.induced_revocations_point.toFixed(1)}</td>
              <td>{s.compliance_blocks}</td>
              <td>{s.compliance_reschedules}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="hint">
        Counters (blocks, rescheduled) are totals across every seed and carry no interval. Attempts per episode is the
        parity check: a strategy that recovers more while attempting more has not been shown to time better.
      </div>
    </div>
  );
}

function Result({ result, job, profile }) {
  // Actual wall-clock time comes from the job, not from the result. `SimulationResult`
  // deliberately carries no timing: the same seed and config must produce a byte-identical
  // payload, and a wall-clock field would break that for no gain.
  const elapsed = job?.elapsed_seconds;
  return (
    <section>
      <h2>Result</h2>

      {/* The cost of a cheap run, in the same glance as the number it produced. */}
      <div className="precision">
        <div>
          <span className="k">Run</span>
          <span className="v">{result.sizing === "publication" ? "Publication" : "Fast / interactive"}</span>
        </div>
        <div>
          <span className="k">Seeds</span>
          <span className="v">{result.n_seeds}</span>
        </div>
        <div>
          <span className="k">Book simulated</span>
          <span className="v">{result.book_size_simulated.toLocaleString("en-IN")} mandates</span>
        </div>
        <div>
          <span className="k">Elapsed</span>
          <span className="v">{elapsed === undefined ? "—" : duration(elapsed)}</span>
        </div>
        <div>
          <span className="k">Expected</span>
          <span className="v">{duration(result.estimated_seconds)}</span>
        </div>
        <div>
          <span className="k">Confidence level</span>
          <span className="v">{pct(result.confidence_level, 0)}</span>
        </div>
        <div>
          <span className="k">Seed / output hash</span>
          <span className="v">
            {result.master_seed} · {result.output_hash.slice(0, 12)}
          </span>
        </div>
      </div>

      {result.book_size_capped ? (
        <div className="not-significant">
          These rupee figures are <strong>per simulated book of {result.book_size_simulated.toLocaleString("en-IN")}{" "}
          mandates</strong>, not your {result.book_size_requested.toLocaleString("en-IN")}. They are deliberately not
          scaled up: doing so would assume mandates are independent and identically distributed, which nothing here has
          tested. For a figure that is genuinely your book's, use the publication run.
        </div>
      ) : null}

      <p className="lede">
        The headline is two line items and is never summed. Phase 7 measured the first to be several times the second:
        re-presenting a blocked retry is scheduler plumbing that any merchant can buy, and only what sits above it is
        attributable to retry logic. A single combined number would sell the first as the second.
      </p>

      <div className="lines">
        <LineItem line={result.rescheduling} />
        <LineItem line={result.strategy} />
      </div>

      <h2 style={{ marginTop: 28 }}>
        ₹ recovered per book, by billing month — {result.baseline} vs {result.headline}
      </h2>
      <Chart months={result.months} baseline={result.baseline} headline={result.headline} />

      <h2 style={{ marginTop: 28 }}>Every strategy in the comparison</h2>
      <StrategyTable result={result} />

      {!result.retry_inherits_original_notice ? (
        <div className="not-significant">
          Running under the strict reading of the pre-debit notification rule: a retry needs fresh notice, so every
          sub-24h retry is blocked.
        </div>
      ) : (
        <p className="hint">
          Assumes a retry inherits the original charge's pre-debit notice. This is the largest single regulatory risk
          in the model and it is unverified — under the strict reading the fast technical retry disappears and the
          strategy line item falls.
        </p>
      )}
    </section>
  );
}

const POLL_MS = 1000;

// Every response body goes through here. Calling `.json()` directly is what produced
// "Failed to execute 'json' on 'Response': Unexpected end of JSON input" when the API was
// down: the Vite proxy answers with an empty body, and parsing that throws a browser
// message that tells the reader nothing about what actually went wrong.
export default function App() {
  const [profile, setProfile] = useState(DEFAULT_PROFILE);
  const [sizing, setSizing] = useState("interactive");
  const [result, setResult] = useState(null);
  const [assumptions, setAssumptions] = useState(null);
  // Distinct from `assumptions === null`. Without it a failed load is indistinguishable
  // from a slow one and the panel says "Loading…" forever.
  const [assumptionsError, setAssumptionsError] = useState(null);
  const [estimates, setEstimates] = useState({});
  const [job, setJob] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    getJson(`${API}/assumptions`)
      .then((view) => {
        setAssumptions(view);
        setAssumptionsError(null);
      })
      .catch((e) => setAssumptionsError(String(e.message ?? e)));
  }, []);

  // Re-price both runs whenever the profile changes, so the cost is on screen before a
  // button is pressed rather than discovered by waiting.
  useEffect(() => {
    let cancelled = false;
    const price = async (which) => {
      try {
        return await getJson(`${API}/simulate/estimate`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ profile, sizing: which, master_seed: 20260801 }),
        });
      } catch (e) {
        // Shown on the estimate card rather than swallowed. Silently dropping this left
        // the run buttons quoting "…" with no indication that anything had failed.
        return { error: String(e.message ?? e) };
      }
    };
    Promise.all([price("interactive"), price("publication")]).then(
      ([interactive, publication]) => {
        if (!cancelled) setEstimates({ interactive, publication });
      }
    );
    return () => {
      cancelled = true;
    };
  }, [profile]);

  const run = async (which) => {
    setBusy(true);
    setSizing(which);
    setError(null);
    setResult(null);
    setJob(null);
    const body = JSON.stringify({ profile, sizing: which, master_seed: 20260801 });
    const headers = { "Content-Type": "application/json" };
    try {
      // Every run goes through the job queue, publication or not. One code path, and the
      // progress display is then the same whether a run takes 35 seconds or 40 minutes.
      const created = await getJson(`${API}/simulate/jobs`, { method: "POST", headers, body });
      setJob(created);

      let status = created;
      while (status.state === "running") {
        await new Promise((r) => setTimeout(r, POLL_MS));
        status = await getJson(`${API}/simulate/jobs/${created.id}`);
        setJob(status);
      }
      if (status.state === "failed") throw new Error(status.error ?? "the run failed");
      setResult(status.result);
    } catch (e) {
      setError(String(e.message ?? e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <main>
      <h1>rebound</h1>
      <p className="lede">
        A simulator for recurring-payment retry strategies on Indian mandate rails. Every number below is produced by a
        seeded simulation of a book that does not exist, under assumptions listed in full at the bottom of this page.
        It is not a measurement of any merchant.
      </p>

      <Inputs profile={profile} setProfile={setProfile} onRun={run} busy={busy} estimates={estimates} />

      {error ? (
        <section>
          <p className="error">{error}</p>
        </section>
      ) : null}

      {busy ? <Progress job={job} sizing={sizing} /> : null}

      {result ? <Result result={result} job={job} profile={profile} /> : null}
      {result ? <Downloads result={result} profile={profile} /> : null}

      {/* SPEC §15.6. Below the simulated headline and independent of it: this section
          answers real failures and needs no run to have happened first.

          Inside a boundary because "independent" has to be true in both directions. A
          runtime error here previously unmounted the whole tree, so a panel that could
          not list mapping profiles took the simulator, the headline and the exports with
          it. Losing this section is an inconvenience; losing a result someone waited
          minutes for is not. */}
      <ErrorBoundary section="The upload section">
        <Ingest />
      </ErrorBoundary>

      <Assumptions view={assumptions} error={assumptionsError} />
    </main>
  );
}

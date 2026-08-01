import React, { useEffect, useState } from "react";
import Chart from "./Chart.jsx";
import Assumptions from "./Assumptions.jsx";
import { inr, pct } from "./format.js";

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

function Inputs({ profile, setProfile, sizing, setSizing, onRun, busy }) {
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
          {busy && sizing === "interactive" ? "Running…" : "Run (fast)"}
        </button>
        <button onClick={() => onRun("publication")} disabled={busy}>
          {busy && sizing === "publication" ? "Running…" : "Publication run (slow)"}
        </button>
        <span className="hint" style={{ marginTop: 0, maxWidth: "52ch" }}>
          A fast run is the same model measured with fewer seeds, not a different one. Its intervals are wider, and
          wide enough that a lift can look real when it is not. The publication run is the number to quote.
        </span>
      </div>
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

function Result({ result }) {
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

export default function App() {
  const [profile, setProfile] = useState(DEFAULT_PROFILE);
  const [sizing, setSizing] = useState("interactive");
  const [result, setResult] = useState(null);
  const [assumptions, setAssumptions] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    fetch(`${API}/assumptions`)
      .then((r) => r.json())
      .then(setAssumptions)
      .catch((e) => setError(String(e)));
  }, []);

  const run = async (which) => {
    setBusy(true);
    setSizing(which);
    setError(null);
    try {
      const response = await fetch(`${API}/simulate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ profile, sizing: which, master_seed: 20260801 }),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail ?? "simulation failed");
      setResult(body);
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

      <Inputs
        profile={profile}
        setProfile={setProfile}
        sizing={sizing}
        setSizing={setSizing}
        onRun={run}
        busy={busy}
      />

      {error ? (
        <section>
          <p className="error">{error}</p>
        </section>
      ) : null}

      {result ? <Result result={result} /> : null}

      <Assumptions view={assumptions} />
    </main>
  );
}

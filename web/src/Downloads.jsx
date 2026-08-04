import React, { useState } from "react";
import { duration } from "./format.js";

// Artifacts you take away, deliberately not rendered on the page. The headline stays two
// line items; a segment table or a trace grid beside the chart would compete with it and
// invite reading a per-segment number as the result.

const API = "/api";

// /trace generates a book of this size at most (api.publication_book_size_cap is a
// different limit). A trace must be of the SAME book as the result it sits under, so
// where the run was larger the buttons are disabled rather than quietly tracing a
// smaller, different population.
const TRACE_MAX_BOOK = 2000;

function save(blob, filename) {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

// The trace is a fresh single-seed run, so its output hash is its own and not the
// headline's. Read it back out of the file rather than labelling the download with a hash
// that belongs to a different run.
function hashFromTrace(text) {
  const line = text.split("\n").find((l) => l.startsWith("# output_hash:"));
  return line ? line.split(":")[1].trim().slice(0, 12) : "unknown";
}

export default function Downloads({ result, profile }) {
  const [seed, setSeed] = useState("");
  const [busy, setBusy] = useState(null);
  const [error, setError] = useState(null);

  const hash = result.output_hash.slice(0, 12);
  const traceable = result.book_size_simulated <= TRACE_MAX_BOOK;

  const query = (extra) =>
    new URLSearchParams({
      book_size: String(profile.book_size),
      avg_ticket_inr: String(profile.avg_ticket_inr),
      upi_autopay_share: String(profile.upi_autopay_share),
      enach_share: String(profile.enach_share),
      failure_mix: profile.failure_mix,
      performance_fee_rate: String(profile.performance_fee_rate),
      master_seed: String(result.master_seed),
      ...extra,
    }).toString();

  const fetchText = async (url) => {
    let response;
    try {
      response = await fetch(url);
    } catch {
      throw new Error(
        "Cannot reach the simulation API. Start it with: " +
          "python -m uvicorn rebound.api.app:app --port 8000"
      );
    }
    const text = await response.text();
    if (!text) {
      throw new Error(
        `The API returned an empty response (HTTP ${response.status}). This usually means ` +
          "the API process is not running behind the dev-server proxy."
      );
    }
    if (!response.ok) {
      let detail = text;
      try {
        detail = JSON.parse(text).detail ?? text;
      } catch {
        /* a non-JSON error body is shown as-is */
      }
      throw new Error(detail);
    }
    return text;
  };

  const download = async (key, run) => {
    setBusy(key);
    setError(null);
    try {
      await run();
    } catch (e) {
      setError(String(e.message ?? e));
    } finally {
      setBusy(null);
    }
  };

  const segments = () =>
    download("segments", async () => {
      const text = await fetchText(
        `${API}/segments?${query({
          sizing: result.sizing,
          format: "text",
          // Refused server-side if this report is not the run above it.
          expect_config: result.config_fingerprint,
        })}`
      );
      save(
        new Blob([text], { type: "text/plain" }),
        `rebound-segments-seed${result.master_seed}-${hash}.txt`
      );
    });

  const trace = (table) =>
    download(`trace-${table}`, async () => {
      const text = await fetchText(
        `${API}/trace?${new URLSearchParams({
          seed,
          table,
          format: "csv",
          book_size: String(result.book_size_simulated),
          // The same six inputs the run used. Without them /trace generates from the
          // shipped defaults and traces a different population than the result above.
          avg_ticket_inr: String(profile.avg_ticket_inr),
          upi_autopay_share: String(profile.upi_autopay_share),
          enach_share: String(profile.enach_share),
          failure_mix: profile.failure_mix,
          performance_fee_rate: String(profile.performance_fee_rate),
          // Refused server-side if it does not match what the trace was built under.
          expect_config: result.config_fingerprint,
        })}`
      );
      save(
        new Blob([text], { type: "text/csv" }),
        `rebound-trace-${table}-seed${seed}-${hashFromTrace(text)}.csv`
      );
    });

  return (
    <section>
      <h2>Take away</h2>
      <p className="lede">
        Neither of these is shown on this page. They are artifacts for inspection: the
        segment report tells you where in the book the opportunity sits, the trace tells you
        what happened row by row. Both re-run the simulation, so neither is instant.
      </p>

      <div className="download">
        <div>
          <strong>Segment report</strong>
          <div className="hint" style={{ marginTop: 2 }}>
            Plain text, every verdict corrected for the number of segments tested. Covers all{" "}
            {result.n_seeds} seeds of this run. Takes about {duration(result.estimated_seconds)}.
          </div>
        </div>
        <button onClick={segments} disabled={busy !== null}>
          {busy === "segments" ? "Running…" : "Download segment report (.txt)"}
        </button>
      </div>

      <div className="download">
        <div>
          <strong>Mandate trace</strong>
          <div className="hint" style={{ marginTop: 2 }}>
            {traceable ? (
              <>
                One seed only — a mandate id means nothing across seeds, so pick which of this
                run&rsquo;s {result.n_seeds} seeds to trace. Two files, joined on the{" "}
                <code>SIM-</code> id.
              </>
            ) : (
              <>
                Unavailable: this run simulated {result.book_size_simulated.toLocaleString("en-IN")}{" "}
                mandates and the trace endpoint caps at {TRACE_MAX_BOOK.toLocaleString("en-IN")}. A
                smaller trace would be a different population from the one above, so it is not
                offered rather than quietly mismatched.
              </>
            )}
          </div>
        </div>
        {traceable ? (
          <div className="trace-controls">
            {/* No default. Picking one for the user is the quiet-merge failure the
                endpoint's 422 exists to prevent, moved up into the UI. */}
            <select value={seed} onChange={(e) => setSeed(e.target.value)}>
              <option value="">Choose a seed…</option>
              {result.seeds.map((s) => (
                <option key={s} value={s}>
                  seed {s}
                </option>
              ))}
            </select>
            <button onClick={() => trace("attempts")} disabled={!seed || busy !== null}>
              {busy === "trace-attempts" ? "Running…" : "Attempts (.csv)"}
            </button>
            <button onClick={() => trace("mandates")} disabled={!seed || busy !== null}>
              {busy === "trace-mandates" ? "Running…" : "Mandate attributes (.csv)"}
            </button>
          </div>
        ) : null}
      </div>

      {error ? <p className="error">{error}</p> : null}
    </section>
  );
}

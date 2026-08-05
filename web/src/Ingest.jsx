import React, { useEffect, useState } from "react";
import { loadMappings } from "./api.js";

// SPEC §15.6: the upload surface. A merchant's own CSV, read through a stored mapping
// profile, answered row by row.
//
// It deliberately does NOT offer to edit a mapping. A profile edited in a browser is a
// mapping supplied per upload wearing a different hat, and SPEC §15.3 exists to stop that:
// an unversioned vocabulary answers the same file differently on two days with nothing
// recording why. Profiles are files in config/merchants/, under the same review as
// everything else in config/.

const API = "/api";

// Shown once, above the table, never per row. A page listing only FixedSchedule and
// ReasonAware must not read as those being the strategies that won a comparison
// (SPEC §11): the other three were never eligible from a CSV.
const TIER_NOTE =
  "A CSV row cannot carry an attempt history, so every uploaded row is minimum-tier: BankAware, SalaryAware and Blended cannot be selected here at all. Where one of them is the measured winner for a row's segment, the row says so and names the fields that would unlock it. Their absence from this table is a limit of the file format, not a measurement.";

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

function Fingerprints({ result }) {
  return (
    <div className="hint">
      mapping <code>{result.mapping_profile ?? "none"}</code>{" "}
      <code>{(result.mapping_fingerprint ?? "none").slice(0, 12)}</code> · evidence{" "}
      <code>{result.evidence_fingerprint.slice(0, 12)}</code>
      <div>
        Both are echoed so a file answered differently on two days can be traced to which of them moved. Re-run with
        the mapping fingerprint you expect and a change is refused rather than absorbed.
      </div>
    </div>
  );
}

function Summary({ result }) {
  const fields = Object.entries(result.refusals_by_field);
  return (
    <div className="summary">
      <p>
        <strong>{result.rows_answered}</strong> of {result.rows_read} rows answered ·{" "}
        <strong>{result.rows_refused}</strong> refused
        {result.rows_malformed ? ` (${result.rows_malformed} malformed)` : ""}
      </p>
      {fields.length ? (
        <p className="hint">
          Refused by column: {fields.map(([field, count]) => `${field} (${count})`).join(", ")}
        </p>
      ) : null}
    </div>
  );
}

function Answers({ rows }) {
  if (!rows.length) return null;
  return (
    <table>
      <thead>
        <tr>
          <th>Reference</th>
          <th>Retry at</th>
          <th>Strategy</th>
          <th>Rule</th>
          <th>Measured in its segment</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={row.mandate_ref}>
            <td>{row.mandate_ref}</td>
            <td>{row.retry_at ?? <span className="hint">{row.status}</span>}</td>
            <td>{row.strategy ?? "—"}</td>
            <td className="hint">{row.rule}</td>
            <td className="hint">{row.evidence ? row.evidence.sentence : "no segment evidence"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Refusals({ rows }) {
  if (!rows.length) return null;
  return (
    <table>
      <thead>
        <tr>
          <th>Row</th>
          <th>Reference</th>
          <th>Why</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={row.row_number}>
            <td>{row.row_number}</td>
            <td>{row.mandate_ref ?? "—"}</td>
            <td className="hint">{row.detail}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export default function Ingest() {
  // Every piece of fetched state starts at a value the render path can handle. `[]` is a
  // list with nothing in it; the failure is carried in `mappingsError` beside it, never
  // in place of it.
  const [mappings, setMappings] = useState([]);
  const [mappingsError, setMappingsError] = useState(null);
  const [selected, setSelected] = useState("");
  const [file, setFile] = useState(null);
  const [result, setResult] = useState(null);
  // A file-level defect is one message about the file, not a list of rows. Held apart from
  // `error` so the page can say which kind of failure it was (SPEC §15.1).
  const [refusal, setRefusal] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    // Not `fetch().then(r => r.json())`. A 404 body is `{"detail": "Not Found"}`, which
    // parses fine, so the error object landed in `mappings` and the next render threw --
    // taking the entire application down with it. `loadMappings` never throws and never
    // returns a non-array.
    let cancelled = false;
    loadMappings(`${API}/recommend/mappings`).then(({ mappings, error }) => {
      if (cancelled) return;
      setMappings(mappings);
      setMappingsError(error);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  // Defensive despite the guarantee above: this lookup is what threw, and it is one line
  // away from being unable to throw at all.
  const profile = Array.isArray(mappings) ? mappings.find((m) => m.name === selected) : undefined;

  const upload = async () => {
    if (!file) return;
    setBusy(true);
    setResult(null);
    setRefusal(null);
    setError(null);
    try {
      const params = new URLSearchParams();
      if (selected) {
        params.set("mapping", selected);
        // The fingerprint we listed is the one the answer must be read through. If the
        // profile changed between listing and upload, that is exactly the case
        // expect_mapping exists to catch, so it is sent rather than trusted.
        params.set("expect_mapping", profile.fingerprint);
      }
      const response = await fetch(`${API}/recommend/batch?${params}`, {
        method: "POST",
        headers: { "Content-Type": "text/csv" },
        body: await file.arrayBuffer(),
      });
      const text = await response.text();
      let body;
      try {
        body = JSON.parse(text);
      } catch {
        throw new Error(`The API returned a non-JSON response (HTTP ${response.status}).`);
      }
      if (!response.ok) {
        // 422 is the file itself; 409 is a mapping that moved under the caller.
        if (response.status === 422 || response.status === 409) setRefusal(body.detail);
        else throw new Error(body.detail ?? response.statusText);
        return;
      }
      setResult(body);
    } catch (e) {
      setError(String(e.message ?? e));
    } finally {
      setBusy(false);
    }
  };

  const download = async (table) => {
    const params = new URLSearchParams({ format: "csv", table });
    if (selected) params.set("mapping", selected);
    const response = await fetch(`${API}/recommend/batch?${params}`, {
      method: "POST",
      headers: { "Content-Type": "text/csv" },
      body: await file.arrayBuffer(),
    });
    if (!response.ok) {
      // Saving the body regardless would hand the user a file named .csv containing an
      // error page, which is worse than no file.
      setError(`The download failed (HTTP ${response.status}). Nothing was saved.`);
      return;
    }
    save(await response.blob(), `rebound-recommend-${table}.csv`);
  };

  return (
    <section className="ingest">
      <h2>Recommend retries for your own failures</h2>
      <p className="lede">
        Upload a CSV of failures that actually happened. Each row gets a recommended retry time, the rule that produced
        it, and the lift measured in the segment that failure belongs to. Nothing here predicts whether a retry will
        succeed — there is no such measurement outside the simulator.
      </p>

      <div className="fields">
        <div>
          <label>Mapping profile</label>
          <select
            value={selected}
            onChange={(e) => setSelected(e.target.value)}
            disabled={Boolean(mappingsError)}
          >
            <option value="">None — my file already uses the canonical codes</option>
            {mappings.map((m) => (
              <option key={m.name} value={m.name}>
                {m.name} ({m.reason_codes} codes, {m.rails} rails, {m.renamed_columns} renamed columns)
              </option>
            ))}
          </select>
          {mappingsError ? (
            <div className="hint">
              <span className="error">The mapping profile list is unavailable.</span> {mappingsError}
              <div>
                You can still upload a file whose codes are already canonical — the rest of this page,
                and the whole of the simulator above it, are unaffected.
              </div>
            </div>
          ) : (
            <div className="hint">
              {profile
                ? `${profile.description ?? ""} Fingerprint ${profile.fingerprint.slice(0, 12)}.`
                : "Profiles are files in config/merchants/. They are not editable here on purpose: a mapping that can be changed per upload is one that cannot be compared between uploads."}
            </div>
          )}
        </div>
        <div>
          <label>Failures (CSV)</label>
          <input type="file" accept=".csv,text/csv" onChange={(e) => setFile(e.target.files[0] ?? null)} />
          <div className="hint">
            <a href={`${API}/recommend/template`}>Download the template</a> — its columns are the only ones accepted,
            unless a profile renames yours onto them.
          </div>
        </div>
      </div>

      <button className="primary" onClick={upload} disabled={!file || busy}>
        {busy ? "Reading…" : "Upload and recommend"}
      </button>

      {refusal ? (
        <div className="file-refusal">
          <p className="error">The file was refused whole. Nothing was answered.</p>
          <p>{refusal}</p>
        </div>
      ) : null}
      {error ? <p className="error">{error}</p> : null}

      {result ? (
        <>
          <Summary result={result} />
          <Fingerprints result={result} />
          <p className="hint">{TIER_NOTE}</p>
          <Answers rows={result.recommendations} />
          <Refusals rows={result.refusals} />
          <div className="download">
            <button onClick={() => download("answers")} disabled={!result.rows_answered}>
              Download answers (CSV)
            </button>
            <button onClick={() => download("refusals")} disabled={!result.rows_refused}>
              Download refusals (CSV)
            </button>
            <div className="hint">
              The refusals table carries each row's own values back to you under the canonical column names, so it can
              be corrected and resubmitted as a file in its own right.
            </div>
          </div>
        </>
      ) : null}
    </section>
  );
}

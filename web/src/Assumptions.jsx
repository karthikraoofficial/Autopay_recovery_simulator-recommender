import React, { useMemo, useState } from "react";

// SPEC §6.3: the whole file, with sources and confidence, always visible and never behind
// a click. The search box filters; it does not hide anything by default, and the estimate
// count is stated up front rather than left to be discovered by scrolling.

function value(v) {
  if (typeof v === "boolean") return v ? "true" : "false";
  if (Array.isArray(v)) return `[${v.join(", ")}]`;
  return String(v);
}

export default function Assumptions({ view, error }) {
  const [query, setQuery] = useState("");
  const [estimatesOnly, setEstimatesOnly] = useState(false);

  const shown = useMemo(() => {
    if (!view) return [];
    const q = query.trim().toLowerCase();
    return view.assumptions.filter((a) => {
      if (estimatesOnly && a.confidence !== "estimate") return false;
      if (!q) return true;
      return `${a.key} ${a.source} ${a.notes ?? ""}`.toLowerCase().includes(q);
    });
  }, [view, query, estimatesOnly]);

  if (error) {
    // Never a permanent "Loading…". SPEC §6.3 requires this panel to be always visible,
    // so when it cannot be shown the page has to say that plainly rather than imply the
    // numbers are still on their way.
    return (
      <section>
        <h2>Assumptions</h2>
        <p className="error">Could not load the assumptions.</p>
        <p className="hint">{error}</p>
        <p className="hint">
          Every number on this page comes from that file. Until it loads, nothing above has
          its sources attached and should not be quoted.
        </p>
      </section>
    );
  }

  if (!view) {
    return (
      <section>
        <h2>Assumptions</h2>
        <p className="hint">Loading…</p>
      </section>
    );
  }

  const estimates = view.assumptions.filter((a) => a.confidence === "estimate").length;

  return (
    <section>
      <h2>Assumptions</h2>
      <p className="lede">
        Every number the simulation uses, with where it came from. <strong>{estimates}</strong> of{" "}
        {view.assumptions.length} are marked <span className="conf conf-estimate">estimate</span> — our guess, with no
        source read — and each one carries the open question that has to be answered before it is defensible. No RBI
        circular or NPCI data file has been read for any value on this page.
      </p>

      <div className="filter-row">
        <input
          type="search"
          placeholder="Filter by key, source, or note"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <label style={{ margin: 0, display: "flex", gap: 6, alignItems: "center" }}>
          <input
            type="checkbox"
            checked={estimatesOnly}
            onChange={(e) => setEstimatesOnly(e.target.checked)}
            style={{ width: "auto" }}
          />
          Estimates only
        </label>
        <span className="hint" style={{ marginTop: 0 }}>
          Showing {shown.length} of {view.assumptions.length}
        </span>
      </div>

      {shown.map((a) => (
        <div className="assumption" key={a.key}>
          <div>
            <span className="k">{a.key}</span>
            <span className="v">
              {value(a.value)} {a.unit}
            </span>{" "}
            <span className={`conf conf-${a.confidence}`}>{a.confidence}</span>
          </div>
          <p>{a.source}</p>
          {a.notes ? <p>{a.notes}</p> : null}
          {a.open_question ? <p className="q">Open question: {a.open_question}</p> : null}
        </div>
      ))}

      <h2 style={{ marginTop: 24 }}>Open questions with no assumption of their own</h2>
      <ul className="hint">
        {view.unkeyed_open_questions.map((q) => (
          <li key={q}>{q}</li>
        ))}
      </ul>
    </section>
  );
}

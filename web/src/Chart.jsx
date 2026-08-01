import React from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { inr } from "./format.js";

// Categorical slots 1 and 2 of the validated palette. Colour follows the entity, not its
// rank: the baseline is always blue and the headline strategy always orange, whichever
// happens to be ahead.
const BASELINE = "var(--series-baseline)";
const HEADLINE = "var(--series-headline)";

function ChartTooltip({ active, payload, label, baseline, headline }) {
  if (!active || !payload?.length) return null;
  const point = payload[0].payload;
  return (
    <div className="tooltip">
      <strong>Month {label}</strong>
      <div className="row">
        <span className="swatch" style={{ background: BASELINE }} />
        {baseline} {inr(point.baseline_inr)}
      </div>
      <div className="row">
        <span className="swatch" style={{ background: HEADLINE }} />
        {headline} {inr(point.headline_inr)}
      </div>
      <div className="row">difference {inr(point.headline_inr - point.baseline_inr)}</div>
    </div>
  );
}

export default function Chart({ months, baseline, headline }) {
  return (
    <>
      {/* Two series, so a legend is always present — identity is never colour alone. */}
      <div className="legend">
        <span>
          <span className="swatch" style={{ background: BASELINE }} />
          {baseline} (baseline)
        </span>
        <span>
          <span className="swatch" style={{ background: HEADLINE }} />
          {headline}
        </span>
      </div>
      <div style={{ width: "100%", height: 300 }}>
        <ResponsiveContainer>
          <LineChart data={months} margin={{ top: 8, right: 16, bottom: 8, left: 8 }}>
            <CartesianGrid stroke="var(--border)" vertical={false} />
            <XAxis
              dataKey="month"
              tick={{ fill: "var(--text-muted)", fontSize: 12 }}
              stroke="var(--border)"
              label={{
                value: "billing month",
                position: "insideBottom",
                offset: -4,
                fill: "var(--text-muted)",
                fontSize: 12,
              }}
            />
            {/* One axis. Both series are ₹ recovered per book, so they share it. */}
            <YAxis
              tick={{ fill: "var(--text-muted)", fontSize: 12 }}
              stroke="var(--border)"
              tickFormatter={inr}
              width={80}
            />
            <Tooltip
              content={<ChartTooltip baseline={baseline} headline={headline} />}
              cursor={{ stroke: "var(--text-muted)", strokeWidth: 1 }}
            />
            <Line
              type="monotone"
              dataKey="baseline_inr"
              stroke={BASELINE}
              strokeWidth={2}
              dot={false}
              activeDot={{ r: 4, strokeWidth: 2, stroke: "var(--surface-1)" }}
              name={baseline}
            />
            <Line
              type="monotone"
              dataKey="headline_inr"
              stroke={HEADLINE}
              strokeWidth={2}
              dot={false}
              activeDot={{ r: 4, strokeWidth: 2, stroke: "var(--surface-1)" }}
              name={headline}
            />
          </LineChart>
        </ResponsiveContainer>
      </div>
      <div className="hint">
        Mean ₹ recovered per seeded book in each billing month, gross of fees. Not cumulative: month 12 is what month
        12 recovered. The gap between the lines is the strategy line item only — the rescheduling line item is the
        difference against a scheduler that is not on this chart.
      </div>
    </>
  );
}

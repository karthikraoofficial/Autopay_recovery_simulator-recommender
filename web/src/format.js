const RUPEES = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 });

export function inr(value) {
  const sign = value < 0 ? "−" : "";
  return `${sign}₹${RUPEES.format(Math.abs(Math.round(value)))}`;
}

export function pct(value, digits = 1) {
  return `${(value * 100).toFixed(digits)}%`;
}

export function duration(seconds) {
  if (seconds < 90) return `${Math.round(seconds)}s`;
  const minutes = seconds / 60;
  if (minutes < 90) return `${minutes.toFixed(minutes < 10 ? 1 : 0)} min`;
  return `${(minutes / 60).toFixed(1)} hr`;
}

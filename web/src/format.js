const RUPEES = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 });

export function inr(value) {
  const sign = value < 0 ? "−" : "";
  return `${sign}₹${RUPEES.format(Math.abs(Math.round(value)))}`;
}

export function pct(value, digits = 1) {
  return `${(value * 100).toFixed(digits)}%`;
}

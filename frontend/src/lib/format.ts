/**
 * Number formatting — the difference between "dashboard" and "data product".
 *
 * Rules of thumb (taostats-style):
 *  - τ amounts: thousands separators, precision scaled to magnitude.
 *  - Alpha prices: enough decimals to be meaningful at sub-cent values.
 *  - USD: standard currency formatting, compact above 1M.
 *  - Percentages: one decimal, explicit sign only for deltas.
 */

const nf = (opts: Intl.NumberFormatOptions) => new Intl.NumberFormat("en-US", opts);

const INT = nf({ maximumFractionDigits: 0 });
const TWO = nf({ minimumFractionDigits: 2, maximumFractionDigits: 2 });
const COMPACT = nf({ notation: "compact", maximumFractionDigits: 1 });

/** Whole-ish TAO amounts (market caps, totals): τ 360,919 / τ 1.2M. */
export function fmtTao(v: number | null | undefined, compact = false): string {
  if (v == null) return "—";
  if (compact && Math.abs(v) >= 1_000_000) return `τ ${COMPACT.format(v)}`;
  if (Math.abs(v) >= 1000) return `τ ${INT.format(v)}`;
  return `τ ${TWO.format(v)}`;
}

/** Alpha pool prices in TAO: small values need real precision. */
export function fmtPrice(v: number | null | undefined): string {
  if (v == null) return "—";
  if (v === 0) return "0";
  if (v >= 1) return TWO.format(v);
  if (v >= 0.01) return v.toFixed(4);
  return v.toFixed(6);
}

export function fmtUsd(v: number | null | undefined): string {
  if (v == null) return "—";
  if (Math.abs(v) >= 1_000_000) return `$${COMPACT.format(v)}`;
  return `$${TWO.format(v)}`;
}

/** Plain percentage (emission share etc.) — no sign. */
export function fmtPct(v: number | null | undefined, digits = 1): string {
  if (v == null) return "—";
  return `${v.toFixed(digits)}%`;
}

/** Signed delta percentage for movement displays. */
export function fmtDelta(v: number | null | undefined): string {
  if (v == null) return "—";
  const sign = v > 0 ? "+" : "";
  return `${sign}${v.toFixed(1)}%`;
}

export function fmtCount(v: number | null | undefined): string {
  if (v == null) return "—";
  return INT.format(v);
}

/** 5FTzJYgS…jNo4vL7F — middle-truncated ss58. */
export function truncAddr(addr: string, edge = 6): string {
  if (addr.length <= edge * 2 + 1) return addr;
  return `${addr.slice(0, edge)}…${addr.slice(-edge)}`;
}

/** "3m ago" style relative timestamp. */
export function timeAgo(iso: string): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return iso;
  const s = Math.max(0, (Date.now() - then) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

/** Direction of a series for coloring sparklines/deltas. */
export function seriesDirection(spark: number[] | null | undefined): "up" | "down" | "flat" {
  if (!spark || spark.length < 2) return "flat";
  const delta = spark[spark.length - 1] - spark[0];
  if (delta > 0) return "up";
  if (delta < 0) return "down";
  return "flat";
}

export function sparkChangePct(spark: number[] | null | undefined): number | null {
  if (!spark || spark.length < 2 || spark[0] === 0) return null;
  return ((spark[spark.length - 1] - spark[0]) / spark[0]) * 100;
}

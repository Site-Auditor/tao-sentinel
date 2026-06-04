import { memo } from "react";
import { seriesDirection } from "../lib/format";

interface SparklineProps {
  data: number[] | null;
  w?: number;
  h?: number;
  strokeWidth?: number;
}

const dirClass: Record<"up" | "down" | "flat", string> = {
  up: "text-up",
  down: "text-down",
  flat: "text-ink-faint",
};

function SparklineImpl({ data, w = 72, h = 20, strokeWidth = 1.5 }: SparklineProps) {
  if (!data || data.length === 0) {
    return <span className="text-ink-faint">—</span>;
  }

  const dir = seriesDirection(data);
  const min = Math.min(...data);
  const max = Math.max(...data);
  const range = max - min;
  const n = data.length;

  // Map a value to the SVG y coordinate (1px padding top/bottom for stroke).
  const pad = 1;
  const innerH = h - pad * 2;
  const yOf = (v: number) =>
    range === 0 ? h / 2 : pad + innerH - ((v - min) / range) * innerH;
  const xOf = (i: number) => (n === 1 ? w / 2 : (i / (n - 1)) * w);

  let points: string;
  if (n === 1 || range === 0) {
    // Degenerate: flat midline across the full width.
    const y = range === 0 ? yOf(data[0]) : h / 2;
    points = `0,${y} ${w},${y}`;
  } else {
    points = data.map((v, i) => `${xOf(i)},${yOf(v)}`).join(" ");
  }

  const lastX = n === 1 || range === 0 ? w : xOf(n - 1);
  const lastY = range === 0 ? yOf(data[0]) : n === 1 ? h / 2 : yOf(data[n - 1]);

  return (
    <span className={dirClass[dir]} style={{ display: "inline-flex", lineHeight: 0 }}>
      <svg
        width={w}
        height={h}
        viewBox={`0 0 ${w} ${h}`}
        preserveAspectRatio="none"
        aria-hidden="true"
      >
        <polyline
          points={points}
          fill="none"
          stroke="currentColor"
          strokeWidth={strokeWidth}
          strokeLinejoin="round"
          strokeLinecap="round"
          vectorEffect="non-scaling-stroke"
        />
        <circle cx={lastX} cy={lastY} r={1.6} fill="currentColor" />
      </svg>
    </span>
  );
}

export const Sparkline = memo(SparklineImpl);

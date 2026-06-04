import { memo, useId } from "react";
import { seriesDirection } from "../lib/format";

interface SparklineProps {
  data: number[] | null;
  w?: number;
  h?: number;
  strokeWidth?: number;
  /** Soft gradient fill under the line (watchlist cards, detail header). */
  area?: boolean;
  /** Draw-in animation on mount (disable inside virtualized/large lists). */
  animate?: boolean;
}

const dirClass: Record<"up" | "down" | "flat", string> = {
  up: "text-up",
  down: "text-down",
  flat: "text-ink-faint",
};

function SparklineImpl({
  data,
  w = 72,
  h = 20,
  strokeWidth = 1.5,
  area = false,
  animate = true,
}: SparklineProps) {
  const gradId = useId();
  if (!data || data.length === 0) {
    return <span className="text-ink-faint">—</span>;
  }

  const dir = seriesDirection(data);
  const min = Math.min(...data);
  const max = Math.max(...data);
  const range = max - min;
  const n = data.length;

  // Map a value to the SVG y coordinate (padding for stroke + end dot).
  const pad = 2;
  const innerH = h - pad * 2;
  const yOf = (v: number) =>
    range === 0 ? h / 2 : pad + innerH - ((v - min) / range) * innerH;
  const xOf = (i: number) => (n === 1 ? w / 2 : (i / (n - 1)) * w);

  let pts: Array<[number, number]>;
  if (n === 1 || range === 0) {
    const y = range === 0 ? yOf(data[0]) : h / 2;
    pts = [
      [0, y],
      [w, y],
    ];
  } else {
    pts = data.map((v, i) => [xOf(i), yOf(v)]);
  }

  const points = pts.map(([x, y]) => `${x},${y}`).join(" ");
  const [lastX, lastY] = pts[pts.length - 1];

  // Rough path length for the draw-in animation (chord-length sum).
  let len = 0;
  for (let i = 1; i < pts.length; i++) {
    len += Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]);
  }

  const areaPoints = `0,${h} ${points} ${w},${h}`;

  return (
    <span
      className={dirClass[dir]}
      style={{ display: "inline-flex", lineHeight: 0 }}
    >
      <svg
        width={w}
        height={h}
        viewBox={`0 0 ${w} ${h}`}
        preserveAspectRatio="none"
        aria-hidden="true"
      >
        {area ? (
          <>
            <defs>
              <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="currentColor" stopOpacity={0.22} />
                <stop offset="100%" stopColor="currentColor" stopOpacity={0} />
              </linearGradient>
            </defs>
            <polygon points={areaPoints} fill={`url(#${gradId})`} className={animate ? "fade-in" : undefined} />
          </>
        ) : null}
        <polyline
          points={points}
          fill="none"
          stroke="currentColor"
          strokeWidth={strokeWidth}
          strokeLinejoin="round"
          strokeLinecap="round"
          vectorEffect="non-scaling-stroke"
          className={animate ? "spark-path" : undefined}
          style={animate ? ({ "--spark-len": `${Math.ceil(len)}` } as React.CSSProperties) : undefined}
        />
        <circle cx={lastX} cy={lastY} r={1.6} fill="currentColor" className={animate ? "fade-in" : undefined} />
      </svg>
    </span>
  );
}

export const Sparkline = memo(SparklineImpl);

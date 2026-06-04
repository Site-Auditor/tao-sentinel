import { memo } from "react";
import { fmtDelta } from "../lib/format";

interface DeltaProps {
  value: number | null;
}

function DeltaImpl({ value }: DeltaProps) {
  const cls =
    value == null
      ? "text-ink-faint"
      : value > 0
        ? "text-up"
        : value < 0
          ? "text-down"
          : "text-ink-faint";
  return <span className={`tnum ${cls}`}>{fmtDelta(value)}</span>;
}

export const Delta = memo(DeltaImpl);

import { memo } from "react";
import { Link } from "react-router-dom";
import type { SubnetRow } from "../lib/api";
import { fmtPrice, sparkChangePct } from "../lib/format";
import { Sparkline } from "./Sparkline";
import { Delta } from "./Delta";
import { GradeBadge } from "./GradeBadge";
import { SECTION_HEADING, NETUID_CHIP } from "./ui";

interface WatchlistStripProps {
  rows: SubnetRow[];
}

function WatchlistStripImpl({ rows }: WatchlistStripProps) {
  const pinned = rows.filter((r) => r.pinned);
  if (pinned.length === 0) return null;

  return (
    <section>
      <h2 className={SECTION_HEADING}>
        Watchlist
        <span className="ml-2 normal-case tracking-normal text-ink-faint font-normal">
          pinned in sentinel.yaml
        </span>
      </h2>
      <div
        className="grid gap-3"
        style={{ gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))" }}
      >
        {pinned.map((r, i) => (
          <Link
            key={r.netuid}
            to={`/subnet/${r.netuid}`}
            className="card card-hover px-3.5 py-3 rise group"
            style={{ "--i": i + 2 } as React.CSSProperties}
          >
            <div className="flex items-center justify-between gap-2">
              <div className="flex items-center gap-2 min-w-0">
                <span className="font-medium text-ink truncate group-hover:text-accent transition-colors">
                  {r.name ?? `subnet ${r.netuid}`}
                </span>
                <span className={`${NETUID_CHIP} mono`}>{r.netuid}</span>
              </div>
              <GradeBadge grade={r.grade} provisional={r.metrics.provisional} />
            </div>
            <div className="mt-2.5 flex items-end justify-between gap-3">
              <Sparkline data={r.spark} w={118} h={34} area />
              <div className="flex flex-col items-end shrink-0">
                <span className="tnum text-[13.5px] font-medium">
                  {fmtPrice(r.metrics.price_tao)}
                  <span className="unit-dim"> τ</span>
                </span>
                <span className="text-[12px]">
                  <Delta value={sparkChangePct(r.spark)} />
                </span>
              </div>
            </div>
          </Link>
        ))}
      </div>
    </section>
  );
}

export const WatchlistStrip = memo(WatchlistStripImpl);

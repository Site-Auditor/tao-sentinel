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
      <h2 className={SECTION_HEADING}>Watchlist</h2>
      <div
        className="grid gap-3"
        style={{ gridTemplateColumns: "repeat(auto-fill, minmax(210px, 1fr))" }}
      >
        {pinned.map((r) => (
          <Link
            key={r.netuid}
            to={`/subnet/${r.netuid}`}
            className="bg-surface border border-line rounded-lg px-3 py-2.5 hover:bg-surface-2 transition-colors"
          >
            <div className="flex items-center justify-between gap-2">
              <div className="flex items-center gap-2 min-w-0">
                <span className="font-medium text-ink truncate">
                  {r.name ?? `subnet ${r.netuid}`}
                </span>
                <span className={NETUID_CHIP}>{r.netuid}</span>
              </div>
              <GradeBadge
                grade={r.grade}
                provisional={r.metrics.provisional}
              />
            </div>
            <div className="mt-2 flex items-center justify-between gap-2">
              <Sparkline data={r.spark} w={110} h={30} />
              <div className="flex flex-col items-end">
                <span className="tnum text-[13px]">
                  {fmtPrice(r.metrics.price_tao)} τ
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

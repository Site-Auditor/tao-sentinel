import { memo } from "react";
import { Link } from "react-router-dom";
import type { Portfolio } from "../lib/api";
import { fmtTao, fmtUsd, fmtPct, truncAddr } from "../lib/format";
import { SECTION_HEADING, NETUID_CHIP } from "./ui";

interface PortfolioPanelProps {
  portfolio: Portfolio;
}

function PortfolioPanelImpl({ portfolio }: PortfolioPanelProps) {
  return (
    <section>
      <h2 className={SECTION_HEADING}>Portfolio</h2>
      <div className="bg-surface border border-line rounded-lg overflow-hidden">
        <div className="px-4 py-3 border-b border-line flex items-center justify-between gap-2">
          <span className="text-[12px] text-ink-dim tnum" title={portfolio.coldkey}>
            {truncAddr(portfolio.coldkey)}
          </span>
          <span className="text-right">
            <span className="font-semibold tnum">
              {fmtTao(portfolio.total_value_tao, true)}
            </span>
            {portfolio.total_value_usd != null ? (
              <span className="ml-2 text-[12px] text-ink-dim tnum">
                {fmtUsd(portfolio.total_value_usd)}
              </span>
            ) : null}
          </span>
        </div>
        <ul>
          {portfolio.positions.map((p, i) => (
            <li
              key={`${p.netuid}-${p.hotkey}-${i}`}
              className="px-4 py-2.5 border-b border-line last:border-b-0"
            >
              <div className="flex items-center justify-between gap-2">
                <div className="flex items-center gap-2 min-w-0">
                  <span className="text-[13px] text-ink truncate">
                    {p.name ?? `subnet ${p.netuid}`}
                  </span>
                  <Link to={`/subnet/${p.netuid}`} className={NETUID_CHIP}>
                    {p.netuid}
                  </Link>
                </div>
                <span className="text-[13px] tnum text-ink-dim">
                  {fmtTao(p.value_tao, true)}
                </span>
              </div>
              <div className="mt-1.5 flex items-center gap-2">
                <span className="text-[11px] text-ink-faint tnum shrink-0">
                  α {p.alpha_staked.toLocaleString("en-US")}
                </span>
                <span className="h-1 flex-1 rounded bg-surface-3 overflow-hidden">
                  <span
                    className="block h-full rounded bg-accent"
                    style={{ width: `${Math.min(100, Math.max(0, p.share_pct ?? 0))}%` }}
                  />
                </span>
                <span className="text-[11px] text-ink-dim tnum shrink-0 w-12 text-right">
                  {fmtPct(p.share_pct)}
                </span>
              </div>
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}

export const PortfolioPanel = memo(PortfolioPanelImpl);

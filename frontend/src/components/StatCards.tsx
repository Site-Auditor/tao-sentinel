import { memo } from "react";
import type { Portfolio, StatusMeta } from "../lib/api";
import { fmtCount, fmtUsd, fmtTao, sparkChangePct } from "../lib/format";
import { Delta } from "./Delta";

interface StatCardsProps {
  meta: StatusMeta;
  portfolio: Portfolio | null;
}

function Card({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="bg-surface border border-line rounded-lg px-4 py-3">
      <div className="text-[11px] uppercase tracking-wider text-ink-faint">
        {label}
      </div>
      <div className="text-xl font-semibold tnum mt-1">{children}</div>
    </div>
  );
}

function StatCardsImpl({ meta, portfolio }: StatCardsProps) {
  return (
    <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
      <Card label="Subnets">{fmtCount(meta.n_subnets)}</Card>
      <Card label="TAO price">
        <span className="flex items-baseline gap-2">
          <span>{fmtUsd(meta.tao_price_usd)}</span>
          <span className="text-[13px] font-medium">
            <Delta value={sparkChangePct(meta.tao_price_spark)} />
          </span>
        </span>
      </Card>
      <Card label="Recent alerts">{fmtCount(meta.n_alerts)}</Card>
      <Card label="Portfolio">
        {portfolio ? (
          <span className="flex items-baseline gap-2">
            <span>{fmtTao(portfolio.total_value_tao, true)}</span>
            {portfolio.total_value_usd != null ? (
              <span className="text-[13px] font-medium text-ink-dim">
                {fmtUsd(portfolio.total_value_usd)}
              </span>
            ) : null}
          </span>
        ) : (
          <span className="text-base font-normal text-ink-faint">
            not configured
          </span>
        )}
      </Card>
    </div>
  );
}

export const StatCards = memo(StatCardsImpl);

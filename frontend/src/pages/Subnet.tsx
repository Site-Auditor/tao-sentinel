import { useParams, Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { fetchStatus, fetchSubnet } from "../lib/api";
import type { SubnetDetail } from "../lib/api";
import { TopBar } from "../components/TopBar";
import { GradeBadge } from "../components/GradeBadge";
import { Delta } from "../components/Delta";
import { PriceChart } from "../components/PriceChart";
import { fmtTao, fmtPrice, fmtPct, truncAddr } from "../lib/format";
import { SECTION_HEADING, NETUID_CHIP } from "../components/ui";

function DefRow({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between py-1.5 border-b border-line last:border-b-0">
      <span className="text-ink-faint text-[12px]">{label}</span>
      <span className="tnum text-[13px]">{value}</span>
    </div>
  );
}

function isProvisional(metrics: Record<string, unknown>): boolean {
  return metrics.provisional === true;
}

/** Metric keys already surfaced elsewhere — keep the breakdown focused. */
const HIDDEN_METRIC_KEYS = new Set(["provisional"]);

function ScoreBreakdown({ metrics }: { metrics: Record<string, unknown> }) {
  const numeric = Object.entries(metrics).filter(
    ([k, v]) => typeof v === "number" && !HIDDEN_METRIC_KEYS.has(k),
  ) as [string, number][];

  if (numeric.length === 0) return null;

  return (
    <section>
      <h2 className={SECTION_HEADING}>Score breakdown</h2>
      <div className="bg-surface border border-line rounded-lg px-4 py-2">
        {numeric.map(([k, v]) => (
          <DefRow
            key={k}
            label={k.replace(/_/g, " ")}
            value={Number.isInteger(v) ? v.toLocaleString("en-US") : v.toFixed(2)}
          />
        ))}
        {isProvisional(metrics) ? (
          <div className="pt-2 text-[11.5px] text-warn">
            Provisional — concentration-blind score.
          </div>
        ) : null}
      </div>
    </section>
  );
}

function WarningsCard({ warnings }: { warnings: string[] }) {
  if (warnings.length === 0) return null;
  return (
    <section>
      <h2 className={SECTION_HEADING}>Warnings</h2>
      <div className="bg-surface border border-line rounded-lg px-4 py-3 flex flex-col gap-2">
        {warnings.map((w, i) => (
          <div key={i} className="flex items-start gap-2 text-[13px] text-ink-dim">
            <span className="mt-1.5 shrink-0 w-1.5 h-1.5 rounded-full bg-warn" />
            <span>{w}</span>
          </div>
        ))}
      </div>
    </section>
  );
}

function ValidatorsTable({ detail }: { detail: SubnetDetail }) {
  const vals = detail.validators;
  return (
    <section>
      <h2 className={SECTION_HEADING}>Validators</h2>
      <div className="bg-surface border border-line rounded-lg overflow-hidden">
        {vals.length === 0 ? (
          <div className="px-4 py-6 text-[13px] text-ink-faint">
            No validator data — try again after the next refresh.
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-[13px]">
              <thead>
                <tr className="text-[11px] uppercase tracking-wider text-ink-faint font-medium">
                  <th className="text-right py-2 px-3 w-12">#</th>
                  <th className="text-left py-2 px-3">hotkey</th>
                  <th className="text-right py-2 px-3">stake τ</th>
                  <th className="text-right py-2 px-3">share</th>
                  <th className="text-right py-2 px-3">vtrust</th>
                </tr>
              </thead>
              <tbody>
                {vals.map((v, i) => (
                  <tr
                    key={v.hotkey}
                    className="h-[38px] border-t border-line hover:bg-surface-2 transition-colors"
                  >
                    <td className="text-right px-3 tnum text-ink-dim">{i + 1}</td>
                    <td className="px-3 tnum" title={v.hotkey}>
                      {truncAddr(v.hotkey)}
                    </td>
                    <td className="text-right px-3 tnum">{fmtTao(v.stake_tao, true)}</td>
                    <td className="px-3">
                      <div className="flex items-center justify-end gap-2">
                        <span className="h-1 w-16 rounded bg-surface-3 overflow-hidden">
                          <span
                            className="block h-full rounded bg-accent"
                            style={{
                              width: `${Math.min(100, Math.max(0, v.share_pct))}%`,
                            }}
                          />
                        </span>
                        <span className="tnum w-12 text-right">
                          {fmtPct(v.share_pct)}
                        </span>
                      </div>
                    </td>
                    <td className="text-right px-3 tnum">
                      {v.vtrust == null ? "—" : v.vtrust.toFixed(2)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </section>
  );
}

export default function Subnet() {
  const { netuid: netuidParam } = useParams();
  const netuid = Number(netuidParam);

  const statusQuery = useQuery({ queryKey: ["status"], queryFn: fetchStatus });
  const {
    data: detail,
    isLoading,
    isError,
  } = useQuery({
    queryKey: ["subnet", netuid],
    queryFn: () => fetchSubnet(netuid),
    enabled: Number.isFinite(netuid),
  });

  const meta = statusQuery.data?.meta ?? null;

  if (isError || (!Number.isFinite(netuid) && !isLoading)) {
    return (
      <>
        <TopBar meta={meta} />
        <main className="max-w-[1200px] mx-auto px-5 pt-16 flex justify-center">
          <div className="bg-surface border border-line rounded-lg px-6 py-8 text-center max-w-sm">
            <div className="text-ink font-medium">Subnet not found.</div>
            <div className="text-ink-dim text-[13px] mt-1">
              No report for subnet {netuidParam}.
            </div>
            <Link
              to="/"
              className="inline-block mt-4 border border-accent text-accent rounded px-3 py-1 text-[13px] hover:bg-surface-2 transition-colors"
            >
              ← Back to subnets
            </Link>
          </div>
        </main>
      </>
    );
  }

  if (isLoading || !detail) {
    return (
      <>
        <TopBar meta={meta} />
        <main className="max-w-[1200px] mx-auto px-5 pt-6 pb-16 flex flex-col gap-6">
          <div className="skeleton h-6 w-48" />
          <div className="grid lg:grid-cols-[1fr_340px] gap-6">
            <div className="skeleton h-[420px]" />
            <div className="skeleton h-[300px]" />
          </div>
        </main>
      </>
    );
  }

  const { report, pool } = detail;
  const title = detail.name ?? `subnet ${detail.netuid}`;

  return (
    <>
      <TopBar meta={meta} />
      <main className="max-w-[1200px] mx-auto px-5 pt-5 pb-16 flex flex-col gap-6">
        <Link
          to="/"
          className="text-ink-dim hover:text-accent text-[13px] transition-colors w-fit"
        >
          ← subnets
        </Link>

        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-2xl font-semibold text-ink">{title}</h1>
          <span className={NETUID_CHIP}>{detail.netuid}</span>
          <GradeBadge grade={report.grade} provisional={isProvisional(report.metrics)} />
          <span className="tnum text-ink-dim text-[15px]">
            {report.score.toFixed(1)}
          </span>
          <span className="flex items-center gap-1.5 text-[12px] text-ink-faint">
            <Delta value={detail.spark_change_pct} />
            <span className="uppercase tracking-wide">24h</span>
          </span>
        </div>

        <div className="grid lg:grid-cols-[minmax(0,1fr)_340px] gap-6">
          <div className="flex flex-col gap-6 min-w-0">
            <section>
              <h2 className={SECTION_HEADING}>Price (24h)</h2>
              <div className="bg-surface border border-line rounded-lg px-3 py-3 overflow-hidden">
                <PriceChart spark={detail.spark} height={280} />
              </div>
            </section>
            <ValidatorsTable detail={detail} />
          </div>

          <div className="flex flex-col gap-6">
            {pool ? (
              <section>
                <h2 className={SECTION_HEADING}>Pool</h2>
                <div className="bg-surface border border-line rounded-lg px-4 py-2">
                  <DefRow label="price" value={`${fmtPrice(pool.price_tao)} τ`} />
                  <DefRow label="market cap" value={fmtTao(pool.market_cap_tao, true)} />
                  <DefRow label="τ in pool" value={fmtTao(pool.tao_in, true)} />
                  <DefRow
                    label="alpha in pool"
                    value={
                      pool.alpha_in == null
                        ? "—"
                        : pool.alpha_in.toLocaleString("en-US")
                    }
                  />
                </div>
              </section>
            ) : null}

            <ScoreBreakdown metrics={report.metrics} />
            <WarningsCard warnings={report.warnings} />
          </div>
        </div>
      </main>
    </>
  );
}

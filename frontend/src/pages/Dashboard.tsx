import { useQuery } from "@tanstack/react-query";
import { fetchStatus } from "../lib/api";
import { TopBar } from "../components/TopBar";
import { StatCards } from "../components/StatCards";
import { WatchlistStrip } from "../components/WatchlistStrip";
import { SubnetsTable } from "../components/SubnetsTable";
import { AlertsPanel } from "../components/AlertsPanel";
import { PortfolioPanel } from "../components/PortfolioPanel";

const REPO_URL = "https://github.com/Site-Auditor/tao-sentinel";

function Skeleton() {
  return (
    <main className="max-w-[1200px] mx-auto px-5 pb-16 flex flex-col gap-8 pt-8">
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        {Array.from({ length: 4 }).map((_, i) => (
          <div key={i} className="skeleton h-[68px]" />
        ))}
      </div>
      <div className="grid grid-cols-[minmax(0,1fr)] lg:grid-cols-[minmax(0,1fr)_340px] gap-6">
        <div className="skeleton h-[420px]" />
        <div className="flex flex-col gap-6 rise" style={{ "--i": 5 } as React.CSSProperties}>
          <div className="skeleton h-[200px]" />
          <div className="skeleton h-[180px]" />
        </div>
      </div>
    </main>
  );
}

function ErrorCard({ onRetry }: { onRetry: () => void }) {
  return (
    <main className="max-w-[1200px] mx-auto px-5 pt-16 flex justify-center">
      <div className="bg-surface border border-line rounded-lg px-6 py-8 text-center max-w-sm">
        <div className="text-ink font-medium">Couldn’t load status.</div>
        <div className="text-ink-dim text-[13px] mt-1">
          The dashboard API didn’t respond.
        </div>
        <button
          type="button"
          onClick={onRetry}
          className="mt-4 border border-accent text-accent rounded px-3 py-1 text-[13px] hover:bg-surface-2 transition-colors"
        >
          Retry
        </button>
      </div>
    </main>
  );
}

export default function Dashboard() {
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ["status"],
    queryFn: fetchStatus,
  });

  if (isLoading) {
    return (
      <>
        <TopBar meta={null} />
        <Skeleton />
      </>
    );
  }

  if (isError || !data) {
    return (
      <>
        <TopBar meta={null} />
        <ErrorCard onRetry={() => void refetch()} />
      </>
    );
  }

  const { meta, subnets, alerts, portfolio } = data;

  return (
    <>
      <TopBar meta={meta} />
      <main className="max-w-[1200px] mx-auto px-5 pb-16 flex flex-col gap-8 pt-6">
        <StatCards meta={meta} portfolio={portfolio} />

        <WatchlistStrip rows={subnets} />

        <div className="flex flex-col gap-3">
          {meta.provisional ? (
            <div className="border-l-2 border-warn bg-surface px-3 py-2 text-[12.5px] text-ink-dim rounded-r">
              Provisional scores exclude validator concentration — treat grades
              as directional until full data lands.
            </div>
          ) : null}

          <div className="grid grid-cols-[minmax(0,1fr)] lg:grid-cols-[minmax(0,1fr)_340px] gap-6">
            <SubnetsTable rows={subnets} />
            <div className="flex flex-col gap-6">
              <AlertsPanel alerts={alerts} />
              {portfolio ? <PortfolioPanel portfolio={portfolio} /> : null}
            </div>
          </div>
        </div>

        <footer className="text-ink-faint text-[12px] border-t border-line pt-4">
          data: taostats · refreshed every 5m · concentration shown as risk
          warnings, not scored ·{" "}
          <a
            href={REPO_URL}
            className="text-ink-faint hover:text-accent transition-colors"
            target="_blank"
            rel="noreferrer"
          >
            GitHub
          </a>
        </footer>
      </main>
    </>
  );
}

import { memo, useEffect, useRef } from "react";
import { animate, useReducedMotion } from "motion/react";
import type { Portfolio, StatusMeta } from "../lib/api";
import { fmtUsd, fmtTao, sparkChangePct } from "../lib/format";
import { Delta } from "./Delta";
import { Sparkline } from "./Sparkline";

interface StatCardsProps {
  meta: StatusMeta;
  portfolio: Portfolio | null;
}

/** A number that counts up to its value on mount — the "instrument coming
 * online" moment. Falls back to a static render under reduced motion. */
function CountUp({
  value,
  format,
}: {
  value: number;
  format: (v: number) => string;
}) {
  const ref = useRef<HTMLSpanElement>(null);
  const reduced = useReducedMotion();

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (reduced) {
      el.textContent = format(value);
      return;
    }
    const controls = animate(0, value, {
      duration: 0.9,
      ease: [0.22, 1, 0.36, 1],
      onUpdate: (v) => {
        el.textContent = format(v);
      },
    });
    return () => controls.stop();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value, reduced]);

  return <span ref={ref} className="tnum" />;
}

function Card({
  label,
  index,
  children,
  foot,
}: {
  label: string;
  index: number;
  children: React.ReactNode;
  foot?: React.ReactNode;
}) {
  return (
    <div
      className="card card-hover px-4 py-3.5 rise"
      style={{ "--i": index } as React.CSSProperties}
    >
      <div className="text-[10.5px] uppercase tracking-[0.14em] text-ink-faint font-medium">
        {label}
      </div>
      <div className="text-[22px] leading-7 font-semibold tnum mt-1.5 tracking-tight">
        {children}
      </div>
      {foot ? <div className="mt-1 text-[12px]">{foot}</div> : null}
    </div>
  );
}

function StatCardsImpl({ meta, portfolio }: StatCardsProps) {
  const taoDelta = sparkChangePct(meta.tao_price_spark);
  return (
    <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
      <Card
        label="Subnets tracked"
        index={0}
        foot={<span className="text-ink-faint">graded A–F each refresh</span>}
      >
        <CountUp value={meta.n_subnets} format={(v) => Math.round(v).toString()} />
      </Card>

      <Card
        label="TAO price"
        index={1}
        foot={
          <span className="flex items-center gap-2">
            <Delta value={taoDelta} />
            <span className="text-ink-faint">24h</span>
            <span className="ml-auto">
              <Sparkline data={meta.tao_price_spark} w={64} h={18} animate={false} />
            </span>
          </span>
        }
      >
        {meta.tao_price_usd != null ? (
          <CountUp value={meta.tao_price_usd} format={(v) => fmtUsd(v)} />
        ) : (
          <span className="text-ink-faint text-base font-normal">—</span>
        )}
      </Card>

      <Card
        label="Recent alerts"
        index={2}
        foot={
          <span className="text-ink-faint">
            {meta.n_alerts === 0 ? "quiet network" : "see timeline →"}
          </span>
        }
      >
        <CountUp value={meta.n_alerts} format={(v) => Math.round(v).toString()} />
      </Card>

      <Card
        label="Portfolio"
        index={3}
        foot={
          portfolio?.total_value_usd != null ? (
            <span className="text-ink-dim tnum">{fmtUsd(portfolio.total_value_usd)}</span>
          ) : portfolio ? null : (
            <span className="text-ink-faint">add a coldkey watch to enable</span>
          )
        }
      >
        {portfolio ? (
          <CountUp
            value={portfolio.total_value_tao}
            format={(v) => fmtTao(v, true)}
          />
        ) : (
          <span className="text-ink-faint text-base font-normal">not configured</span>
        )}
      </Card>
    </div>
  );
}

export const StatCards = memo(StatCardsImpl);

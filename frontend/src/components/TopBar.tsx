import { memo } from "react";
import { Link } from "react-router-dom";
import type { StatusMeta } from "../lib/api";
import { fmtUsd, timeAgo, sparkChangePct } from "../lib/format";
import { Sparkline } from "./Sparkline";
import { Delta } from "./Delta";
import { BellIcon } from "./icons";

interface TopBarProps {
  meta: StatusMeta | null;
}

function TopBarImpl({ meta }: TopBarProps) {
  return (
    <header className="sticky top-0 z-30 backdrop-blur bg-bg/85 border-b border-line">
      <div className="max-w-[1200px] mx-auto px-5 h-[56px] flex items-center justify-between gap-4">
        <Link
          to="/"
          className="font-semibold tracking-tight text-ink text-[15px] shrink-0"
        >
          tao<span className="text-accent">·</span>sentinel
        </Link>

        {meta ? (
          <div className="flex items-center gap-3 sm:gap-5 min-w-0">
            <div className="flex items-center gap-2 min-w-0">
              <span className="hidden sm:inline text-ink-faint text-[11px] uppercase tracking-wide">
                TAO
              </span>
              <span className="font-medium tnum whitespace-nowrap">
                {fmtUsd(meta.tao_price_usd)}
              </span>
              {/* The sparkline is a luxury; price + delta carry the signal
                  on narrow screens. */}
              <span className="hidden sm:inline-flex">
                <Sparkline data={meta.tao_price_spark} w={72} h={20} />
              </span>
              <Delta value={sparkChangePct(meta.tao_price_spark)} />
            </div>

            {meta.mock ? (
              <span className="flex items-center gap-1.5 text-[11px] uppercase tracking-wide border rounded-full px-2.5 py-0.5 text-warn border-warn/40">
                <span className="w-1.5 h-1.5 rounded-full bg-warn" />
                Mock
              </span>
            ) : (
              <span className="flex items-center gap-1.5 text-[11px] uppercase tracking-wide border rounded-full px-2.5 py-0.5 text-up border-up/40">
                <span className="pulse-dot" />
                Live
              </span>
            )}

            <span className="hidden sm:inline text-ink-faint text-[12px]">
              updated {timeAgo(meta.generated_at)}
            </span>

            {/* Funnel: the bot is the product, the dashboard is the shop
                window. Short label so the 390px row never overflows. */}
            <a
              href="https://t.me/TaoSentinelBot"
              target="_blank"
              rel="noopener noreferrer"
              className="shrink-0 flex items-center gap-1.5 text-[11px] uppercase tracking-wide border rounded-full px-2.5 py-0.5 text-accent border-accent/40 hover:bg-accent-dim/40 transition-colors"
            >
              <BellIcon />
              <span className="hidden min-[420px]:inline whitespace-nowrap">Alerts</span>
            </a>
          </div>
        ) : null}
      </div>
    </header>
  );
}

export const TopBar = memo(TopBarImpl);

import { memo } from "react";
import { Link } from "react-router-dom";
import type { AlertItem } from "../lib/api";
import { timeAgo } from "../lib/format";
import { SECTION_HEADING, NETUID_CHIP } from "./ui";

interface AlertsPanelProps {
  alerts: AlertItem[];
}

const sevDot: Record<AlertItem["severity"], string> = {
  info: "bg-accent",
  warning: "bg-warn",
  critical: "bg-down",
};

const MAX = 8;

function AlertsPanelImpl({ alerts }: AlertsPanelProps) {
  const shown = alerts.slice(0, MAX);
  const extra = alerts.length - shown.length;

  return (
    <section>
      <h2 className={SECTION_HEADING}>Recent alerts</h2>
      <div className="bg-surface border border-line rounded-lg overflow-hidden">
        {shown.length === 0 ? (
          <div className="px-4 py-6 text-[13px] text-ink-faint">
            No alerts in the last cycles — quiet network.
          </div>
        ) : (
          <ul>
            {shown.map((a, i) => (
              <li
                key={`${a.timestamp}-${i}`}
                className="flex items-start gap-2.5 px-4 py-2.5 border-b border-line last:border-b-0"
              >
                <span
                  className={`mt-1.5 shrink-0 w-1.5 h-1.5 rounded-full ${sevDot[a.severity]}`}
                />
                <div className="min-w-0 flex-1">
                  <div className="text-[13px] text-ink">{a.title}</div>
                  <div className="text-[12px] text-ink-dim line-clamp-1">
                    {a.message}
                  </div>
                </div>
                <div className="shrink-0 flex flex-col items-end gap-1">
                  {a.netuid != null ? (
                    <Link to={`/subnet/${a.netuid}`} className={NETUID_CHIP}>
                      {a.netuid}
                    </Link>
                  ) : null}
                  <span className="text-ink-faint text-[11px]">
                    {timeAgo(a.timestamp)}
                  </span>
                </div>
              </li>
            ))}
          </ul>
        )}
        {extra > 0 ? (
          <div className="px-4 py-2 text-[12px] text-ink-faint border-t border-line">
            {extra} more
          </div>
        ) : null}
      </div>
    </section>
  );
}

export const AlertsPanel = memo(AlertsPanelImpl);

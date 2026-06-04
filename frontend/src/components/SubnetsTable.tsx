import { memo, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  createColumnHelper,
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  useReactTable,
} from "@tanstack/react-table";
import type { SortingState } from "@tanstack/react-table";
import type { SubnetRow } from "../lib/api";
import { fmtCount, fmtPct, fmtPrice, fmtTao } from "../lib/format";
import { GradeBadge } from "./GradeBadge";

type Grade = "A" | "B" | "C" | "D" | "F";
const GRADES: Grade[] = ["A", "B", "C", "D", "F"];

const gradeVar: Record<Grade, string> = {
  A: "--color-grade-a",
  B: "--color-grade-b",
  C: "--color-grade-c",
  D: "--color-grade-d",
  F: "--color-grade-f",
};

const col = createColumnHelper<SubnetRow>();

interface SubnetsTableProps {
  rows: SubnetRow[];
}

function SortChevron({ dir }: { dir: false | "asc" | "desc" }) {
  return (
    <span className="ml-1 inline-flex flex-col leading-none text-[9px]">
      <span className={dir === "asc" ? "text-accent" : "text-ink-faint/50"}>
        ▲
      </span>
      <span className={dir === "desc" ? "text-accent" : "text-ink-faint/50"}>
        ▼
      </span>
    </span>
  );
}

function SubnetsTableImpl({ rows }: SubnetsTableProps) {
  const [sorting, setSorting] = useState<SortingState>([
    { id: "score", desc: true },
  ]);
  const [query, setQuery] = useState("");
  const [activeGrades, setActiveGrades] = useState<Set<Grade>>(new Set());

  const columns = useMemo(
    () => [
      col.accessor("netuid", {
        header: "netuid",
        cell: (c) => <span className="text-ink-dim tnum">{c.getValue()}</span>,
        size: 60,
      }),
      col.accessor((r) => r.name ?? "", {
        id: "name",
        header: "name",
        cell: (c) => {
          const r = c.row.original;
          return (
            <Link
              to={`/subnet/${r.netuid}`}
              className="font-medium text-ink group-hover:text-accent transition-colors"
            >
              {r.name ?? "—"}
            </Link>
          );
        },
      }),
      col.accessor("score", {
        id: "grade",
        header: "grade",
        cell: (c) => (
          <GradeBadge
            grade={c.row.original.grade}
            provisional={c.row.original.metrics.provisional}
          />
        ),
      }),
      col.accessor("score", {
        id: "score",
        header: "score",
        cell: (c) => (
          <span className="tnum">{c.getValue().toFixed(1)}</span>
        ),
      }),
      col.accessor((r) => r.metrics.price_tao, {
        id: "price",
        header: "price τ",
        cell: (c) => <span className="tnum">{fmtPrice(c.getValue())}</span>,
        sortUndefined: "last",
      }),
      col.accessor((r) => r.metrics.market_cap_tao, {
        id: "mcap",
        header: "mcap τ",
        cell: (c) => <span className="tnum">{fmtTao(c.getValue(), true)}</span>,
        sortUndefined: "last",
      }),
      col.accessor((r) => r.metrics.emission_pct, {
        id: "emission",
        header: "emission %",
        cell: (c) => <span className="tnum">{fmtPct(c.getValue())}</span>,
        sortUndefined: "last",
      }),
      col.accessor(
        (r) => r.metrics.n_active_validators ?? r.metrics.n_validators,
        {
          id: "validators",
          header: "validators",
          cell: (c) => <span className="tnum">{fmtCount(c.getValue())}</span>,
          sortUndefined: "last",
        },
      ),
      col.accessor((r) => r.metrics.n_miners, {
        id: "miners",
        header: "miners",
        cell: (c) => <span className="tnum">{fmtCount(c.getValue())}</span>,
        sortUndefined: "last",
      }),
      col.accessor((r) => r.warnings.length, {
        id: "warnings",
        header: "warnings",
        cell: (c) => {
          const w = c.row.original.warnings;
          if (w.length === 0) {
            return <span className="text-ink-faint">—</span>;
          }
          return (
            <span
              className="inline-flex items-center gap-1.5 tnum"
              title={w.join("\n")}
            >
              <span className="w-1.5 h-1.5 rounded-full bg-warn" />
              {w.length}
            </span>
          );
        },
      }),
    ],
    [],
  );

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return rows.filter((r) => {
      if (activeGrades.size > 0 && !activeGrades.has(r.grade)) return false;
      if (!q) return true;
      const name = (r.name ?? "").toLowerCase();
      return name.includes(q) || String(r.netuid).includes(q);
    });
  }, [rows, query, activeGrades]);

  const table = useReactTable({
    data: filtered,
    columns,
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
  });

  const toggleGrade = (g: Grade) => {
    setActiveGrades((prev) => {
      const next = new Set(prev);
      if (next.has(g)) next.delete(g);
      else next.add(g);
      return next;
    });
  };

  // numeric columns are right-aligned
  const rightAligned = new Set([
    "score",
    "price",
    "mcap",
    "emission",
    "validators",
    "miners",
    "warnings",
    "netuid",
  ]);

  return (
    <section>
      <div className="flex flex-wrap items-center gap-3 mb-3">
        <label className="relative inline-flex items-center w-full sm:w-auto">
          <svg
            className="absolute left-2.5 w-3.5 h-3.5 text-ink-faint pointer-events-none"
            viewBox="0 0 16 16"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.6"
            aria-hidden="true"
          >
            <circle cx="7" cy="7" r="4.5" />
            <path d="m10.5 10.5 3 3" strokeLinecap="round" />
          </svg>
          <input
            type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
            placeholder="Search subnets…"
            className="bg-surface border border-line rounded-md pl-8 pr-3 py-1.5 w-full sm:w-60 placeholder-ink-faint focus:border-accent outline-none text-[13px] transition-colors"
          />
        </label>
        <div className="flex items-center gap-1.5">
          {GRADES.map((g) => {
            const active = activeGrades.has(g);
            const v = `var(${gradeVar[g]})`;
            return (
              <button
                key={g}
                type="button"
                onClick={() => toggleGrade(g)}
                className="text-[12px] font-semibold rounded-full border border-line px-2.5 py-0.5 transition-all hover:border-line-2 active:scale-95"
                style={
                  active
                    ? {
                        color: v,
                        borderColor: v,
                        backgroundColor: `color-mix(in srgb, ${v} 12%, transparent)`,
                      }
                    : undefined
                }
              >
                <span className={active ? "" : "text-ink-dim"}>{g}</span>
              </button>
            );
          })}
        </div>
        <span className="ml-auto text-ink-faint text-[12px] tnum">
          {filtered.length} subnet{filtered.length === 1 ? "" : "s"}
        </span>
      </div>

      <div className="card overflow-hidden rise" style={{ "--i": 4 } as React.CSSProperties}>
        {/* The card itself is the scroll container (both axes); the thead
            sticks to ITS top — sticky inside an overflow ancestor is
            relative to that ancestor, so a viewport offset like top-[56px]
            renders the header pushed down OVER the first rows instead. */}
        <div className="overflow-auto max-h-[calc(100vh-230px)] min-h-[320px]">
          <table className="w-full border-collapse text-[12.5px]">
            <thead>
              {table.getHeaderGroups().map((hg) => (
                <tr key={hg.id}>
                  {hg.headers.map((h) => {
                    const sortable = h.column.getCanSort();
                    const right = rightAligned.has(h.column.id);
                    return (
                      <th
                        key={h.id}
                        onClick={
                          sortable
                            ? h.column.getToggleSortingHandler()
                            : undefined
                        }
                        className={[
                          "sticky top-0 z-10 bg-surface-2/95 backdrop-blur",
                          "text-[11px] uppercase tracking-wider text-ink-faint font-medium",
                          "py-2.5 px-2.5 whitespace-nowrap border-b border-line",
                          right ? "text-right" : "text-left",
                          sortable ? "cursor-pointer select-none" : "",
                        ].join(" ")}
                      >
                        <span
                          className={
                            right
                              ? "inline-flex items-center justify-end"
                              : "inline-flex items-center"
                          }
                        >
                          {flexRender(
                            h.column.columnDef.header,
                            h.getContext(),
                          )}
                          {sortable ? (
                            <SortChevron dir={h.column.getIsSorted()} />
                          ) : null}
                        </span>
                      </th>
                    );
                  })}
                </tr>
              ))}
            </thead>
            <tbody>
              {table.getRowModel().rows.map((row) => (
                <tr
                  key={row.id}
                  className="group row-hover h-[42px] border-b border-line last:border-b-0 hover:bg-surface-2"
                >
                  {row.getVisibleCells().map((cell) => {
                    const right = rightAligned.has(cell.column.id);
                    return (
                      <td
                        key={cell.id}
                        className={[
                          "px-2.5 whitespace-nowrap",
                          right ? "text-right tnum" : "text-left",
                        ].join(" ")}
                      >
                        {flexRender(
                          cell.column.columnDef.cell,
                          cell.getContext(),
                        )}
                      </td>
                    );
                  })}
                </tr>
              ))}
              {table.getRowModel().rows.length === 0 ? (
                <tr>
                  <td
                    colSpan={columns.length}
                    className="px-2.5 py-8 text-center text-ink-faint"
                  >
                    No subnets match your filters.
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}

export const SubnetsTable = memo(SubnetsTableImpl);

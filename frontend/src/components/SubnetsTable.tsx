import React, { memo, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  createColumnHelper,
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  useReactTable,
} from "@tanstack/react-table";
import type { SortingState, VisibilityState } from "@tanstack/react-table";
import { useMediaQuery } from "../lib/useMediaQuery";
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
  // One arrow that flips — quiet until the column is actually sorted.
  if (!dir) {
    return (
      <span
        aria-hidden
        className="ml-1 text-[9px] text-ink-faint/0 group-hover/th:text-ink-faint/60 transition-colors"
      >
        ▲
      </span>
    );
  }
  return (
    <span
      aria-hidden
      className={[
        "ml-1 text-[9px] text-accent inline-block transition-transform",
        dir === "desc" ? "rotate-180" : "",
      ].join(" ")}
    >
      ▲
    </span>
  );
}

function SubnetsTableImpl({ rows }: SubnetsTableProps) {
  const [sorting, setSorting] = useState<SortingState>([
    { id: "score", desc: true },
  ]);
  const [query, setQuery] = useState("");
  const [activeGrades, setActiveGrades] = useState<Set<Grade>>(new Set());
  const navigate = useNavigate();

  // Columns adapt to the table's EFFECTIVE width, not just the viewport:
  // the 340px side panel appears at lg, so the table is actually narrower
  // at 1024–1279 than at 768–1023. No tier ever scrolls horizontally —
  // dropped columns live on the detail page a row-tap opens.
  const isSm = useMediaQuery("(min-width: 640px)");
  const isMd = useMediaQuery("(min-width: 768px)");
  const isLg = useMediaQuery("(min-width: 1024px)");
  const isXl = useMediaQuery("(min-width: 1280px)");
  const midColumns = (isMd && !isLg) || isXl; // full-width md, or wide xl
  // Progressive disclosure: when columns are hidden, tapping a row expands
  // an inline strip with exactly those fields (one row open at a time).
  const [expandedNetuid, setExpandedNetuid] = useState<number | null>(null);
  const columnVisibility: VisibilityState = {
    // Below sm the GRADE badge carries the signal; the numeric score would
    // push 5-column min-content past a 390px viewport.
    score: isSm,
    mcap: isSm,
    emission: midColumns,
    validators: midColumns,
    miners: isXl,
    warnings: isXl,
  };

  // The same visibility map drives the expansion strip, so it always shows
  // exactly what the current tier hides — no separate mobile markup to drift.
  const HIDDEN_FIELDS: Array<{
    id: string;
    label: string;
    value: (r: SubnetRow) => React.ReactNode;
  }> = [
    { id: "score", label: "Score", value: (r) => r.score.toFixed(1) },
    { id: "mcap", label: "Market cap", value: (r) => fmtTao(r.metrics.market_cap_tao, true) },
    { id: "emission", label: "Emission", value: (r) => fmtPct(r.metrics.emission_pct) },
    {
      id: "validators",
      label: "Validators",
      value: (r) => fmtCount(r.metrics.n_active_validators ?? r.metrics.n_validators),
    },
    { id: "miners", label: "Miners", value: (r) => fmtCount(r.metrics.n_miners) },
    {
      id: "warnings",
      label: "Warnings",
      value: (r) =>
        r.warnings.length === 0 ? (
          <span className="text-ink-faint">none</span>
        ) : (
          <span className="text-warn" title={r.warnings.join("\n")}>
            {r.warnings.length}
          </span>
        ),
    },
  ];
  const hiddenFields = HIDDEN_FIELDS.filter(
    (f) => columnVisibility[f.id] === false,
  );
  const expandable = hiddenFields.length > 0;

  const columns = useMemo(
    () => [
      col.accessor("netuid", {
        header: "netuid",
        cell: (c) => <span className="text-ink-dim tnum">{c.getValue()}</span>,
        size: 60,
      }),  // chevron affordance is injected at render time (needs row state)
      col.accessor((r) => r.name ?? "", {
        id: "name",
        header: "name",
        cell: (c) => {
          const r = c.row.original;
          return (
            <Link
              to={`/subnet/${r.netuid}`}
              onClick={(e) => e.stopPropagation()}
              title={r.name ?? undefined}
              className="block truncate max-w-[min(120px,26vw)] sm:max-w-[180px] xl:max-w-[230px] font-medium text-ink group-hover:text-accent transition-colors"
            >
              {r.name ?? "—"}
            </Link>
          );
        },
      }),
      col.accessor("score", {
        id: "grade",
        header: "grade",
        size: 56,
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
        size: 60,
        cell: (c) => (
          <span className="tnum">{c.getValue().toFixed(1)}</span>
        ),
      }),
      col.accessor((r) => r.metrics.price_tao, {
        id: "price",
        header: "price τ",
        size: 76,
        cell: (c) => <span className="tnum">{fmtPrice(c.getValue())}</span>,
        sortUndefined: "last",
      }),
      col.accessor((r) => r.metrics.market_cap_tao, {
        id: "mcap",
        header: "mcap τ",
        size: 86,
        cell: (c) => <span className="tnum">{fmtTao(c.getValue(), true)}</span>,
        sortUndefined: "last",
      }),
      col.accessor((r) => r.metrics.emission_pct, {
        id: "emission",
        header: "emis %",
        size: 86,
        cell: (c) => <span className="tnum">{fmtPct(c.getValue())}</span>,
        sortUndefined: "last",
      }),
      col.accessor(
        (r) => r.metrics.n_active_validators ?? r.metrics.n_validators,
        {
          id: "validators",
          header: "vals",
          size: 84,
          cell: (c) => <span className="tnum">{fmtCount(c.getValue())}</span>,
          sortUndefined: "last",
        },
      ),
      col.accessor((r) => r.metrics.n_miners, {
        id: "miners",
        header: "miners",
        size: 64,
        cell: (c) => <span className="tnum">{fmtCount(c.getValue())}</span>,
        sortUndefined: "last",
      }),
      col.accessor((r) => r.warnings.length, {
        id: "warnings",
        header: "warn",
        size: 84,
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
    state: { sorting, columnVisibility },
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
    <section className="overflow-x-clip">
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

      {/* No inner scroll container: columns are width-adaptive so nothing
          ever overflows horizontally, the PAGE is the only vertical scroll
          (no scroll-trap), and the header can stick to the viewport just
          below the 56px topbar. The card must not clip (no overflow-hidden)
          or sticky would break. */}
      <div className="card rise" style={{ "--i": 4 } as React.CSSProperties}>
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
                          "group/th sticky top-[56px] z-10 bg-surface-2/95 backdrop-blur",
                          "first:rounded-tl-[9px] last:rounded-tr-[9px]",
                          "text-[11px] uppercase tracking-wide text-ink-faint font-medium",
                          "py-2.5 px-2 whitespace-nowrap border-b border-line",
                          right ? "text-right" : "text-left",
                          sortable ? "cursor-pointer select-none hover:text-ink-dim transition-colors" : "",
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
              {table.getRowModel().rows.map((row) => {
                const netuid = row.original.netuid;
                const isOpen = expandable && expandedNetuid === netuid;
                const visibleCells = row.getVisibleCells();
                return (
                  <React.Fragment key={row.id}>
                    <tr
                      onClick={() =>
                        expandable
                          ? setExpandedNetuid(isOpen ? null : netuid)
                          : navigate(`/subnet/${netuid}`)
                      }
                      aria-expanded={expandable ? isOpen : undefined}
                      className={[
                        "group row-hover h-[42px] border-b border-line",
                        "last:border-b-0 hover:bg-surface-2 cursor-pointer",
                        isOpen ? "bg-surface-2" : "",
                      ].join(" ")}
                    >
                      {visibleCells.map((cell, ci) => {
                        const right = rightAligned.has(cell.column.id);
                        return (
                          <td
                            key={cell.id}
                            className={[
                              "px-2 whitespace-nowrap",
                              right ? "text-right tnum" : "text-left",
                            ].join(" ")}
                          >
                            {ci === 0 && expandable ? (
                              <span className="inline-flex items-center gap-1.5 justify-end w-full">
                                <span
                                  aria-hidden
                                  className={[
                                    "text-[8px] text-ink-faint transition-transform",
                                    isOpen ? "rotate-90 text-accent" : "",
                                  ].join(" ")}
                                >
                                  ▶
                                </span>
                                {flexRender(
                                  cell.column.columnDef.cell,
                                  cell.getContext(),
                                )}
                              </span>
                            ) : (
                              flexRender(
                                cell.column.columnDef.cell,
                                cell.getContext(),
                              )
                            )}
                          </td>
                        );
                      })}
                    </tr>
                    {isOpen ? (
                      <tr className="border-b border-line bg-surface-2/40">
                        <td colSpan={visibleCells.length} className="px-3 py-3">
                          <div className="fade-in grid grid-cols-2 min-[480px]:grid-cols-3 gap-x-6 gap-y-2.5">
                            {hiddenFields.map((f) => (
                              <div key={f.id}>
                                <div className="text-[10px] uppercase tracking-[0.12em] text-ink-faint font-medium">
                                  {f.label}
                                </div>
                                <div className="tnum text-[13px] mt-0.5">
                                  {f.value(row.original)}
                                </div>
                              </div>
                            ))}
                            <div className="col-span-full pt-1">
                              <Link
                                to={`/subnet/${netuid}`}
                                onClick={(e) => e.stopPropagation()}
                                className="text-accent text-[12px] hover:underline"
                              >
                                Full details →
                              </Link>
                            </div>
                          </div>
                        </td>
                      </tr>
                    ) : null}
                  </React.Fragment>
                );
              })}
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
    </section>
  );
}

export const SubnetsTable = memo(SubnetsTableImpl);

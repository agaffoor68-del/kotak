"use client";

/**
 * Shared TradingView Presentational Primitives.
 * Strict TradingView Mobile Web Design System:
 * - Panels: #1e222d, border #2a2e39, rounded-md (4px), p-2/p-3, dense typography.
 * - Colors: Green #089981, Red #f23645, Blue #2962ff, Text #d1d4dc, Muted #787b86.
 */

import Link from "next/link";
import type { ReactNode } from "react";
import { ChevronRight } from "lucide-react";
import { toneBg, toneClass } from "@/lib/format";

/* ------------------------------------------------------------------ TV Panel */

export function Panel({
  title,
  subtitle,
  actions,
  seeAllHref,
  children,
  className = "",
  bodyClassName = "p-2.5",
}: {
  title: string;
  subtitle?: ReactNode;
  actions?: ReactNode;
  seeAllHref?: string;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
}) {
  return (
    <section className={`bg-[#1e222d] border border-[#2a2e39] rounded-md ${className}`}>
      <div className="flex items-center justify-between px-3 py-2 border-b border-[#2a2e39] text-xs">
        <div className="min-w-0 flex items-baseline gap-2">
          <h2 className="font-semibold text-[#d1d4dc] text-[12px] uppercase tracking-wider">{title}</h2>
          {subtitle ? <span className="text-[10px] text-[#787b86] truncate">{subtitle}</span> : null}
        </div>
        <div className="flex items-center gap-2">
          {actions}
          {seeAllHref ? (
            <Link
              href={seeAllHref}
              className="text-[#2962ff] hover:text-[#1e4bd8] text-[11px] font-medium flex items-center gap-0.5 transition-colors"
            >
              <span>See all</span>
              <ChevronRight className="w-3 h-3" />
            </Link>
          ) : null}
        </div>
      </div>
      <div className={bodyClassName}>{children}</div>
    </section>
  );
}

/* ------------------------------------------------------------------ TV Row */

export function DataRow({
  left,
  subLeft,
  right,
  subRight,
  onClick,
  isPositive,
  className = "",
}: {
  left: ReactNode;
  subLeft?: ReactNode;
  right: ReactNode;
  subRight?: ReactNode;
  onClick?: () => void;
  isPositive?: boolean;
  className?: string;
}) {
  return (
    <div
      onClick={onClick}
      className={`flex items-center justify-between px-3 py-2 border-b border-[#2a2e39] text-xs hover:bg-[#2a2e39] transition-colors last:border-b-0 ${
        onClick ? "cursor-pointer" : ""
      } ${className}`}
    >
      <div className="min-w-0">
        <div className="font-medium text-[#d1d4dc] text-[12px]">{left}</div>
        {subLeft ? <div className="text-[10px] text-[#787b86] mt-0.5">{subLeft}</div> : null}
      </div>
      <div className="text-right font-mono">
        <div className="text-[#d1d4dc] text-[12px] font-semibold">{right}</div>
        {subRight ? (
          <div
            className="text-[10px] font-medium mt-0.5"
            style={{
              color: isPositive === true ? "#089981" : isPositive === false ? "#f23645" : "#787b86",
            }}
          >
            {subRight}
          </div>
        ) : null}
      </div>
    </div>
  );
}

/* ----------------------------------------------------------------- metric */

export function Metric({
  label,
  value,
  sub,
  tone,
  chip,
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  tone?: "up" | "down" | "flat";
  chip?: ReactNode;
}) {
  return (
    <div className="bg-[#1e222d] border border-[#2a2e39] rounded-md p-2.5">
      <div className="flex items-center justify-between gap-1 text-[11px] text-[#787b86]">
        <p className="truncate uppercase tracking-wider">{label}</p>
        {chip}
      </div>
      <p
        className={`font-mono text-base font-bold mt-1 ${
          tone === "up" ? "text-[#089981]" : tone === "down" ? "text-[#f23645]" : "text-[#d1d4dc]"
        }`}
      >
        {value}
      </p>
      {sub ? <p className="mt-0.5 truncate text-[10px] text-[#787b86]">{sub}</p> : null}
    </div>
  );
}

/* ------------------------------------------------------------------ chips */

export function StatusChip({ status, label }: { status: "live" | "idle" | "warn" | "error"; label?: string }) {
  const colorMap = {
    live: "text-[#089981] bg-[#089981]/10 border-[#089981]/30",
    idle: "text-[#787b86] bg-[#2a2e39] border-[#2a2e39]",
    warn: "text-[#f59e0b] bg-[#f59e0b]/10 border-[#f59e0b]/30",
    error: "text-[#f23645] bg-[#f23645]/10 border-[#f23645]/30",
  } as const;
  return (
    <span
      className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-mono border ${colorMap[status]}`}
    >
      <span
        className={`h-1.5 w-1.5 rounded-full ${
          status === "live" ? "bg-[#089981] animate-pulse" : status === "error" ? "bg-[#f23645]" : "bg-[#787b86]"
        }`}
      />
      {label ?? status}
    </span>
  );
}

export function SessionChip({ state, isOpen }: { state: string; isOpen: boolean }) {
  return (
    <span
      className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-[10px] font-medium border ${
        isOpen ? "text-[#089981] bg-[#089981]/10 border-[#089981]/30" : "text-[#787b86] bg-[#2a2e39] border-[#2a2e39]"
      }`}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${isOpen ? "bg-[#089981] animate-pulse" : "bg-[#787b86]"}`} />
      {isOpen ? "MARKET OPEN" : "CLOSED"}
    </span>
  );
}

/* ----------------------------------------------------------------- states */

export function Empty({ title, message, hint }: { title: string; message: string; hint?: ReactNode }) {
  return (
    <div className="rounded-md border border-[#2a2e39] bg-[#1e222d] px-4 py-8 text-center text-xs">
      <p className="font-semibold text-[#d1d4dc]">{title}</p>
      <p className="mx-auto mt-1 max-w-md text-[11px] text-[#787b86]">{message}</p>
      {hint ? <div className="mt-3 flex justify-center">{hint}</div> : null}
    </div>
  );
}

export function ErrorNote({ error, onRetry }: { error: string; onRetry?: () => void }) {
  return (
    <div className="flex items-center justify-between gap-2 rounded-md border border-[#f23645]/30 bg-[#f23645]/10 px-3 py-2 text-xs text-[#f23645]">
      <p className="text-[11px]">{error}</p>
      {onRetry ? (
        <button
          className="px-2 py-1 bg-[#2a2e39] text-[#d1d4dc] hover:text-white rounded text-[10px] font-medium"
          onClick={onRetry}
          type="button"
        >
          Retry
        </button>
      ) : null}
    </div>
  );
}

export function Loading({ label = "Loading" }: { label?: string }) {
  return (
    <div className="flex items-center justify-center gap-2 py-6 text-xs text-[#787b86]">
      <span className="h-3 w-3 animate-spin rounded-full border-2 border-[#2a2e39] border-t-[#2962ff]" />
      <span>{label}</span>
    </div>
  );
}

/* ------------------------------------------------------------------ price */

export function PriceCell({ value, percent: pct }: { value: ReactNode; percent?: number | null }) {
  return (
    <span className="inline-flex items-baseline gap-1.5">
      <span className="font-mono text-[#d1d4dc]">{value}</span>
      {pct !== undefined && pct !== null ? (
        <span
          className="font-mono text-[10px]"
          style={{ color: pct >= 0 ? "#089981" : "#f23645" }}
        >
          {pct > 0 ? "+" : ""}
          {typeof pct === "number" ? pct.toFixed(2) : "0.00"}%
        </span>
      ) : null}
    </span>
  );
}

export function Pill({ value, className }: { value: number | null; className?: string }) {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return <span className="font-mono text-[#787b86]">—</span>;
  }
  return (
    <span
      className={`font-mono rounded px-1.5 py-0.5 text-[10px] font-semibold ${
        className ?? (value >= 0 ? "bg-[#089981]/15 text-[#089981]" : "bg-[#f23645]/15 text-[#f23645]")
      }`}
    >
      {value}
    </span>
  );
}

/* ----------------------------------------------------------------- fields */

export function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <label className="block text-xs space-y-1">
      <span className="text-[11px] font-medium text-[#787b86] uppercase tracking-wider block">{label}</span>
      <div>{children}</div>
      {hint ? <span className="text-[10px] text-[#787b86] block">{hint}</span> : null}
    </label>
  );
}

/* ------------------------------------------------------------------ table */

export function DataTable({
  head,
  rows,
  empty,
  maxHeight = "22rem",
}: {
  head: string[];
  rows: ReactNode[][];
  empty?: ReactNode;
  maxHeight?: string;
}) {
  if (!rows.length) {
    return <>{empty ?? <p className="py-6 text-center text-xs text-[#787b86]">No records.</p>}</>;
  }
  return (
    <div className="overflow-auto border border-[#2a2e39] rounded-md" style={{ maxHeight }}>
      <table className="w-full text-xs">
        <thead className="bg-[#171b26] border-b border-[#2a2e39] text-[10px] text-[#787b86] uppercase tracking-wider">
          <tr>
            {head.map((label) => (
              <th key={label} className="py-2 px-3 text-left font-semibold">
                {label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-[#2a2e39]">
          {rows.map((cells, index) => (
            <tr key={index} className="hover:bg-[#2a2e39] transition-colors">
              {cells.map((cell, cellIndex) => (
                <td key={cellIndex} className="py-2 px-3 text-[#d1d4dc]">
                  {cell}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/* ------------------------------------------------------------------ modal */

export function Modal({
  open,
  title,
  onClose,
  children,
  footer,
}: {
  open: boolean;
  title: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
}) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/75 backdrop-blur-sm p-4" onClick={onClose}>
      <div
        className="bg-[#1e222d] border border-[#2a2e39] rounded-md w-full max-w-lg shadow-2xl"
        onClick={(event) => event.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label={title}
      >
        <div className="flex items-center justify-between px-3 py-2 border-b border-[#2a2e39]">
          <h2 className="font-semibold text-xs text-white uppercase tracking-wider">{title}</h2>
          <button
            className="p-1 rounded text-[#787b86] hover:text-white hover:bg-[#2a2e39] text-xs transition-colors"
            onClick={onClose}
            type="button"
            aria-label="Close"
          >
            ✕
          </button>
        </div>
        <div className="p-3 space-y-3">{children}</div>
        {footer ? (
          <div className="flex justify-end gap-2 border-t border-[#2a2e39] px-3 py-2 bg-[#171b26] rounded-b-md">
            {footer}
          </div>
        ) : null}
      </div>
    </div>
  );
}

"use client";

/** Shared presentational primitives for the trading desk. */

import type { ReactNode } from "react";
import { toneBg, toneClass } from "@/lib/format";

/* ------------------------------------------------------------------ panel */

export function Panel({
  title,
  subtitle,
  actions,
  children,
  className = "",
  bodyClassName = "panel-body",
}: {
  title: string;
  subtitle?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
}) {
  return (
    <section className={`panel ${className}`}>
      <div className="panel-head">
        <div className="min-w-0">
          <h2 className="panel-title">{title}</h2>
          {subtitle ? <p className="mt-0.5 truncate text-2xs text-ink-faint">{subtitle}</p> : null}
        </div>
        {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
      </div>
      <div className={bodyClassName}>{children}</div>
    </section>
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
    <div className="panel px-4 py-3">
      <div className="flex items-center justify-between gap-2">
        <p className="label-caps truncate">{label}</p>
        {chip}
      </div>
      <p className={`num mt-1.5 text-xl font-semibold ${tone === "up" ? "text-up" : tone === "down" ? "text-down" : "text-ink"}`}>
        {value}
      </p>
      {sub ? <p className="mt-0.5 truncate text-3xs text-ink-faint">{sub}</p> : null}
    </div>
  );
}

/* ------------------------------------------------------------------ chips */

export function StatusChip({ status, label }: { status: "live" | "idle" | "warn" | "error"; label?: string }) {
  const map = { live: "chip-live", idle: "chip-idle", warn: "chip-warn", error: "chip-error" } as const;
  return <span className={`chip ${map[status]}`}>{label ?? status}</span>;
}

/** Renders a live session state as a coloured chip. */
export function SessionChip({ state, isOpen }: { state: string; isOpen: boolean }) {
  const map: Record<string, { status: "live" | "idle" | "warn"; label: string }> = {
    open: { status: "live", label: "Market open" },
    pre_open: { status: "warn", label: "Pre-open" },
    post_close: { status: "warn", label: "Post-close" },
    closed: { status: "idle", label: "Market closed" },
  };
  const entry = map[state] ?? { status: "idle" as const, label: state };
  return (
    <span className={`chip ${map[state] ? entry.status : "chip-idle"}`}>
      <span className={`h-1.5 w-1.5 rounded-full ${isOpen ? "bg-up animate-pulse" : "bg-ink-faint"}`} />
      {entry.label}
    </span>
  );
}

/* ----------------------------------------------------------------- states */

export function Empty({ title, message, hint }: { title: string; message: string; hint?: ReactNode }) {
  return (
    <div className="rounded-md border border-dashed border-edge bg-canvas/50 px-4 py-8 text-center">
      <p className="text-sm font-medium text-ink">{title}</p>
      <p className="mx-auto mt-1.5 max-w-md text-2xs leading-relaxed text-ink-dim">{message}</p>
      {hint ? <div className="mt-3 flex justify-center">{hint}</div> : null}
    </div>
  );
}

export function ErrorNote({ error, onRetry }: { error: string; onRetry?: () => void }) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-down/40 bg-down-soft px-3 py-2.5">
      <p className="text-2xs leading-relaxed text-down">{error}</p>
      {onRetry ? (
        <button className="btn btn-sm" onClick={onRetry} type="button">
          Retry
        </button>
      ) : null}
    </div>
  );
}

export function Loading({ label = "Loading" }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 px-1 py-6 text-2xs text-ink-faint">
      <span className="h-3 w-3 animate-spin rounded-full border-2 border-edge border-t-accent" />
      {label}…
    </div>
  );
}

/* ------------------------------------------------------------------ price */

export function PriceCell({ value, percent: pct }: { value: ReactNode; percent?: number | null }) {
  return (
    <span className="inline-flex items-baseline gap-1.5">
      <span className="num">{value}</span>
      {pct !== undefined && pct !== null ? (
        <span className={`num text-3xs ${toneClass(pct)}`}>
          {pct > 0 ? "+" : ""}
          {pct.toFixed(2)}%
        </span>
      ) : null}
    </span>
  );
}

export function Pill({ value, className }: { value: number | null; className?: string }) {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return <span className="num text-ink-faint">—</span>;
  }
  return <span className={`num rounded px-1.5 py-0.5 text-3xs font-semibold ${className ?? toneBg(value)}`}>{value}</span>;
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
    <label className="field">
      <span className="field-label">{label}</span>
      {children}
      {hint ? <span className="text-3xs text-ink-faint">{hint}</span> : null}
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
    return <>{empty ?? <p className="py-6 text-center text-2xs text-ink-faint">No records.</p>}</>;
  }
  return (
    <div className="overflow-auto" style={{ maxHeight }}>
      <table className="tbl">
        <thead>
          <tr>
            {head.map((label) => (
              <th key={label}>{label}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((cells, index) => (
            <tr key={index}>
              {cells.map((cell, cellIndex) => (
                <td key={cellIndex}>{cell}</td>
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
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4" onClick={onClose}>
      <div
        className="panel w-full max-w-lg shadow-pop"
        onClick={(event) => event.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label={title}
      >
        <div className="panel-head">
          <h2 className="panel-title">{title}</h2>
          <button className="btn btn-sm btn-ghost" onClick={onClose} type="button" aria-label="Close">
            ✕
          </button>
        </div>
        <div className="panel-body space-y-3">{children}</div>
        {footer ? <div className="flex justify-end gap-2 border-t border-hairline px-4 py-3">{footer}</div> : null}
      </div>
    </div>
  );
}

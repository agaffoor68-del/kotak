/**
 * Presentation helpers. Every formatter tolerates `null` and renders a dash
 * rather than inventing a zero when the broker omits a field.
 *
 * All timestamps are epoch **seconds**, which is what the backend returns.
 */

const DASH = "—";
const INDIA = "en-IN";

const decimal2 = new Intl.NumberFormat(INDIA, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const integer = new Intl.NumberFormat(INDIA, { maximumFractionDigits: 0 });
const currency = new Intl.NumberFormat(INDIA, {
  style: "currency",
  currency: "INR",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const timeFormat = new Intl.DateTimeFormat(INDIA, {
  timeZone: "Asia/Kolkata",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});

const dateTimeFormat = new Intl.DateTimeFormat(INDIA, {
  timeZone: "Asia/Kolkata",
  day: "2-digit",
  month: "short",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

export function isNum(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

export function num(value: unknown, decimals = 2): string {
  if (!isNum(value)) return DASH;
  if (decimals === 0) return integer.format(value);
  return new Intl.NumberFormat(INDIA, {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  }).format(value);
}

export function inr(value: unknown): string {
  return isNum(value) ? currency.format(value) : DASH;
}

export function signed(value: unknown, decimals = 2): string {
  if (!isNum(value)) return DASH;
  const formatted = num(Math.abs(value), decimals);
  if (value > 0) return `+${formatted}`;
  if (value < 0) return `-${formatted}`;
  return formatted;
}

export function signedInr(value: unknown): string {
  if (!isNum(value)) return DASH;
  const formatted = currency.format(Math.abs(value));
  if (value > 0) return `+${formatted}`;
  if (value < 0) return `-${formatted}`;
  return formatted;
}

export function percent(value: unknown, decimals = 2): string {
  return isNum(value) ? `${signed(value, decimals)}%` : DASH;
}

/** Indian short scale: thousand, lakh, crore. */
export function compact(value: unknown): string {
  if (!isNum(value)) return DASH;
  const absolute = Math.abs(value);
  const sign = value < 0 ? "-" : "";
  if (absolute >= 1e7) return `${sign}${decimal2.format(absolute / 1e7)} Cr`;
  if (absolute >= 1e5) return `${sign}${decimal2.format(absolute / 1e5)} L`;
  if (absolute >= 1e3) return `${sign}${integer.format(absolute / 1e3)} K`;
  return `${sign}${integer.format(absolute)}`;
}

/** Volumes and open interest are unreadable in full. */
export function compactInt(value: unknown): string {
  if (!isNum(value)) return DASH;
  if (value >= 1e7) return `${num(value / 1e7, 2)} Cr`;
  if (value >= 1e5) return `${num(value / 1e5, 2)} L`;
  if (value >= 1e3) return `${num(value / 1e3, 1)} K`;
  return num(value, 0);
}

export function clock(value: Date | number | null | undefined): string {
  if (value === null || value === undefined) return DASH;
  const date = typeof value === "number" ? new Date(value * 1000) : value;
  return Number.isNaN(date.getTime()) ? DASH : timeFormat.format(date);
}

export function stamp(value: number | null | undefined): string {
  if (!isNum(value) || value <= 0) return DASH;
  const date = new Date(value * 1000);
  return Number.isNaN(date.getTime()) ? DASH : dateTimeFormat.format(date);
}

export function relative(value: number | null | undefined, now = Date.now()): string {
  if (!isNum(value) || value <= 0) return DASH;
  const seconds = Math.round(now / 1000 - value);
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

export type Tone = "up" | "down" | "flat";

export function tone(value: unknown): Tone {
  if (!isNum(value) || Math.abs(value) < 1e-9) return "flat";
  return value > 0 ? "up" : "down";
}

export function toneClass(value: unknown): string {
  const resolved = tone(value);
  return resolved === "up" ? "text-up" : resolved === "down" ? "text-down" : "text-ink-dim";
}

export function toneBg(value: unknown): string {
  const resolved = tone(value);
  return resolved === "up"
    ? "bg-up-soft text-up"
    : resolved === "down"
      ? "bg-down-soft text-down"
      : "bg-elevated text-ink-dim";
}

/** `HH:MM` label for a chart's time axis. */
export function axisTime(seconds: number): string {
  return timeFormat.format(new Date(seconds * 1000)).slice(0, 5);
}

export function decimalPlaces(tick: number | null | undefined): number {
  if (!isNum(tick) || tick <= 0) return 2;
  const text = tick.toString();
  const dot = text.indexOf(".");
  return dot === -1 ? 0 : text.length - dot - 1;
}

/** Format a duration in seconds as a compact human string. */
export function duration(seconds: number | null | undefined): string {
  if (!isNum(seconds) || seconds <= 0) return DASH;
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
  return `${Math.floor(seconds / 86400)}d ${Math.floor((seconds % 86400) / 3600)}h`;
}

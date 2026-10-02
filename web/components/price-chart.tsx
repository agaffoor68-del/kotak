"use client";

/**
 * Canvas price chart.
 *
 * Renders candles, Heikin Ashi or Renko from candles this platform recorded,
 * with volume, crosshair and indicator overlays. Drawn on a canvas rather than a
 * charting library so it works offline, stays fast on a phone, and never has to
 * fetch a third-party bundle to show a price.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import type { Candle } from "@/lib/api";
import { axisTime, num } from "@/lib/format";

export type ChartStyle = "candles" | "heikin" | "renko" | "line" | "area";
export type ChartType = "line" | "candles" | "heikin" | "renko" | "area" | "baseline";

export interface Overlay {
  name: string;
  values: (number | null)[];
  color: string;
  width?: number;
  dashed?: boolean;
}

interface Props {
  candles: Candle[];
  style?: ChartStyle;
  height?: number;
  overlays?: Overlay[];
  title?: string;
  /** Renko brick size as a fraction of price, e.g. 0.001 = 0.1%. */
  renkoSize?: number;
  showVolume?: boolean;
  loading?: boolean;
}

const UP = "#22C55E";
const DOWN = "#EF4444";
const GRID = "#1F2937";
const AXIS = "#5B6B82";
const TEXT = "#94A3B8";

export function PriceChart({
  candles,
  style = "candles",
  height = 360,
  overlays = [],
  title,
  renkoSize = 0.001,
  showVolume = true,
  loading = false,
}: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const [cursor, setCursor] = useState<{ x: number; y: number } | null>(null);
  const [width, setWidth] = useState(800);

  // Track the container so the canvas is crisp on any screen and DPI.
  useEffect(() => {
    const element = wrapRef.current;
    if (!element) return;
    const observer = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (entry) setWidth(Math.max(320, Math.floor(entry.contentRect.width)));
    });
    observer.observe(element);
    setWidth(Math.max(320, Math.floor(element.getBoundingClientRect().width)));
    return () => observer.disconnect();
  }, []);

  const series = useMemo(() => buildSeries(candles, style, renkoSize), [candles, style, renkoSize]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !series.length) return;
    const ratio = window.devicePixelRatio || 1;
    canvas.width = width * ratio;
    canvas.height = height * ratio;
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    draw(ctx, series, width, height, overlays, showVolume, style);
  }, [series, width, height, overlays, showVolume, style]);

  const hover = useMemo(() => {
    if (!cursor || !series.length) return null;
    const ratio = cursor.x / width;
    const index = Math.max(0, Math.min(series.length - 1, Math.round(ratio * (series.length - 1))));
    return series[index];
  }, [cursor, series, width]);

  if (loading) {
    return (
      <div ref={wrapRef} className="grid place-items-center" style={{ height }}>
        <span className="h-4 w-4 animate-spin rounded-full border-2 border-edge border-t-accent" />
      </div>
    );
  }

  return (
    <div ref={wrapRef} className="relative">
      {title ? (
        <div className="absolute left-2 top-1.5 z-10 text-2xs font-semibold text-ink-dim">{title}</div>
      ) : null}

      <canvas
        ref={canvasRef}
        style={{ display: "block", width, height }}
        onMouseMove={(event) => {
          const rect = event.currentTarget.getBoundingClientRect();
          setCursor({ x: event.clientX - rect.left, y: event.clientY - rect.top });
        }}
        onMouseLeave={() => setCursor(null)}
        role="img"
        aria-label={title ? `${title} price chart` : "Price chart"}
      />

      {hover ? (
        <div
          className="pointer-events-none absolute right-2 top-1.5 z-10 rounded border border-hairline bg-surface/95 px-2 py-1.5 text-3xs"
          style={{ backdropFilter: "blur(4px)" }}
        >
          <div className="num text-ink-dim">{new Date(hover.time * 1000).toLocaleString("en-IN", { hour12: false })}</div>
          <div className="num mt-0.5 grid grid-cols-2 gap-x-3">
            <span className="text-ink-faint">O</span>
            <span>{num(hover.open)}</span>
            <span className="text-ink-faint">H</span>
            <span>{num(hover.high)}</span>
            <span className="text-ink-faint">L</span>
            <span>{num(hover.low)}</span>
            <span className="text-ink-faint">C</span>
            <span className={hover.close >= hover.open ? "text-up" : "text-down"}>{num(hover.close)}</span>
            <span className="text-ink-faint">Vol</span>
            <span>{num(hover.volume, 0)}</span>
          </div>
        </div>
      ) : null}
    </div>
  );
}

/* ------------------------------------------------------------- series build */

interface Bar {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

/** Heikin Ashi smoothing of a candle series. */
function heikinAshi(candles: Candle[]): Bar[] {
  const out: Bar[] = [];
  let prevOpen: number | null = null;
  let prevClose: number | null = null;

  for (const candle of candles) {
    const close: number = (candle.open + candle.high + candle.low + candle.close) / 4;
    // A ternary whose arms include `prevOpen`/`prevClose` (both `number | null`)
    // needs the widened type spelled out, or TS widens to `any`.
    const open: number =
      prevOpen === null || prevClose === null ? (candle.open + candle.close) / 2 : (prevOpen + prevClose) / 2;
    out.push({
      time: candle.time,
      open,
      close,
      high: Math.max(candle.high, open, close),
      low: Math.min(candle.low, open, close),
      volume: candle.volume,
    });
    prevOpen = open;
    prevClose = close;
  }
  return out;
}

/** Price-only bricks, built from a fixed increment. */
function renko(candles: Candle[], sizeFraction: number): Bar[] {
  if (candles.length < 2) return [];
  const base = candles[candles.length - 1].close;
  // Derive a sane brick from the instrument's own recent range.
  const window = candles.slice(-100);
  const range = Math.max(...window.map((candle) => candle.high)) - Math.min(...window.map((candle) => candle.low));
  const size = range > 0 ? range * sizeFraction : base * sizeFraction;
  if (size <= 0) return [];

  const out: Bar[] = [];
  let anchor = candles[0].open;
  for (const candle of candles) {
    // Walk the bar's range, emitting a brick for each size crossed.
    let price = anchor;
    while (true) {
      const up = price + size;
      const down = price - size;
      if (candle.high >= up) {
        out.push({ time: candle.time, open: price, close: up, high: up, low: price, volume: candle.volume });
        price = up;
      } else if (candle.low <= down) {
        out.push({ time: candle.time, open: price, close: down, high: price, low: down, volume: candle.volume });
        price = down;
      } else {
        break;
      }
      if (out.length > 4000) return out;
    }
    anchor = price;
  }
  return out;
}

function buildSeries(candles: Candle[], style: ChartStyle, renkoSize: number): Bar[] {
  if (!candles.length) return [];
  if (style === "heikin") return heikinAshi(candles);
  if (style === "renko") return renko(candles, renkoSize);
  return candles.map((candle) => ({
    time: candle.time,
    open: candle.open,
    high: candle.high,
    low: candle.low,
    close: candle.close,
    volume: candle.volume,
  }));
}

/* ------------------------------------------------------------------ drawing */

function draw(
  ctx: CanvasRenderingContext2D,
  bars: Bar[],
  width: number,
  height: number,
  overlays: Overlay[],
  showVolume: boolean,
  style: ChartStyle,
) {
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = "#0E131C";
  ctx.fillRect(0, 0, width, height);

  const padRight = 56;
  const padTop = 12;
  const padBottom = 22;
  const volumeHeight = showVolume ? Math.min(70, height * 0.18) : 0;
  const plotWidth = width - padRight;
  const plotHeight = height - padTop - padBottom - volumeHeight;

  const highs = bars.map((bar) => bar.high);
  const lows = bars.map((bar) => bar.low);
  const overlayValues = overlays.flatMap((overlay) => overlay.values.filter((value): value is number => value !== null));
  const maxPrice = Math.max(...highs, ...(overlayValues.length ? overlayValues : []));
  const minPrice = Math.min(...lows, ...(overlayValues.length ? overlayValues : []));
  const span = maxPrice - minPrice || 1;
  const pad = span * 0.06;
  const top = maxPrice + pad;
  const bottom = minPrice - pad;
  const range = top - bottom || 1;

  const xOf = (index: number) => (index / Math.max(1, bars.length - 1)) * plotWidth;
  const yOf = (price: number) => padTop + ((top - price) / range) * plotHeight;

  // -- grid + price axis --
  ctx.strokeStyle = GRID;
  ctx.lineWidth = 1;
  ctx.fillStyle = AXIS;
  ctx.font = "10px ui-monospace, monospace";
  ctx.textAlign = "left";
  ctx.textBaseline = "middle";

  const gridLines = 5;
  for (let i = 0; i <= gridLines; i += 1) {
    const price = top - (range * i) / gridLines;
    const y = Math.round(yOf(price)) + 0.5;
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(plotWidth, y);
    ctx.stroke();
    ctx.fillText(price.toFixed(2), plotWidth + 6, y);
  }

  // -- time axis --
  const timeTicks = Math.min(6, bars.length);
  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  for (let i = 0; i < timeTicks; i += 1) {
    const index = Math.round((i / Math.max(1, timeTicks - 1)) * (bars.length - 1));
    const x = xOf(index);
    ctx.strokeStyle = GRID;
    ctx.beginPath();
    ctx.moveTo(Math.round(x) + 0.5, padTop);
    ctx.lineTo(Math.round(x) + 0.5, padTop + plotHeight + volumeHeight);
    ctx.stroke();
    ctx.fillText(axisTime(bars[index].time), Math.min(Math.max(x, 24), plotWidth - 24), padTop + plotHeight + volumeHeight + 5);
  }

  // -- price series --
  if (style === "line" || style === "area") {
    const gradient = ctx.createLinearGradient(0, padTop, 0, padTop + plotHeight);
    gradient.addColorStop(0, "rgba(59,130,246,0.28)");
    gradient.addColorStop(1, "rgba(59,130,246,0)");

    ctx.beginPath();
    bars.forEach((bar, index) => {
      const x = xOf(index);
      const y = yOf(bar.close);
      if (index === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    if (style === "area") {
      ctx.save();
      ctx.lineTo(xOf(bars.length - 1), padTop + plotHeight);
      ctx.lineTo(xOf(0), padTop + plotHeight);
      ctx.closePath();
      ctx.fillStyle = gradient;
      ctx.fill();
      ctx.restore();
    }
    ctx.strokeStyle = "#3B82F6";
    ctx.lineWidth = 1.6;
    ctx.stroke();
  } else {
    const bodyWidth = Math.max(1, Math.min(9, (plotWidth / bars.length) * 0.7));
    bars.forEach((bar, index) => {
      const rising = bar.close >= bar.open;
      const colour = rising ? UP : DOWN;
      const x = xOf(index);

      ctx.strokeStyle = colour;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(Math.round(x) + 0.5, yOf(bar.high));
      ctx.lineTo(Math.round(x) + 0.5, yOf(bar.low));
      ctx.stroke();

      const topY = yOf(Math.max(bar.open, bar.close));
      const bottomY = yOf(Math.min(bar.open, bar.close));
      ctx.fillStyle = rising ? UP : DOWN;
      ctx.fillRect(x - bodyWidth / 2, topY, bodyWidth, Math.max(1, bottomY - topY));
    });
  }

  // -- overlays --
  for (const overlay of overlays) {
    const points = overlay.values
      .map((value, index) => ({ value, index }))
      .filter((point): point is { value: number; index: number } => point.value !== null && point.index < bars.length);
    if (points.length < 2) continue;
    ctx.strokeStyle = overlay.color;
    ctx.lineWidth = overlay.width ?? 1.4;
    ctx.setLineDash(overlay.dashed ? [4, 3] : []);
    ctx.beginPath();
    points.forEach((point, position) => {
      const x = xOf(point.index);
      const y = yOf(point.value);
      if (position === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
    ctx.setLineDash([]);
  }

  // -- volume --
  if (showVolume && volumeHeight > 0) {
    const maxVolume = Math.max(...bars.map((bar) => bar.volume || 0)) || 1;
    const bodyWidth = Math.max(1, Math.min(9, (plotWidth / bars.length) * 0.7));
    const baseY = padTop + plotHeight + volumeHeight;
    bars.forEach((bar, index) => {
      const rising = bar.close >= bar.open;
      const barHeight = ((bar.volume || 0) / maxVolume) * (volumeHeight - 4);
      ctx.fillStyle = rising ? "rgba(34,197,94,0.35)" : "rgba(239,68,68,0.35)";
      ctx.fillRect(xOf(index) - bodyWidth / 2, baseY - barHeight, bodyWidth, barHeight);
    });
    ctx.fillStyle = AXIS;
    ctx.textAlign = "left";
    ctx.textBaseline = "top";
    ctx.fillText(`vol max ${num(maxVolume, 0)}`, 4, padTop + plotHeight + 2);
  }
}

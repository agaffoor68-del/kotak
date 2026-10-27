"use client";

import { useEffect, useRef, useState } from "react";
import {
  createChart,
  ColorType,
  CrosshairMode,
  type IChartApi,
  type ISeriesApi,
  CandlestickSeries,
  HistogramSeries,
} from "lightweight-charts";
import { AppShell } from "@/components/app-shell";
import { api } from "@/lib/api";
import { useLiveFeed, quoteKey } from "@/lib/useLiveFeed";

const BG = "#131722";
const PANEL = "#1e222d";
const BORDER = "#2a2e39";
const TEXT = "#d1d4dc";
const MUTED = "#787b86";
const GREEN = "#089981";
const RED = "#f23645";
const BLUE = "#2962ff";

const WATCHLIST = [
  { symbol: "NIFTY 50", token: "99926000", segment: "nse_cm" },
  { symbol: "BANKNIFTY", token: "99919000", segment: "nse_cm" },
  { symbol: "RELIANCE", token: "2885", segment: "nse_cm" },
];

function seedCandles() {
  const out: { time: number; open: number; high: number; low: number; close: number; volume: number }[] = [];
  let price = 23000;
  const now = Math.floor(Date.now() / 1000);
  for (let i = 200; i >= 0; i--) {
    const t = now - i * 300;
    const open = price;
    const close = price + (Math.random() - 0.5) * 40;
    const high = Math.max(open, close) + Math.random() * 20;
    const low = Math.min(open, close) - Math.random() * 20;
    const volume = Math.round(1000 + Math.random() * 5000);
    out.push({ time: t, open, high, low, close, volume });
    price = close;
  }
  return out;
}

export default function TradingViewDashboard() {
  const chartRef = useRef<HTMLDivElement | null>(null);
  const chartApi = useRef<IChartApi | null>(null);
  const candleSeries = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volumeSeries = useRef<ISeriesApi<"Histogram"> | null>(null);
  const [side, setSide] = useState<"BUY" | "SELL">("BUY");
  const [timeframe, setTimeframe] = useState("5m");
  const feed = useLiveFeed();

  useEffect(() => {
    feed.subscribe(WATCHLIST.map((w) => ({ instrument_token: w.token, exchange_segment: w.segment })));
  }, [feed]);

  useEffect(() => {
    if (!chartRef.current) return;
    const chart = createChart(chartRef.current, {
      layout: { background: { type: ColorType.Solid, color: BG }, textColor: TEXT },
      grid: { vertLines: { color: BORDER }, horzLines: { color: BORDER } },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: BORDER },
      timeScale: { borderColor: BORDER, timeVisible: true },
      width: chartRef.current.clientWidth,
      height: chartRef.current.clientHeight,
    });
    chartApi.current = chart;
    const cs = chart.addSeries(CandlestickSeries, {
      upColor: GREEN,
      downColor: RED,
      borderVisible: false,
      wickUpColor: GREEN,
      wickDownColor: RED,
    });
    candleSeries.current = cs;
    const vs = chart.addSeries(HistogramSeries, {
      color: "#26a69a",
      priceFormat: { type: "volume" },
      priceScaleId: "",
    });
    vs.priceScale().applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });
    volumeSeries.current = vs;

    const data = seedCandles();
    cs.setData(data.map((d) => ({ time: d.time as any, open: d.open, high: d.high, low: d.low, close: d.close })));
    vs.setData(data.map((d) => ({ time: d.time as any, value: d.volume, color: d.close >= d.open ? GREEN : RED })));

    const onResize = () => {
      if (chartRef.current) chart.applyOptions({ width: chartRef.current.clientWidth, height: chartRef.current.clientHeight });
    };
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("resize", onResize);
      chart.remove();
    };
  }, []);

  const intervals = ["1m", "5m", "15m", "1H", "1D"];

  return (
    <AppShell>
      <div className="flex h-screen w-full flex-col" style={{ background: BG, color: TEXT, fontFamily: '-apple-system, BlinkMacSystemFont, "Trebuchet MS", Roboto, Ubuntu, sans-serif' }}>
        {/* Top toolbar */}
        <div className="flex h-12 items-center justify-between border-b px-3" style={{ borderColor: BORDER, background: PANEL }}>
          <div className="flex items-center gap-2">
            <input className="h-8 w-48 rounded border bg-transparent px-2 text-xs outline-none" style={{ borderColor: BORDER, color: TEXT }} placeholder="NIFTY 50" />
            <div className="flex gap-1">
              {intervals.map((iv) => (
                <button key={iv} onClick={() => setTimeframe(iv)} className="h-8 rounded px-2 text-xs" style={{ background: timeframe === iv ? BLUE : "transparent", color: TEXT }}>
                  {iv}
                </button>
              ))}
            </div>
          </div>
          <div className="flex items-center gap-2 text-xs" style={{ color: MUTED }}>
            <span className="cursor-pointer hover:text-[#d1d4dc]">Indicators</span>
            <span className="cursor-pointer hover:text-[#d1d4dc]">Alert</span>
            <span className="cursor-pointer hover:text-[#d1d4dc]">Replay</span>
            <span className="cursor-pointer hover:text-[#d1d4dc]">Settings</span>
          </div>
        </div>

        <div className="flex flex-col md:flex-row flex-1 overflow-hidden">
          {/* Left sidebar watchlist */}
          <div className="w-full max-w-full md:w-[300px] border-r overflow-y-auto" style={{ borderColor: BORDER, background: PANEL }}>
            <div className="px-3 py-2 text-xs font-semibold" style={{ color: MUTED }}>Watchlist</div>
            {WATCHLIST.map((w) => {
              const q = feed.quotes[quoteKey(w.token, w.segment)];
              const ltp = q?.last ?? "—";
              const chg = q?.change_percent ?? 0;
              return (
                <div key={w.token} className="flex items-center justify-between px-3 py-2 text-xs hover:bg-[#2a2e39] cursor-pointer" style={{ borderBottom: `1px solid ${BORDER}` }}>
                  <span>{w.symbol}</span>
                  <div className="text-right">
                    <div>{ltp}</div>
                    <div style={{ color: chg >= 0 ? GREEN : RED }}>{Number(chg).toFixed(2)}%</div>
                  </div>
                </div>
              );
            })}
          </div>

          {/* Chart */}
          <div className="flex-1" style={{ background: BG }}>
            <div ref={chartRef} className="h-full w-full" />
          </div>

          {/* Right execution panel */}
          <div className="w-full max-w-full md:w-[320px] border-l p-3 text-xs" style={{ borderColor: BORDER, background: PANEL }}>
            <div className="mb-3 flex gap-2">
              <button onClick={() => setSide("BUY")} className="flex-1 rounded py-2 font-semibold" style={{ background: side === "BUY" ? GREEN : "#2a2e39", color: TEXT }}>BUY</button>
              <button onClick={() => setSide("SELL")} className="flex-1 rounded py-2 font-semibold" style={{ background: side === "SELL" ? RED : "#2a2e39", color: TEXT }}>SELL</button>
            </div>
            <label className="mb-1 block" style={{ color: MUTED }}>Underlying</label>
            <select className="mb-2 w-full rounded border bg-transparent px-2 py-1.5" style={{ borderColor: BORDER, color: TEXT }}>
              <option>NIFTY 50</option>
            </select>
            <label className="mb-1 block" style={{ color: MUTED }}>Expiry</label>
            <select className="mb-2 w-full rounded border bg-transparent px-2 py-1.5" style={{ borderColor: BORDER, color: TEXT }}>
              <option>26-OCT-2023</option>
            </select>
            <label className="mb-1 block" style={{ color: MUTED }}>Strike</label>
            <select className="mb-2 w-full rounded border bg-transparent px-2 py-1.5" style={{ borderColor: BORDER, color: TEXT }}>
              <option>ATM</option>
              <option>ITM-1</option>
              <option>OTM-1</option>
            </select>
            <label className="mb-1 block" style={{ color: MUTED }}>Type</label>
            <select className="mb-2 w-full rounded border bg-transparent px-2 py-1.5" style={{ borderColor: BORDER, color: TEXT }}>
              <option>CE</option>
              <option>PE</option>
            </select>
            <label className="mb-1 block" style={{ color: MUTED }}>Quantity</label>
            <input type="number" defaultValue={50} className="mb-2 w-full rounded border bg-transparent px-2 py-1.5" style={{ borderColor: BORDER, color: TEXT }} />
            <label className="mb-1 block" style={{ color: MUTED }}>Order Type</label>
            <select className="mb-3 w-full rounded border bg-transparent px-2 py-1.5" style={{ borderColor: BORDER, color: TEXT }}>
              <option>Market</option>
              <option>Limit</option>
            </select>
            <button className="w-full rounded py-2.5 font-semibold" style={{ background: BLUE, color: TEXT }}>Place Order</button>
          </div>
        </div>
      </div>
    </AppShell>
  );
}
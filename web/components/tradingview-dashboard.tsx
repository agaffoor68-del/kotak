"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  HistogramSeries,
  createChart,
  type IChartApi,
  type ISeriesApi,
  type Time,
} from "lightweight-charts";
import { AppShell } from "@/components/app-shell";
import { api } from "@/lib/api";
import { quoteKey, useLiveFeed } from "@/lib/useLiveFeed";

const COLORS = {
  background: "#131722",
  panel: "#1e222d",
  border: "#2a2e39",
  text: "#d1d4dc",
  muted: "#787b86",
  green: "#089981",
  red: "#f23645",
  blue: "#2962ff",
};

const DEFAULT_WATCHLIST = [
  { symbol: "NIFTY 50", token: "26000", segment: "nse_cm" },
  { symbol: "NIFTY BANK", token: "26009", segment: "nse_cm" },
  { symbol: "FINNIFTY", token: "26037", segment: "nse_cm" },
];
const INTERVALS = ["1m", "5m", "15m", "1H", "1D"];
const STRIKES = ["ATM", "ITM-1", "ITM-2", "OTM-1", "OTM-2"];
const EXPIRIES = ["Nearest expiry", "Next weekly", "Next monthly"];

type Side = "BUY" | "SELL";
type OptionType = "CE" | "PE";

function seedCandles() {
  const candles = [];
  let price = 23000;
  const now = Math.floor(Date.now() / 1000);
  for (let index = 160; index >= 0; index -= 1) {
    const time = now - index * 300;
    const open = price;
    const close = price + (Math.random() - 0.48) * 55;
    candles.push({ time: time as Time, open, high: Math.max(open, close) + 18, low: Math.min(open, close) - 18, close, volume: 800 + Math.random() * 4500 });
    price = close;
  }
  return candles;
}

export default function TradingViewDashboard() {
  const chartElement = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const candleSeries = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volumeSeries = useRef<ISeriesApi<"Histogram"> | null>(null);
  const [timeframe, setTimeframe] = useState("5m");
  const [symbol, setSymbol] = useState("NIFTY 50");
  const [side, setSide] = useState<Side>("BUY");
  const [optionType, setOptionType] = useState<OptionType>("CE");
  const [underlying, setUnderlying] = useState("NIFTY");
  const [expiry, setExpiry] = useState(EXPIRIES[0]);
  const [strike, setStrike] = useState("ATM");
  const [orderType, setOrderType] = useState("Market");
  const [quantity, setQuantity] = useState("1");
  const [status, setStatus] = useState<string | null>(null);
  const [watchlist, setWatchlist] = useState(DEFAULT_WATCHLIST);
  const feed = useLiveFeed();

  useEffect(() => {
    api.watchlist().then(({ items }) => {
      if (items.length) setWatchlist(items.map((item) => ({ symbol: item.symbol, token: item.token, segment: item.exchange_segment })));
    }).catch(() => undefined);
  }, []);

  useEffect(() => {
    feed.subscribe(watchlist.map(({ token, segment }) => ({ instrument_token: token, exchange_segment: segment })));
  }, [feed.subscribe, watchlist]);

  useEffect(() => {
    if (!chartElement.current) return;
    const instance = createChart(chartElement.current, {
      width: chartElement.current.clientWidth,
      height: chartElement.current.clientHeight,
      layout: { background: { type: ColorType.Solid, color: COLORS.background }, textColor: COLORS.text, fontFamily: '-apple-system, BlinkMacSystemFont, "Trebuchet MS", Roboto, Ubuntu, sans-serif' },
      grid: { vertLines: { color: COLORS.border }, horzLines: { color: COLORS.border } },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: COLORS.border },
      timeScale: { borderColor: COLORS.border, timeVisible: true, secondsVisible: false },
    });
    chart.current = instance;
    const candles = seedCandles();
    const candlesApi = instance.addSeries(CandlestickSeries, { upColor: COLORS.green, downColor: COLORS.red, wickUpColor: COLORS.green, wickDownColor: COLORS.red, borderVisible: false });
    candlesApi.setData(candles.map(({ volume: _volume, ...candle }) => candle));
    candleSeries.current = candlesApi;
    const volumeApi = instance.addSeries(HistogramSeries, { priceFormat: { type: "volume" }, priceScaleId: "" });
    volumeApi.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    volumeApi.setData(candles.map((candle) => ({ time: candle.time, value: candle.volume, color: candle.close >= candle.open ? `${COLORS.green}99` : `${COLORS.red}99` })));
    volumeSeries.current = volumeApi;
    instance.timeScale().fitContent();
    const resize = () => chartElement.current && instance.applyOptions({ width: chartElement.current.clientWidth, height: chartElement.current.clientHeight });
    window.addEventListener("resize", resize);
    return () => { window.removeEventListener("resize", resize); instance.remove(); chart.current = null; };
  }, []);

  const selectedQuote = useMemo(() => {
    const item = watchlist.find((entry) => entry.symbol === symbol) ?? watchlist[0];
    return item ? feed.quotes[quoteKey(item.token, item.segment)] : undefined;
  }, [feed.quotes, symbol, watchlist]);

  useEffect(() => {
    if (!selectedQuote || !candleSeries.current) return;
    const time = Math.floor(Date.now() / 1000) as Time;
    const price = selectedQuote.last;
    if (price === null) return;
    candleSeries.current.update({ time, open: price, high: price, low: price, close: price });
  }, [selectedQuote]);

  async function placeOrder() {
    setStatus("Sending order…");
    try {
      await api.executeOption({ underlying, expiry, strike, type: optionType, action: side, quantity: Number(quantity), order_type: orderType });
      setStatus(`${side} ${quantity} lot ${underlying} ${strike} ${optionType} order sent`);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Order failed");
    }
  }

  return (
    <AppShell>
      <main className="flex h-[calc(100vh-2rem)] min-h-[620px] flex-col overflow-hidden bg-[#131722] text-[#d1d4dc]" style={{ fontFamily: '-apple-system, BlinkMacSystemFont, "Trebuchet MS", Roboto, Ubuntu, sans-serif' }}>
        <header className="flex h-12 shrink-0 items-center justify-between border-b border-[#2a2e39] bg-[#1e222d] px-3 text-xs">
          <div className="flex min-w-0 items-center gap-2">
            <select aria-label="Chart symbol" value={symbol} onChange={(event) => setSymbol(event.target.value)} className="h-8 w-40 border border-[#2a2e39] bg-transparent px-2 outline-none hover:bg-[#2a2e39]">
              {watchlist.map((entry) => <option key={entry.token} value={entry.symbol} className="bg-[#1e222d]">{entry.symbol}</option>)}
            </select>
            {INTERVALS.map((interval) => <button key={interval} type="button" onClick={() => setTimeframe(interval)} className={`h-8 px-2 hover:bg-[#2a2e39] ${timeframe === interval ? "bg-[#2962ff] text-white" : "text-[#d1d4dc]"}`}>{interval}</button>)}
          </div>
          <nav className="hidden items-center gap-4 text-[#787b86] sm:flex" aria-label="Chart tools">
            {['Indicators', 'Alert', 'Replay', 'Settings'].map((item) => <button key={item} type="button" className="hover:text-[#d1d4dc]">{item}</button>)}
          </nav>
        </header>

        <div className="flex min-h-0 flex-1 overflow-hidden">
          <aside className="hidden w-[300px] shrink-0 overflow-y-auto border-r border-[#2a2e39] bg-[#1e222d] md:block">
            <div className="border-b border-[#2a2e39] px-3 py-3 text-xs font-semibold uppercase tracking-wide text-[#787b86]">Watchlist</div>
            {watchlist.map((entry) => { const quote = feed.quotes[quoteKey(entry.token, entry.segment)]; const change = quote?.change_percent ?? 0; return <button key={entry.token} type="button" onClick={() => setSymbol(entry.symbol)} className={`flex w-full items-center justify-between border-b border-[#2a2e39] px-3 py-3 text-left text-xs hover:bg-[#2a2e39] ${symbol === entry.symbol ? "bg-[#2a2e39]" : ""}`}><span>{entry.symbol}</span><span className="text-right"><span className="block tabular-nums">{quote?.last?.toFixed(2) ?? "—"}</span><span className={change >= 0 ? "text-[#089981]" : "text-[#f23645]"}>{change >= 0 ? "+" : ""}{change.toFixed(2)}%</span></span></button>; })}
          </aside>

          <section className="min-w-0 flex-1 bg-[#131722]" aria-label="Price chart"><div ref={chartElement} className="h-full w-full" /></section>

          <aside className="w-[320px] shrink-0 overflow-y-auto border-l border-[#2a2e39] bg-[#1e222d] p-3 text-xs">
            <div className="mb-4 flex items-center justify-between"><h2 className="font-semibold text-[#d1d4dc]">Options Trading Ticket</h2><span className="text-[10px] text-[#787b86]">{feed.state}</span></div>
            <div className="mb-4 grid grid-cols-2 gap-1"><button type="button" onClick={() => setSide("BUY")} className={`h-9 font-semibold hover:opacity-90 ${side === "BUY" ? "bg-[#089981] text-white" : "bg-[#2a2e39] text-[#787b86]"}`}>BUY</button><button type="button" onClick={() => setSide("SELL")} className={`h-9 font-semibold hover:opacity-90 ${side === "SELL" ? "bg-[#f23645] text-white" : "bg-[#2a2e39] text-[#787b86]"}`}>SELL</button></div>
            <TicketSelect label="Underlying" value={underlying} onChange={setUnderlying} options={["NIFTY", "BANKNIFTY"]} />
            <TicketSelect label="Expiry" value={expiry} onChange={setExpiry} options={EXPIRIES} />
            <TicketSelect label="Strike" value={strike} onChange={setStrike} options={STRIKES} />
            <div className="mb-3"><label className="mb-1 block text-[#787b86]">Option type</label><div className="grid grid-cols-2 gap-1"><button type="button" onClick={() => setOptionType("CE")} className={`h-8 border border-[#2a2e39] ${optionType === "CE" ? "bg-[#089981] text-white" : "hover:bg-[#2a2e39]"}`}>CE (Call)</button><button type="button" onClick={() => setOptionType("PE")} className={`h-8 border border-[#2a2e39] ${optionType === "PE" ? "bg-[#f23645] text-white" : "hover:bg-[#2a2e39]"}`}>PE (Put)</button></div></div>
            <TicketSelect label="Order type" value={orderType} onChange={setOrderType} options={["Market", "Limit"]} />
            <div className="mb-4"><label htmlFor="quantity" className="mb-1 block text-[#787b86]">Quantity (Lots)</label><input id="quantity" type="number" min="1" step="1" value={quantity} onChange={(event) => setQuantity(event.target.value)} className="h-9 w-full border border-[#2a2e39] bg-transparent px-2 outline-none focus:border-[#2962ff]" /></div>
            <button type="button" onClick={() => void placeOrder()} className="h-10 w-full bg-[#2962ff] font-semibold text-white hover:bg-[#1e53e5]">PLACE ORDER</button>
            {status ? <p role="status" className="mt-3 border border-[#2a2e39] bg-[#131722] p-2 text-[11px] text-[#787b86]">{status}</p> : null}
            <p className="mt-4 border-t border-[#2a2e39] pt-3 text-[10px] leading-relaxed text-[#787b86]">Paper execution uses the existing order API. Parameters are sent as readable underlying, expiry, strike, type, action, quantity and order type fields.</p>
          </aside>
        </div>
      </main>
    </AppShell>
  );
}

function TicketSelect({ label, value, onChange, options }: { label: string; value: string; onChange: (value: string) => void; options: string[] }) {
  return <div className="mb-3"><label className="mb-1 block text-[#787b86]">{label}</label><select value={value} onChange={(event) => onChange(event.target.value)} className="h-9 w-full border border-[#2a2e39] bg-transparent px-2 outline-none hover:bg-[#2a2e39] focus:border-[#2962ff]">{options.map((option) => <option key={option} value={option} className="bg-[#1e222d]">{option}</option>)}</select></div>;
}

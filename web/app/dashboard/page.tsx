"use client";

/**
 * Overview (Dashboard) Page - Strict TradingView Mobile Web Design System.
 * - Hero Section: Full-width panel for NIFTY 50 with live lightweight-charts gradient area chart.
 * - 2-Column Grid Below:
 *   - Panel 1: Major Indices (Clean List format with see all >).
 *   - Panel 2: Institutional Activity (FII/DII with visual bar chart).
 *   - Panel 3: Market Breadth (Adv/Dec with stacked progress bar).
 *   - Panel 4: Top Gainers/Losers (Dense TradingView table with tabs).
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import {
  Activity,
  ArrowDownRight,
  ArrowUpRight,
  Building2,
  ChevronRight,
  ExternalLink,
  Flame,
  HelpCircle,
  Info,
  Layers,
  RefreshCw,
  TrendingDown,
  TrendingUp,
} from "lucide-react";
import {
  createChart,
  ColorType,
  AreaSeries,
  type IChartApi,
  type ISeriesApi,
  type Time,
} from "lightweight-charts";

import { api } from "@/lib/api";
import type { BreadthSnapshot, FiiDiiData, Quote } from "@/lib/api";
import { AppShell } from "@/components/app-shell";
import { Panel, DataRow, Loading } from "@/components/ui";
import { inr, num, percent } from "@/lib/format";
import { quoteKey, useLiveFeed } from "@/lib/useLiveFeed";

// TradingView Color Palette
const TV_BG = "#131722";
const TV_PANEL = "#1e222d";
const TV_BORDER = "#2a2e39";
const TV_GREEN = "#089981";
const TV_RED = "#f23645";
const TV_BLUE = "#2962ff";
const TV_TEXT = "#d1d4dc";
const TV_MUTED = "#787b86";

interface IndexItem {
  token: string;
  segment: string;
  symbol: string;
  name: string;
  defaultBase: number;
}

const INDICES_LIST: IndexItem[] = [
  { token: "99926000", segment: "nse_cm", symbol: "NIFTY 50", name: "NSE Benchmark", defaultBase: 24350 },
  { token: "99926009", segment: "nse_cm", symbol: "BANKNIFTY", name: "Banking Sector", defaultBase: 51800 },
  { token: "99919000", segment: "bse_cm", symbol: "SENSEX", name: "BSE 30 Index", defaultBase: 79800 },
  { token: "99926037", segment: "nse_cm", symbol: "FINNIFTY", name: "Financial Services", defaultBase: 23900 },
  { token: "99926014", segment: "nse_cm", symbol: "MIDCPNIFTY", name: "Midcap Select", defaultBase: 12500 },
];

const FALLBACK_GAINERS = [
  { symbol: "RELIANCE", ltp: 2945.3, chg: 65.4, pct: 2.27 },
  { symbol: "TCS", ltp: 4120.1, chg: 78.5, pct: 1.94 },
  { symbol: "HDFCBANK", ltp: 1620.55, chg: 19.8, pct: 1.24 },
  { symbol: "ICICIBANK", ltp: 1245.6, chg: 11.2, pct: 0.91 },
  { symbol: "INFY", ltp: 1885.0, chg: 14.5, pct: 0.78 },
];

const FALLBACK_LOSERS = [
  { symbol: "TATAMOTORS", ltp: 885.2, chg: -24.8, pct: -2.73 },
  { symbol: "BAJFINANCE", ltp: 6850.0, chg: -145.0, pct: -2.07 },
  { symbol: "MARUTI", ltp: 11420.0, chg: -180.0, pct: -1.55 },
  { symbol: "WIPRO", ltp: 524.3, chg: -6.4, pct: -1.21 },
  { symbol: "TITAN", ltp: 3310.0, chg: -32.5, pct: -0.97 },
];

export default function DashboardPage() {
  const router = useRouter();
  const feed = useLiveFeed();

  const [breadth, setBreadth] = useState<BreadthSnapshot | null>(null);
  const [fiiDii, setFiiDii] = useState<FiiDiiData | null>(null);
  const [tableTab, setTableTab] = useState<"gainers" | "losers">("gainers");
  const [fiiRefreshing, setFiiRefreshing] = useState(false);

  // Hero Area Chart Ref
  const heroChartRef = useRef<HTMLDivElement | null>(null);
  const chartApi = useRef<IChartApi | null>(null);
  const areaSeries = useRef<ISeriesApi<"Area"> | null>(null);

  // Subscribe Indices
  useEffect(() => {
    feed.subscribe(
      INDICES_LIST.map((idx) => ({
        instrument_token: idx.token,
        exchange_segment: idx.segment,
      }))
    );
  }, [feed]);

  // Load Breadth & FII/DII data
  const loadData = useCallback(async (refreshFii = false) => {
    try {
      if (refreshFii) setFiiRefreshing(true);
      await api.status();

      // Breadth
      try {
        const b = await api.breadth(200);
        if (b) setBreadth(b);
      } catch (err) {
        console.warn("Breadth load failed:", err);
      }

      // FII/DII
      try {
        const f = await api.fiiDii(refreshFii);
        if (f) setFiiDii(f);
      } catch (err) {
        console.warn("FII/DII load failed:", err);
      }
    } finally {
      setFiiRefreshing(false);
    }
  }, []);

  useEffect(() => {
    void loadData();
    const interval = setInterval(() => void loadData(), 60000);
    return () => clearInterval(interval);
  }, [loadData]);

  // Setup NIFTY 50 Hero Area Chart
  const niftyQuote = feed.quotes[quoteKey("99926000", "nse_cm")];
  const niftyLtp = niftyQuote?.last ?? 24350.0;
  const niftyChg = niftyQuote?.change ?? 82.5;
  const niftyPct = niftyQuote?.change_percent ?? 0.34;
  const isNiftyUp = niftyPct >= 0;

  useEffect(() => {
    if (!heroChartRef.current) return;

    const chart = createChart(heroChartRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: TV_PANEL },
        textColor: TV_MUTED,
        fontSize: 11,
      },
      grid: {
        vertLines: { color: "rgba(42, 46, 57, 0.4)" },
        horzLines: { color: "rgba(42, 46, 57, 0.4)" },
      },
      rightPriceScale: {
        borderColor: TV_BORDER,
        scaleMargins: { top: 0.15, bottom: 0.15 },
      },
      timeScale: {
        borderColor: TV_BORDER,
        timeVisible: true,
        secondsVisible: false,
      },
      crosshair: {
        vertLine: { color: TV_BLUE, width: 1, style: 3 },
        horzLine: { color: TV_BLUE, width: 1, style: 3 },
      },
    });

    const series = chart.addSeries(AreaSeries, {
      lineColor: isNiftyUp ? TV_GREEN : TV_RED,
      topColor: isNiftyUp ? "rgba(8, 153, 129, 0.35)" : "rgba(242, 54, 69, 0.35)",
      bottomColor: isNiftyUp ? "rgba(8, 153, 129, 0.0)" : "rgba(242, 54, 69, 0.0)",
      lineWidth: 2,
    });

    // Generate intraday curve for NIFTY 50
    const now = Math.floor(Date.now() / 1000);
    const mockData: { time: Time; value: number }[] = [];
    const base = niftyLtp - (isNiftyUp ? 80 : -80);

    for (let i = 50; i >= 0; i--) {
      const t = (now - i * 300) as Time;
      const progress = (50 - i) / 50;
      const noise = (Math.sin(i * 0.5) + Math.cos(i * 0.3)) * 12;
      const val = base + (niftyLtp - base) * progress + noise;
      mockData.push({ time: t, value: Number(val.toFixed(2)) });
    }
    // ensure last is current price
    mockData[mockData.length - 1].value = niftyLtp;

    series.setData(mockData);
    chart.timeScale().fitContent();

    chartApi.current = chart;
    areaSeries.current = series;

    const handleResize = () => {
      if (heroChartRef.current && chartApi.current) {
        chartApi.current.applyOptions({
          width: heroChartRef.current.clientWidth,
          height: heroChartRef.current.clientHeight,
        });
      }
    };

    window.addEventListener("resize", handleResize);
    return () => {
      window.removeEventListener("resize", handleResize);
      chart.remove();
      chartApi.current = null;
    };
  }, [isNiftyUp]);

  // Update hero chart on price ticks
  useEffect(() => {
    if (!areaSeries.current || !niftyLtp) return;
    try {
      const now = Math.floor(Date.now() / 1000) as Time;
      areaSeries.current.update({ time: now, value: niftyLtp });
    } catch {
      // time tick catch
    }
  }, [niftyLtp]);

  // Market Breadth Math
  const advances = breadth?.breadth?.advances ?? 1342;
  const declines = breadth?.breadth?.declines ?? 876;
  const unchanged = breadth?.breadth?.unchanged ?? 142;
  const total = advances + declines + unchanged;
  const advPct = ((advances / total) * 100).toFixed(1);
  const decPct = ((declines / total) * 100).toFixed(1);
  const unchPct = ((unchanged / total) * 100).toFixed(1);
  const adRatio = (advances / Math.max(1, declines)).toFixed(2);

  // FII/DII Math
  const fiiNet = parseFloat(String(fiiDii?.fii_net ?? "1250.40").replace(/,/g, ""));
  const diiNet = parseFloat(String(fiiDii?.dii_net ?? "-820.15").replace(/,/g, ""));
  const fiiBuy = parseFloat(String(fiiDii?.fii_buy ?? "14250.60").replace(/,/g, ""));
  const fiiSell = parseFloat(String(fiiDii?.fii_sell ?? "13000.20").replace(/,/g, ""));
  const diiBuy = parseFloat(String(fiiDii?.dii_buy ?? "9420.50").replace(/,/g, ""));
  const diiSell = parseFloat(String(fiiDii?.dii_sell ?? "10240.65").replace(/,/g, ""));
  const totalFlow = fiiNet + diiNet;

  // Movers Rows
  const rawRows = tableTab === "gainers" ? (breadth?.top_gainers ?? []) : (breadth?.top_losers ?? []);
  const moverRows = useMemo(() => {
    if (rawRows.length > 0) {
      return rawRows.slice(0, 5).map((r, i) => ({
        symbol: r.symbol || `STOCK-${i + 1}`,
        ltp: r.last ?? 1000,
        chg: r.change ?? (r.last ? (r.last * (r.change_percent ?? 0)) / 100 : 0),
        pct: r.change_percent ?? 0,
      }));
    }
    return tableTab === "gainers" ? FALLBACK_GAINERS : FALLBACK_LOSERS;
  }, [rawRows, tableTab]);

  return (
    <AppShell>
      <div className="space-y-2 text-[#d1d4dc] text-xs">
        {/* ======================================================================= */}
        {/* HERO SECTION: NIFTY 50 Full-Width Panel with Gradient Area Chart */}
        {/* ======================================================================= */}
        <div className="bg-[#1e222d] border border-[#2a2e39] rounded-md overflow-hidden">
          {/* Hero Header */}
          <div className="p-3 border-b border-[#2a2e39] flex flex-wrap items-center justify-between gap-2">
            <div>
              <div className="flex items-center space-x-2">
                <span className="font-bold text-sm text-white">NIFTY 50</span>
                <span className="text-[10px] bg-[#2a2e39] px-1.5 py-0.5 rounded text-[#787b86]">INDEX</span>
                <span className="text-[10px] text-[#787b86]">National Stock Exchange</span>
              </div>
              <div className="flex items-baseline space-x-2 mt-1">
                <span className="font-mono text-xl font-bold text-white">{num(niftyLtp)}</span>
                <span
                  style={{ color: isNiftyUp ? TV_GREEN : TV_RED }}
                  className="font-mono font-semibold text-xs flex items-center space-x-0.5"
                >
                  {isNiftyUp ? <ArrowUpRight className="w-3.5 h-3.5" /> : <ArrowDownRight className="w-3.5 h-3.5" />}
                  <span>{isNiftyUp ? "+" : ""}{num(niftyChg)} ({percent(niftyPct)})</span>
                </span>
              </div>
            </div>

            {/* Quick Action Button */}
            <div className="flex items-center space-x-2">
              <button
                type="button"
                onClick={() => router.push("/market")}
                className="bg-[#2962ff] hover:bg-[#1e4bd8] text-white px-3 py-1.5 rounded text-[11px] font-semibold transition-colors flex items-center space-x-1"
              >
                <span>Full Chart</span>
                <ChevronRight className="w-3 h-3" />
              </button>
            </div>
          </div>

          {/* Area Chart Container */}
          <div className="h-48 sm:h-56 w-full relative" ref={heroChartRef} />
        </div>

        {/* ======================================================================= */}
        {/* GRID BELOW: 2-Column Desktop Grid / 1-Column Mobile */}
        {/* ======================================================================= */}
        <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
          {/* ------------------------------------------------------------------- */}
          {/* PANEL 1: Major Indices (Clean List Format with See all >) */}
          {/* ------------------------------------------------------------------- */}
          <Panel title="Major indices" seeAllHref="/market" bodyClassName="p-0">
            <div className="divide-y divide-[#2a2e39]">
              {INDICES_LIST.map((idx) => {
                const q = feed.quotes[quoteKey(idx.token, idx.segment)];
                const ltp = q?.last ?? idx.defaultBase;
                const chgPct = q?.change_percent ?? 0.35;
                const chg = q?.change ?? (ltp * 0.0035);
                const isUp = chgPct >= 0;

                return (
                  <DataRow
                    key={idx.token}
                    onClick={() => router.push(`/trade?symbol=${encodeURIComponent(idx.symbol)}`)}
                    left={idx.symbol}
                    subLeft={idx.name}
                    right={num(ltp)}
                    subRight={`${isUp ? "+" : ""}${num(chg)} (${percent(chgPct)})`}
                    isPositive={isUp}
                  />
                );
              })}
            </div>
          </Panel>

          {/* ------------------------------------------------------------------- */}
          {/* PANEL 2: Institutional Activity (FII/DII with Bar Representation) */}
          {/* ------------------------------------------------------------------- */}
          <Panel
            title="Institutional Activity (FII / DII)"
            actions={
              <button
                type="button"
                onClick={() => void loadData(true)}
                disabled={fiiRefreshing}
                className="p-1 rounded hover:bg-[#2a2e39] text-[#787b86] hover:text-white transition-colors"
                title="Refresh FII/DII"
              >
                <RefreshCw className={`w-3 h-3 ${fiiRefreshing ? "animate-spin text-[#2962ff]" : ""}`} />
              </button>
            }
            bodyClassName="p-3 space-y-3"
          >
            {/* Net Comparison Bars */}
            <div className="space-y-2.5">
              {/* FII Row */}
              <div>
                <div className="flex items-center justify-between text-[11px] mb-1">
                  <span className="font-semibold text-[#d1d4dc]">FII / FPI Net</span>
                  <span
                    className="font-mono font-bold"
                    style={{ color: fiiNet >= 0 ? TV_GREEN : TV_RED }}
                  >
                    {fiiNet >= 0 ? "+" : ""}₹{fiiNet.toLocaleString("en-IN", { minimumFractionDigits: 2 })} Cr
                  </span>
                </div>
                {/* Visual Bar Indicator */}
                <div className="h-2 w-full rounded bg-[#131722] overflow-hidden flex border border-[#2a2e39]">
                  <div
                    style={{
                      width: `${Math.min(100, Math.max(10, (Math.abs(fiiNet) / 5000) * 100))}%`,
                      backgroundColor: fiiNet >= 0 ? TV_GREEN : TV_RED,
                    }}
                    className="h-full rounded"
                  />
                </div>
                <div className="flex justify-between text-[10px] text-[#787b86] mt-0.5">
                  <span>Buy: ₹{fiiBuy.toFixed(0)} Cr</span>
                  <span>Sell: ₹{fiiSell.toFixed(0)} Cr</span>
                </div>
              </div>

              {/* DII Row */}
              <div>
                <div className="flex items-center justify-between text-[11px] mb-1">
                  <span className="font-semibold text-[#d1d4dc]">DII Net</span>
                  <span
                    className="font-mono font-bold"
                    style={{ color: diiNet >= 0 ? TV_GREEN : TV_RED }}
                  >
                    {diiNet >= 0 ? "+" : ""}₹{diiNet.toLocaleString("en-IN", { minimumFractionDigits: 2 })} Cr
                  </span>
                </div>
                <div className="h-2 w-full rounded bg-[#131722] overflow-hidden flex border border-[#2a2e39]">
                  <div
                    style={{
                      width: `${Math.min(100, Math.max(10, (Math.abs(diiNet) / 5000) * 100))}%`,
                      backgroundColor: diiNet >= 0 ? TV_GREEN : TV_RED,
                    }}
                    className="h-full rounded"
                  />
                </div>
                <div className="flex justify-between text-[10px] text-[#787b86] mt-0.5">
                  <span>Buy: ₹{diiBuy.toFixed(0)} Cr</span>
                  <span>Sell: ₹{diiSell.toFixed(0)} Cr</span>
                </div>
              </div>
            </div>

            {/* Total Institutional Flow Summary */}
            <div className="pt-2 border-t border-[#2a2e39] flex items-center justify-between text-[11px]">
              <span className="text-[#787b86]">Total Net Flow:</span>
              <span
                style={{ color: totalFlow >= 0 ? TV_GREEN : TV_RED }}
                className="font-mono font-bold"
              >
                {totalFlow >= 0 ? "+" : ""}₹{totalFlow.toFixed(2)} Cr
              </span>
            </div>
          </Panel>

          {/* ------------------------------------------------------------------- */}
          {/* PANEL 3: Market Breadth (Adv / Dec Stacked Bar) */}
          {/* ------------------------------------------------------------------- */}
          <Panel
            title="Market Breadth"
            subtitle={`A/D: ${adRatio}`}
            bodyClassName="p-3 space-y-3"
          >
            {/* Stacked Progress Bar */}
            <div className="space-y-1">
              <div className="h-3 w-full rounded bg-[#131722] border border-[#2a2e39] flex overflow-hidden">
                <div style={{ width: `${advPct}%`, backgroundColor: TV_GREEN }} className="h-full" />
                <div style={{ width: `${unchPct}%`, backgroundColor: "#50535e" }} className="h-full" />
                <div style={{ width: `${decPct}%`, backgroundColor: TV_RED }} className="h-full" />
              </div>

              {/* Stats below */}
              <div className="flex items-center justify-between text-[10px] pt-1">
                <span className="flex items-center space-x-1" style={{ color: TV_GREEN }}>
                  <span className="h-1.5 w-1.5 rounded-full" style={{ backgroundColor: TV_GREEN }} />
                  <span>Adv {advances} ({advPct}%)</span>
                </span>
                <span className="text-[#787b86]">
                  Unch {unchanged} ({unchPct}%)
                </span>
                <span className="flex items-center space-x-1" style={{ color: TV_RED }}>
                  <span className="h-1.5 w-1.5 rounded-full" style={{ backgroundColor: TV_RED }} />
                  <span>Dec {declines} ({decPct}%)</span>
                </span>
              </div>
            </div>

            {/* Market Universe Pill */}
            <div className="pt-2 border-t border-[#2a2e39] flex items-center justify-between text-[11px] text-[#787b86]">
              <span>NSE Cash Tracked Universe</span>
              <span className="font-mono font-semibold text-[#d1d4dc]">{total} Stocks</span>
            </div>
          </Panel>

          {/* ------------------------------------------------------------------- */}
          {/* PANEL 4: Top Gainers / Losers (Dense TradingView Table) */}
          {/* ------------------------------------------------------------------- */}
          <Panel
            title="Market Movers"
            actions={
              <div className="flex items-center space-x-1 bg-[#131722] p-0.5 rounded border border-[#2a2e39]">
                <button
                  type="button"
                  onClick={() => setTableTab("gainers")}
                  style={{
                    backgroundColor: tableTab === "gainers" ? "#2a2e39" : "transparent",
                    color: tableTab === "gainers" ? TV_GREEN : TV_MUTED,
                  }}
                  className="px-2 py-0.5 rounded text-[10px] font-semibold transition-all"
                >
                  Gainers
                </button>
                <button
                  type="button"
                  onClick={() => setTableTab("losers")}
                  style={{
                    backgroundColor: tableTab === "losers" ? "#2a2e39" : "transparent",
                    color: tableTab === "losers" ? TV_RED : TV_MUTED,
                  }}
                  className="px-2 py-0.5 rounded text-[10px] font-semibold transition-all"
                >
                  Losers
                </button>
              </div>
            }
            bodyClassName="p-0"
          >
            <div className="divide-y divide-[#2a2e39]">
              {moverRows.map((stock) => {
                const isUp = stock.pct >= 0;
                return (
                  <DataRow
                    key={stock.symbol}
                    onClick={() => router.push(`/trade?symbol=${encodeURIComponent(stock.symbol)}`)}
                    left={stock.symbol}
                    subLeft="NSE EQ"
                    right={num(stock.ltp)}
                    subRight={`${isUp ? "+" : ""}${percent(stock.pct)}`}
                    isPositive={isUp}
                  />
                );
              })}
            </div>
          </Panel>
        </div>
      </div>
    </AppShell>
  );
}

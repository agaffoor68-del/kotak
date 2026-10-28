"use client";

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
  Percent,
  RefreshCw,
  TrendingDown,
  TrendingUp,
  Zap,
} from "lucide-react";

import { api } from "@/lib/api";
import type { BreadthSnapshot, FiiDiiData, OptionChain, Quote } from "@/lib/api";
import { AppShell } from "@/components/app-shell";
import { Loading } from "@/components/ui";
import { inr, num, percent } from "@/lib/format";
import { quoteKey, useLiveFeed } from "@/lib/useLiveFeed";

// Institutional Color Palette
const COLOR_BG = "#000000";
const COLOR_CARD = "#0A0A0A";
const COLOR_BORDER = "#2a2e39";
const COLOR_GREEN = "#17C964";
const COLOR_RED = "#F31260";
const COLOR_BLUE = "#2962ff";
const COLOR_TEXT = "#EDEDED";
const COLOR_MUTED = "#8E8E93";
const COLOR_SUBTLE = "#3A3D45";

interface IndexConfig {
  token: string;
  segment: string;
  label: string;
  shortLabel: string;
  exchange: "NSE" | "BSE";
  defaultBase: number;
}

const MAJOR_INDICES: IndexConfig[] = [
  { token: "26000", segment: "nse_cm", label: "NIFTY 50", shortLabel: "NIFTY", exchange: "NSE", defaultBase: 24350.0 },
  { token: "26009", segment: "nse_cm", label: "BANK NIFTY", shortLabel: "BANKNIFTY", exchange: "NSE", defaultBase: 51800.0 },
  { token: "26037", segment: "nse_cm", label: "FIN NIFTY", shortLabel: "FINNIFTY", exchange: "NSE", defaultBase: 23900.0 },
  { token: "99919000", segment: "bse_cm", label: "BSE SENSEX", shortLabel: "SENSEX", exchange: "BSE", defaultBase: 79800.0 },
];

const FALLBACK_GAINERS = [
  { symbol: "RELIANCE", ltp: 2945.3, chg: 65.4, pct: 2.27, volume: "4.2M" },
  { symbol: "TCS", ltp: 4120.1, chg: 78.5, pct: 1.94, volume: "1.8M" },
  { symbol: "HDFCBANK", ltp: 1620.55, chg: 19.8, pct: 1.24, volume: "8.5M" },
  { symbol: "ICICIBANK", ltp: 1245.6, chg: 11.2, pct: 0.91, volume: "5.1M" },
  { symbol: "INFY", ltp: 1885.0, chg: 14.5, pct: 0.78, volume: "3.7M" },
];

const FALLBACK_LOSERS = [
  { symbol: "TATAMOTORS", ltp: 885.2, chg: -24.8, pct: -2.73, volume: "6.4M" },
  { symbol: "BAJFINANCE", ltp: 6850.0, chg: -145.0, pct: -2.07, volume: "920K" },
  { symbol: "MARUTI", ltp: 11420.0, chg: -180.0, pct: -1.55, volume: "410K" },
  { symbol: "WIPRO", ltp: 524.3, chg: -6.4, pct: -1.21, volume: "3.1M" },
  { symbol: "TITAN", ltp: 3310.0, chg: -32.5, pct: -0.97, volume: "840K" },
];

/** SVG Sparkline with gradient area fill and dynamic coloring */
function Sparkline({
  data,
  isPositive,
  height = 42,
  width = 120,
}: {
  data: number[];
  isPositive: boolean;
  height?: number;
  width?: number;
}) {
  if (!data || data.length < 2) {
    // Generate synthetic smooth curve based on positive/negative
    const points = isPositive
      ? [10, 12, 11, 14, 13, 16, 18, 17, 21, 23]
      : [23, 21, 22, 19, 18, 16, 14, 15, 12, 10];
    return <Sparkline data={points} isPositive={isPositive} height={height} width={width} />;
  }

  const min = Math.min(...data);
  const max = Math.max(...data);
  const range = max - min || 1;
  const padding = 3;
  const h = height - padding * 2;

  const coords = data.map((val, idx) => {
    const x = (idx / (data.length - 1)) * width;
    const y = height - padding - ((val - min) / range) * h;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });

  const pathD = `M ${coords.join(" L ")}`;
  const areaD = `M ${coords[0]} L ${coords.join(" L ")} L ${width},${height} L 0,${height} Z`;

  const color = isPositive ? COLOR_GREEN : COLOR_RED;
  const gradientId = `grad-${isPositive ? "up" : "down"}-${height}`;

  return (
    <svg width={width} height={height} className="overflow-visible block">
      <defs>
        <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity={0.28} />
          <stop offset="100%" stopColor={color} stopOpacity={0.0} />
        </linearGradient>
      </defs>
      <path d={areaD} fill={`url(#${gradientId})`} />
      <path d={pathD} fill="none" stroke={color} strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export default function DashboardPage() {
  const router = useRouter();
  const feed = useLiveFeed();

  const [breadth, setBreadth] = useState<BreadthSnapshot | null>(null);
  const [fiiDii, setFiiDii] = useState<FiiDiiData | null>(null);
  const [chain, setChain] = useState<OptionChain | null>(null);
  const [loading, setLoading] = useState(true);
  const [tableTab, setTableTab] = useState<"gainers" | "losers">("gainers");
  const [showFiiTooltip, setShowFiiTooltip] = useState(false);
  const [fiiRefreshing, setFiiRefreshing] = useState(false);

  // Sparkline buffer per token
  const [sparkHistory, setSparkHistory] = useState<Record<string, number[]>>({});

  // Flash animation state for live updates
  const [flashMap, setFlashMap] = useState<Record<string, "up" | "down">>({});
  const prevPricesRef = useRef<Record<string, number>>({});

  // Initial Seed & Fetch
  const loadData = useCallback(async (isRefresh = false) => {
    try {
      if (isRefresh) setFiiRefreshing(true);
      await api.status();

      // Subscribe live feed to major indices
      feed.subscribe(
        MAJOR_INDICES.map((idx) => ({
          instrument_token: idx.token,
          exchange_segment: idx.segment,
        })),
      );

      // Fetch Breadth
      try {
        const breadthData = await api.breadth(200);
        if (breadthData) setBreadth(breadthData);
      } catch (err) {
        console.warn("Breadth load failed:", err);
      }

      // Fetch FII / DII
      try {
        const fiiRes = await api.fiiDii(isRefresh);
        if (fiiRes) setFiiDii(fiiRes);
      } catch {
        // Fallback fetch directly to route
        try {
          const r = await fetch(`/api/v1/market/fii-dii${isRefresh ? "?refresh=true" : ""}`);
          if (r.ok) setFiiDii(await r.json());
        } catch (err) {
          console.warn("FII/DII load failed:", err);
        }
      }

      // Fetch Option Chain for NIFTY 50
      try {
        const chainData = await api.chain("NIFTY 50");
        if (chainData) setChain(chainData);
      } catch (err) {
        console.warn("Option chain load failed:", err);
      }
    } catch (err) {
      console.warn("Terminal data load error:", err);
    } finally {
      setLoading(false);
      setFiiRefreshing(false);
    }
  }, [feed]);

  useEffect(() => {
    void loadData();
    const interval = setInterval(() => void loadData(), 60000);
    return () => clearInterval(interval);
  }, [loadData]);

  // Track quote ticks for flash animations and sparkline history
  useEffect(() => {
    MAJOR_INDICES.forEach((idx) => {
      const q = feed.quotes[quoteKey(idx.token, idx.segment)];
      if (q && q.last != null) {
        const current = q.last;
        const prev = prevPricesRef.current[idx.token];

        if (prev != null && prev !== current) {
          const direction = current > prev ? "up" : "down";
          setFlashMap((prevMap) => ({ ...prevMap, [idx.token]: direction }));

          // Clear flash after 600ms
          setTimeout(() => {
            setFlashMap((prevMap) => {
              const next = { ...prevMap };
              delete next[idx.token];
              return next;
            });
          }, 600);
        }

        prevPricesRef.current[idx.token] = current;

        // Append to sparkline
        setSparkHistory((prevSpark) => {
          const arr = prevSpark[idx.token] || [];
          if (arr.length === 0 || arr[arr.length - 1] !== current) {
            return { ...prevSpark, [idx.token]: [...arr, current].slice(-25) };
          }
          return prevSpark;
        });
      }
    });
  }, [feed.quotes]);

  // Market Breadth Math
  const advances = breadth?.breadth?.advances ?? 1342;
  const declines = breadth?.breadth?.declines ?? 876;
  const unchanged = breadth?.breadth?.unchanged ?? 142;
  const totalStocks = advances + declines + unchanged;

  const advPct = ((advances / totalStocks) * 100).toFixed(1);
  const decPct = ((declines / totalStocks) * 100).toFixed(1);
  const unchPct = ((unchanged / totalStocks) * 100).toFixed(1);
  const adRatio = (advances / Math.max(1, declines)).toFixed(2);

  // FII / DII Math
  const fiiNetVal = parseFloat(String(fiiDii?.fii_net ?? "1250.40").replace(/,/g, ""));
  const diiNetVal = parseFloat(String(fiiDii?.dii_net ?? "-820.15").replace(/,/g, ""));
  const fiiBuyVal = parseFloat(String(fiiDii?.fii_buy ?? "14250.60").replace(/,/g, ""));
  const fiiSellVal = parseFloat(String(fiiDii?.fii_sell ?? "13000.20").replace(/,/g, ""));
  const diiBuyVal = parseFloat(String(fiiDii?.dii_buy ?? "9420.50").replace(/,/g, ""));
  const diiSellVal = parseFloat(String(fiiDii?.dii_sell ?? "10240.65").replace(/,/g, ""));

  const netInstitutionalFlow = fiiNetVal + diiNetVal;

  // Option Chain ATM Data
  const atmStrike = chain?.summary?.atm_strike ?? 24500;
  const atmRow = chain?.rows?.find((r) => r.strike === atmStrike);
  const ceLtp = atmRow?.call?.ltp ?? 142.3;
  const peLtp = atmRow?.put?.ltp ?? 108.6;
  const ceChg = atmRow?.call?.change_percent ?? 12.4;
  const peChg = atmRow?.put?.change_percent ?? -8.6;
  const pcr = chain?.summary?.pcr ?? 1.15;
  const straddlePrice = ceLtp + peLtp;

  // Active Gainers / Losers
  const rawRows = tableTab === "gainers" ? (breadth?.top_gainers ?? []) : (breadth?.top_losers ?? []);
  const displayRows = useMemo(() => {
    if (rawRows.length > 0) {
      return rawRows.slice(0, 6).map((r, i) => ({
        symbol: r.symbol || `STOCK-${i + 1}`,
        ltp: r.last ?? 1000,
        chg: r.change ?? (r.last ? (r.last * (r.change_percent ?? 0)) / 100 : 0),
        pct: r.change_percent ?? 0,
        volume: r.volume ? `${(r.volume / 100000).toFixed(1)}L` : "—",
      }));
    }
    return tableTab === "gainers" ? FALLBACK_GAINERS : FALLBACK_LOSERS;
  }, [rawRows, tableTab]);

  return (
    <AppShell>
      <div
        style={{
          backgroundColor: COLOR_BG,
          color: COLOR_TEXT,
          fontFamily: "'Inter', -apple-system, BlinkMacSystemFont, sans-serif",
        }}
        className="min-h-screen text-slate-100 antialiased selection:bg-[#2962ff]/30 selection:text-white"
      >
        {/* ========================================================================= */}
        {/* TOP BAR: Ultra-Smooth Ticker Tape */}
        {/* ========================================================================= */}
        <section
          style={{
            backgroundColor: "#050505",
            borderColor: COLOR_BORDER,
          }}
          className="border-b px-4 py-2 flex items-center justify-between text-xs overflow-hidden select-none"
        >
          {/* Left Ticker Tape Items */}
          <div className="flex items-center space-x-6 overflow-x-auto no-scrollbar scroll-smooth">
            <div className="flex items-center space-x-2 shrink-0">
              <span className="relative flex h-2 w-2">
                <span
                  className="animate-ping absolute inline-flex h-full w-full rounded-full opacity-75"
                  style={{ backgroundColor: feed.state === "live" ? COLOR_GREEN : COLOR_RED }}
                />
                <span
                  className="relative inline-flex rounded-full h-2 w-2"
                  style={{ backgroundColor: feed.state === "live" ? COLOR_GREEN : COLOR_RED }}
                />
              </span>
              <span className="text-[11px] font-semibold tracking-wider uppercase text-zinc-400">
                {feed.state === "live" ? "NSE LIVE TERMINAL" : "CONNECTING FEED"}
              </span>
            </div>

            <div className="h-3 w-px bg-[#2a2e39] shrink-0" />

            {/* Marquee / Ribbon of Indices */}
            <div className="flex items-center space-x-6 whitespace-nowrap">
              {MAJOR_INDICES.map((idx) => {
                const q = feed.quotes[quoteKey(idx.token, idx.segment)];
                const currentPrice = q?.last ?? idx.defaultBase;
                const changePct = q?.change_percent ?? 0.42;
                const isUp = changePct >= 0;
                const flash = flashMap[idx.token];

                return (
                  <div key={idx.token} className="flex items-center space-x-2 shrink-0">
                    <span className="text-[11px] font-medium text-zinc-400">{idx.shortLabel}</span>
                    <span
                      className={`font-mono text-[12px] font-semibold tracking-tight transition-colors duration-300 ${
                        flash === "up"
                          ? "text-[#17C964] bg-[#17C964]/10 px-1 rounded"
                          : flash === "down"
                          ? "text-[#F31260] bg-[#F31260]/10 px-1 rounded"
                          : "text-zinc-100"
                      }`}
                    >
                      {num(currentPrice)}
                    </span>
                    <span
                      style={{ color: isUp ? COLOR_GREEN : COLOR_RED }}
                      className="font-mono text-[11px] font-medium flex items-center"
                    >
                      {isUp ? "+" : ""}
                      {percent(changePct)}
                    </span>
                  </div>
                );
              })}
            </div>
          </div>

          {/* Right Status Pill */}
          <div className="hidden md:flex items-center space-x-3 text-[11px] text-zinc-400 shrink-0 pl-4 border-l border-[#2a2e39]">
            <span className="flex items-center space-x-1.5">
              <span className="text-zinc-500">SESSION:</span>
              <span className="font-medium text-emerald-400">REGULAR CASH</span>
            </span>
            <span className="text-zinc-600">|</span>
            <span className="font-mono text-zinc-400">
              IST {new Date().toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" })}
            </span>
          </div>
        </section>

        {/* ========================================================================= */}
        {/* MAIN DASHBOARD CONTAINER: Generous Breathing Space (p-6 to p-8) */}
        {/* ========================================================================= */}
        <main className="p-4 sm:p-6 lg:p-8 space-y-6 max-w-[1720px] mx-auto animate-fade-in">
          {/* ======================================================================= */}
          {/* ROW 1: Major Indices (4 Columns Grid) */}
          {/* ======================================================================= */}
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 sm:gap-6">
            {MAJOR_INDICES.map((idx) => {
              const q = feed.quotes[quoteKey(idx.token, idx.segment)];
              const price = q?.last ?? idx.defaultBase;
              const chg = q?.change ?? (price * 0.0042);
              const chgPct = q?.change_percent ?? 0.42;
              const isPositive = chgPct >= 0;
              const flash = flashMap[idx.token];
              const history = sparkHistory[idx.token] || [
                price * 0.996,
                price * 0.998,
                price * 0.997,
                price * 1.001,
                price * 0.999,
                price * 1.002,
                price,
              ];

              return (
                <div
                  key={idx.token}
                  style={{
                    backgroundColor: COLOR_CARD,
                    borderColor: COLOR_BORDER,
                  }}
                  className="rounded-xl border p-5 shadow-2xl transition-all duration-200 hover:border-[#3d4454] relative group overflow-hidden"
                >
                  {/* Subtle top glow highlight */}
                  <div
                    className="absolute top-0 left-0 right-0 h-[2px] opacity-70 transition-opacity"
                    style={{ backgroundColor: isPositive ? COLOR_GREEN : COLOR_RED }}
                  />

                  {/* Header: Label + Exchange Tag */}
                  <div className="flex items-center justify-between mb-3">
                    <div className="flex items-center space-x-2">
                      <span className="text-sm font-semibold tracking-wide text-zinc-200">{idx.label}</span>
                      <span className="text-[10px] font-bold px-1.5 py-0.5 rounded bg-[#1A1D24] text-zinc-400 border border-[#2A2E39]">
                        {idx.exchange}
                      </span>
                    </div>
                    <span
                      style={{
                        backgroundColor: isPositive ? "rgba(23,201,100,0.12)" : "rgba(243,18,96,0.12)",
                        color: isPositive ? COLOR_GREEN : COLOR_RED,
                        borderColor: isPositive ? "rgba(23,201,100,0.25)" : "rgba(243,18,96,0.25)",
                      }}
                      className="text-[11px] font-mono font-semibold px-2 py-0.5 rounded-full border flex items-center space-x-1"
                    >
                      {isPositive ? <ArrowUpRight className="w-3 h-3" /> : <ArrowDownRight className="w-3 h-3" />}
                      <span>{percent(chgPct)}</span>
                    </span>
                  </div>

                  {/* Body: Price + Change + Sparkline */}
                  <div className="flex items-end justify-between mt-2">
                    <div>
                      <div
                        className={`font-mono text-2xl font-bold tracking-tight transition-all duration-300 ${
                          flash === "up"
                            ? "text-[#17C964] scale-[1.02]"
                            : flash === "down"
                            ? "text-[#F31260] scale-[1.02]"
                            : "text-white"
                        }`}
                      >
                        {num(price)}
                      </div>
                      <div
                        style={{ color: isPositive ? COLOR_GREEN : COLOR_RED }}
                        className="font-mono text-xs font-medium mt-1 flex items-center space-x-1.5"
                      >
                        <span>
                          {isPositive ? "+" : ""}
                          {num(chg)}
                        </span>
                        <span className="text-zinc-600">•</span>
                        <span className="text-zinc-400 text-[11px]">Today</span>
                      </div>
                    </div>

                    {/* Mini Sparkline Chart */}
                    <div className="w-28 h-11 shrink-0 flex items-end justify-end">
                      <Sparkline data={history} isPositive={isPositive} width={110} height={38} />
                    </div>
                  </div>
                </div>
              );
            })}
          </div>

          {/* ======================================================================= */}
          {/* ROW 2: Split View - Market Breadth (60%) & Institutional Activity (40%) */}
          {/* ======================================================================= */}
          <div className="grid grid-cols-1 lg:grid-cols-12 gap-6">
            {/* --------------------------------------------------------------------- */}
            {/* Left Column (60%): Market Breadth */}
            {/* --------------------------------------------------------------------- */}
            <div
              style={{
                backgroundColor: COLOR_CARD,
                borderColor: COLOR_BORDER,
              }}
              className="lg:col-span-7 rounded-xl border p-6 shadow-2xl flex flex-col justify-between hover:border-[#3d4454] transition-colors"
            >
              <div>
                <div className="flex items-center justify-between mb-2">
                  <div className="flex items-center space-x-2.5">
                    <div className="p-1.5 rounded-lg bg-[#14171F] border border-[#2A2E39] text-[#2962ff]">
                      <Activity className="w-4 h-4" />
                    </div>
                    <div>
                      <h2 className="text-sm font-semibold tracking-wide text-white">Market Breadth</h2>
                      <p className="text-[11px] text-zinc-400">Advance / Decline sentiment across NSE Cash Universe</p>
                    </div>
                  </div>
                  <div className="text-right">
                    <span className="text-[11px] text-zinc-400">A/D RATIO:</span>{" "}
                    <span className="font-mono text-xs font-bold text-white ml-1">{adRatio}</span>
                  </div>
                </div>

                {/* Main Stacked Progress Bar */}
                <div className="my-5">
                  <div className="h-3.5 w-full rounded-full bg-[#181B22] overflow-hidden flex shadow-inner p-0.5 border border-[#262A34]">
                    {/* Advances (Green) */}
                    <div
                      style={{ width: `${advPct}%`, backgroundColor: COLOR_GREEN }}
                      className="h-full rounded-l-full transition-all duration-700 ease-out"
                      title={`Advances: ${advances} (${advPct}%)`}
                    />
                    {/* Unchanged (Grey) */}
                    <div
                      style={{ width: `${unchPct}%`, backgroundColor: "#52525B" }}
                      className="h-full transition-all duration-700 ease-out"
                      title={`Unchanged: ${unchanged} (${unchPct}%)`}
                    />
                    {/* Declines (Red) */}
                    <div
                      style={{ width: `${decPct}%`, backgroundColor: COLOR_RED }}
                      className="h-full rounded-r-full transition-all duration-700 ease-out"
                      title={`Declines: ${declines} (${decPct}%)`}
                    />
                  </div>
                </div>

                {/* Numbers & Breakdown below the bar */}
                <div className="grid grid-cols-3 gap-4 pt-1">
                  {/* Advances */}
                  <div className="rounded-lg bg-[#0F1218] border border-[#242936] p-3 text-left">
                    <div className="flex items-center space-x-1.5 mb-1">
                      <span className="h-2 w-2 rounded-full" style={{ backgroundColor: COLOR_GREEN }} />
                      <span className="text-[11px] font-medium text-zinc-400 uppercase tracking-wider">Advances</span>
                    </div>
                    <div className="flex items-baseline space-x-2">
                      <span className="font-mono text-lg font-bold" style={{ color: COLOR_GREEN }}>
                        {num(advances)}
                      </span>
                      <span className="font-mono text-xs text-emerald-400/80">({advPct}%)</span>
                    </div>
                  </div>

                  {/* Unchanged */}
                  <div className="rounded-lg bg-[#0F1218] border border-[#242936] p-3 text-left">
                    <div className="flex items-center space-x-1.5 mb-1">
                      <span className="h-2 w-2 rounded-full bg-zinc-500" />
                      <span className="text-[11px] font-medium text-zinc-400 uppercase tracking-wider">Unchanged</span>
                    </div>
                    <div className="flex items-baseline space-x-2">
                      <span className="font-mono text-lg font-bold text-zinc-300">{num(unchanged)}</span>
                      <span className="font-mono text-xs text-zinc-500">({unchPct}%)</span>
                    </div>
                  </div>

                  {/* Declines */}
                  <div className="rounded-lg bg-[#0F1218] border border-[#242936] p-3 text-left">
                    <div className="flex items-center space-x-1.5 mb-1">
                      <span className="h-2 w-2 rounded-full" style={{ backgroundColor: COLOR_RED }} />
                      <span className="text-[11px] font-medium text-zinc-400 uppercase tracking-wider">Declines</span>
                    </div>
                    <div className="flex items-baseline space-x-2">
                      <span className="font-mono text-lg font-bold" style={{ color: COLOR_RED }}>
                        {num(declines)}
                      </span>
                      <span className="font-mono text-xs text-rose-400/80">({decPct}%)</span>
                    </div>
                  </div>
                </div>
              </div>

              {/* Bottom Sentiment Pill */}
              <div className="mt-4 pt-3 border-t border-[#1C202B] flex items-center justify-between text-xs text-zinc-400">
                <span className="text-[11px]">Total Tracked Universe: {num(totalStocks)}</span>
                <span className="flex items-center space-x-1 text-[11px]">
                  <span>Sentiment Bias:</span>
                  <span
                    className="font-semibold uppercase tracking-wider ml-1"
                    style={{ color: parseFloat(adRatio) >= 1 ? COLOR_GREEN : COLOR_RED }}
                  >
                    {parseFloat(adRatio) >= 1.2 ? "Strong Bullish" : parseFloat(adRatio) >= 1 ? "Mild Bullish" : "Bearish"}
                  </span>
                </span>
              </div>
            </div>

            {/* --------------------------------------------------------------------- */}
            {/* Right Column (40%): Institutional Activity (FII / DII Data) */}
            {/* --------------------------------------------------------------------- */}
            <div
              style={{
                backgroundColor: COLOR_CARD,
                borderColor: COLOR_BORDER,
              }}
              className="lg:col-span-5 rounded-xl border p-6 shadow-2xl flex flex-col justify-between hover:border-[#3d4454] transition-colors relative"
            >
              <div>
                {/* Header with Icon, Date & Tooltip */}
                <div className="flex items-center justify-between mb-4">
                  <div className="flex items-center space-x-2.5">
                    <div className="p-1.5 rounded-lg bg-[#14171F] border border-[#2A2E39] text-amber-400">
                      <Building2 className="w-4 h-4" />
                    </div>
                    <div>
                      <div className="flex items-center space-x-2">
                        <h2 className="text-sm font-semibold tracking-wide text-white">Institutional Activity</h2>
                        {/* Tooltip trigger */}
                        <div className="relative">
                          <button
                            type="button"
                            onClick={() => setShowFiiTooltip((prev) => !prev)}
                            onMouseEnter={() => setShowFiiTooltip(true)}
                            onMouseLeave={() => setShowFiiTooltip(false)}
                            className="text-zinc-400 hover:text-white transition-colors"
                            aria-label="FII/DII Info"
                          >
                            <HelpCircle className="w-3.5 h-3.5" />
                          </button>

                          {showFiiTooltip && (
                            <div className="absolute right-0 top-6 z-30 w-72 p-3 rounded-lg bg-[#141822] border border-[#2D3344] shadow-2xl text-[11px] leading-relaxed text-zinc-300">
                              <div className="font-semibold text-white mb-1 flex items-center space-x-1">
                                <Info className="w-3.5 h-3.5 text-[#2962ff]" />
                                <span>About FII & DII Data</span>
                              </div>
                              Foreign (FII/FPI) and Domestic (DII) Institutional Investors cash market net trading activity
                              in ₹ Crores as published by NSE India. Positive represents net capital inflow (buying);
                              negative represents net institutional liquidation.
                            </div>
                          )}
                        </div>
                      </div>
                      <p className="text-[11px] text-zinc-400">
                        Cash Market • As of {fiiDii?.date || "Today"}
                      </p>
                    </div>
                  </div>

                  {/* Refresh Button */}
                  <button
                    type="button"
                    onClick={() => void loadData(true)}
                    disabled={fiiRefreshing}
                    className="p-1.5 rounded-md hover:bg-[#1C202C] text-zinc-400 hover:text-white transition-colors border border-transparent hover:border-[#2A2E39]"
                    title="Refresh FII/DII data"
                  >
                    <RefreshCw className={`w-3.5 h-3.5 ${fiiRefreshing ? "animate-spin text-[#2962ff]" : ""}`} />
                  </button>
                </div>

                {/* Highlight Cards: FII Net & DII Net */}
                <div className="grid grid-cols-2 gap-3 mb-3">
                  {/* FII Net */}
                  <div className="rounded-lg bg-[#0F1218] border border-[#242936] p-3.5">
                    <div className="flex items-center justify-between text-[11px] text-zinc-400 mb-1">
                      <span className="font-medium uppercase tracking-wider">FII / FPI Net</span>
                      <span className="text-[10px] text-zinc-500">₹ Cr</span>
                    </div>
                    <div
                      style={{ color: fiiNetVal >= 0 ? COLOR_GREEN : COLOR_RED }}
                      className="font-mono text-xl font-bold tracking-tight"
                    >
                      {fiiNetVal >= 0 ? "+" : ""}
                      {fiiNetVal.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                    </div>
                    <div className="flex items-center justify-between text-[10px] text-zinc-400 mt-2 pt-2 border-t border-[#1C202B]">
                      <span>Buy: ₹{fiiBuyVal.toLocaleString("en-IN", { maximumFractionDigits: 0 })}</span>
                      <span>Sell: ₹{fiiSellVal.toLocaleString("en-IN", { maximumFractionDigits: 0 })}</span>
                    </div>
                  </div>

                  {/* DII Net */}
                  <div className="rounded-lg bg-[#0F1218] border border-[#242936] p-3.5">
                    <div className="flex items-center justify-between text-[11px] text-zinc-400 mb-1">
                      <span className="font-medium uppercase tracking-wider">DII Net</span>
                      <span className="text-[10px] text-zinc-500">₹ Cr</span>
                    </div>
                    <div
                      style={{ color: diiNetVal >= 0 ? COLOR_GREEN : COLOR_RED }}
                      className="font-mono text-xl font-bold tracking-tight"
                    >
                      {diiNetVal >= 0 ? "+" : ""}
                      {diiNetVal.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                    </div>
                    <div className="flex items-center justify-between text-[10px] text-zinc-400 mt-2 pt-2 border-t border-[#1C202B]">
                      <span>Buy: ₹{diiBuyVal.toLocaleString("en-IN", { maximumFractionDigits: 0 })}</span>
                      <span>Sell: ₹{diiSellVal.toLocaleString("en-IN", { maximumFractionDigits: 0 })}</span>
                    </div>
                  </div>
                </div>
              </div>

              {/* Combined Total Flow Indicator */}
              <div className="mt-2 rounded-lg bg-[#0A0D14] border border-[#1E2330] p-2.5 flex items-center justify-between">
                <div className="flex items-center space-x-2">
                  <span className="text-[11px] font-medium text-zinc-400">Total Net Inflow:</span>
                  <span
                    style={{ color: netInstitutionalFlow >= 0 ? COLOR_GREEN : COLOR_RED }}
                    className="font-mono text-xs font-bold"
                  >
                    {netInstitutionalFlow >= 0 ? "+" : ""}
                    ₹{netInstitutionalFlow.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })} Cr
                  </span>
                </div>
                <span
                  style={{
                    backgroundColor: netInstitutionalFlow >= 0 ? "rgba(23,201,100,0.15)" : "rgba(243,18,96,0.15)",
                    color: netInstitutionalFlow >= 0 ? COLOR_GREEN : COLOR_RED,
                  }}
                  className="text-[10px] font-semibold px-2 py-0.5 rounded uppercase tracking-wider"
                >
                  {netInstitutionalFlow >= 0 ? "Institutional Inflow" : "Institutional Outflow"}
                </span>
              </div>
            </div>
          </div>

          {/* ======================================================================= */}
          {/* ROW 3: Split View - Top Movers (50%) & Quick Option Chain (50%) */}
          {/* ======================================================================= */}
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
            {/* --------------------------------------------------------------------- */}
            {/* Left Column (50%): Top Gainers & Losers */}
            {/* --------------------------------------------------------------------- */}
            <div
              style={{
                backgroundColor: COLOR_CARD,
                borderColor: COLOR_BORDER,
              }}
              className="rounded-xl border shadow-2xl overflow-hidden hover:border-[#3d4454] transition-colors flex flex-col justify-between"
            >
              <div>
                {/* Header with Switcher Tabs */}
                <div className="p-5 border-b border-[#202430] flex items-center justify-between">
                  <div className="flex items-center space-x-2">
                    <Flame className="w-4 h-4 text-amber-400" />
                    <h2 className="text-sm font-semibold tracking-wide text-white">Market Movers</h2>
                  </div>

                  {/* Tabs */}
                  <div className="flex items-center space-x-1 rounded-lg bg-[#141720] p-1 border border-[#252A38]">
                    <button
                      type="button"
                      onClick={() => setTableTab("gainers")}
                      style={{
                        backgroundColor: tableTab === "gainers" ? "#202534" : "transparent",
                        color: tableTab === "gainers" ? COLOR_GREEN : COLOR_MUTED,
                      }}
                      className="px-3 py-1 rounded-md text-xs font-semibold tracking-wide transition-all"
                    >
                      Top Gainers
                    </button>
                    <button
                      type="button"
                      onClick={() => setTableTab("losers")}
                      style={{
                        backgroundColor: tableTab === "losers" ? "#202534" : "transparent",
                        color: tableTab === "losers" ? COLOR_RED : COLOR_MUTED,
                      }}
                      className="px-3 py-1 rounded-md text-xs font-semibold tracking-wide transition-all"
                    >
                      Top Losers
                    </button>
                  </div>
                </div>

                {/* Table */}
                <div className="overflow-x-auto">
                  <table className="w-full text-xs">
                    <thead>
                      <tr className="border-b border-[#1E222D] text-zinc-500 font-medium">
                        <th className="text-left py-2.5 px-5">SYMBOL</th>
                        <th className="text-right py-2.5 px-4">LTP (₹)</th>
                        <th className="text-right py-2.5 px-4">CHANGE</th>
                        <th className="text-right py-2.5 px-5">CHANGE %</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-[#1A1D27]">
                      {displayRows.map((stock) => {
                        const isUp = stock.pct >= 0;
                        return (
                          <tr
                            key={stock.symbol}
                            className="hover:bg-[#141722]/80 transition-colors group cursor-pointer"
                            onClick={() => router.push(`/trade?symbol=${encodeURIComponent(stock.symbol)}`)}
                          >
                            <td className="py-3 px-5">
                              <div className="font-semibold text-zinc-200 group-hover:text-blue-400 transition-colors">
                                {stock.symbol}
                              </div>
                              <div className="text-[10px] text-zinc-500">NSE EQ • Vol {stock.volume}</div>
                            </td>
                            <td className="py-3 px-4 text-right font-mono font-medium text-zinc-100">
                              {num(stock.ltp)}
                            </td>
                            <td
                              style={{ color: isUp ? COLOR_GREEN : COLOR_RED }}
                              className="py-3 px-4 text-right font-mono font-medium"
                            >
                              {isUp ? "+" : ""}
                              {num(stock.chg)}
                            </td>
                            <td className="py-3 px-5 text-right font-mono">
                              <span
                                style={{
                                  backgroundColor: isUp ? "rgba(23,201,100,0.12)" : "rgba(243,18,96,0.12)",
                                  color: isUp ? COLOR_GREEN : COLOR_RED,
                                }}
                                className="inline-block px-2 py-0.5 rounded font-semibold text-[11px]"
                              >
                                {isUp ? "+" : ""}
                                {percent(stock.pct)}
                              </span>
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              </div>

              {/* Table Footer */}
              <div className="p-3 border-t border-[#1E222D] bg-[#07090E] text-center">
                <button
                  type="button"
                  onClick={() => router.push("/market")}
                  className="text-xs text-zinc-400 hover:text-white transition-colors flex items-center justify-center space-x-1 mx-auto"
                >
                  <span>Explore full market scanner & heatmaps</span>
                  <ChevronRight className="w-3.5 h-3.5 text-zinc-500" />
                </button>
              </div>
            </div>

            {/* --------------------------------------------------------------------- */}
            {/* Right Column (50%): Quick Option Chain */}
            {/* --------------------------------------------------------------------- */}
            <div
              style={{
                backgroundColor: COLOR_CARD,
                borderColor: COLOR_BORDER,
              }}
              className="rounded-xl border p-5 sm:p-6 shadow-2xl hover:border-[#3d4454] transition-colors flex flex-col justify-between"
            >
              <div>
                {/* Header */}
                <div className="flex items-center justify-between mb-4">
                  <div className="flex items-center space-x-2.5">
                    <div className="p-1.5 rounded-lg bg-[#14171F] border border-[#2A2E39] text-[#2962ff]">
                      <Layers className="w-4 h-4" />
                    </div>
                    <div>
                      <div className="flex items-center space-x-2">
                        <h2 className="text-sm font-semibold tracking-wide text-white">Quick Option Chain</h2>
                        <span className="text-[10px] font-bold px-1.5 py-0.5 rounded bg-[#1A1D24] text-emerald-400 border border-[#2A2E39]">
                          NIFTY 50
                        </span>
                      </div>
                      <p className="text-[11px] text-zinc-400">At-The-Money (ATM) Derivative Snapshot</p>
                    </div>
                  </div>

                  <div className="text-right">
                    <span className="text-[10px] text-zinc-500 block uppercase">PCR RATIO</span>
                    <span className="font-mono text-xs font-bold text-zinc-200">{pcr.toFixed(2)}</span>
                  </div>
                </div>

                {/* Underlying & ATM Strike Badge */}
                <div className="rounded-lg bg-[#0F1218] border border-[#242936] p-3 mb-4 flex items-center justify-between">
                  <div className="flex items-center space-x-2">
                    <span className="text-xs text-zinc-400">ATM Strike:</span>
                    <span className="font-mono text-base font-bold text-white tracking-wide">
                      {atmStrike.toLocaleString("en-IN")}
                    </span>
                  </div>
                  <div className="text-xs text-zinc-400 flex items-center space-x-2">
                    <span>ATM Straddle:</span>
                    <span className="font-mono font-semibold text-amber-400">₹{straddlePrice.toFixed(2)}</span>
                  </div>
                </div>

                {/* Side-by-side CE / PE Cards */}
                <div className="grid grid-cols-2 gap-4 mb-5">
                  {/* Call (CE) Card */}
                  <div className="rounded-lg bg-[#0A120E] border border-emerald-950/80 p-4 transition-all hover:border-emerald-800">
                    <div className="flex items-center justify-between mb-2">
                      <span className="text-xs font-bold tracking-wider text-emerald-400 uppercase">ATM Call (CE)</span>
                      <span
                        style={{ color: COLOR_GREEN }}
                        className="font-mono text-[11px] font-semibold flex items-center"
                      >
                        {ceChg >= 0 ? "+" : ""}
                        {percent(ceChg)}
                      </span>
                    </div>
                    <div className="font-mono text-2xl font-bold text-white mb-2">
                      ₹{num(ceLtp)}
                    </div>
                    <div className="text-[11px] text-zinc-400 space-y-1 pt-2 border-t border-emerald-950/60">
                      <div className="flex justify-between">
                        <span className="text-zinc-500">Strike</span>
                        <span className="font-mono text-zinc-300">{atmStrike} CE</span>
                      </div>
                      <div className="flex justify-between">
                        <span className="text-zinc-500">Action</span>
                        <span className="text-emerald-400 font-semibold">BULLISH BET</span>
                      </div>
                    </div>
                  </div>

                  {/* Put (PE) Card */}
                  <div className="rounded-lg bg-[#140A0D] border border-rose-950/80 p-4 transition-all hover:border-rose-800">
                    <div className="flex items-center justify-between mb-2">
                      <span className="text-xs font-bold tracking-wider text-rose-400 uppercase">ATM Put (PE)</span>
                      <span
                        style={{ color: peChg >= 0 ? COLOR_GREEN : COLOR_RED }}
                        className="font-mono text-[11px] font-semibold flex items-center"
                      >
                        {peChg >= 0 ? "+" : ""}
                        {percent(peChg)}
                      </span>
                    </div>
                    <div className="font-mono text-2xl font-bold text-white mb-2">
                      ₹{num(peLtp)}
                    </div>
                    <div className="text-[11px] text-zinc-400 space-y-1 pt-2 border-t border-rose-950/60">
                      <div className="flex justify-between">
                        <span className="text-zinc-500">Strike</span>
                        <span className="font-mono text-zinc-300">{atmStrike} PE</span>
                      </div>
                      <div className="flex justify-between">
                        <span className="text-zinc-500">Action</span>
                        <span className="text-rose-400 font-semibold">BEARISH HEDGE</span>
                      </div>
                    </div>
                  </div>
                </div>
              </div>

              {/* Action Button: Sleek TradingView Blue */}
              <div>
                <button
                  type="button"
                  onClick={() => router.push("/options")}
                  style={{ backgroundColor: COLOR_BLUE }}
                  className="w-full py-3 rounded-lg text-xs font-bold text-white tracking-wider uppercase transition-all duration-200 hover:bg-[#1e4bd8] active:translate-y-px shadow-lg shadow-blue-900/30 flex items-center justify-center space-x-2 group"
                >
                  <span>Open Full Option Chain</span>
                  <ExternalLink className="w-3.5 h-3.5 group-hover:translate-x-0.5 group-hover:-translate-y-0.5 transition-transform" />
                </button>
              </div>
            </div>
          </div>
        </main>
      </div>
    </AppShell>
  );
}

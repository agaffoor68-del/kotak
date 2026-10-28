"use client";

/**
 * Market Watch Page - Strict TradingView Mobile Web Design System.
 * - Single full-width dense panel: #1e222d, border #2a2e39, rounded-md (4px).
 * - Dense list of symbols with mini SVG Sparklines, LTP, and Change %.
 * - Search bar with filter, quick trade action, and live WebSocket streaming updates.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import {
  ArrowDownRight,
  ArrowUpRight,
  ChevronRight,
  Plus,
  Search,
  SlidersHorizontal,
  TrendingDown,
  TrendingUp,
} from "lucide-react";

import { api } from "@/lib/api";
import type { Quote } from "@/lib/api";
import { AppShell } from "@/components/app-shell";
import { Panel } from "@/components/ui";
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

interface WatchItem {
  symbol: string;
  name: string;
  token: string;
  segment: string;
  exchange: "NSE" | "BSE";
  defaultBase: number;
}

const MARKET_WATCH_LIST: WatchItem[] = [
  { symbol: "NIFTY 50", name: "NSE Benchmark Index", token: "99926000", segment: "nse_cm", exchange: "NSE", defaultBase: 24350 },
  { symbol: "BANKNIFTY", name: "Nifty Bank Index", token: "99926009", segment: "nse_cm", exchange: "NSE", defaultBase: 51800 },
  { symbol: "SENSEX", name: "BSE SENSEX 30", token: "99919000", segment: "bse_cm", exchange: "BSE", defaultBase: 79800 },
  { symbol: "FINNIFTY", name: "Nifty Financial Services", token: "99926037", segment: "nse_cm", exchange: "NSE", defaultBase: 23900 },
  { symbol: "MIDCPNIFTY", name: "Nifty Midcap Select", token: "99926014", segment: "nse_cm", exchange: "NSE", defaultBase: 12500 },
  { symbol: "RELIANCE", name: "Reliance Industries Ltd", token: "2885", segment: "nse_cm", exchange: "NSE", defaultBase: 2945 },
  { symbol: "HDFCBANK", name: "HDFC Bank Ltd", token: "1333", segment: "nse_cm", exchange: "NSE", defaultBase: 1620 },
  { symbol: "TCS", name: "Tata Consultancy Services", token: "11536", segment: "nse_cm", exchange: "NSE", defaultBase: 4120 },
  { symbol: "INFY", name: "Infosys Ltd", token: "1594", segment: "nse_cm", exchange: "NSE", defaultBase: 1885 },
  { symbol: "ICICIBANK", name: "ICICI Bank Ltd", token: "4963", segment: "nse_cm", exchange: "NSE", defaultBase: 1245 },
  { symbol: "TATAMOTORS", name: "Tata Motors Ltd", token: "3456", segment: "nse_cm", exchange: "NSE", defaultBase: 885 },
  { symbol: "SBIN", name: "State Bank of India", token: "3045", segment: "nse_cm", exchange: "NSE", defaultBase: 810 },
  { symbol: "BHARTIARTL", name: "Bharti Airtel Ltd", token: "31181", segment: "nse_cm", exchange: "NSE", defaultBase: 1720 },
  { symbol: "ITC", name: "ITC Ltd", token: "1660", segment: "nse_cm", exchange: "NSE", defaultBase: 475 },
  { symbol: "KOTAKBANK", name: "Kotak Mahindra Bank Ltd", token: "1922", segment: "nse_cm", exchange: "NSE", defaultBase: 1780 },
  { symbol: "LT", name: "Larsen & Toubro Ltd", token: "11483", segment: "nse_cm", exchange: "NSE", defaultBase: 3560 },
  { symbol: "HINDUNILVR", name: "Hindustan Unilever Ltd", token: "1394", segment: "nse_cm", exchange: "NSE", defaultBase: 2380 },
  { symbol: "AXISBANK", name: "Axis Bank Ltd", token: "5900", segment: "nse_cm", exchange: "NSE", defaultBase: 1190 },
  { symbol: "BAJFINANCE", name: "Bajaj Finance Ltd", token: "317", segment: "nse_cm", exchange: "NSE", defaultBase: 6850 },
  { symbol: "MARUTI", name: "Maruti Suzuki India", token: "10999", segment: "nse_cm", exchange: "NSE", defaultBase: 11420 },
];

/** Minimal SVG Sparkline for TradingView watchlist row */
function MiniSparkline({ isPositive, width = 64, height = 24 }: { isPositive: boolean; width?: number; height?: number }) {
  const points = isPositive
    ? [5, 7, 6, 9, 8, 12, 11, 15, 14, 18, 20]
    : [20, 18, 19, 14, 15, 12, 10, 11, 8, 6, 5];

  const min = Math.min(...points);
  const max = Math.max(...points);
  const range = max - min || 1;
  const padding = 2;
  const h = height - padding * 2;

  const coords = points.map((val, idx) => {
    const x = (idx / (points.length - 1)) * width;
    const y = height - padding - ((val - min) / range) * h;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });

  const pathD = `M ${coords.join(" L ")}`;
  const color = isPositive ? TV_GREEN : TV_RED;

  return (
    <svg width={width} height={height} className="overflow-visible block">
      <path d={pathD} fill="none" stroke={color} strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export default function MarketWatchPage() {
  const router = useRouter();
  const feed = useLiveFeed();

  const [filter, setFilter] = useState("");
  const [activeTab, setActiveTab] = useState<"all" | "indices" | "stocks">("all");

  // Subscribe watchlist to live WebSocket
  useEffect(() => {
    feed.subscribe(
      MARKET_WATCH_LIST.map((item) => ({
        instrument_token: item.token,
        exchange_segment: item.segment,
      }))
    );
  }, [feed]);

  // Filtered List
  const filteredList = useMemo(() => {
    return MARKET_WATCH_LIST.filter((item) => {
      const matchFilter =
        !filter ||
        item.symbol.toLowerCase().includes(filter.toLowerCase()) ||
        item.name.toLowerCase().includes(filter.toLowerCase());

      if (!matchFilter) return false;

      if (activeTab === "indices") {
        return item.symbol.includes("NIFTY") || item.symbol.includes("SENSEX");
      }
      if (activeTab === "stocks") {
        return !item.symbol.includes("NIFTY") && !item.symbol.includes("SENSEX");
      }
      return true;
    });
  }, [filter, activeTab]);

  return (
    <AppShell>
      <div className="space-y-2">
        {/* Full-width Single TradingView Panel */}
        <div className="bg-[#1e222d] border border-[#2a2e39] rounded-md overflow-hidden">
          {/* Watchlist Header & Filters */}
          <div className="p-2 sm:p-2.5 border-b border-[#2a2e39] flex flex-wrap items-center justify-between gap-2">
            {/* Title & Category Tabs */}
            <div className="flex items-center space-x-2">
              <h1 className="font-bold text-xs uppercase tracking-wider text-[#d1d4dc]">Market Watch</h1>
              <span className="text-[10px] text-[#787b86]">({filteredList.length})</span>

              <div className="flex items-center space-x-1 bg-[#131722] p-0.5 rounded border border-[#2a2e39] ml-2">
                <button
                  type="button"
                  onClick={() => setActiveTab("all")}
                  style={{
                    backgroundColor: activeTab === "all" ? "#2a2e39" : "transparent",
                    color: activeTab === "all" ? "#ffffff" : TV_MUTED,
                  }}
                  className="px-2 py-0.5 rounded text-[10px] font-semibold transition-all"
                >
                  All
                </button>
                <button
                  type="button"
                  onClick={() => setActiveTab("indices")}
                  style={{
                    backgroundColor: activeTab === "indices" ? "#2a2e39" : "transparent",
                    color: activeTab === "indices" ? "#ffffff" : TV_MUTED,
                  }}
                  className="px-2 py-0.5 rounded text-[10px] font-semibold transition-all"
                >
                  Indices
                </button>
                <button
                  type="button"
                  onClick={() => setActiveTab("stocks")}
                  style={{
                    backgroundColor: activeTab === "stocks" ? "#2a2e39" : "transparent",
                    color: activeTab === "stocks" ? "#ffffff" : TV_MUTED,
                  }}
                  className="px-2 py-0.5 rounded text-[10px] font-semibold transition-all"
                >
                  Stocks
                </button>
              </div>
            </div>

            {/* Search Input Box */}
            <div className="relative w-full sm:w-64">
              <Search className="w-3.5 h-3.5 absolute left-2.5 top-1/2 -translate-y-1/2 text-[#787b86]" />
              <input
                type="text"
                placeholder="Filter symbols..."
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
                className="w-full bg-[#131722] border border-[#2a2e39] rounded pl-8 pr-2 py-1 text-xs text-[#d1d4dc] placeholder-[#787b86] focus:outline-none focus:border-[#2962ff]"
              />
            </div>
          </div>

          {/* Table Header */}
          <div className="grid grid-cols-12 px-3 py-1.5 bg-[#171b26] border-b border-[#2a2e39] text-[10px] font-semibold text-[#787b86] uppercase tracking-wider select-none">
            <div className="col-span-5 sm:col-span-4">Symbol / Name</div>
            <div className="hidden sm:block sm:col-span-3 text-center">Chart</div>
            <div className="col-span-4 sm:col-span-3 text-right">Last Price</div>
            <div className="col-span-3 sm:col-span-2 text-right">Change</div>
          </div>

          {/* Dense Symbol Rows */}
          <div className="divide-y divide-[#2a2e39] overflow-x-hidden">
            {filteredList.map((item) => {
              const q = feed.quotes[quoteKey(item.token, item.segment)];
              const ltp = q?.last ?? item.defaultBase;
              const chg = q?.change ?? (ltp * 0.004);
              const chgPct = q?.change_percent ?? 0.4;
              const isUp = chgPct >= 0;

              return (
                <div
                  key={item.symbol}
                  onClick={() => router.push(`/trade?symbol=${encodeURIComponent(item.symbol)}`)}
                  className="grid grid-cols-12 px-3 py-2 items-center hover:bg-[#2a2e39] transition-colors cursor-pointer group text-xs"
                >
                  {/* Left: Symbol & Exchange */}
                  <div className="col-span-5 sm:col-span-4 min-w-0 pr-2">
                    <div className="flex items-center space-x-1.5">
                      <span className="font-semibold text-[#d1d4dc] group-hover:text-[#2962ff] transition-colors truncate">
                        {item.symbol}
                      </span>
                      <span className="text-[9px] bg-[#131722] text-[#787b86] px-1 py-0.5 rounded border border-[#2a2e39]">
                        {item.exchange}
                      </span>
                    </div>
                    <div className="text-[10px] text-[#787b86] truncate mt-0.5">{item.name}</div>
                  </div>

                  {/* Center: Mini Sparkline */}
                  <div className="hidden sm:flex sm:col-span-3 items-center justify-center">
                    <MiniSparkline isPositive={isUp} width={70} height={22} />
                  </div>

                  {/* Right: LTP */}
                  <div className="col-span-4 sm:col-span-3 text-right font-mono">
                    <div className="text-white font-medium text-xs">{num(ltp)}</div>
                    <div className="text-[10px] text-[#787b86]">{isUp ? "+" : ""}{num(chg)}</div>
                  </div>

                  {/* Far Right: Change Badge */}
                  <div className="col-span-3 sm:col-span-2 text-right font-mono">
                    <span
                      style={{
                        backgroundColor: isUp ? "rgba(8, 153, 129, 0.15)" : "rgba(242, 54, 69, 0.15)",
                        color: isUp ? TV_GREEN : TV_RED,
                      }}
                      className="inline-block px-1.5 py-0.5 rounded text-[11px] font-semibold"
                    >
                      {isUp ? "+" : ""}
                      {percent(chgPct)}
                    </span>
                  </div>
                </div>
              );
            })}
          </div>

          {/* Footer note */}
          <div className="px-3 py-2 bg-[#171b26] border-t border-[#2a2e39] text-[10px] text-[#787b86] flex items-center justify-between">
            <span>Click any symbol to open Instant Trading Desk & Charts</span>
            <span className="font-mono">Streaming 100ms via WebSocket</span>
          </div>
        </div>
      </div>
    </AppShell>
  );
}

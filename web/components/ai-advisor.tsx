"use client";

import { useCallback, useEffect, useState } from "react";
import { api, type LiveAnalysis } from "@/lib/api";

const MONITORED_INDICES = ["NIFTY 50", "BANKNIFTY", "SENSEX"];

export function AiAdvisor() {
  const [data, setData] = useState<LiveAnalysis[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [refreshing, setRefreshing] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);

  const loadData = useCallback(async (isManual = false) => {
    if (isManual) setRefreshing(true);
    try {
      const response = await api.liveAnalysis();
      const items: LiveAnalysis[] = Array.isArray(response)
        ? response
        : response
        ? [response]
        : [];
      
      // Ensure all 3 indices have an entry
      if (items.length > 0) {
        setData(items);
        setError(null);
        setLastUpdated(new Date());
      }
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Live AI Scanner is currently warming up.");
    } finally {
      setLoading(false);
      if (isManual) setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    void loadData();
    const timer = window.setInterval(() => void loadData(), 15_000);
    return () => window.clearInterval(timer);
  }, [loadData]);

  const executeTrade = (item: LiveAnalysis) => {
    const option = item.signal.includes("PE") ? "PE" : "CE";
    const strike = item.strike_price ? String(item.strike_price) : item.strike || "ATM";
    const targetUrl = `/trade?underlying=${encodeURIComponent(item.index || item.symbol || item.underlying)}&option=${option}&strike=${strike}`;
    window.location.assign(targetUrl);
  };

  // Build mapped index list to guarantee 3 cards render even during initial boot
  const displayItems: LiveAnalysis[] = MONITORED_INDICES.map((indexName) => {
    const found = data.find(
      (d) =>
        d.index?.toUpperCase() === indexName.toUpperCase() ||
        d.symbol?.toUpperCase() === indexName.toUpperCase() ||
        d.underlying?.toUpperCase() === indexName.toUpperCase()
    );
    if (found) return found;

    return {
      index: indexName,
      symbol: indexName,
      underlying: indexName,
      signal: "WAIT",
      strike: "ATM",
      confidence: "50%",
      reason: "Initializing indicators confluence...",
      trend: "Bullish",
      price: indexName === "NIFTY 50" ? 24350 : indexName === "BANKNIFTY" ? 51250 : 79800,
      timestamp: Date.now() / 1000,
      status: "live",
    };
  });

  return (
    <div className="space-y-4 text-ink" aria-live="polite">
      {/* Top Status & Controls Bar */}
      <div className="flex flex-wrap items-center justify-between gap-3 rounded border border-hairline bg-surface px-4 py-3 shadow-panel">
        <div className="flex items-center gap-3">
          <div className="flex h-3 w-3 items-center justify-center">
            <span className="relative flex h-2.5 w-2.5">
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-up opacity-75"></span>
              <span className="relative inline-flex h-2.5 w-2.5 rounded-full bg-up"></span>
            </span>
          </div>
          <div>
            <h2 className="text-sm font-semibold tracking-wide">Multi-Index AI Live Engine</h2>
            <p className="text-2xs text-ink-dim">
              Confluence Scoring: EMA (20/50) · RSI (14) · Supertrend (10,3) · VWAP with Self-Learning Memory
            </p>
          </div>
        </div>

        <div className="flex items-center gap-3 text-2xs text-ink-faint">
          {lastUpdated && (
            <span>
              Updated {lastUpdated.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", second: "2-digit" })}
            </span>
          )}
          <button
            onClick={() => void loadData(true)}
            disabled={refreshing}
            className="flex items-center gap-1.5 rounded border border-hairline bg-canvas px-2.5 py-1 font-medium text-ink hover:border-edge disabled:opacity-50"
            type="button"
          >
            <svg
              className={`h-3 w-3 ${refreshing ? "animate-spin text-accent" : ""}`}
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <path d="M21 12a9 9 0 0 0-9-9 9.75 9.75 0 0 0-6.74 2.74L3 8" />
              <path d="M3 3v5h5" />
              <path d="M3 12a9 9 0 0 0 9 9 9.75 9.75 0 0 0 6.74-2.74L21 16" />
              <path d="M16 21h5v-5" />
            </svg>
            <span>{refreshing ? "Scanning..." : "Refresh"}</span>
          </button>
        </div>
      </div>

      {error && (
        <div className="rounded border border-down/30 bg-down-soft/40 px-3 py-2 text-2xs text-down">
          {error}
        </div>
      )}

      {/* Multi-Index Grid (3 Cards for NIFTY 50, BANKNIFTY, SENSEX) */}
      <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
        {displayItems.map((item) => {
          const isBuyCE = item.signal === "BUY CE";
          const isBuyPE = item.signal === "BUY PE";
          const isWait = item.signal === "WAIT";
          const isBullish = item.trend === "Bullish";
          const isBearish = item.trend === "Bearish";

          const signalDisplay = isBuyCE
            ? `BUY CE ${item.strike_price || ""}`
            : isBuyPE
            ? `BUY PE ${item.strike_price || ""}`
            : item.avoiding_loss_pattern
            ? "WAIT (Pattern Avoidance)"
            : "WAIT (Neutral Confluence)";

          return (
            <div
              key={item.index}
              className={`flex flex-col justify-between rounded border bg-surface p-4 shadow-panel transition-all duration-200 ${
                isBuyCE
                  ? "border-up/40 bg-gradient-to-b from-up-soft/20 to-surface"
                  : isBuyPE
                  ? "border-down/40 bg-gradient-to-b from-down-soft/20 to-surface"
                  : "border-hairline"
              }`}
            >
              <div>
                {/* Index Header */}
                <div className="flex items-start justify-between border-b border-hairline/60 pb-3">
                  <div>
                    <span className="text-3xs font-semibold uppercase tracking-widest text-ink-faint">
                      INDEX
                    </span>
                    <h3 className="text-base font-bold text-ink">{item.index}</h3>
                    {item.price !== undefined && (
                      <p className="mt-0.5 font-mono text-xs font-semibold text-ink">
                        ₹{item.price.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                      </p>
                    )}
                  </div>
                  <div className="flex flex-col items-end gap-1">
                    <span
                      className={`rounded px-2 py-0.5 text-2xs font-bold uppercase tracking-wider ${
                        isBullish
                          ? "bg-up-soft text-up border border-up/30"
                          : isBearish
                          ? "bg-down-soft text-down border border-down/30"
                          : "bg-canvas text-ink-dim border border-hairline"
                      }`}
                    >
                      {item.trend}
                    </span>
                    <span className="text-3xs font-mono text-ink-faint">
                      {item.confluence_score ? `${item.confluence_score} Confluence` : ""}
                    </span>
                  </div>
                </div>

                {/* Main Signal Display */}
                <div
                  className={`mt-3 rounded border p-3 ${
                    isBuyCE
                      ? "border-up/50 bg-up-soft/50"
                      : isBuyPE
                      ? "border-down/50 bg-down-soft/50"
                      : "border-hairline bg-canvas"
                  }`}
                >
                  <div className="flex items-center justify-between">
                    <span className="text-3xs font-semibold uppercase tracking-wider text-ink-faint">
                      AI Generated Signal
                    </span>
                    <span
                      className={`font-mono text-2xs font-bold ${
                        isBuyCE ? "text-up" : isBuyPE ? "text-down" : "text-warn"
                      }`}
                    >
                      {item.confidence} Confidence
                    </span>
                  </div>

                  <div className="mt-1 flex items-center gap-2">
                    {isBuyCE && (
                      <svg className="h-5 w-5 text-up flex-shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                        <polyline points="23 6 13.5 15.5 8.5 10.5 1 18" />
                        <polyline points="17 6 23 6 23 12" />
                      </svg>
                    )}
                    {isBuyPE && (
                      <svg className="h-5 w-5 text-down flex-shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                        <polyline points="23 18 13.5 8.5 8.5 13.5 1 6" />
                        <polyline points="17 18 23 18 23 12" />
                      </svg>
                    )}
                    {isWait && (
                      <svg className="h-4 w-4 text-warn flex-shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                        <circle cx="12" cy="12" r="10" />
                        <line x1="12" y1="8" x2="12" y2="12" />
                        <line x1="12" y1="16" x2="12.01" y2="16" />
                      </svg>
                    )}
                    <span
                      className={`text-lg font-black tracking-tight ${
                        isBuyCE ? "text-up" : isBuyPE ? "text-down" : "text-ink-dim"
                      }`}
                    >
                      {signalDisplay}
                    </span>
                  </div>

                  {item.avoiding_loss_pattern && (
                    <div className="mt-2 flex items-center gap-1.5 rounded bg-warn-soft/60 px-2 py-1 text-3xs font-medium text-warn border border-warn/30">
                      <svg className="h-3 w-3 flex-shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                        <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
                      </svg>
                      <span>AI Memory: Avoiding previous loss pattern</span>
                    </div>
                  )}
                </div>

                {/* AI Reasoning Section */}
                <div className="mt-3">
                  <p className="text-3xs font-semibold uppercase tracking-wider text-ink-faint">
                    AI Reasoning
                  </p>
                  <p className="mt-1 text-2xs leading-relaxed text-ink-dim">
                    {item.reason}
                  </p>
                </div>

                {/* Technical Indicator Metrics */}
                <div className="mt-3 grid grid-cols-2 gap-1.5 rounded border border-hairline/60 bg-canvas p-2 font-mono text-3xs">
                  <div className="flex justify-between border-b border-hairline/40 pb-1">
                    <span className="text-ink-faint">RSI (14):</span>
                    <span className={item.rsi && item.rsi > 55 ? "text-up" : item.rsi && item.rsi < 45 ? "text-down" : "text-ink"}>
                      {item.rsi !== undefined ? item.rsi.toFixed(1) : "—"}
                    </span>
                  </div>
                  <div className="flex justify-between border-b border-hairline/40 pb-1">
                    <span className="text-ink-faint">VWAP:</span>
                    <span className="text-ink">{item.vwap !== undefined ? item.vwap.toFixed(1) : "—"}</span>
                  </div>
                  <div className="flex justify-between pt-0.5">
                    <span className="text-ink-faint">EMA 20/50:</span>
                    <span className={item.ema20 && item.ema50 && item.ema20 > item.ema50 ? "text-up" : "text-down"}>
                      {item.ema20 && item.ema50 ? (item.ema20 > item.ema50 ? "Bullish" : "Bearish") : "—"}
                    </span>
                  </div>
                  <div className="flex justify-between pt-0.5">
                    <span className="text-ink-faint">Supertrend:</span>
                    <span className="text-ink">{item.supertrend !== undefined ? item.supertrend.toFixed(1) : "—"}</span>
                  </div>
                </div>
              </div>

              {/* Action Button */}
              <div className="mt-4">
                <button
                  type="button"
                  disabled={isWait}
                  onClick={() => executeTrade(item)}
                  className={`w-full rounded py-2 px-3 text-xs font-bold tracking-wide transition-colors ${
                    isBuyCE
                      ? "bg-up text-black hover:bg-up/90 active:scale-[0.98]"
                      : isBuyPE
                      ? "bg-down text-white hover:bg-down/90 active:scale-[0.98]"
                      : "cursor-not-allowed border border-hairline bg-canvas text-ink-faint opacity-60"
                  }`}
                >
                  {isWait
                    ? "WAIT FOR CONFLUENCE"
                    : `EXECUTE ${item.index} ${item.strike_price || "ATM"} ${item.signal.replace("BUY ", "")}`}
                </button>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

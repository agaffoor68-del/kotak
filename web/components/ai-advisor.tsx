"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";

interface LiveAnalysis {
  underlying: string;
  signal: "BUY CE" | "BUY PE" | "WAIT";
  strike: string;
  confidence: string;
  reason: string;
  trend: "Bullish" | "Bearish" | "Unknown";
  timestamp: number;
  status: string;
}

export function AiAdvisor() {
  const [data, setData] = useState<LiveAnalysis | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setData(await api.liveAnalysis("NIFTY 50"));
      setError(null);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Live analysis is unavailable.");
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 30_000);
    return () => window.clearInterval(timer);
  }, [load]);

  const execute = () => {
    const option = data?.signal === "BUY PE" ? "PE" : "CE";
    window.location.assign(`/trade?underlying=${encodeURIComponent(data?.underlying ?? "NIFTY 50")}&option=${option}&strike=ATM`);
  };

  return (
    <section className="rounded border border-hairline bg-panel p-4 text-ink shadow-sm" aria-live="polite">
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="text-2xs font-semibold uppercase tracking-[0.18em] text-ink-faint">Live AI Market Analyzer</p>
          <h2 className="mt-1 text-lg font-semibold">{data?.underlying ?? "NIFTY 50"}</h2>
        </div>
        <span className={`rounded px-2 py-1 text-2xs font-semibold ${data?.trend === "Bullish" ? "bg-up-soft text-up" : data?.trend === "Bearish" ? "bg-down-soft text-down" : "bg-canvas text-ink-faint"}`}>
          {data?.trend ?? "Connecting"}
        </span>
      </div>

      {data ? (
        <>
          <div className="mt-5 rounded border border-hairline bg-canvas p-4">
            <p className="text-2xs text-ink-faint">Best option execution</p>
            <p className="mt-1 text-2xl font-bold tracking-tight">
              {data.signal === "WAIT" ? "WAIT (Sideways Market)" : `RECOMMENDED: BUY ${data.underlying} ${data.strike} ${data.signal.replace("BUY ", "")}`}
            </p>
            <p className="mt-2 text-xs text-ink-dim">{data.reason}</p>
            <p className="mt-2 text-2xs text-ink-faint">Confidence {data.confidence} · updates every 30 seconds</p>
          </div>
          <button className="btn btn-primary mt-4 w-full" disabled={data.signal === "WAIT"} onClick={execute} type="button">
            {data.signal === "WAIT" ? "WAIT FOR CONFIRMATION" : "EXECUTE THIS TRADE"}
          </button>
        </>
      ) : (
        <p className="mt-4 text-xs text-ink-faint">{error ?? "Connecting to the live market scanner…"}</p>
      )}
      {error && data ? <p className="mt-2 text-2xs text-down">{error}</p> : null}
    </section>
  );
}

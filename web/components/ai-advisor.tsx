"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api, type TradeSuggestion } from "@/lib/api";

export function AiAdvisor() {
  const router = useRouter();
  const [data, setData] = useState<TradeSuggestion | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    const load = async () => {
      try {
        const suggestion = await api.suggestTrade();
        if (active) { setData(suggestion); setError(null); }
      } catch (failure) {
        if (active) setError(failure instanceof Error ? failure.message : "Advisor unavailable");
      }
    };
    void load();
    const timer = window.setInterval(() => void load(), 300_000);
    return () => { active = false; window.clearInterval(timer); };
  }, []);

  const actionable = data && data.signal !== "WAIT";
  return (
    <section className="panel relative overflow-hidden border-accent/40 bg-accent/5 shadow-[0_0_28px_hsl(var(--accent)/0.12)]" aria-labelledby="ai-advisor-title">
      <div className="pointer-events-none absolute -right-10 -top-16 size-40 rounded-full bg-accent/20 blur-3xl" />
      <div className="relative flex flex-wrap items-start justify-between gap-4 px-5 py-4">
        <div>
          <p id="ai-advisor-title" className="font-semibold text-ink">🤖 Alpha AI Advisor</p>
          <p className="mt-1 text-2xs text-ink-faint">5-minute momentum scan · live market data</p>
        </div>
        {data ? <span className="chip chip-live">{data.confidence} confidence</span> : null}
      </div>
      <div className="relative border-t border-hairline px-5 py-4">
        {error ? <p className="text-2xs text-warn">{error}</p> : data ? (
          <div className="flex flex-wrap items-center justify-between gap-4">
            <div>
              <p className={`num text-2xl font-bold ${data.signal === "BUY PE" ? "text-down" : data.signal === "WAIT" ? "text-warn" : "text-up"}`}>
                {data.signal === "WAIT" ? "WAIT" : `NIFTY ${data.strike} ${data.signal === "BUY CE" ? "CE" : "PE"} - BUY`}
              </p>
              <p className="mt-1 text-2xs text-ink-dim">{data.reason} · VIX {data.india_vix === null ? "—" : data.india_vix.toFixed(2)}</p>
            </div>
            <button type="button" className="btn btn-sm btn-primary" disabled={!actionable} onClick={() => router.push(`/trade?underlying=NIFTY&strike=ATM&type=${data.signal === "BUY CE" ? "CE" : "PE"}`)}>
              Execute this Trade
            </button>
          </div>
        ) : <p className="text-2xs text-ink-faint">Calculating the next signal…</p>}
      </div>
    </section>
  );
}

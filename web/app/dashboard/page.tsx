"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import type { BreadthSnapshot, MarketStatus, Quote } from "@/lib/api";
import { AppShell } from "@/components/app-shell";
import { Loading } from "@/components/ui";
import { num, percent, inr } from "@/lib/format";
import { quoteKey, useLiveFeed } from "@/lib/useLiveFeed";

const INDEX_TOKENS = [
  { token: "26000", segment: "nse_cm", label: "NIFTY 50" },
  { token: "26009", segment: "nse_cm", label: "BANKNIFTY" },
  { token: "26037", segment: "nse_cm", label: "FINNIFTY" },
  { token: "99919000", segment: "bse_cm", label: "SENSEX" },
];

const CARD = "bg-[#0A0A0A] border border-[#333333] rounded-lg";
const UP = "#17C964";
const DOWN = "#F31260";

export default function DashboardPage() {
  const router = useRouter();
  const feed = useLiveFeed();
  const [status, setStatus] = useState<MarketStatus | null>(null);
  const [breadth, setBreadth] = useState<BreadthSnapshot | null>(null);
  const [fiiDii, setFiiDii] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [now, setNow] = useState(() => Date.now());
  const [spark, setSpark] = useState<Record<string, number[]>>({});

  const load = useCallback(async () => {
    try {
      const market = await api.status();
      setStatus(market);
      setError(null);
      feed.subscribe(INDEX_TOKENS.map((e) => ({ instrument_token: e.token, exchange_segment: e.segment })));
      setBreadth(await api.breadth(200));
      try {
        const r = await fetch("/api/v1/market/fii-dii");
        if (r.ok) setFiiDii(await r.json());
      } catch {}
    } catch (f) {
      setError(f instanceof Error ? f.message : "Could not load");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    const t = setInterval(() => void load(), 60000);
    return () => clearInterval(t);
  }, [load]);

  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, []);

  useEffect(() => {
    INDEX_TOKENS.forEach((e) => {
      const q = feed.quotes[quoteKey(e.token, e.segment)];
      if (q?.last != null) {
        setSpark((s) => ({ ...s, [e.token]: [...(s[e.token] || []), q.last as number].slice(-20) }));
      }
    });
  }, [feed.quotes]);

  const advances = breadth?.breadth?.advances ?? 0;
  const declines = breadth?.breadth?.declines ?? 0;
  const priced = breadth?.breadth?.priced ?? breadth?.priced ?? 0;

  const sparkPoints = (values: number[]) => {
    if (values.length < 2) return "";
    const min = Math.min(...values);
    const max = Math.max(...values);
    const range = max - min || 1;
    return values.map((v, i) => `${(i / (values.length - 1)) * 100},${100 - ((v - min) / range) * 100}`).join(" ");
  };

  return (
    <AppShell>
      <div className="bg-black text-[#EDEDED] min-h-screen p-4 font-sans" style={{ fontFamily: "Inter, system-ui, sans-serif" }}>
        {(error || feed.state !== "live") && (
          <div className="mb-3 px-3 py-2 rounded-md bg-[#1a1a1a] border border-[#333333] text-[#888888] text-xs">
            Connecting to live feed...
          </div>
        )}

        <div className="grid grid-cols-4 gap-3 mb-3">
          {INDEX_TOKENS.map((e) => {
            const q = feed.quotes[quoteKey(e.token, e.segment)];
            const ch = q?.change_percent ?? 0;
            const up = ch >= 0;
            return (
              <div key={e.token} className={`${CARD} p-3`}>
                <div className="flex items-center justify-between">
                  <span className="text-[#888888] text-[11px] uppercase tracking-wider">{e.label}</span>
                  <span className="text-[11px] font-semibold" style={{ color: up ? UP : DOWN }}>{percent(ch)}</span>
                </div>
                <div className="text-xl font-semibold mt-1" style={{ color: UP, transition: "color 0.3s" }}>{num(q?.last)}</div>
                <svg viewBox="0 0 100 100" preserveAspectRatio="none" className="w-full h-6 mt-1.5">
                  <polyline fill="none" stroke={up ? UP : DOWN} strokeWidth="1.5" points={sparkPoints(spark[e.token] || [])} />
                </svg>
              </div>
            );
          })}
        </div>

        <div className="grid grid-cols-[60%_40%] gap-3 mb-3">
          <div className={`${CARD} p-3`}>
            <div className="text-[#888888] text-[11px] uppercase tracking-wider mb-2">Market Breadth & Institutional Activity</div>
            <div className="flex h-2.5 rounded overflow-hidden mb-2">
              <div style={{ width: `${priced ? (advances / priced) * 100 : 0}%`, background: UP }} />
              <div className="flex-1" style={{ background: DOWN }} />
            </div>
            <div className="flex justify-between text-[11px] text-[#888888] mb-3">
              <span style={{ color: UP }}>{num(advances)} Adv</span>
              <span style={{ color: DOWN }}>{num(declines)} Dec</span>
            </div>
            {fiiDii ? (
              <div className="text-xs space-y-1.5">
                <div className="flex justify-between">
                  <span className="text-[#888888]">FII Net</span>
                  <span style={{ color: (fiiDii.fii_net ?? 0) >= 0 ? UP : DOWN }}>{inr(fiiDii.fii_net ?? 0)}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-[#888888]">DII Net</span>
                  <span style={{ color: (fiiDii.dii_net ?? 0) >= 0 ? UP : DOWN }}>{inr(fiiDii.dii_net ?? 0)}</span>
                </div>
              </div>
            ) : (
              <div className="text-[#888888] text-xs">Loading FII/DII...</div>
            )}
          </div>
          <div className={`${CARD} p-3`}>
            <div className="text-[#888888] text-[11px] uppercase tracking-wider mb-2">Quick Option Chain</div>
            <div className="text-[#888888] text-xs mb-3">ATM CE / PE snapshot for NIFTY 50</div>
            <button
              onClick={() => router.push("/options")}
              className="px-3 py-1.5 rounded-md text-xs font-semibold cursor-pointer border-none"
              style={{ background: UP, color: "#000" }}
            >
              Open Full Chain
            </button>
          </div>
        </div>

        <div className="grid grid-cols-2 gap-3">
          <div className={`${CARD} p-3`}>
            <div className="text-[#888888] text-[11px] uppercase tracking-wider mb-2">Top Gainers</div>
            <table className="w-full text-xs border-collapse">
              <thead>
                <tr className="text-[#888888]">
                  <th className="text-left py-1">Symbol</th>
                  <th className="text-right">LTP</th>
                  <th className="text-right">Change %</th>
                </tr>
              </thead>
              <tbody>
                {(breadth?.top_gainers ?? []).slice(0, 8).map((q: Quote) => (
                  <tr key={quoteKey(q.token, q.exchange_segment)} className="border-t border-[#333333]">
                    <td className="py-1 text-[#EDEDED]">{q.symbol ?? q.token}</td>
                    <td className="text-right text-[#EDEDED]">{num(q.last)}</td>
                    <td className="text-right" style={{ color: UP }}>{percent(q.change_percent)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className={`${CARD} p-3`}>
            <div className="text-[#888888] text-[11px] uppercase tracking-wider mb-2">Top Losers</div>
            <table className="w-full text-xs border-collapse">
              <thead>
                <tr className="text-[#888888]">
                  <th className="text-left py-1">Symbol</th>
                  <th className="text-right">LTP</th>
                  <th className="text-right">Change %</th>
                </tr>
              </thead>
              <tbody>
                {(breadth?.top_losers ?? []).slice(0, 8).map((q: Quote) => (
                  <tr key={quoteKey(q.token, q.exchange_segment)} className="border-t border-[#333333]">
                    <td className="py-1 text-[#EDEDED]">{q.symbol ?? q.token}</td>
                    <td className="text-right text-[#EDEDED]">{num(q.last)}</td>
                    <td className="text-right" style={{ color: DOWN }}>{percent(q.change_percent)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        {loading && !breadth ? <Loading label="Scanning the market" /> : null}
      </div>
    </AppShell>
  );
}

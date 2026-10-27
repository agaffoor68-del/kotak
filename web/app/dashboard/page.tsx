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

const BG = "#131722";
const PANEL = "#1e222d";
const BORDER = "#2a2e39";
const TEXT = "#d1d4dc";
const MUTED = "#787b86";
const UP = "#26a69a";
const DOWN = "#ef5350";
const BLUE = "#2962ff";

export default function DashboardPage() {
  const router = useRouter();
  const feed = useLiveFeed();
  const [breadth, setBreadth] = useState<BreadthSnapshot | null>(null);
  const [fiiDii, setFiiDii] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [tab, setTab] = useState<"gainers" | "losers">("gainers");
  const [spark, setSpark] = useState<Record<string, number[]>>({});

  const load = useCallback(async () => {
    try {
      await api.status();
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
    INDEX_TOKENS.forEach((e) => {
      const q = feed.quotes[quoteKey(e.token, e.segment)];
      if (q?.last != null) {
        setSpark((s) => ({ ...s, [e.token]: [...(s[e.token] || []), q.last as number].slice(-20) }));
      }
    });
  }, [feed.quotes]);

  const advances = breadth?.breadth?.advances ?? 0;
  const declines = breadth?.breadth?.declines ?? 0;
  const unchanged = breadth?.breadth?.unchanged ?? 0;
  const priced = breadth?.breadth?.priced ?? breadth?.priced ?? 0;

  const sparkPoints = (values: number[]) => {
    if (values.length < 2) return "";
    const min = Math.min(...values);
    const max = Math.max(...values);
    const range = max - min || 1;
    return values.map((v, i) => `${(i / (values.length - 1)) * 100},${100 - ((v - min) / range) * 100}`).join(" ");
  };

  const rows = tab === "gainers" ? (breadth?.top_gainers ?? []) : (breadth?.top_losers ?? []);

  return (
    <AppShell>
      <div style={{ background: BG, color: TEXT, minHeight: "100vh", fontFamily: "Inter, system-ui, sans-serif" }} className="text-xs sm:text-sm">
        {/* Ticker tape */}
        <div className="overflow-x-auto border-b" style={{ borderColor: BORDER, background: PANEL }}>
          <div className="flex items-center gap-4 px-3 py-2 whitespace-nowrap">
            {INDEX_TOKENS.map((e, i) => {
              const q = feed.quotes[quoteKey(e.token, e.segment)];
              const ch = q?.change_percent ?? 0;
              const up = ch >= 0;
              return (
                <div key={e.token} className="flex items-center gap-2">
                  {i > 0 && <span className="h-4 w-px" style={{ background: BORDER }} />}
                  <span style={{ color: MUTED }}>{e.label}</span>
                  <span style={{ color: TEXT }}>{num(q?.last)}</span>
                  <span style={{ color: up ? UP : DOWN }}>{percent(ch)}</span>
                </div>
              );
            })}
          </div>
        </div>

        {/* Status dot */}
        <div className="flex items-center gap-2 px-3 py-1 text-[10px]" style={{ color: MUTED }}>
          <span className="h-1.5 w-1.5 rounded-full" style={{ background: feed.state === "live" ? UP : DOWN }} />
          {feed.state === "live" ? "Live" : "Connecting to live feed..."}
        </div>

        <div className="p-3 space-y-3">
          {/* Section A: Breadth & FII/DII */}
          <section style={{ background: PANEL, border: `1px solid ${BORDER}`, borderRadius: 8 }} className="p-3">
            <h2 className="text-sm font-semibold mb-2" style={{ color: TEXT }}>Market Breadth & Institutional Activity</h2>
            <div className="flex h-2 rounded overflow-hidden mb-2">
              <div style={{ width: `${priced ? (advances / priced) * 100 : 0}%`, background: UP }} />
              <div className="flex-1" style={{ background: DOWN }} />
            </div>
            <div className="flex flex-wrap gap-3 text-[11px] mb-3" style={{ color: MUTED }}>
              <span>Adv: <span style={{ color: UP }}>{num(advances)}</span></span>
              <span>Dec: <span style={{ color: DOWN }}>{num(declines)}</span></span>
              <span>Unch: <span style={{ color: TEXT }}>{num(unchanged)}</span></span>
            </div>
            {fiiDii ? (
              <table className="w-full text-[11px]">
                <tbody>
                  <tr className="border-t" style={{ borderColor: BORDER }}>
                    <td className="py-1.5" style={{ color: MUTED }}>FII Net</td>
                    <td className="py-1.5 text-right" style={{ color: (fiiDii.fii_net ?? 0) >= 0 ? UP : DOWN }}>{inr(fiiDii.fii_net ?? 0)}</td>
                  </tr>
                  <tr className="border-t" style={{ borderColor: BORDER }}>
                    <td className="py-1.5" style={{ color: MUTED }}>DII Net</td>
                    <td className="py-1.5 text-right" style={{ color: (fiiDii.dii_net ?? 0) >= 0 ? UP : DOWN }}>{inr(fiiDii.dii_net ?? 0)}</td>
                  </tr>
                </tbody>
              </table>
            ) : (
              <div style={{ color: MUTED }} className="text-[11px]">Loading FII/DII...</div>
            )}
          </section>

          {/* Section B: Top Gainers/Losers */}
          <section style={{ background: PANEL, border: `1px solid ${BORDER}`, borderRadius: 8 }} className="overflow-hidden">
            <div className="flex border-b" style={{ borderColor: BORDER }}>
              {(["gainers", "losers"] as const).map((t) => (
                <button
                  key={t}
                  onClick={() => setTab(t === "gainers" ? "gainers" : "losers")}
                  className="px-4 py-2 text-xs font-medium"
                  style={{
                    color: tab === t ? TEXT : MUTED,
                    borderBottom: tab === t ? `2px solid ${BLUE}` : "2px solid transparent",
                    background: "transparent",
                    border: "none",
                    cursor: "pointer",
                  }}
                >
                  {t === "gainers" ? "Top Gainers" : "Top Losers"}
                </button>
              ))}
            </div>
            <table className="w-full text-xs">
              <thead>
                <tr style={{ color: MUTED }} className="text-[11px]">
                  <th className="text-left py-2 px-3 font-medium">SYMBOL</th>
                  <th className="text-right py-2 px-3 font-medium">LTP</th>
                  <th className="text-right py-2 px-3 font-medium">CHANGE %</th>
                </tr>
              </thead>
              <tbody>
                {rows.slice(0, 10).map((q: Quote) => {
                  const up = (q.change_percent ?? 0) >= 0;
                  return (
                    <tr key={quoteKey(q.token, q.exchange_segment)} className="hover:bg-[#2a2e39] transition-colors" style={{ borderTop: `1px solid ${BORDER}` }}>
                      <td className="py-3 px-3 text-left" style={{ color: TEXT }}>{q.symbol ?? q.token}</td>
                      <td className="py-3 px-3 text-right" style={{ color: TEXT }}>{num(q.last)}</td>
                      <td className="py-3 px-3 text-right" style={{ color: up ? UP : DOWN }}>{percent(q.change_percent)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {loading && !breadth ? <Loading label="Scanning the market" /> : null}
          </section>

          {/* Section C: Quick Option Chain */}
          <section style={{ background: PANEL, border: `1px solid ${BORDER}`, borderRadius: 8 }} className="p-3 flex items-center justify-between gap-3">
            <div>
              <h2 className="text-sm font-semibold" style={{ color: TEXT }}>Quick Option Chain</h2>
              <p className="text-[11px]" style={{ color: MUTED }}>ATM CE / PE snapshot for NIFTY 50</p>
            </div>
            <button
              onClick={() => router.push("/options")}
              className="px-3 py-1.5 rounded-md text-xs font-semibold text-white"
              style={{ background: BLUE }}
            >
              Open Full Chain
            </button>
          </section>
        </div>

        {error ? (
          <div className="fixed bottom-3 left-1/2 -translate-x-1/2 px-3 py-1.5 rounded-md text-[11px]" style={{ background: PANEL, border: `1px solid ${BORDER}`, color: MUTED }}>
            Connecting to live feed...
          </div>
        ) : null}
      </div>
    </AppShell>
  );
}

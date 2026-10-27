"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import type { BreadthSnapshot, Quote, OptionChain } from "@/lib/api";
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

const MOCK_ROWS = [
  { symbol: "RELIANCE", ltp: 2945.30, chg: 2.45 },
  { symbol: "TCS", ltp: 4120.10, chg: 1.87 },
  { symbol: "HDFCBANK", ltp: 1620.55, chg: 1.22 },
  { symbol: "INFY", ltp: 1580.00, chg: 0.95 },
  { symbol: "ICICIBANK", ltp: 1245.60, chg: 0.71 },
];

const MOCK_SECTORS = [
  { name: "NIFTY IT", chg: 1.32 },
  { name: "NIFTY AUTO", chg: -0.45 },
  { name: "NIFTY FMCG", chg: 0.18 },
  { name: "NIFTY PHARMA", chg: -0.67 },
];

export default function DashboardPage() {
  const router = useRouter();
  const feed = useLiveFeed();
  const [breadth, setBreadth] = useState<BreadthSnapshot | null>(null);
  const [fiiDii, setFiiDii] = useState<any>(null);
  const [chain, setChain] = useState<OptionChain | null>(null);
  const [sectors, setSectors] = useState(MOCK_SECTORS);
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
      try {
        setChain(await api.chain("NIFTY 50"));
      } catch {}
      try {
        const idx = await api.indices();
        const found = ["IT", "AUTO", "FMCG", "PHARMA"].map((key) => {
          const m = idx.find((i: any) => (i.symbol || i.name || "").toUpperCase().includes(key));
          return m ? { name: `NIFTY ${key}`, chg: m.quote?.change_percent ?? 0 } : null;
        }).filter(Boolean) as { name: string; chg: number }[];
        if (found.length) setSectors(found);
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
  const priced = Math.max(1, (breadth?.breadth?.priced ?? breadth?.priced ?? 0));
  const advR = advances || 1;
  const decR = declines || 1;
  const unchR = unchanged || 1;

  const sparkPoints = (values: number[]) => {
    if (values.length < 2) return "";
    const min = Math.min(...values);
    const max = Math.max(...values);
    const range = max - min || 1;
    return values.map((v, i) => `${(i / (values.length - 1)) * 100},${100 - ((v - min) / range) * 100}`).join(" ");
  };

  const rows = tab === "gainers" ? (breadth?.top_gainers ?? []) : (breadth?.top_losers ?? []);
  const displayRows = rows.length ? rows.slice(0, 8) : MOCK_ROWS.map((m, i) => ({
    token: String(i),
    symbol: m.symbol,
    exchange_segment: "nse_cm",
    last: m.ltp,
    change_percent: m.chg,
  })) as any[];

  const atm = chain?.summary?.atm_strike ?? null;
  const atmRow = chain?.rows?.find((r) => r.strike === atm);
  const ceLtp = atmRow?.call?.ltp ?? null;
  const peLtp = atmRow?.put?.ltp ?? null;

  return (
    <AppShell>
      <div style={{ background: BG, color: TEXT, minHeight: "100vh", fontFamily: "Inter, system-ui, sans-serif" }} className="text-xs">
        {/* Ticker tape */}
        <div className="overflow-x-auto border-b" style={{ borderColor: BORDER, background: PANEL }}>
          <div className="flex items-center gap-0 px-2 py-1.5 whitespace-nowrap">
            {INDEX_TOKENS.map((e, i) => {
              const q = feed.quotes[quoteKey(e.token, e.segment)];
              const ch = q?.change_percent ?? 0;
              const up = ch >= 0;
              return (
                <div key={e.token} className="flex items-center gap-2 pr-3 border-r" style={{ borderColor: BORDER }}>
                  {i > 0 && <span />}
                  <span style={{ color: MUTED }} className="text-[11px]">{e.label}</span>
                  <span style={{ color: TEXT }} className="text-[11px] font-medium">{num(q?.last)}</span>
                  <span style={{ color: up ? UP : DOWN }} className="text-[11px]">{percent(ch)}</span>
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

        <div className="p-2 space-y-2">
          {/* Section A: Breadth & FII/DII */}
          <section style={{ background: PANEL, border: `1px solid ${BORDER}`, borderRadius: 6 }} className="p-2.5">
            <h2 className="text-xs font-semibold mb-1.5" style={{ color: TEXT }}>Market Breadth & Institutional Activity</h2>
            <div className="flex h-2 rounded overflow-hidden mb-1.5">
              <div style={{ flexGrow: advR, background: UP }} />
              <div style={{ flexGrow: unchR, background: "#4a4e59" }} />
              <div style={{ flexGrow: decR, background: DOWN }} />
            </div>
            <div className="flex flex-wrap gap-3 text-[10px] mb-2" style={{ color: MUTED }}>
              <span>Adv: <span style={{ color: UP }}>{num(advances)}</span></span>
              <span>Dec: <span style={{ color: DOWN }}>{num(declines)}</span></span>
              <span>Unch: <span style={{ color: TEXT }}>{num(unchanged)}</span></span>
            </div>
            {fiiDii ? (
              <div className="grid grid-cols-2 gap-1.5">
                <div className="rounded p-1.5" style={{ background: BG, border: `1px solid ${BORDER}` }}>
                  <div style={{ color: MUTED }} className="text-[10px]">FII Net</div>
                  <div style={{ color: (fiiDii.fii_net ?? 0) >= 0 ? UP : DOWN }} className="text-xs font-semibold">{inr(fiiDii.fii_net ?? 0)}</div>
                </div>
                <div className="rounded p-1.5" style={{ background: BG, border: `1px solid ${BORDER}` }}>
                  <div style={{ color: MUTED }} className="text-[10px]">DII Net</div>
                  <div style={{ color: (fiiDii.dii_net ?? 0) >= 0 ? UP : DOWN }} className="text-xs font-semibold">{inr(fiiDii.dii_net ?? 0)}</div>
                </div>
                <div className="rounded p-1.5" style={{ background: BG, border: `1px solid ${BORDER}` }}>
                  <div style={{ color: MUTED }} className="text-[10px]">FII Buy</div>
                  <div style={{ color: TEXT }} className="text-xs font-semibold">{inr(fiiDii.fii_buy ?? 0)}</div>
                </div>
                <div className="rounded p-1.5" style={{ background: BG, border: `1px solid ${BORDER}` }}>
                  <div style={{ color: MUTED }} className="text-[10px]">DII Sell</div>
                  <div style={{ color: TEXT }} className="text-xs font-semibold">{inr(fiiDii.dii_sell ?? 0)}</div>
                </div>
              </div>
            ) : (
              <div style={{ color: MUTED }} className="text-[10px]">Loading FII/DII...</div>
            )}
          </section>

          {/* Section B: Top Gainers/Losers */}
          <section style={{ background: PANEL, border: `1px solid ${BORDER}`, borderRadius: 6 }} className="overflow-hidden">
            <div className="flex border-b" style={{ borderColor: BORDER }}>
              {(["gainers", "losers"] as const).map((t) => (
                <button
                  key={t}
                  onClick={() => setTab(t)}
                  className="px-3 py-1.5 text-[11px] font-medium"
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
            <table className="w-full text-[11px]">
              <thead>
                <tr style={{ color: MUTED }}>
                  <th className="text-left py-1.5 px-2 font-medium">SYMBOL</th>
                  <th className="text-left py-1.5 px-2 font-medium">SPARK</th>
                  <th className="text-right py-1.5 px-2 font-medium">LTP</th>
                  <th className="text-right py-1.5 px-2 font-medium">CHANGE %</th>
                </tr>
              </thead>
              <tbody>
                {displayRows.map((q: any, idx: number) => {
                  const up = (q.change_percent ?? 0) >= 0;
                  const sparkVals = [q.last ?? 0, (q.last ?? 0) * (1 + (q.change_percent ?? 0) / 100)];
                  return (
                    <tr key={q.token ?? idx} className="hover:bg-[#2a2e39] transition-colors" style={{ borderTop: `1px solid ${BORDER}` }}>
                      <td className="py-2 px-2 text-left font-medium" style={{ color: TEXT }}>{q.symbol ?? q.token}</td>
                      <td className="py-2 px-2">
                        <svg viewBox="0 0 40 16" className="w-10 h-4">
                          <polyline fill="none" stroke={up ? UP : DOWN} strokeWidth="1" points={sparkPoints(sparkVals)} />
                        </svg>
                      </td>
                      <td className="py-2 px-2 text-right" style={{ color: TEXT }}>{num(q.last)}</td>
                      <td className="py-2 px-2 text-right" style={{ color: up ? UP : DOWN }}>{percent(q.change_percent)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {loading && !breadth ? <Loading label="Scanning the market" /> : null}
          </section>

          {/* Section C: Quick Option Chain */}
          <section style={{ background: PANEL, border: `1px solid ${BORDER}`, borderRadius: 6 }} className="p-2.5">
            <div className="flex items-center justify-between mb-2">
              <h2 className="text-xs font-semibold" style={{ color: TEXT }}>Quick Option Chain</h2>
              <span className="text-[10px]" style={{ color: MUTED }}>ATM Strike: {atm ?? "—"}</span>
            </div>
            <div className="flex items-center gap-2 mb-2">
              {chain ? (
                <>
                  <div className="flex-1 rounded p-2 text-center" style={{ background: BG, border: `1px solid ${BORDER}` }}>
                    <div style={{ color: MUTED }} className="text-[10px]">ATM CE</div>
                    <div style={{ color: UP }} className="text-sm font-semibold">{num(ceLtp)}</div>
                  </div>
                  <div className="text-[10px] font-medium" style={{ color: MUTED }}>ATM</div>
                  <div className="flex-1 rounded p-2 text-center" style={{ background: BG, border: `1px solid ${BORDER}` }}>
                    <div style={{ color: MUTED }} className="text-[10px]">ATM PE</div>
                    <div style={{ color: DOWN }} className="text-sm font-semibold">{num(peLtp)}</div>
                  </div>
                </>
              ) : (
                <div className="flex-1 text-center text-[10px]" style={{ color: MUTED }}>Loading option chain...</div>
              )}
            </div>
            <button
              onClick={() => router.push("/options")}
              className="w-full py-1.5 rounded-md text-xs font-semibold text-white"
              style={{ background: BLUE }}
            >
              Open Full Chain
            </button>
          </section>

          {/* Section D: Trending Sectors */}
          <section style={{ background: PANEL, border: `1px solid ${BORDER}`, borderRadius: 6 }} className="p-2.5">
            <h2 className="text-xs font-semibold mb-2" style={{ color: TEXT }}>Trending Sectors</h2>
            <div className="grid grid-cols-2 gap-1.5">
              {sectors.map((s) => {
                const up = s.chg >= 0;
                return (
                  <div key={s.name} className="rounded p-2" style={{ background: BG, border: `1px solid ${BORDER}` }}>
                    <div style={{ color: MUTED }} className="text-[10px]">{s.name}</div>
                    <div style={{ color: up ? UP : DOWN }} className="text-xs font-semibold">{percent(s.chg)}</div>
                  </div>
                );
              })}
            </div>
          </section>

          {error ? (
            <div className="fixed bottom-3 left-1/2 -translate-x-1/2 px-3 py-1.5 rounded-md text-[10px]" style={{ background: PANEL, border: `1px solid ${BORDER}`, color: MUTED }}>
              Connecting to live feed...
            </div>
          ) : null}
        </div>
      </div>
    </AppShell>
  );
}

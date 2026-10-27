"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { BreadthSnapshot, MarketStatus, Quote } from "@/lib/api";
import { AppShell } from "@/components/app-shell";
import { Empty, Loading, Panel } from "@/components/ui";
import { clock, compact, inr, num, percent, toneClass } from "@/lib/format";
import { quoteKey, useLiveFeed } from "@/lib/useLiveFeed";
import { useRouter } from "next/navigation";

const INDEX_TOKENS = [
  { token: "26000", segment: "nse_cm", label: "NIFTY 50" },
  { token: "26009", segment: "nse_cm", label: "BANKNIFTY" },
  { token: "26037", segment: "nse_cm", label: "FINNIFTY" },
  { token: "99919000", segment: "bse_cm", label: "SENSEX" },
];

const VERCEL = {
  bg: "#000000",
  card: "#0A0A0A",
  border: "#333333",
  text: "#EDEDED",
  muted: "#888888",
  up: "#17C964",
  down: "#F31260",
};

function sparkPoints(values: number[]) {
  if (values.length < 2) return "";
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;
  return values
    .map((v, i) => `${(i / (values.length - 1)) * 100},${100 - ((v - min) / range) * 100}`)
    .join(" ");
}

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
        setSpark((s) => {
          const arr = [...(s[e.token] || []), q.last as number].slice(-20);
          return { ...s, [e.token]: arr };
        });
      }
    });
  }, [feed.quotes]);

  const advances = breadth?.breadth?.advances ?? 0;
  const declines = breadth?.breadth?.declines ?? 0;
  const priced = breadth?.breadth?.priced ?? breadth?.priced ?? 0;

  return (
    <AppShell>
      <div style={{ background: VERCEL.bg, color: VERCEL.text, minHeight: "100vh", padding: 16, fontFamily: "Inter, system-ui, sans-serif" }}>
        {error ? (
          <div style={{ background: "#1a1a1a", border: `1px solid ${VERCEL.border}`, padding: "8px 12px", borderRadius: 6, marginBottom: 12, color: VERCEL.muted, fontSize: 12 }}>
            Connecting to live feed...
          </div>
        ) : null}

        {/* Top row indices */}
        <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 12, marginBottom: 12 }}>
          {INDEX_TOKENS.map((e) => {
            const q = feed.quotes[quoteKey(e.token, e.segment)];
            const ch = q?.change_percent ?? 0;
            const up = ch >= 0;
            return (
              <div key={e.token} style={{ background: VERCEL.card, border: `1px solid ${VERCEL.border}`, borderRadius: 8, padding: 12 }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <span style={{ color: VERCEL.muted, fontSize: 11, textTransform: "uppercase", letterSpacing: 1 }}>{e.label}</span>
                  <span style={{ color: up ? VERCEL.up : VERCEL.down, fontSize: 11, fontWeight: 600 }}>{percent(ch)}</span>
                </div>
                <div style={{ fontSize: 20, fontWeight: 600, marginTop: 4, color: VERCEL.text, transition: "color 0.3s" }}>{num(q?.last)}</div>
                <svg viewBox="0 0 100 100" preserveAspectRatio="none" style={{ width: "100%", height: 24, marginTop: 6 }}>
                  <polyline fill="none" stroke={up ? VERCEL.up : VERCEL.down} strokeWidth="1.5" points={sparkPoints(spark[e.token] || [])} />
                </svg>
              </div>
            );
          })}
        </div>

        {/* Middle row */}
        <div style={{ display: "grid", gridTemplateColumns: "60% 40%", gap: 12, marginBottom: 12 }}>
          <div style={{ background: VERCEL.card, border: `1px solid ${VERCEL.border}`, borderRadius: 8, padding: 12 }}>
            <div style={{ color: VERCEL.muted, fontSize: 11, textTransform: "uppercase", letterSpacing: 1, marginBottom: 8 }}>Market Breadth & Institutional Activity</div>
            <div style={{ display: "flex", height: 10, borderRadius: 4, overflow: "hidden", marginBottom: 8 }}>
              <div style={{ width: `${priced ? (advances / priced) * 100 : 0}%`, background: VERCEL.up }} />
              <div style={{ flex: 1, background: VERCEL.down }} />
            </div>
            <div style={{ display: "flex", justifyContent: "space-between", fontSize: 11, color: VERCEL.muted, marginBottom: 12 }}>
              <span style={{ color: VERCEL.up }}>{num(advances)} Adv</span>
              <span style={{ color: VERCEL.down }}>{num(declines)} Dec</span>
            </div>
            {fiiDii ? (
              <div style={{ fontSize: 12 }}>
                <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
                  <span style={{ color: VERCEL.muted }}>FII Net</span>
                  <span style={{ color: (fiiDii.fii_net ?? 0) >= 0 ? VERCEL.up : VERCEL.down }}>{inr(fiiDii.fii_net ?? 0)}</span>
                </div>
                <div style={{ display: "flex", justifyContent: "space-between" }}>
                  <span style={{ color: VERCEL.muted }}>DII Net</span>
                  <span style={{ color: (fiiDii.dii_net ?? 0) >= 0 ? VERCEL.up : VERCEL.down }}>{inr(fiiDii.dii_net ?? 0)}</span>
                </div>
              </div>
            ) : (
              <div style={{ color: VERCEL.muted, fontSize: 11 }}>Loading FII/DII...</div>
            )}
          </div>
          <div style={{ background: VERCEL.card, border: `1px solid ${VERCEL.border}`, borderRadius: 8, padding: 12 }}>
            <div style={{ color: VERCEL.muted, fontSize: 11, textTransform: "uppercase", letterSpacing: 1, marginBottom: 8 }}>Quick Option Chain</div>
            <div style={{ fontSize: 12, color: VERCEL.muted, marginBottom: 12 }}>ATM CE / PE snapshot for NIFTY 50</div>
            <button
              onClick={() => router.push("/options")}
              style={{ background: VERCEL.up, color: "#000", border: "none", borderRadius: 6, padding: "6px 12px", fontSize: 12, fontWeight: 600, cursor: "pointer" }}
            >
              Open Full Chain
            </button>
          </div>
        </div>

        {/* Bottom row */}
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
          <div style={{ background: VERCEL.card, border: `1px solid ${VERCEL.border}`, borderRadius: 8, padding: 12 }}>
            <div style={{ color: VERCEL.muted, fontSize: 11, textTransform: "uppercase", letterSpacing: 1, marginBottom: 8 }}>Top Gainers</div>
            <table style={{ width: "100%", fontSize: 12, borderCollapse: "collapse" }}>
              <thead>
                <tr style={{ color: VERCEL.muted }}>
                  <th style={{ textAlign: "left", padding: "4px 0" }}>Symbol</th>
                  <th style={{ textAlign: "right" }}>LTP</th>
                  <th style={{ textAlign: "right" }}>Change %</th>
                </tr>
              </thead>
              <tbody>
                {(breadth?.top_gainers ?? []).slice(0, 8).map((q: Quote) => (
                  <tr key={quoteKey(q.token, q.exchange_segment)} style={{ borderTop: `1px solid ${VERCEL.border}` }}>
                    <td style={{ padding: "4px 0", color: VERCEL.text }}>{q.symbol ?? q.token}</td>
                    <td style={{ textAlign: "right", color: VERCEL.text }}>{num(q.last)}</td>
                    <td style={{ textAlign: "right", color: VERCEL.up }}>{percent(q.change_percent)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div style={{ background: VERCEL.card, border: `1px solid ${VERCEL.border}`, borderRadius: 8, padding: 12 }}>
            <div style={{ color: VERCEL.muted, fontSize: 11, textTransform: "uppercase", letterSpacing: 1, marginBottom: 8 }}>Top Losers</div>
            <table style={{ width: "100%", fontSize: 12, borderCollapse: "collapse" }}>
              <thead>
                <tr style={{ color: VERCEL.muted }}>
                  <th style={{ textAlign: "left", padding: "4px 0" }}>Symbol</th>
                  <th style={{ textAlign: "right" }}>LTP</th>
                  <th style={{ textAlign: "right" }}>Change %</th>
                </tr>
              </thead>
              <tbody>
                {(breadth?.top_losers ?? []).slice(0, 8).map((q: Quote) => (
                  <tr key={quoteKey(q.token, q.exchange_segment)} style={{ borderTop: `1px solid ${VERCEL.border}` }}>
                    <td style={{ padding: "4px 0", color: VERCEL.text }}>{q.symbol ?? q.token}</td>
                    <td style={{ textAlign: "right", color: VERCEL.text }}>{num(q.last)}</td>
                    <td style={{ textAlign: "right", color: VERCEL.down }}>{percent(q.change_percent)}</td>
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

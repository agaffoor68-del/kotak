"use client";

/** Trading desk overview: indices, breadth, movers, risk posture. */

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { MarketStatus, Quote, RiskStatus } from "@/lib/api";
import { AppShell } from "@/components/app-shell";
import { Empty, ErrorNote, Loading, Metric, Panel, StatusChip } from "@/components/ui";
import { SharePanel } from "@/components/share-panel";
import { compact, compactInt, inr, num, percent, relative, toneClass } from "@/lib/format";
import { quoteKey, snapshot, useLiveFeed } from "@/lib/useLiveFeed";

interface Breadth {
  universe_size: number;
  priced: number;
  breadth: {
    priced?: number;
    advances?: number;
    declines?: number;
    unchanged?: number;
    limit_up?: number;
    limit_down?: number;
    advance_decline_ratio?: number | null;
    volume_ratio?: number | null;
  };
  top_gainers: Quote[];
  top_losers: Quote[];
  most_active: Quote[];
}

const INDEX_TOKENS = [
  { token: "26000", segment: "nse_cm", label: "NIFTY 50" },
  { token: "26009", segment: "nse_cm", label: "NIFTY BANK" },
  { token: "26037", segment: "nse_cm", label: "FIN NIFTY" },
  { token: "26074", segment: "nse_cm", label: "MIDCAP NIFTY" },
  { token: "265", segment: "bse_cm", label: "SENSEX" },
];

function Overview() {
  const feed = useLiveFeed();
  const [status, setStatus] = useState<MarketStatus | null>(null);
  const [breadth, setBreadth] = useState<Breadth | null>(null);
  const [risk, setRisk] = useState<RiskStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [market, riskStatus] = await Promise.all([
        api.status(),
        api.risk().catch(() => null),
      ]);
      setStatus(market);
      setRisk(riskStatus);
      setError(null);

      // Stream the indices live.
      feed.subscribe(INDEX_TOKENS.map((entry) => ({ instrument_token: entry.token, exchange_segment: entry.segment })));
      // Breadth is a heavier, all-market scan: fetch it once, then let it refresh.
      setBreadth(await api.breadth(200));
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Could not load the desk");
    } finally {
      setLoading(false);
    }
    // `feed` is stable enough for this effect; re-subscribing on every render
    // would be wasteful.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    void load();
    const timer = setInterval(() => void load(), 60_000);
    return () => clearInterval(timer);
  }, [load]);

  const breadthStats = breadth?.breadth;
  // The API types breadth figures as nullable, so normalise once for rendering.
  const advances = breadthStats?.advances ?? 0;
  const declines = breadthStats?.declines ?? 0;
  const unchanged = breadthStats?.unchanged ?? 0;
  const priced = breadthStats?.priced ?? breadth?.priced ?? 0;

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold tracking-tight">Market overview</h1>
          <p className="text-2xs text-ink-faint">
            Live from Kotak Neo · {status ? `updated ${relative(status.session ? Date.now() / 1000 : null)}` : "connecting"}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {status ? (
            <span className="chip chip-idle">
              {num(breadth?.priced ?? 0)} / {num(breadth?.universe_size ?? 0)} priced
            </span>
          ) : null}
          {status?.broker_session?.authenticated ? (
            <StatusChip status="live" label="Kotak session live" />
          ) : (
            <StatusChip status="warn" label="Kotak offline" />
          )}
          <button className="btn btn-sm" onClick={() => void load()} type="button">
            Refresh
          </button>
        </div>
      </header>

      {error ? <ErrorNote error={error} onRetry={() => void load()} /> : null}

      {/* ---------------- indices ---------------- */}
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5">
        {INDEX_TOKENS.map((entry) => {
          const live = feed.quotes[quoteKey(entry.token, entry.segment)];
          return (
            <Metric
              key={entry.token}
              label={entry.label}
              value={live?.last !== null && live?.last !== undefined ? num(live.last) : "—"}
              sub={
                live?.change_percent !== null && live?.change_percent !== undefined ? (
                  <span className={toneClass(live.change_percent)}>
                    {percent(live.change_percent)} · {num(live.change)}
                  </span>
                ) : (
                  "awaiting tick"
                )
              }
              tone={
                live?.change_percent === null || live?.change_percent === undefined
                  ? "flat"
                  : live.change_percent > 0
                    ? "up"
                    : live.change_percent < 0
                      ? "down"
                      : "flat"
              }
            />
          );
        })}
      </div>

      {/* ---------------- breadth ---------------- */}
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Metric label="Advances" value={num(advances)} tone="up" sub={`of ${num(priced)} priced`} />
        <Metric label="Declines" value={num(declines)} tone="down" />
        <Metric
          label="A / D ratio"
          value={num(breadthStats?.advance_decline_ratio ?? 0)}
          sub={breadthStats?.volume_ratio ? `volume ratio ${num(breadthStats.volume_ratio)}` : undefined}
        />
        <Metric
          label="Circuit extremes"
          value={`${num(breadthStats?.limit_up ?? 0)} / ${num(breadthStats?.limit_down ?? 0)}`}
          sub="upper / lower"
        />
      </div>

      {/* ---------------- breadth bar ---------------- */}
      {priced > 0 ? (
        <Panel title="Market breadth" subtitle={`${num(priced)} instruments with a live price`}>
          <div className="flex h-7 overflow-hidden rounded border border-hairline">
            <div
              className="bg-up/70"
              style={{ width: `${(advances / priced) * 100}%` }}
              title={`${advances} advancing`}
            />
            <div className="bg-elevated" style={{ width: `${(unchanged / priced) * 100}%` }} />
            <div
              className="bg-down/70"
              style={{ width: `${(declines / priced) * 100}%` }}
              title={`${declines} declining`}
            />
          </div>
          <div className="mt-2 flex flex-wrap justify-between text-3xs text-ink-faint">
            <span className="text-up">{num(advances)} advancing</span>
            <span>{num(unchanged)} unchanged</span>
            <span className="text-down">{num(declines)} declining</span>
          </div>
        </Panel>
      ) : null}

      {/* ---------------- movers ---------------- */}
      <div className="grid gap-4 lg:grid-cols-3">
        <MoverList
          title="Top gainers"
          quotes={breadth?.top_gainers ?? []}
          emptyText="No advancing instruments in the scanned universe."
        />
        <MoverList
          title="Top losers"
          quotes={breadth?.top_losers ?? []}
          emptyText="No declining instruments in the scanned universe."
        />
        <Panel title="Most active" subtitle="By traded volume">
          {breadth?.most_active.length ? (
            <table className="tbl">
              <thead>
                <tr>
                  <th>Instrument</th>
                  <th>Last</th>
                  <th>Volume</th>
                </tr>
              </thead>
              <tbody>
                {breadth.most_active.map((quote) => (
                  <tr key={quoteKey(quote.token, quote.exchange_segment)}>
                    <td className="num text-ink-dim">{quote.token}</td>
                    <td className="num">{num(quote.last)}</td>
                    <td className="num">{compactInt(quote.volume)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <p className="py-6 text-center text-2xs text-ink-faint">No volume data yet.</p>
          )}
        </Panel>
      </div>

      {/* ---------------- risk + system ---------------- */}
      <div className="grid gap-4 lg:grid-cols-2">
        <Panel
          title="Risk posture"
          subtitle="Every order clears these controls before reaching Kotak"
          actions={
            risk?.trading_halted ? <StatusChip status="error" label="Trading halted" /> : <StatusChip status="live" label="Trading allowed" />
          }
        >
          {risk ? (
            <div className="grid gap-3 sm:grid-cols-2">
              <Metric label="Equity" value={inr(risk.equity)} />
              <Metric label="Peak equity" value={inr(risk.peak_equity)} />
              <Metric
                label="Drawdown"
                value={inr(risk.current_drawdown)}
                sub={`${num(risk.drawdown_percent)}% from peak`}
                tone={risk.current_drawdown > 0 ? "down" : "flat"}
              />
              <Metric
                label="Daily loss left"
                value={risk.daily_loss_remaining === null ? "unlimited" : inr(risk.daily_loss_remaining)}
                sub={`realised today ${inr(risk.realised_today)}`}
                tone={risk.daily_limit_breached ? "down" : "flat"}
              />
              <Metric label="Open positions" value={num(risk.open_positions)} />
              <Metric label="Orders / minute" value={num(risk.orders_last_minute)} />
              {risk.halt_reason ? (
                <p className="sm:col-span-2 rounded border border-down/40 bg-down-soft px-3 py-2 text-2xs text-down">
                  {risk.halt_reason}
                </p>
              ) : null}
            </div>
          ) : (
            <Loading label="Loading risk" />
          )}
        </Panel>

        <Panel title="System" subtitle="Broker, feed and data provenance">
          <dl className="space-y-2.5 text-2xs">
            <Row
              label="Kotak Neo session"
              value={status?.broker_session?.status ?? "—"}
              tone={status?.broker_session?.authenticated ? "up" : "down"}
            />
            <Row label="WebSocket feed" value={status?.feed?.status ?? "—"} tone={feed.state === "live" ? "up" : "warn"} />
            <Row label="Ticks received" value={compact(feed.frames)} />
            <Row label="Reconnects" value={compact(status?.feed?.reconnects ?? 0)} />
            <Row
              label="Instruments in master"
              value={compact(status?.scrip_master?.instruments ?? 0)}
              sub={status?.scrip_master?.last_error ?? undefined}
            />
            <Row label="Tick recording" value={status?.record_ticks ? "enabled" : "disabled"} tone={status?.record_ticks ? "up" : "warn"} />
          </dl>
          {status?.broker_session?.last_error ? (
            <p className="mt-3 rounded border border-warn/40 bg-warn-soft px-3 py-2 text-2xs text-warn">
              {status.broker_session.last_error}
            </p>
          ) : null}
        </Panel>
      </div>

      {loading && !breadth ? <Loading label="Scanning the market" /> : null}

      <SharePanel />
    </div>
  );
}

function Row({ label, value, tone, sub }: { label: string; value: string; tone?: "up" | "down" | "warn"; sub?: string }) {
  return (
    <div className="flex items-start justify-between gap-3 border-b border-hairline/60 pb-2 last:border-0">
      <dt className="text-ink-faint">{label}</dt>
      <dd className={`text-right ${tone === "up" ? "text-up" : tone === "down" ? "text-down" : tone === "warn" ? "text-warn" : "text-ink"}`}>
        {value}
        {sub ? <p className="mt-0.5 max-w-xs text-3xs text-ink-faint">{sub}</p> : null}
      </dd>
    </div>
  );
}

function MoverList({ title, quotes, emptyText }: { title: string; quotes: Quote[]; emptyText: string }) {
  return (
    <Panel title={title}>
      {quotes.length ? (
        <table className="tbl">
          <thead>
            <tr>
              <th>Token</th>
              <th>Last</th>
              <th>Change</th>
            </tr>
          </thead>
          <tbody>
            {quotes.map((quote) => (
              <tr key={quoteKey(quote.token, quote.exchange_segment)}>
                <td className="num text-ink-dim">{quote.token}</td>
                <td className="num">{num(quote.last)}</td>
                <td className={`num ${toneClass(quote.change_percent)}`}>{percent(quote.change_percent)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <Empty title="Nothing to show" message={emptyText} />
      )}
    </Panel>
  );
}

export default function DashboardPage() {
  return (
    <AppShell>
      <Overview />
    </AppShell>
  );
}

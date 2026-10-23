"use client";

/**
 * Trading desk overview: indices, breadth, movers, risk posture.
 *
 * The layout is the desk's front page: the session and the index strip at the
 * top, the breadth gauge in the middle and the movers, risk and system panels
 * below. Every number comes from Angel One SmartAPI — the feed for the
 * indices, the background breadth snapshot for the market-wide figures.
 */

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { BreadthSnapshot, MarketStatus, Quote, RiskStatus } from "@/lib/api";
import { AppShell } from "@/components/app-shell";
import { Empty, ErrorNote, Loading, Metric, Panel, StatusChip } from "@/components/ui";
import { SharePanel } from "@/components/share-panel";
import { AiAdvisor } from "@/components/ai-advisor";
import { clock, compact, compactInt, inr, num, percent, relative, toneClass } from "@/lib/format";
import { quoteKey, useLiveFeed } from "@/lib/useLiveFeed";

const INDEX_TOKENS = [
  { token: "26000", segment: "nse_cm", label: "NIFTY 50" },
  { token: "26009", segment: "nse_cm", label: "NIFTY BANK" },
  { token: "26037", segment: "nse_cm", label: "FIN NIFTY" },
  { token: "26074", segment: "nse_cm", label: "MIDCAP NIFTY" },
  { token: "99919000", segment: "bse_cm", label: "SENSEX" },
];

function Overview() {
  const feed = useLiveFeed();
  const [status, setStatus] = useState<MarketStatus | null>(null);
  const [breadth, setBreadth] = useState<BreadthSnapshot | null>(null);
  const [risk, setRisk] = useState<RiskStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [now, setNow] = useState(() => Date.now());

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

  // The desk clock is IST wall-clock time and ticks every second.
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  const breadthStats = breadth?.breadth;
  // The API types breadth figures as nullable, so normalise once for rendering.
  const advances = breadthStats?.advances ?? 0;
  const declines = breadthStats?.declines ?? 0;
  const unchanged = breadthStats?.unchanged ?? 0;
  const priced = breadthStats?.priced ?? breadth?.priced ?? 0;
  const session = status?.session;

  return (
    <div className="space-y-4">
      {/* ---------------- desk header ---------------- */}
      <header className="panel relative overflow-hidden">
        <div className="pointer-events-none absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-accent/70 to-transparent" />
        <div className="pointer-events-none absolute -right-24 -top-24 h-56 w-56 rounded-full bg-accent/10 blur-3xl" />
        <div className="relative flex flex-wrap items-end justify-between gap-4 px-5 py-4">
          <div className="min-w-0">
            <p className="label-caps">AlphaTradePro · Market desk</p>
            <div className="mt-1.5 flex flex-wrap items-baseline gap-3">
              <h1 className="num text-2xl font-semibold tracking-tight text-ink">
                {clock(now)}
                <span className="ml-1.5 text-xs font-sans font-normal text-ink-faint">IST</span>
              </h1>
              {session ? (
                <span className="text-2xs text-ink-faint">
                  {session.weekday}, {session.date} · {session.reason}
                </span>
              ) : null}
            </div>
            <p className="mt-1 text-2xs text-ink-faint">
              Live from Angel One SmartAPI
              {status ? ` · status ${relative(status.session ? now / 1000 : null)}` : " · connecting"}
            </p>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            {status ? (
              <span className="chip chip-idle">
                {num(breadth?.priced ?? 0)} / {num(breadth?.universe_size ?? 0)} priced
              </span>
            ) : null}
            {status?.feed?.status === "connected" ? (
              <StatusChip status="live" label="Angel data feed live" />
            ) : (
              <StatusChip status="warn" label="Angel feed offline" />
            )}
            <button className="btn btn-sm btn-primary" onClick={() => void load()} type="button">
              Refresh
            </button>
          </div>
        </div>
      </header>

      {error ? <ErrorNote error={error} onRetry={() => void load()} /> : null}

      <AiAdvisor />

      {/* ---------------- indices ---------------- */}
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5">
        {INDEX_TOKENS.map((entry) => {
          const live = feed.quotes[quoteKey(entry.token, entry.segment)];
          return <IndexTile key={entry.token} label={entry.label} quote={live} />;
        })}
      </div>

      {/* ---------------- breadth ---------------- */}
      <div className="grid gap-4 lg:grid-cols-3">
        <Panel
          title="Breadth"
          subtitle={`${num(priced)} instruments with a live price`}
          className="lg:col-span-1"
        >
          <div className="flex items-center gap-4">
            <BreadthRing advances={advances} declines={declines} unchanged={unchanged} />
            <dl className="min-w-0 flex-1 space-y-2.5 text-2xs">
              <LegendRow label="Advancing" value={advances} tone="text-up" bar="bg-up" share={priced ? advances / priced : 0} />
              <LegendRow label="Unchanged" value={unchanged} tone="text-ink-dim" bar="bg-elevated" share={priced ? unchanged / priced : 0} />
              <LegendRow label="Declining" value={declines} tone="text-down" bar="bg-down" share={priced ? declines / priced : 0} />
            </dl>
          </div>
        </Panel>

        <Panel title="Market breadth" subtitle="Where the session closed" className="lg:col-span-2">
          {priced > 0 ? (
            <>
              <div className="flex h-8 overflow-hidden rounded border border-hairline">
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
            </>
          ) : (
            <Empty
              title="No breadth yet"
              message="The scan needs a live Angel One session and an instrument master. The last known snapshot is kept until then."
            />
          )}

          <div className="mt-4 grid gap-3 sm:grid-cols-3">
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
            <Metric
              label="Universe"
              value={num(breadth?.universe_size ?? 0)}
              sub={`${num(priced)} priced`}
            />
          </div>
        </Panel>
      </div>

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
                    <td className="text-ink-dim" title={quote.name ?? undefined}>
                      {quote.symbol ?? quote.token}
                    </td>
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
          subtitle="Every order clears these controls before reaching the broker"
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

        <Panel title="System" subtitle="Data source, feed and provenance">
          <dl className="space-y-2.5 text-2xs">
            <Row label="Market data" value={status?.data_source === "angel_one" ? "Angel One SmartAPI" : status?.data_source ?? "—"} tone="up" />
            <Row label="Live feed" value={status?.feed?.status ?? "—"} tone={feed.state === "live" ? "up" : "warn"} />
            <Row label="Ticks received" value={compact(feed.frames)} />
            <Row label="Reconnects" value={compact(status?.feed?.reconnects ?? 0)} />
            <Row
              label="Instruments in master"
              value={compact(status?.scrip_master?.instruments ?? 0)}
              sub={status?.scrip_master?.last_error ?? undefined}
            />
            <Row label="Tick recording" value={status?.record_ticks ? "enabled" : "disabled"} tone={status?.record_ticks ? "up" : "warn"} />
          </dl>
          {status?.angel_instruments?.last_error ? (
            <p className="mt-3 rounded border border-warn/40 bg-warn-soft px-3 py-2 text-2xs text-warn">
              {status.angel_instruments.last_error}
            </p>
          ) : null}
          {status?.feed?.last_error ? (
            <p className="mt-3 rounded border border-warn/40 bg-warn-soft px-3 py-2 text-2xs text-warn">
              {status.feed.last_error}
            </p>
          ) : null}
        </Panel>
      </div>

      {loading && !breadth ? <Loading label="Scanning the market" /> : null}

      <SharePanel />
    </div>
  );
}

/** One index: the price, the day's move and where it sits in the day's range. */
function IndexTile({ label, quote }: { label: string; quote: Quote | undefined }) {
  const last = quote?.last ?? null;
  const change = quote?.change_percent ?? null;
  const open = quote?.open ?? null;
  const high = quote?.high ?? null;
  const low = quote?.low ?? null;
  const hasRange = high !== null && low !== null;
  const span = hasRange ? high - low : 0;
  const marker = span > 0 && last !== null && hasRange ? ((last - low) / span) * 100 : null;
  const openMarker = span > 0 && open !== null && hasRange ? ((open - low) / span) * 100 : null;
  const tone = change === null ? "flat" : change > 0 ? "up" : change < 0 ? "down" : "flat";

  return (
    <div className="panel px-4 py-3 transition-colors duration-150 hover:border-edge">
      <div className="flex items-center justify-between gap-2">
        <p className="label-caps truncate">{label}</p>
        <span
          className={`num rounded px-1.5 py-0.5 text-3xs font-semibold ${
            tone === "up" ? "bg-up-soft text-up" : tone === "down" ? "bg-down-soft text-down" : "bg-elevated text-ink-dim"
          }`}
        >
          {change === null ? "—" : percent(change)}
        </span>
      </div>

      <p className="num mt-2 text-xl font-semibold text-ink">{last === null ? "—" : num(last)}</p>
      <p className={`num mt-0.5 text-3xs ${toneClass(change)}`}>
        {quote?.change === null || quote?.change === undefined ? "awaiting tick" : `${num(quote.change)} today`}
      </p>

      <div className="relative mt-3 h-1.5 rounded-full bg-elevated">
        {marker !== null ? (
          <div
            className={`absolute top-1/2 h-2.5 w-2.5 -translate-x-1/2 -translate-y-1/2 rounded-full border border-canvas ${
              tone === "up" ? "bg-up" : tone === "down" ? "bg-down" : "bg-ink-faint"
            }`}
            style={{ left: `${Math.min(100, Math.max(0, marker))}%` }}
          />
        ) : null}
        {openMarker !== null ? (
          <div
            className="absolute top-1/2 h-2 w-px -translate-y-1/2 bg-ink-faint/70"
            style={{ left: `${Math.min(100, Math.max(0, openMarker))}%` }}
          />
        ) : null}
      </div>
      <div className="mt-1 flex justify-between text-3xs text-ink-faint">
        <span className="num">{low === null ? "—" : num(low)}</span>
        <span className="num">{high === null ? "—" : num(high)}</span>
      </div>
    </div>
  );
}

/** The advance/decline split as a ring. */
function BreadthRing({
  advances,
  declines,
  unchanged,
}: {
  advances: number;
  declines: number;
  unchanged: number;
}) {
  const total = advances + declines + unchanged;
  const radius = 54;
  const circumference = 2 * Math.PI * radius;
  const advanceShare = total > 0 ? (advances / total) * 100 : 0;
  const declineShare = total > 0 ? (declines / total) * 100 : 0;
  const ratio = declines > 0 ? advances / declines : advances > 0 ? advances : 0;

  return (
    <div className="relative h-36 w-36 shrink-0">
      <svg viewBox="0 0 140 140" className="h-36 w-36 -rotate-0">
        <circle cx="70" cy="70" r={radius} fill="none" stroke="#151C28" strokeWidth="14" />
        <circle
          cx="70"
          cy="70"
          r={radius}
          fill="none"
          stroke="#22C55E"
          strokeWidth="14"
          strokeLinecap="round"
          strokeDasharray={`${(advanceShare / 100) * circumference} ${circumference}`}
          transform="rotate(-90 70 70)"
        />
        <circle
          cx="70"
          cy="70"
          r={radius}
          fill="none"
          stroke="#EF4444"
          strokeWidth="14"
          strokeLinecap="round"
          strokeDasharray={`${(declineShare / 100) * circumference} ${circumference}`}
          strokeDashoffset={-(advanceShare / 100) * circumference}
          transform="rotate(-90 70 70)"
        />
      </svg>
      <div className="absolute inset-0 flex flex-col items-center justify-center">
        <span className="num text-lg font-semibold text-ink">{num(ratio)}</span>
        <span className="text-3xs uppercase tracking-[0.12em] text-ink-faint">A / D</span>
      </div>
    </div>
  );
}

function LegendRow({
  label,
  value,
  tone,
  bar,
  share,
}: {
  label: string;
  value: number;
  tone: string;
  bar: string;
  share: number;
}) {
  return (
    <div>
      <div className="flex items-center justify-between gap-2">
        <dt className="text-ink-faint">{label}</dt>
        <dd className={`num font-semibold ${tone}`}>{num(value)}</dd>
      </div>
      <div className="mt-1 h-1 overflow-hidden rounded-full bg-elevated">
        <div className={`h-full ${bar}`} style={{ width: `${Math.min(100, Math.max(0, share * 100))}%` }} />
      </div>
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
              <th>Symbol</th>
              <th>Last</th>
              <th>Change</th>
            </tr>
          </thead>
          <tbody>
            {quotes.map((quote) => (
              <tr key={quoteKey(quote.token, quote.exchange_segment)}>
                <td className="text-ink-dim" title={quote.name ?? undefined}>
                  {quote.symbol ?? quote.token}
                </td>
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

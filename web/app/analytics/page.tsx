"use client";

/** Analytics: P&L, risk metrics, strategy breakdown and a month/weekday heatmap. */

import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import type { PerformancePayload, PnlBucket, PerformanceSummary, StrategyRow } from "@/lib/api";
import { AppShell } from "@/components/app-shell";
import { PriceChart } from "@/components/price-chart";
import { Empty, ErrorNote, Loading, Metric, Panel } from "@/components/ui";
import { inr, num, percent, signedInr, toneClass } from "@/lib/format";

interface AnalyticsView extends PerformancePayload {
  /** Present only on the heatmap response, not on the main performance call. */
  grid?: Record<string, Record<string, number>>;
}

function Analytics() {
  const [data, setData] = useState<AnalyticsView | null>(null);
  const [byStrategy, setByStrategy] = useState<StrategyRow[]>([]);
  const [heatmap, setHeatmap] = useState<{ grid: Record<string, Record<string, number>> } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [view, setView] = useState<"daily" | "monthly">("daily");

  const load = useCallback(async () => {
    try {
      const [performance, strategies, grid] = await Promise.all([
        api.performance(),
        api.strategyAnalytics().catch(() => [] as StrategyRow[]),
        api.performanceHeatmap(180).catch(() => ({ grid: {}, weekdays: [], days: 180 })),
      ]);
      setData(performance);
      setByStrategy(strategies);
      setHeatmap(grid);
      setError(null);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Could not load analytics");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const summary: PerformanceSummary | undefined = data?.summary;
  const buckets = view === "daily" ? data?.daily ?? [] : data?.monthly ?? [];

  // Drawdown series as a chart, offset so the area is visible.
  const drawdownBars = useMemo(
    () =>
      (data?.drawdown_curve ?? []).map((value, index) => ({
        time: index,
        open: 0,
        high: value,
        low: value,
        close: value,
        volume: 0,
        trades: 0,
      })),
    [data],
  );

  if (loading && !data) return <Loading label="Crunching analytics" />;

  if (!data?.has_data) {
    return (
      <div className="space-y-4">
        <header>
          <h1 className="text-lg font-semibold tracking-tight">Analytics</h1>
        </header>
        {error ? <ErrorNote error={error} onRetry={() => void load()} /> : null}
        <Empty
          title="No performance data yet"
          message={data?.note ?? "Metrics appear once trades are closed. Run some paper trades, or trade live."}
        />
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold tracking-tight">Analytics</h1>
          <p className="text-2xs text-ink-faint">Computed from your own closed trades, net of charges</p>
        </div>
        <button className="btn btn-sm" onClick={() => void load()} type="button">
          Refresh
        </button>
      </header>

      {error ? <ErrorNote error={error} onRetry={() => void load()} /> : null}

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4 xl:grid-cols-6">
        <Metric
          label="Net P&L"
          value={signedInr(summary?.total_return)}
          tone={(summary?.total_return ?? 0) >= 0 ? "up" : "down"}
          sub={percent(summary?.total_return_percent ?? null)}
        />
        <Metric label="CAGR" value={percent(summary?.cagr_percent ?? null)} />
        <Metric label="Sharpe" value={num(summary?.sharpe_ratio ?? null, 2)} sub={`Sortino ${num(summary?.sortino_ratio ?? null, 2)}`} />
        <Metric label="Profit factor" value={num(summary?.profit_factor ?? null, 2)} />
        <Metric label="Max drawdown" value={inr(summary?.max_drawdown?.absolute)} sub={percent(summary?.max_drawdown?.percent ?? null)} tone="down" />
        <Metric
          label="Win rate"
          value={percent(summary?.trades.win_rate_percent ?? null, 1)}
          sub={`${summary?.trades.count ?? 0} trades`}
        />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Panel title="Drawdown" subtitle="Percent below the running equity peak">
          {drawdownBars.length > 1 ? (
            <PriceChart candles={drawdownBars} style="area" height={220} showVolume={false} />
          ) : (
            <p className="text-2xs text-ink-faint">Not enough data points.</p>
          )}
        </Panel>

        <Panel
          title={view === "daily" ? "Daily P&L" : "Monthly P&L"}
          actions={
            <div className="flex gap-1">
              {(["daily", "monthly"] as const).map((key) => (
                <button key={key} className={`btn btn-sm ${view === key ? "btn-primary" : ""}`} onClick={() => setView(key)} type="button">
                  {key}
                </button>
              ))}
            </div>
          }
        >
          {buckets.length ? (
            <div className="overflow-auto" style={{ maxHeight: "18rem" }}>
              <table className="tbl">
                <thead>
                  <tr>
                    <th>{view === "daily" ? "Date" : "Month"}</th>
                    <th>Trades</th>
                    <th>Win rate</th>
                    <th>Net P&L</th>
                  </tr>
                </thead>
                <tbody>
                  {buckets.map((bucket) => (
                    <tr key={bucket.date ?? bucket.month}>
                      <td className="text-xs">{bucket.date ?? bucket.month}</td>
                      <td className="num">{num(bucket.trades, 0)}</td>
                      <td className="num text-ink-faint">{num(bucket.win_rate, 1)}%</td>
                      <td className={`num ${toneClass(bucket.net_pnl)}`}>{signedInr(bucket.net_pnl)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="text-2xs text-ink-faint">No closed trades in this view.</p>
          )}
        </Panel>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Panel title="Strategy performance">
          {byStrategy.length ? (
            <table className="tbl">
              <thead>
                <tr>
                  <th>Strategy</th>
                  <th>Trades</th>
                  <th>Win rate</th>
                  <th>PF</th>
                  <th>Net</th>
                </tr>
              </thead>
              <tbody>
                {byStrategy.map((row) => (
                  <tr key={String(row.strategy)}>
                    <td className="text-xs">{String(row.strategy)}</td>
                    <td className="num">{num(Number(row.trades), 0)}</td>
                    <td className="num text-ink-faint">{num(Number(row.win_rate_percent), 1)}%</td>
                    <td className="num text-ink-faint">{num(Number(row.profit_factor), 2)}</td>
                    <td className={`num ${toneClass(Number(row.net_pnl))}`}>{signedInr(Number(row.net_pnl))}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <p className="text-2xs text-ink-faint">No strategy-attributed trades yet.</p>
          )}
        </Panel>

        <Panel title="P&L heatmap" subtitle="Month by weekday, net of charges">
          {heatmap && Object.keys(heatmap.grid).length ? (
            <Heatmap grid={heatmap.grid} />
          ) : (
            <p className="text-2xs text-ink-faint">Not enough closed trades to build a heatmap.</p>
          )}
        </Panel>
      </div>

      <Panel title="How these metrics are calculated">
        <ul className="space-y-1.5 text-2xs leading-relaxed text-ink-faint">
          <li>• <span className="text-ink-dim">Returns</span> are computed from the equity series, which is built from net P&amp;L after all charges.</li>
          <li>• <span className="text-ink-dim">Sharpe and Sortino</span> annualise using the risk-free rate and the trade period, and are only defined when there is dispersion.</li>
          <li>• <span className="text-ink-dim">Max drawdown</span> is measured peak-to-trough, not from the starting balance.</li>
          <li>• <span className="text-ink-dim">Profit factor</span> is gross profit divided by gross loss; undefined when there are no losses.</li>
        </ul>
      </Panel>
    </div>
  );
}

const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

function Heatmap({ grid }: { grid: Record<string, Record<string, number>> }) {
  const values = Object.values(grid).flatMap((row) => Object.values(row));
  const max = Math.max(...values.map(Math.abs), 1);

  return (
    <div className="overflow-auto" style={{ maxHeight: "22rem" }}>
      <table className="w-full border-separate border-spacing-0.5 text-2xs">
        <thead>
          <tr>
            <th className="label-caps px-1 pb-1 text-left">Month</th>
            {WEEKDAYS.map((day) => (
              <th key={day} className="label-caps px-1 pb-1">
                {day}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {Object.entries(grid)
            .sort(([a], [b]) => b.localeCompare(a))
            .map(([month, row]) => (
              <tr key={month}>
                <td className="whitespace-nowrap px-1 text-ink-faint">{month}</td>
                {WEEKDAYS.map((day) => {
                  const value = row[day];
                  if (value === undefined) {
                    return (
                      <td key={day} className="h-6 w-10 rounded-sm bg-canvas/40" />
                    );
                  }
                  const intensity = Math.min(1, Math.abs(value) / max);
                  return (
                    <td
                      key={day}
                      className="h-6 w-10 rounded-sm text-center"
                      style={{
                        backgroundColor:
                          value >= 0 ? `rgba(34,197,94,${0.15 + intensity * 0.7})` : `rgba(239,68,68,${0.15 + intensity * 0.7})`,
                      }}
                      title={`${month} ${day}: ${inr(value)}`}
                    >
                      <span className="text-3xs text-white/90">{inr(value)}</span>
                    </td>
                  );
                })}
              </tr>
            ))}
        </tbody>
      </table>
    </div>
  );
}

export default function AnalyticsPage() {
  return (
    <AppShell>
      <Analytics />
    </AppShell>
  );
}

"use client";

/** Backtest lab: run a strategy over recorded candles and review the metrics. */

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { BacktestResult, StrategyRowRecord } from "@/lib/api";
import { AppShell } from "@/components/app-shell";
import { PriceChart } from "@/components/price-chart";
import { Empty, ErrorNote, Field, Loading, Metric, Panel } from "@/components/ui";
import { inr, num, percent, signedInr, toneClass } from "@/lib/format";

const SLIPPAGE = ["none", "fixed_bps", "moderate", "high"];

interface MetricsShape {
  total_return?: number;
  total_return_percent?: number | null;
  cagr_percent?: number | null;
  sharpe_ratio?: number | null;
  sortino_ratio?: number | null;
  profit_factor?: number | null;
  recovery_factor?: number | null;
  max_drawdown?: { absolute: number; percent: number };
  trades?: {
    count: number;
    wins: number;
    losses: number;
    win_rate_percent: number | null;
    gross_profit: number;
    gross_loss: number;
    net_profit: number;
    average_win: number | null;
    average_loss: number | null;
    largest_win: number | null;
    largest_loss: number | null;
    expectancy: number | null;
  };
  warmup_bars?: number;
  slippage_model?: string;
}

function BacktestLab() {
  const [strategies, setStrategies] = useState<StrategyRowRecord[]>([]);
  const [selected, setSelected] = useState<string>("");
  const [capital, setCapital] = useState(500000);
  const [slippage, setSlippage] = useState("moderate");
  const [result, setResult] = useState<BacktestResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .strategies()
      .then((rows) => {
        setStrategies(rows);
        if (rows[0]) setSelected(String(rows[0].id));
      })
      .catch((failure) => setError(failure instanceof Error ? failure.message : "Could not load strategies"));
  }, []);

  async function run() {
    if (!selected) return;
    setBusy(true);
    setResult(null);
    try {
      setResult(await api.backtest({ strategy_id: selected, initial_capital: capital, slippage }));
      setError(null);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Backtest failed");
    } finally {
      setBusy(false);
    }
  }

  const metrics = result?.metrics as MetricsShape | undefined;
  const trades = metrics?.trades;

  // Turn the equity curve into chart candles so the same chart renders it.
  const equityBars = (result?.equity_curve ?? []).map((value, index) => ({
    time: index,
    open: index > 0 ? (result?.equity_curve[index - 1] ?? value) : value,
    high: Math.max(value, index > 0 ? (result?.equity_curve[index - 1] ?? value) : value),
    low: Math.min(value, index > 0 ? (result?.equity_curve[index - 1] ?? value) : value),
    close: value,
    volume: 0,
    trades: 0,
  }));

  return (
    <div className="space-y-4">
      <header>
        <h1 className="text-lg font-semibold tracking-tight">Backtest lab</h1>
        <p className="text-2xs text-ink-faint">
          Runs the same strategy engine used in paper and live, over candles this platform recorded
        </p>
      </header>

      {error ? <ErrorNote error={error} /> : null}

      <Panel title="Configuration">
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <Field label="Strategy">
            <select className="field-select" value={selected} onChange={(e) => setSelected(e.target.value)}>
              {strategies.length ? (
                strategies.map((strategy) => (
                  <option key={String(strategy.id)} value={String(strategy.id)}>
                    {String(strategy.name)}
                  </option>
                ))
              ) : (
                <option value="">Save a strategy first</option>
              )}
            </select>
          </Field>
          <Field label="Starting capital (₹)">
            <input className="field-input" type="number" min={1000} step={10000} value={capital} onChange={(e) => setCapital(Number(e.target.value) || 0)} />
          </Field>
          <Field label="Slippage model" hint="Applied against you on every fill">
            <select className="field-select" value={slippage} onChange={(e) => setSlippage(e.target.value)}>
              {SLIPPAGE.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          </Field>
          <div className="flex items-end">
            <button className="btn btn-primary w-full" onClick={() => void run()} disabled={busy || !selected} type="button">
              {busy ? "Running…" : "Run backtest"}
            </button>
          </div>
        </div>
      </Panel>

      {busy && !result ? <Loading label="Simulating trades" /> : null}

      {result && result.status === "no_data" ? (
        <Empty
          title="Not enough recorded history"
          message={result.note}
          hint={
            <p className="text-3xs text-ink-faint">
              Warm-up required by this strategy: {metrics?.warmup_bars ?? "—"} bars. Keep the market feed running
              and try again later.
            </p>
          }
        />
      ) : null}

      {result && result.status === "invalid" ? (
        <Empty title="The strategy is invalid" message={result.error ?? "Fix the definition in the builder."} />
      ) : null}

      {result && result.status === "ok" ? (
        <>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4 xl:grid-cols-6">
            <Metric
              label="Net P&L"
              value={signedInr(metrics?.total_return)}
              tone={(metrics?.total_return ?? 0) >= 0 ? "up" : "down"}
              sub={percent(metrics?.total_return_percent ?? null)}
            />
            <Metric label="CAGR" value={percent(metrics?.cagr_percent ?? null)} />
            <Metric
              label="Sharpe"
              value={num(metrics?.sharpe_ratio ?? null, 2)}
              sub={`Sortino ${num(metrics?.sortino_ratio ?? null, 2)}`}
            />
            <Metric label="Profit factor" value={num(metrics?.profit_factor ?? null, 2)} />
            <Metric
              label="Max drawdown"
              value={inr(metrics?.max_drawdown?.absolute)}
              sub={percent(metrics?.max_drawdown?.percent ?? null)}
              tone="down"
            />
            <Metric label="Win rate" value={percent(trades?.win_rate_percent ?? null, 1)} sub={`${trades?.count ?? 0} trades`} />
          </div>

          <Panel title="Equity curve" subtitle="One point per closed trade">
            {equityBars.length > 1 ? (
              <PriceChart candles={equityBars} style="area" height={280} showVolume={false} />
            ) : (
              <Empty title="No equity curve" message="The strategy did not close any trades over this data." />
            )}
          </Panel>

          <div className="grid gap-4 lg:grid-cols-3">
            <Panel title="Trade statistics">
              <dl className="space-y-2 text-2xs">
                <Row label="Trades" value={num(trades?.count ?? 0, 0)} />
                <Row label="Wins / losses" value={`${trades?.wins ?? 0} / ${trades?.losses ?? 0}`} />
                <Row label="Gross profit" value={inr(trades?.gross_profit)} tone="up" />
                <Row label="Gross loss" value={inr(trades?.gross_loss)} tone="down" />
                <Row label="Average win" value={inr(trades?.average_win)} />
                <Row label="Average loss" value={inr(trades?.average_loss)} />
                <Row label="Largest win" value={inr(trades?.largest_win)} />
                <Row label="Largest loss" value={inr(trades?.largest_loss)} />
                <Row label="Expectancy per trade" value={inr(trades?.expectancy)} />
                <Row label="Recovery factor" value={num(metrics?.recovery_factor ?? null, 2)} />
              </dl>
            </Panel>

            <Panel title="Assumptions" subtitle="A metric without its assumptions is meaningless">
              <ul className="space-y-1.5 text-2xs leading-relaxed text-ink-faint">
                <li>• Start {inr(capital)} · slippage model: {String(metrics?.slippage_model ?? slippage)}</li>
                <li>• Warm-up: {num(metrics?.warmup_bars ?? 0, 0)} bars before signals are evaluated</li>
                <li>• Indian charges applied to both legs: STT, exchange, SEBI, GST, stamp duty, brokerage</li>
                <li>• No look-ahead: a signal on bar N fills at bar N+1's open or worse</li>
              </ul>
            </Panel>

            <Panel title="Data coverage">
              {result.coverage?.length ? (
                <ul className="space-y-1 text-2xs">
                  {result.coverage.map((row) => (
                    <li key={row.label} className="flex justify-between">
                      <span className="text-ink-faint">{row.label}</span>
                      <span className="num">{num(row.bars, 0)} bars</span>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="text-2xs text-ink-faint">No coverage reported.</p>
              )}
            </Panel>
          </div>

          {result.trades.length ? (
            <Panel title="Trades" subtitle={`${result.trades.length} closed trades`}>
              <div className="overflow-auto" style={{ maxHeight: "30rem" }}>
                <table className="tbl">
                  <thead>
                    <tr>
                      <th>Symbol</th>
                      <th>Side</th>
                      <th>Qty</th>
                      <th>Entry</th>
                      <th>Exit</th>
                      <th>Reason</th>
                      <th>Charges</th>
                      <th>Net P&L</th>
                      <th>%</th>
                    </tr>
                  </thead>
                  <tbody>
                    {result.trades.map((trade, index) => (
                      <tr key={index}>
                        <td className="text-xs">{trade.symbol}</td>
                        <td className={trade.side === "B" ? "text-up" : "text-down"}>{trade.side === "B" ? "BUY" : "SELL"}</td>
                        <td className="num">{num(trade.quantity, 0)}</td>
                        <td className="num">{num(trade.entry_price)}</td>
                        <td className="num">{num(trade.exit_price)}</td>
                        <td className="text-3xs text-ink-faint">{trade.reason}</td>
                        <td className="num text-ink-faint">{num(trade.charges)}</td>
                        <td className={`num ${toneClass(trade.net_pnl)}`}>{signedInr(trade.net_pnl)}</td>
                        <td className={`num ${toneClass(trade.pnl_percent)}`}>{percent(trade.pnl_percent)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Panel>
          ) : null}

          {result.warnings.length ? (
            <Panel title="Warnings">
              <ul className="space-y-1 text-2xs text-warn">
                {result.warnings.map((warning) => (
                  <li key={warning}>• {warning}</li>
                ))}
              </ul>
            </Panel>
          ) : null}

          <p className="text-3xs leading-relaxed text-ink-faint">{result.note}</p>
        </>
      ) : null}
    </div>
  );
}

function Row({ label, value, tone }: { label: string; value: string; tone?: "up" | "down" }) {
  return (
    <div className="flex items-center justify-between gap-3 border-b border-hairline/60 pb-1.5 last:border-0">
      <dt className="text-ink-faint">{label}</dt>
      <dd className={`num ${tone === "up" ? "text-up" : tone === "down" ? "text-down" : "text-ink"}`}>{value}</dd>
    </div>
  );
}

export default function BacktestPage() {
  return (
    <AppShell>
      <BacktestLab />
    </AppShell>
  );
}

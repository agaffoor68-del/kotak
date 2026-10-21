"use client";

/**
 * Quick visual strategy builder.
 *
 * A Pine Script alternative for the operator who wants the rule in one line:
 * two indicators, one comparison, and the exits and sizing handled for them.
 * The document it produces is exactly the one the full rule-tree builder
 * produces, so it is saved to the same table and backtested by the same
 * engine — no second dialect to maintain.
 */

import { useMemo, useState } from "react";
import { api } from "@/lib/api";
import type { Instrument } from "@/lib/api";
import { InstrumentSearch } from "@/components/instrument-search";
import { Empty, ErrorNote, Field, Loading, Metric, Panel, StatusChip } from "@/components/ui";
import { inr, num, percent, signedInr } from "@/lib/format";

type FirstIndicator = "ema" | "vwap" | "rsi" | "supertrend";
type SecondIndicator = "price" | "ema" | "vwap";
type Condition = "gt" | "lt" | "crosses_above";
type ExitRule = "rsi" | "ema_cross" | "supertrend_flip";

const FIRST_INDICATORS: { value: FirstIndicator; label: string }[] = [
  { value: "ema", label: "EMA" },
  { value: "vwap", label: "VWAP" },
  { value: "rsi", label: "RSI" },
  { value: "supertrend", label: "Supertrend" },
];

const SECOND_INDICATORS: { value: SecondIndicator; label: string }[] = [
  { value: "price", label: "Price" },
  { value: "ema", label: "EMA" },
  { value: "vwap", label: "VWAP" },
];

const CONDITIONS: { value: Condition; label: string }[] = [
  { value: "gt", label: "is above (>)" },
  { value: "lt", label: "is below (<)" },
  { value: "crosses_above", label: "crosses above" },
];

const EXITS: { value: ExitRule; label: string }[] = [
  { value: "rsi", label: "RSI overbought (70)" },
  { value: "ema_cross", label: "Close crosses below EMA(20)" },
  { value: "supertrend_flip", label: "Supertrend flips down" },
];

/** The fields and parameters each indicator needs in the strategy document. */
function indicatorSpec(indicator: FirstIndicator | SecondIndicator, period: number) {
  switch (indicator) {
    case "ema":
      return { field: "value", params: { period } };
    case "vwap":
      return { field: "value", params: {} };
    case "rsi":
      return { field: "value", params: { period } };
    case "supertrend":
      return { field: "supertrend", params: { period, multiplier: 3 } };
    default:
      return { field: "value", params: {} };
  }
}

export function QuickStrategyBuilder() {
  const [name, setName] = useState("EMA crossover");
  const [timeframe, setTimeframe] = useState("5m");
  const [instrument, setInstrument] = useState<Instrument | null>(null);
  const [first, setFirst] = useState<FirstIndicator>("ema");
  const [firstPeriod, setFirstPeriod] = useState(20);
  const [condition, setCondition] = useState<Condition>("crosses_above");
  const [second, setSecond] = useState<SecondIndicator>("ema");
  const [secondPeriod, setSecondPeriod] = useState(50);
  const [priceLevel, setPriceLevel] = useState(22000);
  const [exit, setExit] = useState<ExitRule>("rsi");
  const [stopLossPct, setStopLossPct] = useState(1);
  const [targetPct, setTargetPct] = useState(2);
  const [lots, setLots] = useState(1);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [metrics, setMetrics] = useState<Record<string, unknown> | null>(null);

  const definition = useMemo(() => {
    const firstSpec = indicatorSpec(first, firstPeriod);
    const against =
      second === "price"
        ? { value: priceLevel }
        : (() => {
            const spec = indicatorSpec(second, secondPeriod);
            return { indicator: second, params: spec.params, field: spec.field };
          })();

    const entry = {
      indicator: first,
      params: firstSpec.params,
      field: firstSpec.field,
      compare: condition,
      against,
    };

    const exitNode =
      exit === "rsi"
        ? { all: [{ indicator: "rsi", params: { period: 14 }, compare: "gt", value: 70 }] }
        : exit === "ema_cross"
          ? {
              all: [
                {
                  indicator: "close",
                  params: {},
                  compare: "crosses_below",
                  against: { indicator: "ema", params: { period: 20 }, field: "value" },
                },
              ],
            }
          : {
              all: [
                {
                  indicator: "supertrend",
                  params: { period: 10, multiplier: 3 },
                  compare: "crosses_below",
                  against: { indicator: "close", params: {}, field: "value" },
                },
              ],
            };

    return {
      name,
      timeframe,
      universe: [
        {
          token: instrument?.token ?? "",
          exchange_segment: instrument?.exchange_segment ?? "nse_cm",
          label: instrument?.trading_symbol || instrument?.symbol || "NIFTY 50",
        },
      ],
      entry,
      exit: exitNode,
      risk: {
        stop_loss_pct: stopLossPct,
        target_pct: targetPct,
        trailing_stop_pct: Math.max(0.2, stopLossPct / 2),
        timeframe_exit_bars: 40,
      },
      position_sizing: { mode: "fixed", quantity: lots },
    };
  }, [name, timeframe, instrument, first, firstPeriod, condition, second, secondPeriod, priceLevel, exit, stopLossPct, targetPct, lots]);

  const sentence = useMemo(() => {
    const firstLabel = FIRST_INDICATORS.find((entry) => entry.value === first)?.label ?? first;
    const secondLabel =
      second === "price" ? `price ${num(priceLevel, 0)}` : `${SECOND_INDICATORS.find((entry) => entry.value === second)?.label}(${secondPeriod})`;
    const conditionLabel = CONDITIONS.find((entry) => entry.value === condition)?.label ?? condition;
    const firstParams = first === "ema" || first === "rsi" ? `(${firstPeriod})` : first === "supertrend" ? `(${firstPeriod}, 3)` : "";
    return `Enter when ${firstLabel}${firstParams} ${conditionLabel} ${secondLabel}.`;
  }, [first, firstPeriod, condition, second, secondPeriod, priceLevel]);

  async function save() {
    setBusy(true);
    setError(null);
    try {
      const result = await api.saveStrategy(name, "Built with the quick strategy builder", definition);
      setSaved(result.id);
      return result.id;
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Could not save the strategy");
      return null;
    } finally {
      setBusy(false);
    }
  }

  async function saveAndTest() {
    setBusy(true);
    setError(null);
    setMetrics(null);
    try {
      const result = await api.saveStrategy(name, "Built with the quick strategy builder", definition);
      setSaved(result.id);
      const backtest = await api.runBacktest({
        strategy_id: result.id,
        symbol: instrument?.trading_symbol || instrument?.symbol,
        symbol_token: instrument?.token,
        exchange_segment: instrument?.exchange_segment,
        timeframe,
        history_days: 30,
        auto_download: true,
      });
      setMetrics((backtest.metrics ?? {}) as Record<string, unknown>);
      if (backtest.status === "no_data") {
        setError(backtest.error ?? "No history is available for this instrument yet.");
      }
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "The backtest could not be run");
    } finally {
      setBusy(false);
    }
  }

  const trades = (metrics?.trades ?? null) as
    | { count?: number; wins?: number; losses?: number; win_rate_percent?: number | null; net_profit?: number }
    | null;

  return (
    <Panel
      title="Quick build"
      subtitle="Two indicators, one comparison — the rest is handled for you"
      actions={saved ? <StatusChip status="live" label="saved" /> : null}
    >
      <div className="space-y-3">
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <Field label="Strategy name">
            <input className="field-input" value={name} onChange={(event) => setName(event.target.value)} />
          </Field>
          <Field label="Timeframe">
            <select className="field-select" value={timeframe} onChange={(event) => setTimeframe(event.target.value)}>
              <option value="1m">1m</option>
              <option value="5m">5m</option>
              <option value="15m">15m</option>
              <option value="1d">1d</option>
            </select>
          </Field>
          <div className="sm:col-span-2">
            <InstrumentSearch
              label="Instrument"
              scope="all"
              placeholder="Type a symbol, e.g. NIFTY"
              onSelect={setInstrument}
            />
          </div>
        </div>

        <div className="grid gap-3 rounded-md border border-hairline bg-canvas/60 p-3 sm:grid-cols-2 lg:grid-cols-5">
          <Field label="Indicator 1">
            <select className="field-select" value={first} onChange={(event) => setFirst(event.target.value as FirstIndicator)}>
              {FIRST_INDICATORS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Period">
            <input
              className="field-input"
              type="number"
              min={1}
              value={firstPeriod}
              onChange={(event) => setFirstPeriod(Math.max(1, Number(event.target.value) || 1))}
            />
          </Field>
          <Field label="Condition">
            <select className="field-select" value={condition} onChange={(event) => setCondition(event.target.value as Condition)}>
              {CONDITIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Indicator 2">
            <select className="field-select" value={second} onChange={(event) => setSecond(event.target.value as SecondIndicator)}>
              {SECOND_INDICATORS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </Field>
          {second === "price" ? (
            <Field label="Price level">
              <input
                className="field-input"
                type="number"
                min={0}
                step={1}
                value={priceLevel}
                onChange={(event) => setPriceLevel(Number(event.target.value) || 0)}
              />
            </Field>
          ) : (
            <Field label="Period 2">
              <input
                className="field-input"
                type="number"
                min={1}
                value={secondPeriod}
                onChange={(event) => setSecondPeriod(Math.max(1, Number(event.target.value) || 1))}
              />
            </Field>
          )}
        </div>

        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <Field label="Exit">
            <select className="field-select" value={exit} onChange={(event) => setExit(event.target.value as ExitRule)}>
              {EXITS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Stop loss (%)">
            <input
              className="field-input"
              type="number"
              min={0.1}
              step={0.1}
              value={stopLossPct}
              onChange={(event) => setStopLossPct(Number(event.target.value) || 0)}
            />
          </Field>
          <Field label="Target (%)">
            <input
              className="field-input"
              type="number"
              min={0.1}
              step={0.1}
              value={targetPct}
              onChange={(event) => setTargetPct(Number(event.target.value) || 0)}
            />
          </Field>
          <Field label="Quantity (lots)">
            <input
              className="field-input"
              type="number"
              min={1}
              value={lots}
              onChange={(event) => setLots(Math.max(1, Number(event.target.value) || 1))}
            />
          </Field>
        </div>

        <p className="rounded border border-accent/30 bg-accent-soft px-3 py-2 text-2xs text-ink-dim">{sentence}</p>

        <div className="flex flex-wrap items-center gap-2">
          <button className="btn btn-secondary" onClick={() => void save()} disabled={busy} type="button">
            {busy ? "Saving…" : "Save strategy"}
          </button>
          <button className="btn btn-primary" onClick={() => void saveAndTest()} disabled={busy || !instrument} type="button">
            {busy ? <span className="h-3 w-3 animate-spin rounded-full border-2 border-edge border-t-canvas" /> : null}
            {busy ? "Running…" : "Save & backtest"}
          </button>
          {saved ? <p className="text-3xs text-up">Saved to the database. Open Backtest to run it on any window.</p> : null}
        </div>

        {error ? <ErrorNote error={error} /> : null}

        {busy && !metrics ? <Loading label="Backtesting the saved strategy" /> : null}

        {metrics ? (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Metric
              label="Net P&L"
              value={signedInr((metrics.total_return as number) ?? null)}
              tone={((metrics.total_return as number) ?? 0) >= 0 ? "up" : "down"}
              sub={percent((metrics.total_return_percent as number) ?? null)}
            />
            <Metric label="Win rate" value={percent(trades?.win_rate_percent ?? null, 1)} sub={`${trades?.count ?? 0} trades`} />
            <Metric label="Wins / losses" value={`${trades?.wins ?? 0} / ${trades?.losses ?? 0}`} />
            <Metric label="Max drawdown" value={inr((metrics.max_drawdown as { absolute?: number })?.absolute ?? null)} tone="down" />
          </div>
        ) : null}

        {!metrics && !busy && !saved ? (
          <Empty
            title="Nothing to test yet"
            message="Pick an instrument, then save the strategy or run it straight away."
          />
        ) : null}
      </div>
    </Panel>
  );
}

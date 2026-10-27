"use client";

import { useState } from "react";
import { api } from "@/lib/api";

const INDICATOR_OPTIONS = [
  { label: "EMA (Exponential Moving Average)", value: "ema", defaultPeriod: 20 },
  { label: "SMA (Simple Moving Average)", value: "sma", defaultPeriod: 50 },
  { label: "RSI (Relative Strength Index)", value: "rsi", defaultPeriod: 14 },
  { label: "Supertrend", value: "supertrend", defaultPeriod: 10 },
  { label: "VWAP (Volume Weighted Avg Price)", value: "vwap", defaultPeriod: 0 },
  { label: "Bollinger Bands", value: "bbands", defaultPeriod: 20 },
  { label: "MACD", value: "macd", defaultPeriod: 12 },
  { label: "Stochastic RSI", value: "stochrsi", defaultPeriod: 14 },
  { label: "ATR (Average True Range)", value: "atr", defaultPeriod: 14 },
  { label: "Close Price", value: "close", defaultPeriod: 0 },
];

const OPERATORS = [
  { label: "Crosses Above (Crossover)", value: "crosses_above" },
  { label: "Crosses Below (Crossunder)", value: "crosses_below" },
  { label: "Greater Than (>)", value: "gt" },
  { label: "Less Than (<)", value: "lt" },
  { label: "Greater Than or Equal (>=)", value: "gte" },
  { label: "Less Than or Equal (<=)", value: "lte" },
];

interface VisualRule {
  indicator: string;
  period: number;
  operator: string;
  compareType: "value" | "indicator" | "multiplier";
  targetValue: string;
  targetIndicator: string;
  targetPeriod: number;
  multiplier: string;
}

export function VisualStrategyBuilder({
  onClose,
  initialSymbol = "NIFTY 50",
}: {
  onClose?: () => void;
  initialSymbol?: string;
}) {
  const [strategyName, setStrategyName] = useState("Alpha Confluence Strategy");
  const [timeframe, setTimeframe] = useState("5m");
  const [symbol, setSymbol] = useState(initialSymbol);

  // Entry condition
  const [entryRule, setEntryRule] = useState<VisualRule>({
    indicator: "ema",
    period: 20,
    operator: "crosses_above",
    compareType: "indicator",
    targetValue: "50",
    targetIndicator: "ema",
    targetPeriod: 50,
    multiplier: "1.02",
  });

  // Secondary Entry Filter (optional)
  const [enableFilter, setEnableFilter] = useState(true);
  const [filterRule, setFilterRule] = useState<VisualRule>({
    indicator: "rsi",
    period: 14,
    operator: "lt",
    compareType: "value",
    targetValue: "65",
    targetIndicator: "sma",
    targetPeriod: 20,
    multiplier: "1.0",
  });

  // Exit condition
  const [exitRule, setExitRule] = useState<VisualRule>({
    indicator: "rsi",
    period: 14,
    operator: "gt",
    compareType: "value",
    targetValue: "75",
    targetIndicator: "sma",
    targetPeriod: 50,
    multiplier: "0.98",
  });

  const [stopLossPct, setStopLossPct] = useState("1.0");
  const [targetPct, setTargetPct] = useState("2.5");
  const [saving, setSaving] = useState(false);
  const [statusMsg, setStatusMsg] = useState<string | null>(null);
  const [showPineCode, setShowPineCode] = useState(false);

  const buildDSL = () => {
    const entryAll: Record<string, unknown>[] = [];

    // Main entry rule
    const mainEntry: Record<string, unknown> = {
      indicator: entryRule.indicator,
      field: "value",
      params: entryRule.period > 0 ? { period: entryRule.period } : {},
      compare: entryRule.operator,
    };
    if (entryRule.compareType === "value") {
      mainEntry.value = parseFloat(entryRule.targetValue) || 0;
    } else if (entryRule.compareType === "indicator") {
      mainEntry.against = {
        indicator: entryRule.targetIndicator,
        field: "value",
        params: entryRule.targetPeriod > 0 ? { period: entryRule.targetPeriod } : {},
      };
    } else {
      mainEntry.against = {
        indicator: "close",
        field: "value",
        params: {},
        multiplier: parseFloat(entryRule.multiplier) || 1.0,
      };
    }
    entryAll.push(mainEntry);

    // Optional filter
    if (enableFilter) {
      const fNode: Record<string, unknown> = {
        indicator: filterRule.indicator,
        field: "value",
        params: filterRule.period > 0 ? { period: filterRule.period } : {},
        compare: filterRule.operator,
      };
      if (filterRule.compareType === "value") {
        fNode.value = parseFloat(filterRule.targetValue) || 0;
      } else {
        fNode.against = {
          indicator: filterRule.targetIndicator,
          field: "value",
          params: filterRule.targetPeriod > 0 ? { period: filterRule.targetPeriod } : {},
        };
      }
      entryAll.push(fNode);
    }

    // Exit rule
    const exitNode: Record<string, unknown> = {
      indicator: exitRule.indicator,
      field: "value",
      params: exitRule.period > 0 ? { period: exitRule.period } : {},
      compare: exitRule.operator,
    };
    if (exitRule.compareType === "value") {
      exitNode.value = parseFloat(exitRule.targetValue) || 0;
    } else {
      exitNode.against = {
        indicator: exitRule.targetIndicator,
        field: "value",
        params: exitRule.targetPeriod > 0 ? { period: exitRule.targetPeriod } : {},
      };
    }

    return {
      name: strategyName,
      timeframe,
      universe: [{ token: "99926000", exchange_segment: "nse_cm", label: symbol }],
      entry: { all: entryAll },
      exit: { all: [exitNode] },
      risk: {
        stop_loss_pct: parseFloat(stopLossPct) || 1.0,
        target_pct: parseFloat(targetPct) || 2.0,
        trailing_stop_pct: 0.8,
        quantity: 1,
        timeframe_exit_bars: 40,
      },
      position_sizing: { mode: "fixed", quantity: 1 },
    };
  };

  const handleSave = async () => {
    setSaving(true);
    setStatusMsg(null);
    try {
      const dsl = buildDSL();
      const res = await api.saveStrategy(
        strategyName,
        `Visual Builder Strategy for ${symbol} (${timeframe})`,
        dsl
      );
      setStatusMsg(`Strategy saved successfully with ID: ${res.id}`);
    } catch (err) {
      setStatusMsg(err instanceof Error ? err.message : "Failed to save strategy");
    } finally {
      setSaving(false);
    }
  };

  const handleBacktest = () => {
    window.location.assign(`/backtest?symbol=${encodeURIComponent(symbol)}`);
  };

  const generatePinePreview = () => {
    return `//@version=5
strategy("${strategyName}", overlay=true)

// Indicator Setup
ind1 = ta.${entryRule.indicator}(close, ${entryRule.period || 14})
${
  entryRule.compareType === "indicator"
    ? `ind2 = ta.${entryRule.targetIndicator}(close, ${entryRule.targetPeriod || 50})`
    : ""
}
${enableFilter ? `filterInd = ta.${filterRule.indicator}(close, ${filterRule.period || 14})` : ""}
exitInd = ta.${exitRule.indicator}(close, ${exitRule.period || 14})

// Entry Logic
${
  entryRule.operator === "crosses_above"
    ? `longCondition = ta.crossover(ind1, ${entryRule.compareType === "indicator" ? "ind2" : entryRule.targetValue})`
    : entryRule.operator === "crosses_below"
    ? `longCondition = ta.crossunder(ind1, ${entryRule.compareType === "indicator" ? "ind2" : entryRule.targetValue})`
    : `longCondition = ind1 ${entryRule.operator === "gt" ? ">" : "<"} ${
        entryRule.compareType === "indicator" ? "ind2" : entryRule.targetValue
      }`
}
${
  enableFilter
    ? `filterCondition = filterInd ${filterRule.operator === "gt" ? ">" : "<"} ${filterRule.targetValue}`
    : ""
}

if (longCondition ${enableFilter ? "and filterCondition" : ""})
    strategy.entry("Long", strategy.long)

// Exit Logic
exitCondition = exitInd ${exitRule.operator === "gt" ? ">" : "<"} ${exitRule.targetValue}
if (exitCondition)
    strategy.close("Long")
`;
  };

  const renderRuleEditor = (
    rule: VisualRule,
    setRule: React.Dispatch<React.SetStateAction<VisualRule>>,
    title: string
  ) => (
    <div className="rounded border border-[#2a2e39] bg-[#131722] p-3 text-xs">
      <div className="mb-2 font-semibold text-[#d1d4dc]">{title}</div>
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
        {/* Left Indicator */}
        <div>
          <label className="text-3xs text-[#787b86]">Indicator</label>
          <select
            value={rule.indicator}
            onChange={(e) => {
              const opt = INDICATOR_OPTIONS.find((o) => o.value === e.target.value);
              setRule({ ...rule, indicator: e.target.value, period: opt?.defaultPeriod ?? 14 });
            }}
            className="mt-1 w-full rounded border border-[#2a2e39] bg-[#1e222d] px-2 py-1.5 text-xs text-[#d1d4dc] outline-none"
          >
            {INDICATOR_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
          {rule.period > 0 && (
            <div className="mt-1 flex items-center gap-1">
              <span className="text-3xs text-[#787b86]">Length:</span>
              <input
                type="number"
                value={rule.period}
                onChange={(e) => setRule({ ...rule, period: parseInt(e.target.value) || 14 })}
                className="w-16 rounded border border-[#2a2e39] bg-[#1e222d] px-1.5 py-0.5 text-2xs text-[#d1d4dc]"
              />
            </div>
          )}
        </div>

        {/* Operator */}
        <div>
          <label className="text-3xs text-[#787b86]">Operator / Condition</label>
          <select
            value={rule.operator}
            onChange={(e) => setRule({ ...rule, operator: e.target.value })}
            className="mt-1 w-full rounded border border-[#2a2e39] bg-[#1e222d] px-2 py-1.5 text-xs text-[#d1d4dc] outline-none"
          >
            {OPERATORS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </div>

        {/* Target */}
        <div>
          <div className="flex items-center justify-between">
            <label className="text-3xs text-[#787b86]">Target</label>
            <div className="flex gap-1 text-3xs">
              <button
                type="button"
                onClick={() => setRule({ ...rule, compareType: "indicator" })}
                className={`px-1 rounded ${rule.compareType === "indicator" ? "bg-[#2962ff] text-white" : "text-[#787b86]"}`}
              >
                Indicator
              </button>
              <button
                type="button"
                onClick={() => setRule({ ...rule, compareType: "value" })}
                className={`px-1 rounded ${rule.compareType === "value" ? "bg-[#2962ff] text-white" : "text-[#787b86]"}`}
              >
                Value
              </button>
            </div>
          </div>

          {rule.compareType === "value" ? (
            <input
              type="text"
              value={rule.targetValue}
              onChange={(e) => setRule({ ...rule, targetValue: e.target.value })}
              className="mt-1 w-full rounded border border-[#2a2e39] bg-[#1e222d] px-2 py-1.5 text-xs text-[#d1d4dc]"
              placeholder="e.g. 70"
            />
          ) : (
            <div>
              <select
                value={rule.targetIndicator}
                onChange={(e) => {
                  const opt = INDICATOR_OPTIONS.find((o) => o.value === e.target.value);
                  setRule({ ...rule, targetIndicator: e.target.value, targetPeriod: opt?.defaultPeriod ?? 50 });
                }}
                className="mt-1 w-full rounded border border-[#2a2e39] bg-[#1e222d] px-2 py-1.5 text-xs text-[#d1d4dc] outline-none"
              >
                {INDICATOR_OPTIONS.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
              {rule.targetPeriod > 0 && (
                <div className="mt-1 flex items-center gap-1">
                  <span className="text-3xs text-[#787b86]">Length:</span>
                  <input
                    type="number"
                    value={rule.targetPeriod}
                    onChange={(e) => setRule({ ...rule, targetPeriod: parseInt(e.target.value) || 50 })}
                    className="w-16 rounded border border-[#2a2e39] bg-[#1e222d] px-1.5 py-0.5 text-2xs text-[#d1d4dc]"
                  />
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );

  return (
    <div className="rounded-lg border border-[#2a2e39] bg-[#1e222d] p-5 text-[#d1d4dc] shadow-2xl">
      {/* Header */}
      <div className="flex items-center justify-between border-b border-[#2a2e39] pb-3">
        <div className="flex items-center gap-2">
          <div className="h-3 w-3 rounded-full bg-[#2962ff]" />
          <h2 className="text-sm font-bold tracking-wide">TradingView Visual Strategy Builder</h2>
        </div>
        {onClose && (
          <button
            onClick={onClose}
            className="rounded px-2 py-1 text-xs text-[#787b86] hover:bg-[#2a2e39] hover:text-[#d1d4dc]"
          >
            ✕ Close
          </button>
        )}
      </div>

      {/* Strategy Meta */}
      <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-3">
        <div>
          <label className="text-3xs text-[#787b86]">Strategy Name</label>
          <input
            value={strategyName}
            onChange={(e) => setStrategyName(e.target.value)}
            className="mt-1 w-full rounded border border-[#2a2e39] bg-[#131722] px-2.5 py-1.5 text-xs text-[#d1d4dc]"
          />
        </div>
        <div>
          <label className="text-3xs text-[#787b86]">Symbol</label>
          <input
            value={symbol}
            onChange={(e) => setSymbol(e.target.value)}
            className="mt-1 w-full rounded border border-[#2a2e39] bg-[#131722] px-2.5 py-1.5 text-xs text-[#d1d4dc]"
          />
        </div>
        <div>
          <label className="text-3xs text-[#787b86]">Timeframe</label>
          <select
            value={timeframe}
            onChange={(e) => setTimeframe(e.target.value)}
            className="mt-1 w-full rounded border border-[#2a2e39] bg-[#131722] px-2.5 py-1.5 text-xs text-[#d1d4dc]"
          >
            {["1m", "3m", "5m", "15m", "30m", "1h", "1d"].map((tf) => (
              <option key={tf} value={tf}>
                {tf}
              </option>
            ))}
          </select>
        </div>
      </div>

      {/* Rules Section */}
      <div className="mt-4 space-y-3">
        {renderRuleEditor(entryRule, setEntryRule, "1. Long Entry Condition (Primary)")}

        <div className="rounded border border-[#2a2e39] bg-[#131722] p-3 text-xs">
          <div className="flex items-center justify-between">
            <span className="font-semibold text-[#d1d4dc]">2. Confluence Filter (Optional)</span>
            <input
              type="checkbox"
              checked={enableFilter}
              onChange={(e) => setEnableFilter(e.target.checked)}
              className="accent-[#2962ff]"
            />
          </div>
          {enableFilter && (
            <div className="mt-2">
              {renderRuleEditor(filterRule, setFilterRule, "Filter Rule (e.g. RSI < 65)")}
            </div>
          )}
        </div>

        {renderRuleEditor(exitRule, setExitRule, "3. Exit Condition (Target / Reversal)")}
      </div>

      {/* Risk Parameters */}
      <div className="mt-4 grid grid-cols-2 gap-3 rounded border border-[#2a2e39] bg-[#131722] p-3 text-xs">
        <div>
          <label className="text-3xs text-[#787b86]">Stop Loss (%)</label>
          <input
            type="number"
            step="0.1"
            value={stopLossPct}
            onChange={(e) => setStopLossPct(e.target.value)}
            className="mt-1 w-full rounded border border-[#2a2e39] bg-[#1e222d] px-2 py-1 text-xs text-[#d1d4dc]"
          />
        </div>
        <div>
          <label className="text-3xs text-[#787b86]">Target / Take Profit (%)</label>
          <input
            type="number"
            step="0.1"
            value={targetPct}
            onChange={(e) => setTargetPct(e.target.value)}
            className="mt-1 w-full rounded border border-[#2a2e39] bg-[#1e222d] px-2 py-1 text-xs text-[#d1d4dc]"
          />
        </div>
      </div>

      {/* Status Message */}
      {statusMsg && (
        <div className="mt-3 rounded border border-[#2a2e39] bg-[#131722] p-2 text-2xs text-[#26a69a]">
          {statusMsg}
        </div>
      )}

      {/* Actions */}
      <div className="mt-5 flex flex-wrap items-center justify-between gap-3 border-t border-[#2a2e39] pt-4">
        <button
          type="button"
          onClick={() => setShowPineCode(!showPineCode)}
          className="rounded border border-[#2a2e39] bg-[#131722] px-3 py-1.5 text-xs text-[#787b86] hover:text-[#d1d4dc]"
        >
          {showPineCode ? "Hide Pine Script" : "View Pine Script v5 Code"}
        </button>

        <div className="flex gap-2">
          <button
            type="button"
            onClick={handleBacktest}
            className="rounded border border-[#2a2e39] bg-[#131722] px-4 py-1.5 text-xs font-semibold text-[#d1d4dc] hover:bg-[#2a2e39]"
          >
            Backtest
          </button>
          <button
            type="button"
            disabled={saving}
            onClick={handleSave}
            className="rounded bg-[#2962ff] px-5 py-1.5 text-xs font-semibold text-white hover:bg-[#1e4bd8] disabled:opacity-50"
          >
            {saving ? "Saving..." : "Save Strategy"}
          </button>
        </div>
      </div>

      {/* Pine Script Preview Modal / Box */}
      {showPineCode && (
        <div className="mt-4 rounded border border-[#2a2e39] bg-[#0b0e14] p-3">
          <div className="mb-1 text-3xs font-semibold uppercase text-[#787b86]">
            Generated Pine Script v5 (Ready for TradingView)
          </div>
          <pre className="max-h-48 overflow-y-auto font-mono text-3xs text-[#26a69a] leading-relaxed">
            {generatePinePreview()}
          </pre>
        </div>
      )}
    </div>
  );
}

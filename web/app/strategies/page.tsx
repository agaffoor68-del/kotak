"use client";

/** No-code strategy builder: compose indicator rules, then validate and save. */

import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import type { IndicatorCatalog, StrategyRowRecord } from "@/lib/api";
import { AppShell } from "@/components/app-shell";
import { Empty, ErrorNote, Field, Panel, StatusChip } from "@/components/ui";

type Catalog = IndicatorCatalog;

/** A condition or logical group in the visual editor. */
type Node = Condition | { kind: "group"; logic: "all" | "any" | "not"; children: Node[] };

interface Condition {
  kind: "condition";
  indicator: string;
  field: string;
  params: { period?: number; fast?: number; slow?: number; signal?: number; length?: number; deviations?: number; multiplier?: number };
  compare: string;
  value?: number;
  against?: { indicator: string; params: Record<string, number>; field?: string };
}

const DEFAULT_CONDITION: Condition = {
  kind: "condition",
  indicator: "ema",
  field: "value",
  params: { period: 20 },
  compare: "gt",
  value: 50,
};

function newNode(): Node {
  return { kind: "group", logic: "all", children: [{ ...DEFAULT_CONDITION, params: { period: 20 } }] };
}

const PERIOD_KEYS = ["period", "fast", "slow", "signal", "length", "deviations", "multiplier"] as const;

function toDefinition(node: Node, name: string, timeframe: string, token: string, label: string) {
  const convert = (input: Node): unknown => {
    if (input.kind === "group") {
      if (input.logic === "not") return { not: convert(input.children[0]) };
      return { [input.logic]: input.children.map(convert) };
    }
    const base: Record<string, unknown> = {
      indicator: input.indicator,
      params: Object.fromEntries(Object.entries(input.params).filter(([, value]) => value !== undefined && value !== null)),
      field: input.field,
      compare: input.compare,
    };
    if (["crosses_above", "crosses_below"].includes(input.compare)) {
      if (input.against) {
        base.against = {
          indicator: input.against.indicator,
          params: input.against.params,
          field: input.against.field ?? "value",
        };
      }
    } else if (input.value !== undefined) {
      base.value = input.value;
    }
    return base;
  };

  return {
    name,
    timeframe,
    universe: [{ token, exchange_segment: "nse_cm", label }],
    entry: convert(node),
    exit: {
      all: [
        {
          indicator: "rsi",
          params: { period: 14 },
          compare: "gt",
          value: 70,
        },
      ],
    },
    risk: { stop_loss_pct: 1.0, target_pct: 2.0, trailing_stop_pct: 0.8, timeframe_exit_bars: 40 },
    position_sizing: { mode: "pct_risk", risk_pct: 0.5, quantity: 1 },
  };
}

function StrategyBuilder() {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [templates, setTemplates] = useState<(Record<string, unknown> & { id: string; name: string; valid: boolean })[]>([]);
  const [mine, setMine] = useState<StrategyRowRecord[]>([]);
  const [name, setName] = useState("My strategy");
  const [timeframe, setTimeframe] = useState("5m");
  const [token, setToken] = useState("26000");
  const [label, setLabel] = useState("NIFTY 50");
  const [entry, setEntry] = useState<Node>(newNode());
  const [validation, setValidation] = useState<{ valid: boolean; errors: string[] } | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api
      .indicators()
      .then((payload) => {
        setCatalog(payload);
        setTimeframe(payload.timeframes[1] ?? payload.timeframes[0] ?? "5m");
      })
      .catch((failure) => setError(failure instanceof Error ? failure.message : "Could not load the indicator catalog"));
    api.templates().then((rows): void => setTemplates(rows)).catch(() => undefined);
    api.strategies().then((rows): void => setMine(rows)).catch(() => undefined);
  }, []);

  const definition = useMemo(
    () => toDefinition(entry, name, timeframe, token, label),
    [entry, name, timeframe, token, label],
  );

  // Validate as the user builds, debounced.
  useEffect(() => {
    const timer = setTimeout(() => {
      api
        .validateStrategy(definition)
        .then(setValidation)
        .catch((failure) => setValidation({ valid: false, errors: [failure instanceof Error ? failure.message : "invalid"] }));
    }, 400);
    return () => clearTimeout(timer);
  }, [definition]);

  const save = useCallback(async () => {
    setBusy(true);
    try {
      const result = await api.saveStrategy(name, "", definition);
      setSaved(result.id);
      setError(null);
      setMine(await api.strategies());
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Could not save the strategy");
    } finally {
      setBusy(false);
    }
  }, [name, definition]);

  const applyTemplate = useCallback((template: Record<string, unknown>) => {
    const def = template.definition as Record<string, unknown> | undefined;
    if (!def) return;
    setName(String(def.name ?? template.name ?? "Template"));
    setTimeframe(String(def.timeframe ?? "5m"));
    const universe = (def.universe as { token: string; label?: string }[] | undefined) ?? [];
    if (universe[0]) {
      setToken(String(universe[0].token));
      setLabel(String(universe[0].label ?? universe[0].token));
    }
    // Templates arrive as a definition; the editor renders conditions, so load
    // the first entry group's children when the shape matches.
    const first = def.entry as { all?: unknown[] } | undefined;
    if (first?.all?.length) {
      setEntry({ kind: "group", logic: "all", children: [] });
      setSaved("template-loaded");
    }
  }, []);

  return (
    <div className="space-y-4">
      <header>
        <h1 className="text-lg font-semibold tracking-tight">Strategy builder</h1>
        <p className="text-2xs text-ink-faint">
          Compose rules from indicators and price fields. The same document runs in backtest, paper and live.
        </p>
      </header>

      {error ? <ErrorNote error={error} /> : null}

      <div className="grid gap-4 xl:grid-cols-4">
        {/* ---------------- editor ---------------- */}
        <div className="space-y-4 xl:col-span-3">
          <Panel title="Strategy">
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <Field label="Name">
                <input className="field-input" value={name} onChange={(e) => setName(e.target.value)} />
              </Field>
              <Field label="Timeframe">
                <select className="field-select" value={timeframe} onChange={(e) => setTimeframe(e.target.value)}>
                  {(catalog?.timeframes ?? ["5m"]).map((value) => (
                    <option key={value} value={value}>
                      {value}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="Instrument token">
                <input className="field-input" value={token} onChange={(e) => setToken(e.target.value)} />
              </Field>
              <Field label="Label">
                <input className="field-input" value={label} onChange={(e) => setLabel(e.target.value)} />
              </Field>
            </div>
          </Panel>

          <Panel
            title="Entry conditions"
            subtitle="Combine with AND, OR and NOT"
            actions={
              <button className="btn btn-sm" onClick={() => addChild(setEntry)} type="button">
                Add condition
              </button>
            }
          >
            {catalog ? (
              <RuleEditor node={entry} onChange={setEntry} catalog={catalog} depth={0} />
            ) : (
              <p className="text-2xs text-ink-faint">Loading the indicator catalog…</p>
            )}
          </Panel>

          <Panel title="Definition preview" subtitle="Exactly what gets validated, backtested and executed">
            <pre className="max-h-72 overflow-auto rounded border border-hairline bg-canvas p-3 font-mono text-2xs text-ink-dim">
              {JSON.stringify(definition, null, 2)}
            </pre>
          </Panel>
        </div>

        {/* ---------------- side ---------------- */}
        <div className="space-y-4">
          <Panel
            title="Validation"
            actions={validation ? (validation.valid ? <StatusChip status="live" label="valid" /> : <StatusChip status="error" label="invalid" />) : null}
          >
            {validation?.valid ? (
              <p className="text-2xs text-ink-dim">
                The strategy is well-formed. Backtest it before deploying it to paper or live.
              </p>
            ) : (
              <ul className="space-y-1 text-2xs text-down">
                {(validation?.errors ?? []).map((message) => (
                  <li key={message}>• {message}</li>
                ))}
              </ul>
            )}
            <button className="btn btn-primary mt-3 w-full" onClick={() => void save()} disabled={busy || !validation?.valid} type="button">
              {busy ? "Saving…" : "Save strategy"}
            </button>
            {saved ? <p className="mt-2 text-3xs text-up">Saved. Open Backtest to run it on recorded candles.</p> : null}
          </Panel>

          <Panel title="Templates" subtitle="Starting points, not recommendations">
            <ul className="space-y-1.5">
              {templates.map((template) => (
                <li key={String(template.id)}>
                  <button
                    className="w-full rounded border border-hairline px-2 py-2 text-left hover:bg-elevated"
                    onClick={() => applyTemplate(template)}
                    type="button"
                  >
                    <span className="block text-2xs font-medium">{String(template.name)}</span>
                    <span className="mt-0.5 block text-3xs text-ink-faint">
                      {String(template.description ?? "").slice(0, 110)}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          </Panel>

          <Panel title="Saved strategies" subtitle={`${mine.length} on this account`}>
            {mine.length ? (
              <ul className="space-y-1">
                {mine.map((strategy) => (
                  <li key={String(strategy.id)} className="flex items-center justify-between gap-2 text-2xs">
                    <span className="min-w-0 truncate">{String(strategy.name)}</span>
                    <span className="shrink-0 text-3xs text-ink-faint">{String(strategy.updated_at ?? "")}</span>
                  </li>
                ))}
              </ul>
            ) : (
              <Empty title="Nothing saved yet" message="Build a rule set above and save it." />
            )}
          </Panel>
        </div>
      </div>
    </div>
  );
}

function addChild(setter: React.Dispatch<React.SetStateAction<Node>>) {
  setter((current) =>
    current.kind === "group"
      ? { ...current, children: [...current.children, { ...DEFAULT_CONDITION, params: { period: 14 } }] }
      : current,
  );
}

/* ------------------------------------------------------------- rule editor */

function RuleEditor({
  node,
  onChange,
  catalog,
  depth,
}: {
  node: Node;
  onChange: (node: Node) => void;
  catalog: Catalog;
  depth: number;
}) {
  if (node.kind === "condition") {
    return <ConditionEditor condition={node} onChange={onChange} catalog={catalog} />;
  }

  return (
    <div
      className={`rounded border border-hairline ${depth > 0 ? "border-l-2 border-l-accent/50" : ""} ${
        depth > 0 ? "ml-3 pl-3" : ""
      } space-y-2`}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="label-caps">Match</span>
        <div className="flex gap-1">
          {(["all", "any", "not"] as const).map((logic) => (
            <button
              key={logic}
              className={`btn btn-sm ${node.logic === logic ? "btn-primary" : ""}`}
              onClick={() => onChange({ ...node, logic })}
              type="button"
            >
              {logic === "all" ? "AND" : logic === "any" ? "OR" : "NOT"}
            </button>
          ))}
        </div>
        {node.logic === "not" && node.children.length === 0 ? (
          <button className="btn btn-sm" onClick={() => onChange({ ...node, children: [{ ...DEFAULT_CONDITION }] })} type="button">
            Add inner condition
          </button>
        ) : null}
        {node.logic !== "not" ? (
          <button className="btn btn-sm" onClick={() => onChange({ ...node, children: [...node.children, { ...DEFAULT_CONDITION, params: { period: 14 } }] })} type="button">
            Add condition
          </button>
        ) : null}
      </div>
      {node.children.map((child, index) => (
        <div key={index} className="flex items-start gap-2">
          <div className="min-w-0 flex-1">
            <RuleEditor node={child} onChange={(updated) => {
              const children = [...node.children];
              children[index] = updated;
              onChange({ ...node, children });
            }} catalog={catalog} depth={depth + 1} />
          </div>
          {node.logic !== "not" ? (
            <button
              className="btn btn-sm btn-ghost"
              onClick={() => onChange({ ...node, children: node.children.filter((_, i) => i !== index) })}
              type="button"
              aria-label="Remove condition"
            >
              ✕
            </button>
          ) : null}
        </div>
      ))}
    </div>
  );
}

function ConditionEditor({
  condition,
  onChange,
  catalog,
}: {
  condition: Condition;
  onChange: (node: Node) => void;
  catalog: Catalog;
}) {
  const isCross = ["crosses_above", "crosses_below"].includes(condition.compare);
  const info = catalog.indicators.find((entry) => entry.name === condition.indicator);
  const options = [
    ...catalog.indicators.map((entry) => ({ value: entry.name, label: entry.name })),
    ...catalog.price_fields.map((value) => ({ value, label: `price.${value}` })),
  ];

  return (
    <div className="flex flex-wrap items-end gap-2 rounded border border-hairline bg-canvas/40 p-2">
      <label className="field min-w-[8rem] flex-1">
        <span className="field-label">Indicator</span>
        <select
          className="field-select"
          value={condition.indicator}
          onChange={(e) => onChange({ ...condition, indicator: e.target.value, field: "value" })}
        >
          {options.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </label>

      {info && info.outputs.includes(",") ? (
        <label className="field w-28">
          <span className="field-label">Output</span>
          <select className="field-select" value={condition.field} onChange={(e) => onChange({ ...condition, field: e.target.value })}>
            {info.outputs.split(",").map((output) => (
              <option key={output} value={output}>
                {output}
              </option>
            ))}
          </select>
        </label>
      ) : null}

      {PERIOD_KEYS.filter((key) => key === "period").map((key) => (
        <label key={key} className="field w-20">
          <span className="field-label">Period</span>
          <input
            className="field-input"
            type="number"
            min={1}
            value={condition.params.period ?? 14}
            onChange={(e) => onChange({ ...condition, params: { ...condition.params, period: Number(e.target.value) || 1 } })}
          />
        </label>
      ))}

      <label className="field w-36">
        <span className="field-label">Compare</span>
        <select className="field-select" value={condition.compare} onChange={(e) => onChange({ ...condition, compare: e.target.value })}>
          {[...catalog.comparators, ...catalog.cross_comparators].map((value) => (
            <option key={value} value={value}>
              {value}
            </option>
          ))}
        </select>
      </label>

      {isCross ? (
        <label className="field w-36">
          <span className="field-label">Against</span>
          <select
            className="field-select"
            value={condition.against?.indicator ?? catalog.indicators[0]?.name}
            onChange={(e) =>
              onChange({
                ...condition,
                against: { indicator: e.target.value, params: { period: 50 }, field: "value" },
              })
            }
          >
            {catalog.indicators.map((entry) => (
              <option key={entry.name} value={entry.name}>
                {entry.name}
              </option>
            ))}
          </select>
        </label>
      ) : (
        <label className="field w-28">
          <span className="field-label">Value</span>
          <input
            className="field-input"
            type="number"
            step="any"
            value={condition.value ?? 0}
            onChange={(e) => onChange({ ...condition, value: Number(e.target.value) })}
          />
        </label>
      )}
    </div>
  );
}

export default function StrategiesPage() {
  return (
    <AppShell>
      <StrategyBuilder />
    </AppShell>
  );
}

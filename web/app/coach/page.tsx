"use client";

/** AI trading coach: pattern detection over your own history, plus guidance. */

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { CoachResponse } from "@/lib/api";
import { AppShell } from "@/components/app-shell";
import { Empty, ErrorNote, Loading, Panel, StatusChip } from "@/components/ui";

type CoachPayload = CoachResponse;

function Coach() {
  const [mode, setMode] = useState<"loss_analysis" | "strategy_suggestions">("loss_analysis");
  const [data, setData] = useState<CoachPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setData(await api.coach(mode));
      setError(null);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Could not load the coach");
    } finally {
      setLoading(false);
    }
  }, [mode]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold tracking-tight">AI trading coach</h1>
          <p className="text-2xs text-ink-faint">Reviews your own recorded behaviour — never gives market calls</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {data && <StatusChip status={data.llm_enabled ? "live" : "idle"} label={data.llm_enabled ? "LLM narrative on" : "Deterministic only"} />}
          <div className="flex gap-1">
            {(["loss_analysis", "strategy_suggestions"] as const).map((key) => (
              <button key={key} className={`btn btn-sm ${mode === key ? "btn-primary" : ""}`} onClick={() => setMode(key)} type="button">
                {key === "loss_analysis" ? "Loss analysis" : "Strategy ideas"}
              </button>
            ))}
          </div>
        </div>
      </header>

      {error ? <ErrorNote error={error} onRetry={() => void load()} /> : null}
      {loading && !data ? <Loading label="Analysing your trade history" /> : null}

      {data && !data.has_data ? (
        <Empty
          title="Not enough history yet"
          message={data.note ?? "The coach analyses your own trades, so it needs some closed trades first."}
        />
      ) : null}

      {data?.has_data ? (
        <>
          <div className="rounded-md border border-warn/40 bg-warn-soft px-3 py-2 text-2xs leading-relaxed text-warn">
            {data.disclaimer}
          </div>

          {data.narrative ? (
            <Panel title="Coach's read" subtitle={data.llm_enabled ? "Generated from the findings below" : undefined}>
              <p className="whitespace-pre-wrap text-sm leading-relaxed text-ink-dim">{data.narrative}</p>
            </Panel>
          ) : null}

          {mode === "loss_analysis" ? (
            <Panel title="Patterns detected" subtitle={`Across ${data.analysed_trades ?? 0} closed trades`}>
              {data.findings.length ? (
                <div className="space-y-3">
                  {data.findings.map((finding) => (
                    <div key={finding.pattern} className="rounded border border-hairline p-3">
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <h3 className="text-xs font-semibold">{finding.title}</h3>
                        <span className="chip chip-warn">{finding.occurrences} occurrence{finding.occurrences === 1 ? "" : "s"}</span>
                      </div>
                      <p className="mt-2 text-2xs leading-relaxed text-ink-dim">{finding.recommendation}</p>
                    </div>
                  ))}
                </div>
              ) : (
                <Empty
                  title="No repeated pattern detected"
                  message="Nothing in your history stands out yet. Keep logging setups and outcomes — the coach refines as the sample grows."
                />
              )}
            </Panel>
          ) : (
            <Panel title="Suggested changes" subtitle="Derived from the patterns in your own history">
              <ul className="space-y-3">
                {(data.suggestions ?? []).map((suggestion, index) => (
                  <li key={index} className="rounded border border-hairline p-3">
                    <p className="text-2xs text-ink-faint">{suggestion.reason}</p>
                    <p className="mt-1 text-sm">{suggestion.change}</p>
                    {suggestion.rule ? (
                      <details className="mt-2">
                        <summary className="cursor-pointer text-3xs text-accent">Show the rule</summary>
                        <pre className="mt-1.5 overflow-auto rounded border border-hairline bg-canvas p-2 font-mono text-3xs">
                          {JSON.stringify(suggestion.rule, null, 2)}
                        </pre>
                      </details>
                    ) : null}
                    {suggestion.timeframe ? (
                      <p className="mt-1.5 text-3xs text-ink-faint">Suggested timeframe: {suggestion.timeframe}</p>
                    ) : null}
                  </li>
                ))}
              </ul>
            </Panel>
          )}

          <Panel title="How the coach works">
            <ul className="space-y-1.5 text-2xs leading-relaxed text-ink-faint">
              <li>
                <span className="text-ink-dim">Pattern detection is deterministic</span> — it runs here, on your
                records, with no external service. It looks for late entries, oversized losers, size increases
                right after a loss, overtrading, and stops wider than usual.
              </li>
              <li>
                <span className="text-ink-dim">The LLM layer is optional</span> — when OPENAI_API_KEY is set, the
                findings are turned into prose. The model receives numbers already computed above and has no
                broker access, so it cannot place a trade.
              </li>
              <li>
                <span className="text-ink-dim">It is not advice.</span> It reviews process, not market direction.
              </li>
            </ul>
          </Panel>
        </>
      ) : null}
    </div>
  );
}

export default function CoachPage() {
  return (
    <AppShell>
      <Coach />
    </AppShell>
  );
}

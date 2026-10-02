"use client";

/** Alerts: price, indicator and option-chain triggers with delivery channels. */

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { AlertRow } from "@/lib/api";
import { AppShell } from "@/components/app-shell";
import { Empty, ErrorNote, Field, Panel, StatusChip } from "@/components/ui";
import { num, relative } from "@/lib/format";

const CONDITION_TYPES = [
  { value: "above", label: "Price above", needs: "value" },
  { value: "below", label: "Price below", needs: "value" },
  { value: "crosses_above", label: "Crosses above", needs: "value" },
  { value: "crosses_below", label: "Crosses below", needs: "value" },
  { value: "percent_change", label: "Moves more than %", needs: "percent" },
  { value: "oi_change", label: "Open interest changes by %", needs: "percent" },
] as const;

const CHANNELS = ["telegram", "whatsapp", "email"] as const;

function Alerts() {
  const [alerts, setAlerts] = useState<AlertRow[]>([]);
  const [channels, setChannels] = useState<Record<string, boolean>>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  const [symbol, setSymbol] = useState("NIFTY");
  const [type, setType] = useState<string>("above");
  const [value, setValue] = useState(25000);
  const [selected, setSelected] = useState<string[]>([]);

  const load = useCallback(async () => {
    try {
      const payload = await api.alerts();
      setAlerts(payload.alerts);
      setChannels(payload.channels);
      setError(null);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Could not load alerts");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function create() {
    setBusy(true);
    try {
      const spec = CONDITION_TYPES.find((entry) => entry.value === type);
      const condition = spec?.needs === "percent" ? { type, percent: value } : { type, value };
      await api.createAlert({ kind: "price", symbol: symbol.toUpperCase(), condition, channels: selected });
      setNote("Alert created.");
      setError(null);
      await load();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Could not create the alert");
    } finally {
      setBusy(false);
    }
  }

  async function remove(id: string) {
    setBusy(true);
    try {
      await api.deleteAlert(id);
      await load();
    } finally {
      setBusy(false);
    }
  }

  async function test(id: string) {
    setBusy(true);
    setNote(null);
    try {
      const result = await api.testAlert(id);
      const summary = Object.entries(result.delivery)
        .map(([channel, outcome]) => `${channel}: ${outcome.status}`)
        .join(" · ");
      setNote(summary || "No channels configured for this alert.");
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Test failed");
    } finally {
      setBusy(false);
    }
  }

  const activeChannels = CHANNELS.filter((channel) => channels[channel]);

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold tracking-tight">Alerts</h1>
          <p className="text-2xs text-ink-faint">Price and open-interest triggers, delivered to your channels</p>
        </div>
        <button className="btn btn-sm" onClick={() => void load()} type="button">
          Refresh
        </button>
      </header>

      {error ? <ErrorNote error={error} onRetry={() => void load()} /> : null}
      {note ? <p className="rounded-md border border-accent/40 bg-accent-soft px-3 py-2 text-2xs text-accent">{note}</p> : null}

      <Panel title="Delivery channels" subtitle="Configured through the server environment">
        <div className="flex flex-wrap gap-2">
          {CHANNELS.map((channel) => (
            <span key={channel} className={`chip ${channels[channel] ? "chip-live" : "chip-idle"}`}>
              {channel} {channels[channel] ? "ready" : "not configured"}
            </span>
          ))}
        </div>
        {!activeChannels.length ? (
          <p className="mt-3 text-3xs leading-relaxed text-ink-faint">
            No channel is configured, so alerts will be created and evaluated but not delivered. Set the matching
            environment variables on the server (for example <code className="text-ink-dim">TELEGRAM_BOT_TOKEN</code> and{" "}
            <code className="text-ink-dim">TELEGRAM_CHAT_ID</code>).
          </p>
        ) : null}
      </Panel>

      <div className="grid gap-4 lg:grid-cols-3">
        <Panel title="New alert">
          <div className="space-y-3">
            <Field label="Symbol">
              <input className="field-input" value={symbol} onChange={(event) => setSymbol(event.target.value.toUpperCase())} />
            </Field>
            <Field label="Condition">
              <select className="field-select" value={type} onChange={(event) => setType(event.target.value)}>
                {CONDITION_TYPES.map((entry) => (
                  <option key={entry.value} value={entry.value}>
                    {entry.label}
                  </option>
                ))}
              </select>
            </Field>
            <Field label={CONDITION_TYPES.find((entry) => entry.value === type)?.needs === "percent" ? "Percent" : "Price"}>
              <input className="field-input" type="number" step="any" value={value} onChange={(event) => setValue(Number(event.target.value))} />
            </Field>
            <Field label="Channels">
              <div className="flex flex-wrap gap-1.5">
                {CHANNELS.map((channel) => (
                  <button
                    key={channel}
                    className={`btn btn-sm ${selected.includes(channel) ? "btn-primary" : ""}`}
                    onClick={() =>
                      setSelected((current) =>
                        current.includes(channel) ? current.filter((c) => c !== channel) : [...current, channel],
                      )
                    }
                    type="button"
                  >
                    {channel}
                  </button>
                ))}
              </div>
            </Field>
            <button
              className="btn btn-primary w-full"
              onClick={() => void create()}
              disabled={busy || !selected.length || !symbol}
              type="button"
            >
              {busy ? "Creating…" : "Create alert"}
            </button>
          </div>
        </Panel>

        <div className="lg:col-span-2">
          <Panel title="Active alerts" subtitle={`${alerts.length} configured`}>
            {alerts.length ? (
              <div className="space-y-2">
                {alerts.map((alert) => (
                  <div
                    key={alert.id}
                    className="flex flex-wrap items-center justify-between gap-2 rounded border border-hairline px-3 py-2"
                  >
                    <div className="min-w-0">
                      <p className="text-xs font-medium">
                        {alert.symbol ?? "—"} · {String(alert.condition.type)} {String(alert.condition.value ?? alert.condition.percent ?? "")}
                      </p>
                      <p className="mt-0.5 text-3xs text-ink-faint">
                        {alert.channels.join(", ") || "no channel"} · triggered {alert.trigger_count}×
                        {alert.triggered_at ? ` · last ${relative(alert.triggered_at)}` : ""}
                      </p>
                    </div>
                    <div className="flex gap-1.5">
                      <button className="btn btn-sm" onClick={() => void test(alert.id)} disabled={busy} type="button">
                        Test
                      </button>
                      <button className="btn btn-sm btn-ghost" onClick={() => void remove(alert.id)} disabled={busy} type="button">
                        Delete
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <Empty title="No alerts configured" message="Create one on the left to start receiving notifications." />
            )}
          </Panel>

          <Panel title="What gets evaluated">
            <ul className="space-y-1.5 text-2xs leading-relaxed text-ink-faint">
              <li>• Alerts are checked against the live quote cache as ticks arrive.</li>
              <li>• A cross needs both the current and previous value, so it fires once at the crossing.</li>
              <li>• Delivery failures are reported honestly — an alert never shows as sent when it was not.</li>
              <li>• Use <span className="text-ink-dim">Test</span> to verify a channel before relying on it.</li>
            </ul>
          </Panel>
        </div>
      </div>
    </div>
  );
}

export default function AlertsPage() {
  return (
    <AppShell>
      <Alerts />
    </AppShell>
  );
}

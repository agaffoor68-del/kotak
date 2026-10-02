"use client";

/** Risk controls: limits, live posture and the emergency kill switch. */

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { RiskStatus } from "@/lib/api";
import { AppShell } from "@/components/app-shell";
import { ErrorNote, Field, Metric, Modal, Panel, StatusChip } from "@/components/ui";
import { inr, num, percent, stamp } from "@/lib/format";

interface RiskEvent {
  id: string;
  kind: string;
  detail: string | null;
  created_at: number;
}

function RiskDesk() {
  const [risk, setRisk] = useState<RiskStatus | null>(null);
  const [events, setEvents] = useState<RiskEvent[]>([]);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<Record<string, number>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmKill, setConfirmKill] = useState(false);
  const [killReason, setKillReason] = useState("manual halt");
  const [runs, setRuns] = useState<Record<string, unknown>[]>([]);

  const load = useCallback(async () => {
    try {
      const [status, eventLog, algoRuns] = await Promise.all([
        api.risk(),
        api.riskEvents(30),
        api.algoRuns().catch(() => []),
      ]);
      setRisk(status);
      setEvents(eventLog as unknown as RiskEvent[]);
      setRuns(algoRuns);
      setError(null);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Could not load risk status");
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = setInterval(() => void load(), 20_000);
    return () => clearInterval(timer);
  }, [load]);

  async function saveLimits() {
    setBusy(true);
    try {
      // The PUT returns the stored config, not the full status object.
      await api.updateRisk(draft);
      setEditing(false);
      setError(null);
      await load();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Could not update limits");
    } finally {
      setBusy(false);
    }
  }

  async function toggleKillSwitch() {
    setBusy(true);
    try {
      if (risk?.kill_switch) {
        await api.releaseKillSwitch();
      } else {
        await api.killSwitch(killReason);
      }
      setConfirmKill(false);
      await load();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Kill switch action failed");
    } finally {
      setBusy(false);
    }
  }

  const config = risk?.config ?? {};
  const activeRuns = runs.filter((run) => run.status === "running");

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold tracking-tight">Risk management</h1>
          <p className="text-2xs text-ink-faint">
            Every order — manual, paper or algorithmic — passes through these controls first
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {risk?.trading_halted ? <StatusChip status="error" label="Trading halted" /> : <StatusChip status="live" label="Trading allowed" />}
          <button className="btn btn-sm" onClick={() => void load()} type="button">
            Refresh
          </button>
        </div>
      </header>

      {error ? <ErrorNote error={error} onRetry={() => void load()} /> : null}

      {risk?.halt_reason ? (
        <div className="rounded-md border border-down/50 bg-down-soft px-4 py-3">
          <p className="text-xs font-semibold text-down">Trading is halted</p>
          <p className="mt-1 text-2xs text-down/90">{risk.halt_reason}</p>
        </div>
      ) : null}

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Metric label="Account equity" value={inr(risk?.equity)} />
        <Metric
          label="Current drawdown"
          value={inr(risk?.current_drawdown)}
          sub={`${num(risk?.drawdown_percent)}% below peak`}
          tone={(risk?.current_drawdown ?? 0) > 0 ? "down" : "flat"}
        />
        <Metric
          label="Daily loss remaining"
          value={risk?.daily_loss_remaining === null || risk?.daily_loss_remaining === undefined ? "unlimited" : inr(risk.daily_loss_remaining)}
          sub={`realised today ${inr(risk?.realised_today)}`}
          tone={risk?.daily_limit_breached ? "down" : "flat"}
        />
        <Metric label="Open positions" value={num(risk?.open_positions)} sub={`${num(risk?.orders_last_minute)} orders in the last minute`} />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Panel
          title="Limits"
          subtitle="Loss and exposure caps enforced before every order"
          actions={
            <button
              className="btn btn-sm"
              onClick={() => {
                setDraft({
                  max_daily_loss: config.max_daily_loss ?? 0,
                  max_drawdown: config.max_drawdown ?? 0,
                  max_position_pct: (config.max_position_pct ?? 0) * 100,
                  max_positions: config.max_positions ?? 0,
                  max_orders_per_minute: config.max_orders_per_minute ?? 0,
                  max_exposure_pct: (config.max_exposure_pct ?? 0) * 100,
                });
                setEditing((value) => !value);
              }}
              type="button"
            >
              {editing ? "Cancel" : "Edit limits"}
            </button>
          }
        >
          {editing ? (
            <div className="space-y-3">
              <div className="grid gap-3 sm:grid-cols-2">
                <Field label="Max daily loss (₹)">
                  <input
                    className="field-input"
                    type="number"
                    min={0}
                    value={draft.max_daily_loss ?? 0}
                    onChange={(e) => setDraft({ ...draft, max_daily_loss: Number(e.target.value) })}
                  />
                </Field>
                <Field label="Max drawdown (₹)">
                  <input
                    className="field-input"
                    type="number"
                    min={0}
                    value={draft.max_drawdown ?? 0}
                    onChange={(e) => setDraft({ ...draft, max_drawdown: Number(e.target.value) })}
                  />
                </Field>
                <Field label="Max position (% of equity)">
                  <input
                    className="field-input"
                    type="number"
                    min={0}
                    max={100}
                    step={1}
                    value={draft.max_position_pct ?? 0}
                    onChange={(e) => setDraft({ ...draft, max_position_pct: Number(e.target.value) })}
                  />
                </Field>
                <Field label="Max total exposure (% of equity)">
                  <input
                    className="field-input"
                    type="number"
                    min={0}
                    max={100}
                    step={1}
                    value={draft.max_exposure_pct ?? 0}
                    onChange={(e) => setDraft({ ...draft, max_exposure_pct: Number(e.target.value) })}
                  />
                </Field>
                <Field label="Max open positions">
                  <input
                    className="field-input"
                    type="number"
                    min={0}
                    value={draft.max_positions ?? 0}
                    onChange={(e) => setDraft({ ...draft, max_positions: Number(e.target.value) })}
                  />
                </Field>
                <Field label="Max orders per minute">
                  <input
                    className="field-input"
                    type="number"
                    min={0}
                    value={draft.max_orders_per_minute ?? 0}
                    onChange={(e) => setDraft({ ...draft, max_orders_per_minute: Number(e.target.value) })}
                  />
                </Field>
              </div>
              <p className="text-3xs text-ink-faint">
                Percentage caps are stored as fractions. A limit of 0 means unlimited, except the loss and
                drawdown stops, which are what protect the account.
              </p>
              <button className="btn btn-primary w-full" onClick={() => void saveLimits()} disabled={busy} type="button">
                {busy ? "Saving…" : "Save limits"}
              </button>
            </div>
          ) : (
            <dl className="space-y-2.5 text-2xs">
              <Row label="Max daily loss" value={inr(config.max_daily_loss)} />
              <Row label="Max drawdown" value={inr(config.max_drawdown)} />
              <Row label="Max position size" value={percent((config.max_position_pct ?? 0) * 100, 1)} />
              <Row label="Max total exposure" value={percent((config.max_exposure_pct ?? 0) * 100, 1)} />
              <Row label="Max open positions" value={num(config.max_positions, 0)} />
              <Row label="Max orders per minute" value={num(config.max_orders_per_minute, 0)} />
            </dl>
          )}
        </Panel>

        <div className="space-y-4">
          <Panel
            title="Emergency kill switch"
            subtitle="Stops every new order immediately and halts running strategies"
            className={risk?.kill_switch ? "border-down/50" : "border-warn/40"}
          >
            <p className="text-2xs leading-relaxed text-ink-dim">
              {risk?.kill_switch
                ? "The kill switch is engaged. No order can be sent until an operator releases it."
                : "Engage this to block all order flow instantly. It is one-way: releasing it is a deliberate operator action."}
            </p>
            {risk?.kill_switch ? (
              <button className="btn mt-3 w-full" onClick={() => void toggleKillSwitch()} disabled={busy} type="button">
                {busy ? "Working…" : "Release kill switch"}
              </button>
            ) : (
              <div className="mt-3 space-y-2">
                <Field label="Reason (recorded in the event log)">
                  <input className="field-input" value={killReason} onChange={(e) => setKillReason(e.target.value)} />
                </Field>
                <button className="btn btn-danger w-full" onClick={() => setConfirmKill(true)} type="button">
                  Engage kill switch
                </button>
              </div>
            )}
          </Panel>

          <Panel title="Running strategies" subtitle="Auto-halted when the kill switch is engaged">
            {activeRuns.length ? (
              <ul className="space-y-1.5">
                {activeRuns.map((run) => (
                  <li key={String(run.id)} className="flex items-center justify-between gap-2 rounded border border-hairline px-2 py-1.5 text-2xs">
                    <span className="min-w-0 truncate">
                      {String(run.name)} · <span className="text-ink-faint">{String(run.mode)}</span>
                    </span>
                    <StatusChip status="live" label={String(run.status)} />
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-2xs text-ink-faint">No strategies are currently running.</p>
            )}
          </Panel>
        </div>
      </div>

      <Panel title="Risk event log">
        {events.length ? (
          <ul className="space-y-1.5">
            {events.map((event) => (
              <li key={event.id} className="flex flex-wrap items-baseline justify-between gap-2 border-b border-hairline/60 pb-1.5 text-2xs last:border-0">
                <span className="text-ink">{event.kind.replace(/_/g, " ").toLowerCase()}</span>
                <span className="text-ink-faint">{event.detail ?? ""}</span>
                <span className="num text-3xs text-ink-faint">{stamp(event.created_at)}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-2xs text-ink-faint">No risk events recorded yet.</p>
        )}
      </Panel>

      <Modal
        open={confirmKill}
        title="Engage the emergency kill switch?"
        onClose={() => setConfirmKill(false)}
        footer={
          <>
            <button className="btn" onClick={() => setConfirmKill(false)} type="button">
              Cancel
            </button>
            <button className="btn btn-danger" onClick={() => void toggleKillSwitch()} disabled={busy} type="button">
              {busy ? "Working…" : "Halt all trading"}
            </button>
          </>
        }
      >
        <p className="text-2xs leading-relaxed text-ink-dim">
          Every new order will be refused, and {activeRuns.length} running strateg{activeRuns.length === 1 ? "y" : "ies"} will
          be stopped. Existing positions are not closed — cancel them deliberately if that is what you want.
        </p>
        <p className="text-2xs text-ink-faint">Reason: {killReason}</p>
      </Modal>
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between gap-3 border-b border-hairline/60 pb-2 last:border-0">
      <dt className="text-ink-faint">{label}</dt>
      <dd className="num text-ink">{value}</dd>
    </div>
  );
}

export default function RiskPage() {
  return (
    <AppShell>
      <RiskDesk />
    </AppShell>
  );
}

"use client";

import { useMemo, useState } from "react";
import { api } from "@/lib/api";
import type { OptionChain } from "@/lib/api";
import { Field, Panel } from "@/components/ui";

export type StrikePreset = "ATM" | "ITM-1" | "ITM-2" | "OTM-1" | "OTM-2";
const STRIKES: StrikePreset[] = ["ATM", "ITM-1", "ITM-2", "OTM-1", "OTM-2"];

type Choice = { value: string; label: string; tone?: "up" | "down" };

function Toggle({ value, options, onChange, label }: { value: string; options: Choice[]; onChange: (value: string) => void; label: string }) {
  return (
    <div className="flex rounded-md border border-hairline bg-canvas p-0.5" role="group" aria-label={label}>
      {options.map((option) => {
        const active = option.value === value;
        const tone = active ? (option.tone === "up" ? "bg-up/20 text-up" : option.tone === "down" ? "bg-down/20 text-down" : "bg-accent/20 text-accent") : "text-ink-dim hover:text-ink";
        return (
          <button key={option.value} type="button" aria-pressed={active} onClick={() => onChange(option.value)} className={`flex-1 rounded px-2 py-2 text-2xs font-semibold transition-colors ${tone}`}>
            {option.label}
          </button>
        );
      })}
    </div>
  );
}

export function OptionTradePanel({
  chain,
  underlying,
  expiry,
  expiries,
  onUnderlyingChange,
  onExpiryChange,
}: {
  chain: OptionChain;
  underlying: string;
  expiry: string;
  expiries: string[];
  onUnderlyingChange: (value: string) => void;
  onExpiryChange: (value: string) => void;
}) {
  const [strike, setStrike] = useState<StrikePreset>("ATM");
  const [type, setType] = useState<"CE" | "PE">("CE");
  const [action, setAction] = useState<"BUY" | "SELL">("BUY");
  const [lots, setLots] = useState(1);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ tone: "up" | "down"; text: string } | null>(null);

  const resolvedStrike = useMemo(() => {
    const strikes = chain.rows.map((row) => row.strike).sort((a, b) => a - b);
    const atm = chain.summary.atm_strike ?? chain.spot ?? strikes[Math.floor(strikes.length / 2)];
    const atmIndex = strikes.reduce((best, value, index) => Math.abs(value - atm) < Math.abs(strikes[best] - atm) ? index : best, 0);
    const distance = strike === "ITM-1" || strike === "OTM-1" ? 1 : strike === "ITM-2" || strike === "OTM-2" ? 2 : 0;
    const direction = type === "CE" ? (strike.startsWith("ITM") ? -1 : 1) : (strike.startsWith("ITM") ? 1 : -1);
    return strikes[Math.max(0, Math.min(strikes.length - 1, atmIndex + direction * distance))] ?? null;
  }, [chain.rows, chain.summary.atm_strike, chain.spot, strike, type]);

  async function execute() {
    if (!expiry || !resolvedStrike || lots < 1) {
      setMessage({ tone: "down", text: "Select an expiry and enter at least one lot." });
      return;
    }
    setBusy(true);
    setMessage(null);
    try {
      await api.executeOption({ underlying, expiry, strike, type, action, lots });
      setMessage({ tone: "up", text: `${action} ${lots} lot ${underlying} ${strike} ${type} order sent.` });
    } catch (failure) {
      setMessage({ tone: "down", text: failure instanceof Error ? failure.message : "The order could not be sent." });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Panel title="Options trading ticket" subtitle="Send a human-readable paper order to the execution API">
      <div className="flex flex-col gap-4">
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Underlying">
            <select className="field-select" value={underlying} onChange={(event) => onUnderlyingChange(event.target.value)}>
              <option value="NIFTY">NIFTY 50</option>
              <option value="BANKNIFTY">BANKNIFTY</option>
            </select>
          </Field>
          <Field label="Expiry">
            <select className="field-select" value={expiry} onChange={(event) => onExpiryChange(event.target.value)} disabled={!expiries.length}>
              {expiries.length ? expiries.map((value) => <option key={value} value={value}>{value}</option>) : <option value="">Nearest expiry</option>}
            </select>
          </Field>
        </div>

        <Field label="Strike" hint={resolvedStrike ? `Resolves to ${resolvedStrike}` : "No strike available"}>
          <div className="grid grid-cols-5 gap-1.5" role="group" aria-label="Strike selector">
            {STRIKES.map((value) => <button key={value} type="button" aria-pressed={strike === value} onClick={() => setStrike(value)} className={`rounded border px-1 py-2 text-2xs font-semibold transition-colors ${strike === value ? "border-accent bg-accent/20 text-accent" : "border-hairline bg-canvas text-ink-dim hover:text-ink"}`}>{value}</button>)}
          </div>
        </Field>

        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Option type"><Toggle label="Option type" value={type} onChange={(value) => setType(value as "CE" | "PE")} options={[{ value: "CE", label: "CE (Call)" }, { value: "PE", label: "PE (Put)" }]} /></Field>
          <Field label="Position type"><Toggle label="Position type" value={action} onChange={(value) => setAction(value as "BUY" | "SELL")} options={[{ value: "BUY", label: "BUY", tone: "up" }, { value: "SELL", label: "SELL", tone: "down" }]} /></Field>
        </div>

        <Field label="Quantity (lots)">
          <input className="field-input" type="number" min={1} step={1} value={lots} onChange={(event) => setLots(Math.max(1, Number(event.target.value) || 1))} />
        </Field>

        <div className="rounded-md border border-hairline bg-canvas/60 px-3 py-2.5 text-2xs">
          <div className="flex items-center justify-between gap-3"><span className="text-ink-faint">Order preview</span><span className="num font-semibold text-ink">{action} · {underlying} · {strike} · {type}</span></div>
          <div className="mt-1 flex items-center justify-between gap-3"><span className="text-ink-faint">Expiry / quantity</span><span className="num text-ink">{expiry || "—"} · {lots} lot{lots === 1 ? "" : "s"}</span></div>
        </div>

        {message ? <p className={`text-2xs ${message.tone === "up" ? "text-up" : "text-down"}`} role="status">{message.text}</p> : null}
        <button className={`btn w-full ${action === "BUY" ? "btn-buy" : "btn-sell"}`} type="button" onClick={() => void execute()} disabled={busy || !expiry || !resolvedStrike}>
          {busy ? "Sending…" : "EXECUTE OPTION ORDER"}
        </button>
      </div>
    </Panel>
  );
}

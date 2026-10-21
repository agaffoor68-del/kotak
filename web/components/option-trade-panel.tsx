"use client";

/**
 * Options trade execution panel.
 *
 * The operator picks a side, a leg and a strike, sizes the position in lots
 * and sends it. The panel shows what will be sent — contract, premium,
 * quantity, charges, target and stop — before the order leaves, and it never
 * invents a price: everything comes from the live chain.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import type { OptionChain } from "@/lib/api";
import { Field, Panel } from "@/components/ui";
import { inr, num } from "@/lib/format";

export type PositionSide = "long" | "short";
export type OptionKind = "CE" | "PE" | "BOTH";
export type StrikePreset = "ATM" | "ITM-1" | "ITM-2" | "OTM-1" | "OTM-2";

const PRESETS: StrikePreset[] = ["ATM", "ITM-1", "ITM-2", "OTM-1", "OTM-2"];

const LIVE_ORDER_PHRASE = "LIVE";

interface Leg {
  kind: "call" | "put";
  strike: number;
  token?: string;
  trading_symbol?: string;
  ltp: number | null;
  bid: number | null;
  ask: number | null;
  lot_size: number | null;
}

function Toggle<T extends string>({
  value,
  options,
  onChange,
  name,
}: {
  value: T;
  options: { value: T; label: string; tone?: "up" | "down" }[];
  onChange: (next: T) => void;
  name: string;
}) {
  return (
    <div className="flex rounded-md border border-hairline bg-canvas p-0.5" role="group" aria-label={name}>
      {options.map((option) => {
        const active = option.value === value;
        const tone = active
          ? option.tone === "up"
            ? "bg-up/20 text-up"
            : option.tone === "down"
              ? "bg-down/20 text-down"
              : "bg-accent/20 text-accent"
          : "text-ink-dim hover:text-ink";
        return (
          <button
            key={option.value}
            type="button"
            onClick={() => onChange(option.value)}
            className={`flex-1 rounded px-2 py-1.5 text-2xs font-semibold transition-colors ${tone}`}
            aria-pressed={active}
          >
            {option.label}
          </button>
        );
      })}
    </div>
  );
}

export function OptionTradePanel({
  chain,
  spot,
  selectedStrike,
  onSelectStrike,
  liveTradingEnabled,
}: {
  chain: OptionChain;
  spot: number | null;
  selectedStrike: number | null;
  onSelectStrike: (strike: number) => void;
  liveTradingEnabled: boolean;
}) {
  const [side, setSide] = useState<PositionSide>("long");
  const [kind, setKind] = useState<OptionKind>("CE");
  const [preset, setPreset] = useState<StrikePreset>("ATM");
  const [lots, setLots] = useState(1);
  const [orderType, setOrderType] = useState<"MKT" | "L">("MKT");
  const [limitPrice, setLimitPrice] = useState(0);
  const [takeProfitPct, setTakeProfitPct] = useState(10);
  const [stopLossPct, setStopLossPct] = useState(5);
  const [mode, setMode] = useState<"paper" | "live">("paper");
  const [confirmation, setConfirmation] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ tone: "up" | "down"; text: string } | null>(null);

  const strikes = useMemo(() => chain.rows.map((row) => row.strike).sort((a, b) => a - b), [chain.rows]);

  /** The index of the strike closest to the money. */
  const atmIndex = useMemo(() => {
    if (!strikes.length) return -1;
    const atm = chain.summary.atm_strike;
    if (atm !== null) {
      const exact = strikes.findIndex((strike) => Math.abs(strike - atm) < 1e-9);
      if (exact >= 0) return exact;
    }
    if (spot === null) return Math.floor(strikes.length / 2);
    let best = 0;
    let distance = Math.abs(strikes[0] - spot);
    strikes.forEach((strike, index) => {
      const current = Math.abs(strike - spot);
      if (current < distance) {
        distance = current;
        best = index;
      }
    });
    return best;
  }, [strikes, chain.summary.atm_strike, spot]);

  /** The legs the current selection resolves to. */
  const legs = useMemo<Leg[]>(() => {
    if (atmIndex < 0) return [];
    const step = (index: number) => strikes[Math.min(strikes.length - 1, Math.max(0, index))];
    const rowFor = (strike: number) => chain.rows.find((row) => Math.abs(row.strike - strike) < 1e-9);
    const toLeg = (kind: "call" | "put", strike: number): Leg | null => {
      const row = rowFor(strike);
      if (!row) return null;
      const source = kind === "call" ? row.call : row.put;
      return {
        kind,
        strike,
        token: source.token,
        trading_symbol: source.trading_symbol,
        ltp: source.ltp,
        bid: source.bid,
        ask: source.ask,
        lot_size: source.lot_size ?? null,
      };
    };

    const distance = preset === "ITM-1" || preset === "OTM-1" ? 1 : preset === "ITM-2" || preset === "OTM-2" ? 2 : 0;
    const inTheMoney = preset === "ATM" || preset.startsWith("ITM");

    if (kind === "BOTH") {
      // A straddle at the money, a strangle away from it: the call sits on one
      // side of the money and the put on the other.
      const callIndex = inTheMoney ? atmIndex - distance : atmIndex + distance;
      const putIndex = inTheMoney ? atmIndex + distance : atmIndex - distance;
      const call = toLeg("call", step(callIndex));
      const put = toLeg("put", step(putIndex));
      return [call, put].filter((leg): leg is Leg => leg !== null);
    }

    const direction = kind === "CE" ? (inTheMoney ? -1 : 1) : inTheMoney ? 1 : -1;
    const leg = toLeg(kind === "CE" ? "call" : "put", step(atmIndex + direction * distance));
    return leg ? [leg] : [];
  }, [atmIndex, chain.rows, kind, preset, strikes]);

  // Keep the chain selection in step with the panel.
  useEffect(() => {
    if (!legs.length) return;
    const first = legs[0].strike;
    if (selectedStrike === null || Math.abs(selectedStrike - first) > 1e-9) {
      onSelectStrike(first);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [legs]);

  const lotSize = legs[0]?.lot_size ?? null;
  const totalQuantity = Math.max(0, Math.round(lots * (lotSize ?? 1)));
  const perLegQuantity = kind === "BOTH" ? Math.floor(totalQuantity / 2) : totalQuantity;
  const referencePrice = legs[0]?.ltp ?? null;
  const estimatedCharges = useRefCharges(perLegQuantity, referencePrice);

  const target = useMemo(() => {
    if (!referencePrice) return null;
    const factor = side === "long" ? 1 + takeProfitPct / 100 : 1 - takeProfitPct / 100;
    return referencePrice * factor;
  }, [referencePrice, side, takeProfitPct]);

  const stop = useMemo(() => {
    if (!referencePrice) return null;
    const factor = side === "long" ? 1 - stopLossPct / 100 : 1 + stopLossPct / 100;
    return referencePrice * factor;
  }, [referencePrice, side, stopLossPct]);

  const execute = useCallback(async () => {
    if (!legs.length || !perLegQuantity) {
      setMessage({ tone: "down", text: "Pick a strike with a live price before sending the order." });
      return;
    }
    setBusy(true);
    setMessage(null);
    try {
      const placed: string[] = [];
      for (const leg of legs) {
        const quantity = kind === "BOTH" ? perLegQuantity : totalQuantity;
        const price = orderType === "L" ? limitPrice : leg.ltp ?? 0;
        const payload = {
          exchange_segment: "nse_fo",
          product: "NRML",
          trading_symbol: leg.trading_symbol ?? "",
          transaction_type: side === "long" ? "B" : "S",
          order_type: orderType,
          quantity,
          price: orderType === "L" ? price : 0,
          trigger_price: 0,
          validity: "DAY",
          instrument_token: leg.token ?? "",
          // The exit levels travel with the position.
          take_profit_pct: takeProfitPct,
          stop_loss_pct: stopLossPct,
          // A live order is only unlocked by typing the phrase.
          confirmation: mode === "live" ? confirmation : "",
        };
        if (mode === "paper") {
          await api.paperOrder(payload);
        } else {
          await api.liveOrder(payload);
        }
        placed.push(`${leg.kind === "call" ? "CE" : "PE"} ${num(leg.strike, 0)}`);
      }
      setMessage({
        tone: "up",
        text: `${mode === "paper" ? "Paper" : "Live"} order sent: ${placed.join(" + ")} · ${perLegQuantity * legs.length} units.`,
      });
    } catch (failure) {
      setMessage({
        tone: "down",
        text: failure instanceof Error ? failure.message : "The order could not be sent.",
      });
    } finally {
      setBusy(false);
    }
  }, [legs, kind, limitPrice, mode, orderType, perLegQuantity, side, stopLossPct, takeProfitPct, totalQuantity, confirmation]);

  return (
    <Panel
      title="Trade execution"
      subtitle="Build the position, then send it"
      actions={
        <Toggle
          name="Execution mode"
          value={mode}
          onChange={setMode}
          options={[
            { value: "paper", label: "Paper" },
            { value: "live", label: "Live", tone: "down" },
          ]}
        />
      }
    >
      <div className="space-y-3">
        <Field label="Position type">
          <Toggle
            name="Position type"
            value={side}
            onChange={setSide}
            options={[
              { value: "long", label: "Long (Buy)", tone: "up" },
              { value: "short", label: "Short (Sell)", tone: "down" },
            ]}
          />
        </Field>

        <Field label="Option type">
          <Toggle
            name="Option type"
            value={kind}
            onChange={setKind}
            options={[
              { value: "CE", label: "Call (CE)" },
              { value: "PE", label: "Put (PE)" },
              { value: "BOTH", label: "Both" },
            ]}
          />
        </Field>

        <Field label="Strike" hint={legs.length ? `Resolves to ${legs.map((leg) => num(leg.strike, 0)).join(" / ")}` : undefined}>
          <select
            className="field-select"
            value={preset}
            onChange={(event) => setPreset(event.target.value as StrikePreset)}
          >
            {PRESETS.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </Field>

        <div className="grid grid-cols-2 gap-3">
          <Field label="Quantity (lots)">
            <input
              className="field-input"
              type="number"
              min={1}
              step={1}
              value={lots}
              onChange={(event) => setLots(Math.max(1, Number(event.target.value) || 1))}
            />
          </Field>
          <Field label="Order type">
            <select
              className="field-select"
              value={orderType}
              onChange={(event) => setOrderType(event.target.value as "MKT" | "L")}
            >
              <option value="MKT">Market</option>
              <option value="L">Limit</option>
            </select>
          </Field>
        </div>

        {orderType === "L" ? (
          <Field label="Limit price">
            <input
              className="field-input"
              type="number"
              min={0}
              step={0.05}
              value={limitPrice}
              onChange={(event) => setLimitPrice(Number(event.target.value) || 0)}
            />
          </Field>
        ) : null}

        <div className="grid grid-cols-2 gap-3">
          <Field label="Take profit (%)">
            <input
              className="field-input"
              type="number"
              min={0}
              step={0.5}
              value={takeProfitPct}
              onChange={(event) => setTakeProfitPct(Number(event.target.value) || 0)}
            />
          </Field>
          <Field label="Stop loss (%)">
            <input
              className="field-input"
              type="number"
              min={0}
              step={0.5}
              value={stopLossPct}
              onChange={(event) => setStopLossPct(Number(event.target.value) || 0)}
            />
          </Field>
        </div>

        <dl className="space-y-1.5 rounded-md border border-hairline bg-canvas/60 px-3 py-2.5 text-2xs">
          <Summary label="Contracts" value={legs.map((leg) => leg.trading_symbol ?? "—").join("  ·  ") || "—"} />
          <Summary label="Premium (LTP)" value={referencePrice === null ? "—" : num(referencePrice)} />
          <Summary label="Quantity" value={`${perLegQuantity * legs.length} units${lotSize ? ` (${lots} × ${lotSize})` : ""}`} />
          <Summary label="Target / stop" value={`${target === null ? "—" : num(target)} / ${stop === null ? "—" : num(stop)}`} />
          <Summary label="Est. charges (round trip)" value={estimatedCharges === null ? "—" : inr(estimatedCharges)} />
        </dl>

        {mode === "live" && !liveTradingEnabled ? (
          <p className="rounded border border-warn/40 bg-warn-soft px-3 py-2 text-2xs text-warn">
            Live trading is disabled on this deployment. Switch to Paper, or enable live trading in the settings.
          </p>
        ) : null}

        {mode === "live" ? (
          <Field label="Confirm the live order" hint={`Type "${LIVE_ORDER_PHRASE}" to unlock it`}>
            <input
              className="field-input"
              value={confirmation}
              placeholder={LIVE_ORDER_PHRASE}
              onChange={(event) => setConfirmation(event.target.value)}
            />
          </Field>
        ) : null}

        {message ? (
          <p className={`text-2xs ${message.tone === "up" ? "text-up" : "text-down"}`} role="status">
            {message.text}
          </p>
        ) : null}

        <button
          className={`btn w-full ${mode === "live" ? "btn-danger" : "btn-primary"}`}
          type="button"
          onClick={() => void execute()}
          disabled={
            busy ||
            !legs.length ||
            !perLegQuantity ||
            (mode === "live" && (!liveTradingEnabled || confirmation !== LIVE_ORDER_PHRASE))
          }
        >
          {busy ? <span className="h-3 w-3 animate-spin rounded-full border-2 border-edge border-t-canvas" /> : null}
          {busy ? "Sending…" : mode === "live" ? "EXECUTE ORDER · LIVE" : "EXECUTE ORDER · PAPER"}
        </button>
      </div>
    </Panel>
  );
}

function Summary({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-start justify-between gap-3">
      <dt className="text-ink-faint">{label}</dt>
      <dd className="num text-right text-ink">{value}</dd>
    </div>
  );
}

/** The statutory charges for the prospective order, for the summary line. */
function useRefCharges(quantity: number, price: number | null) {
  const [charges, setCharges] = useState<number | null>(null);

  useEffect(() => {
    if (!quantity || !price) {
      setCharges(null);
      return;
    }
    let cancelled = false;
    api
      .charges("nse_fo", "NRML", quantity, price, true)
      .then((payload) => {
        if (!cancelled) setCharges(payload.round_trip);
      })
      .catch(() => {
        if (!cancelled) setCharges(null);
      });
    return () => {
      cancelled = true;
    };
  }, [quantity, price]);

  return charges;
}

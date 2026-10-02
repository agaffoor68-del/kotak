"use client";

/** Order ticket: live Kotak Neo routing and paper simulation, side by side. */

import { useCallback, useEffect, useMemo, useState } from "react";
import { api, LIVE_PHRASE } from "@/lib/api";
import { AppShell } from "@/components/app-shell";
import { ErrorNote, Field, Modal, Panel } from "@/components/ui";
import { inr, num, percent } from "@/lib/format";

const SEGMENTS = [
  { value: "nse_cm", label: "NSE cash" },
  { value: "bse_cm", label: "BSE cash" },
  { value: "nse_fo", label: "NSE F&O" },
  { value: "bse_fo", label: "BSE F&O" },
  { value: "cde_fo", label: "Currency" },
  { value: "mcx_fo", label: "Commodity" },
];

const PRODUCTS = ["MIS", "CNC", "NRML"];
const ORDER_TYPES = [
  { value: "MKT", label: "Market" },
  { value: "L", label: "Limit" },
  { value: "SL", label: "Stop loss limit" },
  { value: "SL-M", label: "Stop loss market" },
];

const needsPrice = (type: string) => type === "L" || type === "SL";
const needsTrigger = (type: string) => type === "SL" || type === "SL-M";

interface Ticket {
  exchange_segment: string;
  product: string;
  trading_symbol: string;
  transaction_type: "B" | "S";
  order_type: string;
  quantity: number;
  price: number;
  trigger_price: number;
  validity: string;
  instrument_token?: string;
  equity?: number;
}

function TradeDesk() {
  const [ticket, setTicket] = useState<Ticket>({
    exchange_segment: "nse_cm",
    product: "MIS",
    trading_symbol: "",
    transaction_type: "B",
    order_type: "L",
    quantity: 1,
    price: 0,
    trigger_price: 0,
    validity: "DAY",
  });
  const [equity, setEquity] = useState(500000);
  const [charges, setCharges] = useState<{ round_trip: number; break_even_move_percent: number | null } | null>(null);
  const [result, setResult] = useState<{ tone: "ok" | "err"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [phrase, setPhrase] = useState("");

  const set = <K extends keyof Ticket>(key: K, value: Ticket[K]) =>
    setTicket((current) => ({ ...current, [key]: value }));

  const notional = useMemo(() => ticket.quantity * (ticket.price || 0), [ticket]);

  // Indian charges preview, refreshed as the ticket changes.
  useEffect(() => {
    if (!ticket.price || !ticket.quantity) {
      setCharges(null);
      return;
    }
    const timer = setTimeout(() => {
      api
        .charges(ticket.exchange_segment, ticket.product, ticket.quantity, ticket.price)
        .then(setCharges)
        .catch(() => setCharges(null));
    }, 400);
    return () => clearTimeout(timer);
  }, [ticket.exchange_segment, ticket.product, ticket.quantity, ticket.price]);

  const submit = useCallback(
    async (mode: "live" | "paper") => {
      if (mode === "live") {
        setConfirming(true);
        return;
      }
      setBusy(true);
      setResult(null);
      try {
        const response = await api.paperOrder({ ...ticket, equity });
        setResult({ tone: "ok", text: `Paper fill: ${JSON.stringify(response, null, 2)}` });
        setError(null);
      } catch (failure) {
        setResult({ tone: "err", text: failure instanceof Error ? failure.message : "Paper order failed" });
      } finally {
        setBusy(false);
      }
    },
    [ticket, equity],
  );

  const sendLive = useCallback(async () => {
    setBusy(true);
    setResult(null);
    try {
      const response = await api.liveOrder({ ...ticket, equity, confirmation: LIVE_PHRASE });
      setResult({ tone: "ok", text: `Submitted to Kotak Neo:\n${JSON.stringify(response, null, 2)}` });
      setError(null);
      setConfirming(false);
      setPhrase("");
    } catch (failure) {
      setResult({ tone: "err", text: failure instanceof Error ? failure.message : "Live order failed" });
    } finally {
      setBusy(false);
    }
  }, [ticket, equity]);

  return (
    <div className="space-y-4">
      <header>
        <h1 className="text-lg font-semibold tracking-tight">Order ticket</h1>
        <p className="text-2xs text-ink-faint">
          Real orders route to Kotak Neo and the exchange. Paper orders fill against the same live quotes.
        </p>
      </header>

      {error ? <ErrorNote error={error} /> : null}

      <div className="grid gap-4 lg:grid-cols-3">
        <div className="space-y-4 lg:col-span-2">
          <Panel title="Order details">
            <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
              <Field label="Exchange">
                <select className="field-select" value={ticket.exchange_segment} onChange={(e) => set("exchange_segment", e.target.value)}>
                  {SEGMENTS.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="Product">
                <select className="field-select" value={ticket.product} onChange={(e) => set("product", e.target.value)}>
                  {PRODUCTS.map((value) => (
                    <option key={value} value={value}>
                      {value}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="Order type">
                <select className="field-select" value={ticket.order_type} onChange={(e) => set("order_type", e.target.value)}>
                  {ORDER_TYPES.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="Trading symbol" hint="e.g. RELIANCE-EQ">
                <input
                  className="field-input"
                  placeholder="RELIANCE-EQ"
                  value={ticket.trading_symbol}
                  onChange={(e) => set("trading_symbol", e.target.value.toUpperCase())}
                />
              </Field>
              <Field label="Quantity">
                <input
                  className="field-input"
                  type="number"
                  min={1}
                  step={1}
                  value={ticket.quantity}
                  onChange={(e) => set("quantity", Math.max(1, Number(e.target.value) || 1))}
                />
              </Field>
              <Field label="Price" hint={needsPrice(ticket.order_type) ? undefined : "market order — not used"}>
                <input
                  className="field-input"
                  type="number"
                  min={0}
                  step={0.05}
                  disabled={!needsPrice(ticket.order_type)}
                  value={ticket.price}
                  onChange={(e) => set("price", Number(e.target.value) || 0)}
                />
              </Field>
              <Field label="Trigger price" hint={needsTrigger(ticket.order_type) ? undefined : "stop order — not used"}>
                <input
                  className="field-input"
                  type="number"
                  min={0}
                  step={0.05}
                  disabled={!needsTrigger(ticket.order_type)}
                  value={ticket.trigger_price}
                  onChange={(e) => set("trigger_price", Number(e.target.value) || 0)}
                />
              </Field>
              <Field label="Validity">
                <select className="field-select" value={ticket.validity} onChange={(e) => set("validity", e.target.value)}>
                  <option value="DAY">DAY</option>
                  <option value="IOC">IOC</option>
                </select>
              </Field>
              <Field label="Risk equity" hint="Used for position-sizing limits">
                <input
                  className="field-input"
                  type="number"
                  min={0}
                  step={1000}
                  value={equity}
                  onChange={(e) => setEquity(Number(e.target.value) || 0)}
                />
              </Field>
            </div>
          </Panel>

          <Panel title="Submit" subtitle="A live order is checked by the risk engine before it reaches Kotak">
            <div className="flex flex-wrap items-center gap-2">
              <button className="btn btn-buy btn-lg" onClick={() => set("transaction_type", "B")} type="button">
                Buy
              </button>
              <button className="btn btn-sell btn-lg" onClick={() => set("transaction_type", "S")} type="button">
                Sell
              </button>
              <span className="num min-w-0 flex-1 truncate text-xs text-ink-dim">
                {ticket.transaction_type === "B" ? "BUY" : "SELL"} {num(ticket.quantity, 0)} {ticket.trading_symbol || "—"} ·{" "}
                {ticket.order_type} · {ticket.product} · {ticket.validity}
              </span>
              <button className="btn" onClick={() => void submit("paper")} disabled={busy} type="button">
                Paper order
              </button>
              <button className="btn btn-primary" onClick={() => void submit("live")} disabled={busy} type="button">
                {busy ? "Working…" : "Place live order"}
              </button>
            </div>

            {result ? (
              <pre
                className={`mt-3 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded border p-3 font-mono text-2xs ${
                  result.tone === "ok" ? "border-up/40 bg-up-soft text-up" : "border-down/40 bg-down-soft text-down"
                }`}
              >
                {result.text}
              </pre>
            ) : null}
          </Panel>
        </div>

        <div className="space-y-4">
          <Panel title="Order economics">
            <dl className="space-y-2 text-2xs">
              <Row label="Notional" value={inr(notional)} />
              <Row label="Round-trip charges" value={charges ? inr(charges.round_trip) : "enter a price"} />
              <Row
                label="Break-even move"
                value={charges?.break_even_move_percent !== null && charges?.break_even_move_percent !== undefined ? percent(charges.break_even_move_percent) : "—"}
              />
              <Row label="Account equity" value={inr(equity)} />
              <Row label="Capital used" value={`${num(equity > 0 ? (notional / equity) * 100 : 0)}%`} />
            </dl>
            <p className="mt-3 border-t border-hairline pt-3 text-3xs leading-relaxed text-ink-faint">
              Charges follow the published Indian slabs: STT, exchange transaction charges, SEBI turnover fee,
              GST and stamp duty, with brokerage per your plan. Market orders fill at the exchange price, which
              can differ from the price shown here.
            </p>
          </Panel>

          <Panel title="Before you send">
            <ul className="space-y-2 text-2xs leading-relaxed text-ink-faint">
              <li>• A live order needs <code className="text-ink-dim">ENABLE_LIVE_TRADING=1</code> on the server.</li>
              <li>• It also needs the typed phrase below — the server rejects the request without it.</li>
              <li>• The risk engine can block it on position size, exposure, daily loss or drawdown.</li>
              <li>• Paper orders fill at the live bid or ask, so they track the real market.</li>
            </ul>
          </Panel>
        </div>
      </div>

      <Modal
        open={confirming}
        title="Confirm a real order"
        onClose={() => {
          setConfirming(false);
          setPhrase("");
        }}
        footer={
          <>
            <button
              className="btn"
              onClick={() => {
                setConfirming(false);
                setPhrase("");
              }}
              type="button"
            >
              Cancel
            </button>
            <button className="btn btn-danger" onClick={() => void sendLive()} disabled={busy || phrase !== LIVE_PHRASE} type="button">
              Send to Kotak Neo
            </button>
          </>
        }
      >
        <p className="text-2xs leading-relaxed text-ink-dim">
          This submits a <span className="font-semibold text-down">real order</span> to Kotak Neo. It will be
          routed to the exchange and may fill.
        </p>
        <dl className="space-y-1.5 rounded border border-hairline bg-canvas p-3 text-2xs">
          <Row label="Side" value={ticket.transaction_type === "B" ? "BUY" : "SELL"} />
          <Row label="Instrument" value={ticket.trading_symbol || "—"} />
          <Row label="Quantity" value={num(ticket.quantity, 0)} />
          <Row label="Order type" value={ticket.order_type} />
          <Row label="Price" value={ticket.order_type === "MKT" || ticket.order_type === "SL-M" ? "market" : num(ticket.price)} />
          <Row label="Product" value={ticket.product} />
        </dl>
        <Field label={`Type ${LIVE_PHRASE} to enable the button`}>
          <input
            className="field-input"
            value={phrase}
            onChange={(event) => setPhrase(event.target.value)}
            placeholder={LIVE_PHRASE}
            autoFocus
          />
        </Field>
      </Modal>
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between gap-3">
      <dt className="text-ink-faint">{label}</dt>
      <dd className="num text-ink">{value}</dd>
    </div>
  );
}

export default function TradePage() {
  return (
    <AppShell>
      <TradeDesk />
    </AppShell>
  );
}

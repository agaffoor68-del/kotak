"use client";

import { useState } from "react";
import { api } from "@/lib/api";

export function OptionsTicket() {
  const [underlying, setUnderlying] = useState("NIFTY 50");
  const [expiry, setExpiry] = useState("2023-10-26");
  const [strike, setStrike] = useState("ATM");
  const [type, setType] = useState<"CE" | "PE">("CE");
  const [action, setAction] = useState<"BUY" | "SELL">("BUY");
  const [lots, setLots] = useState(1);
  const [submitting, setSubmitting] = useState(false);

  async function execute() {
    setSubmitting(true);
    try {
      await api.executeOption?.({
        underlying,
        expiry,
        strike,
        type,
        action,
        lots,
      });
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="flex flex-col gap-3 text-xs">
      <div>
        <label className="mb-1 block text-[#787b86]">Underlying</label>
        <select value={underlying} onChange={(e) => setUnderlying(e.target.value)} className="w-full rounded border bg-transparent px-2 py-1.5" style={{ borderColor: "#2a2e39", color: "#d1d4dc" }}>
          <option>NIFTY 50</option>
          <option>BANKNIFTY</option>
        </select>
      </div>
      <div>
        <label className="mb-1 block text-[#787b86]">Expiry</label>
        <select value={expiry} onChange={(e) => setExpiry(e.target.value)} className="w-full rounded border bg-transparent px-2 py-1.5" style={{ borderColor: "#2a2e39", color: "#d1d4dc" }}>
          <option value="2023-10-26">26-OCT-2023</option>
        </select>
      </div>
      <div>
        <label className="mb-1 block text-[#787b86]">Strike</label>
        <div className="grid grid-cols-5 gap-1">
          {["ATM", "ITM-1", "ITM-2", "OTM-1", "OTM-2"].map((s) => (
            <button key={s} onClick={() => setStrike(s)} className="rounded py-1.5" style={{ background: strike === s ? "#2962ff" : "#2a2e39", color: "#d1d4dc" }}>{s}</button>
          ))}
        </div>
      </div>
      <div>
        <label className="mb-1 block text-[#787b86]">Option Type</label>
        <div className="flex gap-2">
          {(["CE", "PE"] as const).map((t) => (
            <button key={t} onClick={() => setType(t)} className="flex-1 rounded py-1.5" style={{ background: type === t ? "#2962ff" : "#2a2e39", color: "#d1d4dc" }}>{t === "CE" ? "CE (Call)" : "PE (Put)"}</button>
          ))}
        </div>
      </div>
      <div>
        <label className="mb-1 block text-[#787b86]">Position</label>
        <div className="flex gap-2">
          {(["BUY", "SELL"] as const).map((a) => (
            <button key={a} onClick={() => setAction(a)} className="flex-1 rounded py-1.5" style={{ background: action === a ? (a === "BUY" ? "#089981" : "#f23645") : "#2a2e39", color: "#d1d4dc" }}>{a}</button>
          ))}
        </div>
      </div>
      <div>
        <label className="mb-1 block text-[#787b86]">Quantity (Lots)</label>
        <input type="number" value={lots} onChange={(e) => setLots(Number(e.target.value))} className="w-full rounded border bg-transparent px-2 py-1.5" style={{ borderColor: "#2a2e39", color: "#d1d4dc" }} />
      </div>
      <button disabled={submitting} onClick={execute} className="w-full rounded py-2.5 font-semibold" style={{ background: action === "BUY" ? "#089981" : "#f23645", color: "#d1d4dc" }}>
        EXECUTE OPTION ORDER
      </button>
    </div>
  );
}
EOF
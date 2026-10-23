"use client";

import { useState } from "react";

const INDICATORS = ["RSI", "MACD", "Supertrend", "Bollinger Bands", "EMA", "SMA", "VWAP", "ADX", "StochRSI", "ATR"];

export function VisualStrategyBuilder() {
  const [entryCond, setEntryCond] = useState({ ind1: "RSI", op: ">", val: "50" });
  const [exitCond, setExitCond] = useState({ ind1: "RSI", op: "<", val: "30" });

  return (
    <div className="rounded border p-4" style={{ borderColor: "#2a2e39", background: "#1e222d", color: "#d1d4dc" }}>
      <div className="mb-3 font-semibold">Visual Strategy Builder</div>
      <div className="mb-4">
        <div className="mb-2 text-xs text-[#787b86]">Entry Condition</div>
        <div className="flex gap-2">
          <select value={entryCond.ind1} onChange={(e) => setEntryCond({...entryCond, ind1: e.target.value})} className="rounded border bg-transparent px-2 py-1" style={{ borderColor: "#2a2e39" }}>
            {INDICATORS.map(i => <option key={i}>{i}</option>)}
          </select>
          <select value={entryCond.op} onChange={(e) => setEntryCond({...entryCond, op: e.target.value})} className="rounded border bg-transparent px-2 py-1" style={{ borderColor: "#2a2e39" }}>
            {[" >", "<", "Crossover", "Crossunder"].map(o => <option key={o}>{o}</option>)}
          </select>
          <input value={entryCond.val} onChange={(e) => setEntryCond({...entryCond, val: e.target.value})} className="rounded border bg-transparent px-2 py-1" style={{ borderColor: "#2a2e39" }} />
        </div>
      </div>
      <div className="mb-4">
        <div className="mb-2 text-xs text-[#787b86]">Exit Condition</div>
        <div className="flex gap-2">
          <select value={exitCond.ind1} onChange={(e) => setExitCond({...exitCond, ind1: e.target.value})} className="rounded border bg-transparent px-2 py-1" style={{ borderColor: "#2a2e39" }}>
            {INDICATORS.map(i => <option key={i}>{i}</option>)}
          </select>
          <select value={exitCond.op} onChange={(e) => setExitCond({...exitCond, op: e.target.value})} className="rounded border bg-transparent px-2 py-1" style={{ borderColor: "#2a2e39" }}>
            {[" >", "<", "Crossover", "Crossunder"].map(o => <option key={o}>{o}</option>)}
          </select>
          <input value={exitCond.val} onChange={(e) => setExitCond({...exitCond, val: e.target.value})} className="rounded border bg-transparent px-2 py-1" style={{ borderColor: "#2a2e39" }} />
        </div>
      </div>
      <button className="rounded px-4 py-2" style={{ background: "#2962ff", color: "#d1d4dc" }}>Save Strategy</button>
    </div>
  );
}

"use client";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";

export function AiAdvisor() {
  const [data, setData] = useState<any>(null);
  useEffect(() => {
    const load = async () => setData(await fetch("/api/v1/ai/suggest_trade").then(r=>r.json()));
    load();
    const t = setInterval(load, 300000);
    return () => clearInterval(t);
  }, []);
  if (!data) return null;
  return (
    <div className="rounded border p-3" style={{ borderColor: "#2a2e39", background: "#1e222d", color: "#d1d4dc" }}>
      <div className="mb-2 font-semibold">🤖 Alpha AI Advisor</div>
      <div className="text-lg font-bold">{data.signal}</div>
      <div className="text-xs text-[#787b86]">{data.reason}</div>
      <button className="mt-2 w-full rounded py-1.5" style={{ background: "#2962ff", color: "#d1d4dc" }}>Execute this Trade</button>
    </div>
  );
}
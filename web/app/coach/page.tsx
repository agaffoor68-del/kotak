"use client";

import { AppShell } from "@/components/app-shell";
import { AiAdvisor } from "@/components/ai-advisor";

export default function CoachPage() {
  return (
    <AppShell>
      <div className="space-y-4">
        <header>
          <h1 className="text-lg font-semibold tracking-tight">Live AI Market Analyzer</h1>
          <p className="text-2xs text-ink-faint">
            Multi-Index Confluence Engine & Self-Learning Memory (NIFTY 50 · BANKNIFTY · SENSEX)
          </p>
        </header>
        <AiAdvisor />
      </div>
    </AppShell>
  );
}

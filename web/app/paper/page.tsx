"use client";

/** Paper trading: virtual portfolio, P&L, trade journal and analytics. */

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { AppShell } from "@/components/app-shell";
import { Empty, ErrorNote, Field, Loading, Metric, Panel } from "@/components/ui";
import { inr, num, signedInr, stamp, toneClass } from "@/lib/format";

interface PaperPosition {
  symbol: string;
  quantity: number;
  avg_entry: number;
  last_price: number | null;
  unrealised_pnl: number | null;
}

interface PaperPortfolio {
  equity: number;
  starting_capital: number;
  realised_pnl: number;
  unrealised_pnl: number;
  open_positions: PaperPosition[];
}

interface PaperTrade {
  id: string;
  symbol: string;
  side: string;
  quantity: number;
  entry_price: number;
  exit_price: number | null;
  entry_time: number;
  exit_time: number | null;
  net_pnl: number | null;
  gross_pnl: number | null;
  charges: number | null;
  reason: string | null;
}

function PaperDesk() {
  const [portfolio, setPortfolio] = useState<PaperPortfolio | null>(null);
  const [trades, setTrades] = useState<PaperTrade[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ tone: "ok" | "err"; text: string } | null>(null);

  const [journal, setJournal] = useState({ setup: "", emotion: "Calm", followed_plan: true, notes: "" });

  const load = useCallback(async () => {
    try {
      const [snapshot, journalRows] = await Promise.all([api.paperPortfolio(), api.paperJournal()]);
      setPortfolio(snapshot);
      setTrades(journalRows as unknown as PaperTrade[]);
      setError(null);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Could not load the paper portfolio");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = setInterval(() => void load(), 20_000);
    return () => clearInterval(timer);
  }, [load]);

  async function close(symbol: string) {
    setBusy(true);
    try {
      await api.paperClose(symbol);
      setNote({ tone: "ok", text: `Closed the ${symbol} paper position.` });
      await load();
    } catch (failure) {
      setNote({ tone: "err", text: failure instanceof Error ? failure.message : "Close failed" });
    } finally {
      setBusy(false);
    }
  }

  async function saveNote() {
    const latest = trades.find((trade) => trade.exit_price !== null);
    if (!latest) return;
    setBusy(true);
    try {
      await api.addJournalNote({
        symbol: latest.symbol,
        side: latest.side,
        quantity: latest.quantity,
        entry_price: latest.entry_price,
        exit_price: latest.exit_price,
        trade_id: latest.id,
        ...journal,
      });
      setNote({ tone: "ok", text: "Journal entry saved." });
    } catch (failure) {
      setNote({ tone: "err", text: failure instanceof Error ? failure.message : "Could not save the note" });
    } finally {
      setBusy(false);
    }
  }

  async function reset() {
    if (!window.confirm("Delete every paper order, trade and equity point? This cannot be undone.")) return;
    setBusy(true);
    try {
      await api.resetPaper();
      setNote({ tone: "ok", text: "Paper account reset." });
      await load();
    } catch (failure) {
      setNote({ tone: "err", text: failure instanceof Error ? failure.message : "Reset failed" });
    } finally {
      setBusy(false);
    }
  }

  const closed = trades.filter((trade) => trade.net_pnl !== null);
  const wins = closed.filter((trade) => (trade.net_pnl ?? 0) > 0);
  const losses = closed.filter((trade) => (trade.net_pnl ?? 0) < 0);

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold tracking-tight">Paper trading</h1>
          <p className="text-2xs text-ink-faint">
            Same strategy engine, filled against live Kotak quotes — no exchange involvement
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button className="btn btn-sm" onClick={() => void load()} type="button">
            Refresh
          </button>
          <button className="btn btn-sm btn-danger" onClick={() => void reset()} disabled={busy} type="button">
            Reset account
          </button>
        </div>
      </header>

      {error ? <ErrorNote error={error} onRetry={() => void load()} /> : null}
      {note ? (
        <p
          className={`rounded-md border px-3 py-2 text-2xs ${
            note.tone === "ok" ? "border-up/40 bg-up-soft text-up" : "border-down/40 bg-down-soft text-down"
          }`}
        >
          {note.text}
        </p>
      ) : null}

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Metric label="Equity" value={inr(portfolio?.equity)} sub={`start ${inr(portfolio?.starting_capital)}`} />
        <Metric
          label="Realised P&L"
          value={signedInr(portfolio?.realised_pnl)}
          tone={(portfolio?.realised_pnl ?? 0) >= 0 ? "up" : "down"}
        />
        <Metric
          label="Unrealised P&L"
          value={signedInr(portfolio?.unrealised_pnl)}
          tone={(portfolio?.unrealised_pnl ?? 0) >= 0 ? "up" : "down"}
        />
        <Metric
          label="Win rate"
          value={closed.length ? `${num((wins.length / closed.length) * 100, 1)}%` : "—"}
          sub={`${wins.length}W / ${losses.length}L of ${closed.length}`}
        />
      </div>

      {loading && !portfolio ? <Loading label="Loading paper portfolio" /> : null}

      <div className="grid gap-4 lg:grid-cols-2">
        <Panel title="Open positions" subtitle="Marked against the live price">
          {portfolio?.open_positions.length ? (
            <table className="tbl">
              <thead>
                <tr>
                  <th>Symbol</th>
                  <th>Qty</th>
                  <th>Avg entry</th>
                  <th>Last</th>
                  <th>Unrealised</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {portfolio.open_positions.map((position) => (
                  <tr key={position.symbol}>
                    <td className="text-xs font-medium">{position.symbol}</td>
                    <td className="num">{num(position.quantity, 0)}</td>
                    <td className="num">{num(position.avg_entry)}</td>
                    <td className="num">{num(position.last_price)}</td>
                    <td className={`num ${toneClass(position.unrealised_pnl)}`}>{signedInr(position.unrealised_pnl)}</td>
                    <td>
                      <button className="btn btn-sm" onClick={() => void close(position.symbol)} disabled={busy} type="button">
                        Close
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <Empty
              title="No open paper positions"
              message="Place a paper order from the order ticket. It fills at the live bid or ask."
            />
          )}
        </Panel>

        <Panel title="Trade journal" subtitle="Every simulated fill">
          {trades.length ? (
            <div className="overflow-auto" style={{ maxHeight: "26rem" }}>
              <table className="tbl">
                <thead>
                  <tr>
                    <th>Time</th>
                    <th>Symbol</th>
                    <th>Side</th>
                    <th>Qty</th>
                    <th>Entry</th>
                    <th>Exit</th>
                    <th>Charges</th>
                    <th>Net P&L</th>
                  </tr>
                </thead>
                <tbody>
                  {trades.map((trade) => (
                    <tr key={trade.id}>
                      <td className="num text-3xs text-ink-faint">{stamp(trade.entry_time)}</td>
                      <td className="text-xs">{trade.symbol}</td>
                      <td className="text-xs">
                        <span className={trade.side === "B" ? "text-up" : "text-down"}>{trade.side === "B" ? "B" : "S"}</span>
                      </td>
                      <td className="num">{num(trade.quantity, 0)}</td>
                      <td className="num">{num(trade.entry_price)}</td>
                      <td className="num">{trade.exit_price === null ? "—" : num(trade.exit_price)}</td>
                      <td className="num text-ink-faint">{trade.charges === null ? "—" : num(trade.charges)}</td>
                      <td className={`num ${trade.net_pnl === null ? "" : toneClass(trade.net_pnl)}`}>
                        {trade.net_pnl === null ? "open" : signedInr(trade.net_pnl)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <Empty title="No paper trades yet" message="The journal fills as soon as you place a paper order." />
          )}
        </Panel>
      </div>

      <Panel title="Log a review" subtitle="The AI coach learns from these notes, not from the P&L alone">
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <Field label="Setup">
            <input
              className="field-input"
              placeholder="Breakout, mean reversion…"
              value={journal.setup}
              onChange={(event) => setJournal({ ...journal, setup: event.target.value })}
            />
          </Field>
          <Field label="Emotion during the trade">
            <select
              className="field-select"
              value={journal.emotion}
              onChange={(event) => setJournal({ ...journal, emotion: event.target.value })}
            >
              {["Calm", "Confident", "Anxious", "FOMO", "Revenge", "Bored"].map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Followed the plan">
            <select
              className="field-select"
              value={journal.followed_plan ? "yes" : "no"}
              onChange={(event) => setJournal({ ...journal, followed_plan: event.target.value === "yes" })}
            >
              <option value="yes">Yes</option>
              <option value="no">No</option>
            </select>
          </Field>
          <Field label="Notes">
            <input
              className="field-input"
              placeholder="What did you see, and what did you do?"
              value={journal.notes}
              onChange={(event) => setJournal({ ...journal, notes: event.target.value })}
            />
          </Field>
        </div>
        <button className="btn btn-primary mt-3" onClick={() => void saveNote()} disabled={busy || !closed.length} type="button">
          Save review
        </button>
        {!closed.length ? (
          <p className="mt-2 text-3xs text-ink-faint">Close at least one paper trade before logging a review.</p>
        ) : null}
      </Panel>
    </div>
  );
}

export default function PaperPage() {
  return (
    <AppShell>
      <PaperDesk />
    </AppShell>
  );
}

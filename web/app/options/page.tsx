"use client";

/** Option chain: live OI, IV, Greeks, PCR, max pain and support/resistance. */

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { OptionChain } from "@/lib/api";
import { AppShell } from "@/components/app-shell";
import { Empty, ErrorNote, Loading, Metric, Panel, Pill } from "@/components/ui";
import { compactInt, num, percent, toneClass } from "@/lib/format";

const UNDERLYINGS = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"];

function OptionDesk() {
  const [underlying, setUnderlying] = useState("NIFTY");
  const [chain, setChain] = useState<OptionChain | null>(null);
  const [underlyings, setUnderlyings] = useState<string[]>(UNDERLYINGS);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showGreeks, setShowGreeks] = useState(true);

  useEffect(() => {
    // Prefer the real underlyings from the master; fall back to the known list.
    api
      .underlyings()
      .then((list) => list.length && setUnderlyings(list))
      .catch(() => undefined);
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setChain(await api.chain(underlying));
      setError(null);
    } catch (failure) {
      setChain(null);
      setError(failure instanceof Error ? failure.message : "Could not load the option chain");
    } finally {
      setLoading(false);
    }
  }, [underlying]);

  useEffect(() => {
    void load();
    const timer = setInterval(() => void load(), 30_000);
    return () => clearInterval(timer);
  }, [load]);

  const summary = chain?.summary;

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold tracking-tight">Option chain</h1>
          <p className="text-2xs text-ink-faint">
            {underlying} · live open interest and prices from Kotak Neo · Greeks from Black-Scholes
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <select className="field-select w-auto" value={underlying} onChange={(event) => setUnderlying(event.target.value)}>
            {(underlyings.length ? underlyings : UNDERLYINGS).map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
          <button className="btn btn-sm" onClick={() => void load()} type="button">
            Refresh
          </button>
        </div>
      </header>

      {error ? <ErrorNote error={error} onRetry={() => void load()} /> : null}

      {loading && !chain ? <Loading label="Loading option chain" /> : null}

      {chain && !chain.has_data ? (
        <Empty title="No contracts in the symbol master" message={chain.note ?? "Run a scrip-master sync to load contracts."} />
      ) : null}

      {chain?.has_data && summary ? (
        <>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4 xl:grid-cols-6">
            <Metric label="Spot" value={num(chain.spot)} sub={chain.spot === null ? "index quote unavailable" : "underlying index"} />
            <Metric
              label="PCR (OI)"
              value={num(summary.pcr)}
              sub={summary.oi_change_bias}
              tone={summary.pcr !== null && summary.pcr > 1.2 ? "up" : summary.pcr !== null && summary.pcr < 0.8 ? "down" : "flat"}
            />
            <Metric label="Max pain" value={num(summary.max_pain)} sub="least option-writer payout" />
            <Metric label="ATM strike" value={num(summary.atm_strike)} sub="nearest to spot" />
            <Metric
              label="Call OI"
              value={compactInt(summary.total_call_oi)}
              sub={`${num(chain.rows.length)} strikes`}
            />
            <Metric label="Put OI" value={compactInt(summary.total_put_oi)} />
          </div>

          <div className="grid gap-4 lg:grid-cols-3">
            <Panel title="Support" subtitle="Heaviest put open interest">
              {summary.support.length ? (
                <div className="flex flex-wrap gap-2">
                  {summary.support.map((strike) => (
                    <span key={strike} className="num rounded border border-up/40 bg-up-soft px-2 py-1 text-xs text-up">
                      {num(strike, 0)}
                    </span>
                  ))}
                </div>
              ) : (
                <p className="text-2xs text-ink-faint">Not enough open interest to identify support.</p>
              )}
            </Panel>
            <Panel title="Resistance" subtitle="Heaviest call open interest">
              {summary.resistance.length ? (
                <div className="flex flex-wrap gap-2">
                  {summary.resistance.map((strike) => (
                    <span key={strike} className="num rounded border border-down/40 bg-down-soft px-2 py-1 text-xs text-down">
                      {num(strike, 0)}
                    </span>
                  ))}
                </div>
              ) : (
                <p className="text-2xs text-ink-faint">Not enough open interest to identify resistance.</p>
              )}
            </Panel>
            <Panel title="Chain">
              <dl className="space-y-2 text-2xs">
                <Row label="Expiry" value={chain.expiry ?? "—"} />
                <Row label="Greeks" value={chain.greeks_available ? "available" : "unavailable"} />
                <Row label="OI bias" value={summary.oi_change_bias} />
                {chain.fetch_error ? <Row label="Feed warning" value={chain.fetch_error} tone="warn" /> : null}
              </dl>
            </Panel>
          </div>

          <Panel
            title={`${underlying} ${chain.expiry ?? "chain"}`}
            subtitle="Open interest and implied volatility per strike"
            actions={
              <button className="btn btn-sm" onClick={() => setShowGreeks((value) => !value)} type="button">
                {showGreeks ? "Hide Greeks" : "Show Greeks"}
              </button>
            }
          >
            <div className="overflow-auto" style={{ maxHeight: "32rem" }}>
              <table className="tbl text-xs">
                <thead>
                  <tr>
                    <th colSpan={showGreeks ? 4 : 3} className="border-r border-hairline text-center text-up">
                      CALLS
                    </th>
                    <th className="text-center">STRIKE</th>
                    <th colSpan={showGreeks ? 4 : 3} className="border-l border-hairline text-center text-down">
                      PUTS
                    </th>
                  </tr>
                  <tr>
                    <th>OI</th>
                    <th>Vol</th>
                    <th>IV</th>
                    {showGreeks ? <th>Delta</th> : null}
                    <th className="border-r border-hairline text-center">Price</th>
                    <th className="border-r border-hairline text-center text-ink">STRIKE</th>
                    <th className="border-l border-hairline text-center text-ink">Price</th>
                    <th>OI</th>
                    <th>Vol</th>
                    <th>IV</th>
                    {showGreeks ? <th>Delta</th> : null}
                  </tr>
                </thead>
                <tbody>
                  {chain.rows.map((row) => {
                    const isAtm = summary.atm_strike === row.strike;
                    return (
                      <tr key={row.strike} className={isAtm ? "bg-accent-soft/60" : undefined}>
                        <td className="num">{compactInt(row.call.open_interest)}</td>
                        <td className="num text-ink-faint">{compactInt(row.call.volume)}</td>
                        <td className="num text-ink-faint">{row.call.iv ? percent(row.call.iv * 100) : "—"}</td>
                        {showGreeks ? <td className="num text-ink-dim">{num(row.call.delta, 3)}</td> : null}
                        <td className={`num border-r border-hairline text-up ${isAtm ? "font-semibold" : ""}`}>
                          {num(row.call.ltp)}
                        </td>
                        <td className="num border-x border-hairline text-center font-semibold">{num(row.strike, 0)}</td>
                        <td className={`num border-l border-hairline text-down ${isAtm ? "font-semibold" : ""}`}>
                          {num(row.put.ltp)}
                        </td>
                        <td className="num">{compactInt(row.put.open_interest)}</td>
                        <td className="num text-ink-faint">{compactInt(row.put.volume)}</td>
                        <td className="num text-ink-faint">{row.put.iv ? percent(row.put.iv * 100) : "—"}</td>
                        {showGreeks ? <td className="num text-ink-dim">{num(row.put.delta, 3)}</td> : null}
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </Panel>

          <Panel title="How these numbers are produced">
            <ul className="space-y-1.5 text-2xs leading-relaxed text-ink-faint">
              <li>
                <span className="text-ink-dim">OI, volume, IV and prices</span> are live values from the Kotak
                Neo quote API for each contract.
              </li>
              <li>
                <span className="text-ink-dim">Greeks</span> use Black-Scholes with the live underlying index as
                spot and the exchange-reported IV. When IV is unavailable, the engine backs it out of the traded
                premium by bisection rather than assuming a number.
              </li>
              <li>
                <span className="text-ink-dim">Max pain</span> is the strike minimising total intrinsic payout
                across the whole chain, computed from real OI.
              </li>
              <li>
                <span className="text-ink-dim">Support and resistance</span> are the strikes carrying the most
                put and call open interest.
              </li>
            </ul>
          </Panel>
        </>
      ) : null}
    </div>
  );
}

function Row({ label, value, tone }: { label: string; value: string; tone?: "warn" }) {
  return (
    <div className="flex items-start justify-between gap-3">
      <dt className="text-ink-faint">{label}</dt>
      <dd className={tone === "warn" ? "text-warn" : "text-ink"}>{value}</dd>
    </div>
  );
}

export default function OptionsPage() {
  return (
    <AppShell>
      <OptionDesk />
    </AppShell>
  );
}

"use client";

/**
 * Options desk: the strike visualizer and the execution panel side by side.
 *
 * The chain comes from Angel One SmartAPI — live open interest, volume and
 * premiums for every contract of the selected expiry — and the spot price is
 * the live index quote, so the in-the-money, at-the-money and out-of-the-money
 * bands are always drawn against the real market.
 */

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { OptionChain } from "@/lib/api";
import { OptionChainVisualizer } from "@/components/option-chain-visualizer";
import { OptionTradePanel } from "@/components/option-trade-panel";
import { AppShell } from "@/components/app-shell";
import { Empty, ErrorNote, Loading, Metric, Panel, SessionChip, StatusChip } from "@/components/ui";
import { compactInt, num, percent } from "@/lib/format";

const UNDERLYINGS = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"];

function OptionDesk() {
  const [underlying, setUnderlying] = useState("NIFTY");
  const [expiry, setExpiry] = useState("");
  const [expiries, setExpiries] = useState<string[]>([]);
  const [chain, setChain] = useState<OptionChain | null>(null);
  const [underlyings, setUnderlyings] = useState<string[]>(UNDERLYINGS);
  const [selectedStrike, setSelectedStrike] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [session, setSession] = useState<{ state: string; isOpen: boolean } | null>(null);

  useEffect(() => {
    api
      .underlyings()
      .then((list) => list.length && setUnderlyings(list))
      .catch(() => undefined);
    api
      .status()
      .then((status) => {
        setSession({ state: status.session?.state ?? "closed", isOpen: Boolean(status.session?.is_open) });
      })
      .catch(() => undefined);
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const nextChain = await api.chain(underlying, expiry || undefined);
      setChain(nextChain);
      setExpiries(nextChain.expiries ?? []);
      if (!expiry && nextChain.expiry) setExpiry(nextChain.expiry);
      setError(null);
    } catch (failure) {
      setChain(null);
      setError(failure instanceof Error ? failure.message : "Could not load the option chain");
    } finally {
      setLoading(false);
    }
  }, [underlying, expiry]);

  useEffect(() => {
    void load();
    const timer = setInterval(() => void load(), 30_000);
    return () => clearInterval(timer);
  }, [load]);

  const spot = chain?.spot ?? null;
  const summary = chain?.summary;

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold tracking-tight">Options desk</h1>
          <p className="text-2xs text-ink-faint">
            {underlying} · live chain, open interest and Greeks from Angel One SmartAPI
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {session ? <SessionChip state={session.state} isOpen={session.isOpen} /> : null}
          <select
            className="field-select w-auto"
            value={underlying}
            onChange={(event) => {
              setUnderlying(event.target.value);
              setExpiry("");
              setSelectedStrike(null);
            }}
          >
            {(underlyings.length ? underlyings : UNDERLYINGS).map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
          <select
            className="field-select w-auto"
            value={expiry}
            onChange={(event) => setExpiry(event.target.value)}
            disabled={!expiries.length}
          >
            {expiries.length ? (
              expiries.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))
            ) : (
              <option value="">nearest expiry</option>
            )}
          </select>
          <button className="btn btn-sm" onClick={() => void load()} type="button">
            Refresh
          </button>
        </div>
      </header>

      {error ? <ErrorNote error={error} onRetry={() => void load()} /> : null}
      {loading && !chain ? <Loading label="Loading the option chain" /> : null}

      {chain && !chain.has_data ? (
        <Empty
          title="No contracts in the symbol master"
          message={chain.note ?? "Run a scrip-master sync to load the contracts."}
        />
      ) : null}

      {chain?.has_data ? (
        <>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6">
            <Metric label="Spot" value={num(spot)} sub={chain.expiry ?? "nearest expiry"} />
            <Metric
              label="ATM strike"
              value={num(summary?.atm_strike ?? null, 0)}
              sub="nearest to spot"
            />
            <Metric
              label="PCR (OI)"
              value={num(summary?.pcr ?? null)}
              sub={summary?.oi_change_bias}
              tone={summary?.pcr !== null && summary?.pcr !== undefined && summary.pcr > 1.2 ? "up" : summary?.pcr !== null && summary?.pcr !== undefined && summary.pcr < 0.8 ? "down" : "flat"}
            />
            <Metric label="Max pain" value={num(summary?.max_pain ?? null, 0)} sub="least writer payout" />
            <Metric label="Call OI" value={compactInt(summary?.total_call_oi ?? 0)} sub={`${chain.rows.length} strikes`} />
            <Metric label="Put OI" value={compactInt(summary?.total_put_oi ?? 0)} />
          </div>

          <div className="grid gap-4 xl:grid-cols-3">
            <Panel
              title={`${underlying} strike ladder`}
              subtitle="Calls left, strike centre, puts right — tinted by the money"
              className="xl:col-span-2"
              bodyClassName="panel-body"
            >
              <OptionChainVisualizer
                chain={chain}
                spot={spot}
                selectedStrike={selectedStrike}
                onSelectStrike={setSelectedStrike}
              />
            </Panel>

            <OptionTradePanel
              chain={chain}
              underlying={underlying}
              expiry={expiry}
              expiries={expiries}
              onUnderlyingChange={(value) => {
                setUnderlying(value);
                setExpiry("");
                setSelectedStrike(null);
              }}
              onExpiryChange={setExpiry}
            />
          </div>

          <div className="grid gap-4 lg:grid-cols-3">
            <Panel title="Support" subtitle="Heaviest put open interest">
              {summary?.support.length ? (
                <div className="flex flex-wrap gap-2">
                  {summary.support.map((strike) => (
                    <button
                      key={strike}
                      type="button"
                      onClick={() => setSelectedStrike(strike)}
                      className="num rounded border border-up/40 bg-up-soft px-2 py-1 text-xs text-up"
                    >
                      {num(strike, 0)}
                    </button>
                  ))}
                </div>
              ) : (
                <p className="text-2xs text-ink-faint">Not enough open interest to identify support.</p>
              )}
            </Panel>
            <Panel title="Resistance" subtitle="Heaviest call open interest">
              {summary?.resistance.length ? (
                <div className="flex flex-wrap gap-2">
                  {summary.resistance.map((strike) => (
                    <button
                      key={strike}
                      type="button"
                      onClick={() => setSelectedStrike(strike)}
                      className="num rounded border border-down/40 bg-down-soft px-2 py-1 text-xs text-down"
                    >
                      {num(strike, 0)}
                    </button>
                  ))}
                </div>
              ) : (
                <p className="text-2xs text-ink-faint">Not enough open interest to identify resistance.</p>
              )}
            </Panel>
            <Panel title="Chain" subtitle="What the feed reported">
              <dl className="space-y-2 text-2xs">
                <Row label="Expiry" value={chain.expiry ?? "—"} />
                <Row label="Greeks" value={chain.greeks_available ? "available" : "unavailable"} />
                <Row label="OI bias" value={summary?.oi_change_bias ?? "—"} />
                <Row label="Contracts quoted" value={num(chain.rows.length, 0)} />
                {chain.fetch_error ? <Row label="Feed warning" value={chain.fetch_error} tone="warn" /> : null}
              </dl>
            </Panel>
          </div>

          <Panel title="How these numbers are produced">
            <ul className="space-y-1.5 text-2xs leading-relaxed text-ink-faint">
              <li>
                <span className="text-ink-dim">Open interest, volume and premiums</span> are live values from the
                Angel One SmartAPI quote API for each contract, fetched in batches of fifty.
              </li>
              <li>
                <span className="text-ink-dim">The spot price</span> is the live index quote, and it is what decides
                the in-the-money, at-the-money and out-of-the-money bands.
              </li>
              <li>
                <span className="text-ink-dim">Greeks</span> use Black-Scholes with the live underlying index as
                spot and the exchange-reported implied volatility.
              </li>
              <li>
                <span className="text-ink-dim">Max pain</span> is the strike minimising total intrinsic payout
                across the whole chain, computed from real open interest.
              </li>
            </ul>
          </Panel>
        </>
      ) : null}

      {chain?.has_data ? (
        <p className="flex items-center gap-2 text-3xs text-ink-faint">
          <StatusChip status={chain.fetch_error ? "warn" : "live"} label={chain.fetch_error ? "partial feed" : "feed healthy"} />
          Prices refresh every 30 seconds.
        </p>
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

"use client";

/** Market watch: instrument search, live watchlist and a multi-style chart. */

import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import type { Candle, Coverage, Instrument } from "@/lib/api";
import { AppShell } from "@/components/app-shell";
import { PriceChart } from "@/components/price-chart";
import type { ChartStyle, Overlay } from "@/components/price-chart";
import { Empty, ErrorNote, Field, Loading, Panel } from "@/components/ui";
import { compactInt, num, percent, relative, toneClass } from "@/lib/format";
import { quoteKey, useLiveFeed } from "@/lib/useLiveFeed";

interface WatchRow {
  instrument: Instrument;
}

const STYLES: { value: ChartStyle; label: string }[] = [
  { value: "candles", label: "Candles" },
  { value: "heikin", label: "Heikin Ashi" },
  { value: "renko", label: "Renko" },
  { value: "line", label: "Line" },
  { value: "area", label: "Area" },
];

const OVERLAY_PRESETS: { id: string; label: string; color: string; build: (closes: number[]) => Overlay }[] = [
  { id: "ema20", label: "EMA 20", color: "#F59E0B", build: (c) => ({ name: "ema20", color: "#F59E0B", values: ema(c, 20) }) },
  { id: "ema50", label: "EMA 50", color: "#A855F7", build: (c) => ({ name: "ema50", color: "#A855F7", values: ema(c, 50) }) },
  { id: "sma200", label: "SMA 200", color: "#64748B", build: (c) => ({ name: "sma200", color: "#64748B", values: sma(c, 200) }) },
  { id: "vwap", label: "VWAP", color: "#22D3EE", build: () => ({ name: "vwap", color: "#22D3EE", values: [] }) },
];

function MarketWatch() {
  const feed = useLiveFeed();
  const [watch, setWatch] = useState<WatchRow[]>([]);
  const [selected, setSelected] = useState<WatchRow | null>(null);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<Instrument[]>([]);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);

  const [candles, setCandles] = useState<Candle[]>([]);
  const [coverage, setCoverage] = useState<Coverage | null>(null);
  const [interval, setInterval] = useState("5m");
  const [style, setStyle] = useState<ChartStyle>("candles");
  const [overlays, setOverlays] = useState<string[]>(["ema20", "ema50"]);
  const [loadingChart, setLoadingChart] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Start with the major indices, which always have a known Kotak token.
  useEffect(() => {
    const seeds: Instrument[] = [
      { token: "26000", exchange_segment: "nse_cm", symbol: "NIFTY", trading_symbol: "NIFTY", name: "NIFTY 50", is_index: 1 },
      { token: "26009", exchange_segment: "nse_cm", symbol: "BANKNIFTY", trading_symbol: "BANKNIFTY", name: "NIFTY BANK", is_index: 1 },
      { token: "26037", exchange_segment: "nse_cm", symbol: "FINNIFTY", trading_symbol: "FINNIFTY", name: "FIN NIFTY", is_index: 1 },
      { token: "26074", exchange_segment: "nse_cm", symbol: "MIDCPNIFTY", trading_symbol: "MIDCPNIFTY", name: "MIDCAP NIFTY", is_index: 1 },
      { token: "265", exchange_segment: "bse_cm", symbol: "SENSEX", trading_symbol: "SENSEX", name: "S&P BSE SENSEX", is_index: 1 },
    ];
    setWatch(seeds.map((instrument) => ({ instrument })));
    setSelected({ instrument: seeds[0] });
    // Index quotes are needed from the moment the page mounts.
    feed.subscribe(
      seeds.map((entry) => ({ instrument_token: entry.token, exchange_segment: entry.exchange_segment })),
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Keep the backend subscription aligned with the watchlist.
  useEffect(() => {
    if (!watch.length) return;
    feed.subscribe(
      watch.map(({ instrument }) => ({ instrument_token: instrument.token, exchange_segment: instrument.exchange_segment })),
    );
  }, [watch, feed]);

  // Load candles for the selected instrument.
  useEffect(() => {
    if (!selected) return;
    let active = true;
    setLoadingChart(true);
    api
      .candles(selected.instrument.token, selected.instrument.exchange_segment, interval, 500)
      .then((response) => {
        if (!active) return;
        setCandles(response.candles);
        setCoverage(response.coverage);
        setError(null);
      })
      .catch((failure) => {
        if (!active) return;
        setCandles([]);
        setError(failure instanceof Error ? failure.message : "Could not load candles");
      })
      .finally(() => active && setLoadingChart(false));
    return () => {
      active = false;
    };
  }, [selected, interval]);

  // Debounced instrument search against the scrip master.
  useEffect(() => {
    const term = query.trim();
    if (term.length < 2) {
      setResults([]);
      setSearchError(null);
      return;
    }
    const timer = setTimeout(async () => {
      setSearching(true);
      try {
        const response = await api.search(term, 20);
        setResults(response.results);
        setSearchError(null);
      } catch (failure) {
        setResults([]);
        setSearchError(failure instanceof Error ? failure.message : "Search failed");
      } finally {
        setSearching(false);
      }
    }, 350);
    return () => clearTimeout(timer);
  }, [query]);

  const overlaysToDraw = useMemo<Overlay[]>(() => {
    if (!candles.length) return [];
    const closes = candles.map((candle) => candle.close);
    return OVERLAY_PRESETS.filter((preset) => overlays.includes(preset.id)).map((preset) => {
      if (preset.id === "vwap") {
        // Session VWAP from the recorded candles.
        let pv = 0;
        let volume = 0;
        return {
          name: "vwap",
          color: preset.color,
          dashed: true,
          values: candles.map((candle) => {
            const typical = (candle.high + candle.low + candle.close) / 3;
            pv += typical * (candle.volume || 0);
            volume += candle.volume || 0;
            return volume > 0 ? pv / volume : null;
          }),
        };
      }
      return preset.build(closes);
    });
  }, [candles, overlays]);

  const addInstrument = useCallback((instrument: Instrument) => {
    setWatch((current) =>
      current.some((row) => row.instrument.token === instrument.token && row.instrument.exchange_segment === instrument.exchange_segment)
        ? current
        : [...current, { instrument }],
    );
    setSelected({ instrument });
    setQuery("");
    setResults([]);
  }, []);

  const live = selected ? feed.quotes[quoteKey(selected.instrument.token, selected.instrument.exchange_segment)] : undefined;

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold tracking-tight">Market watch</h1>
          <p className="text-2xs text-ink-faint">Live Kotak Neo quotes over WebSocket · candles from the recorded tape</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <select className="field-select w-auto" value={interval} onChange={(event) => setInterval(event.target.value)}>
            {["1m", "3m", "5m", "15m", "30m", "60m", "1d"].map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
          <select className="field-select w-auto" value={style} onChange={(event) => setStyle(event.target.value as ChartStyle)}>
            {STYLES.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </div>
      </header>

      {error ? <ErrorNote error={error} /> : null}

      <div className="grid gap-4 xl:grid-cols-4">
        {/* ---------------- watchlist ---------------- */}
        <div className="space-y-3 xl:col-span-1">
          <Panel title="Watchlist" subtitle={`${watch.length} instruments`}>
            <ul className="space-y-1">
              {watch.map(({ instrument }) => {
                const quote = feed.quotes[quoteKey(instrument.token, instrument.exchange_segment)];
                const active = selected?.instrument.token === instrument.token;
                return (
                  <li key={`${instrument.exchange_segment}:${instrument.token}`}>
                    <button
                      className={`flex w-full items-center justify-between gap-2 rounded px-2 py-2 text-left transition-colors ${active ? "bg-accent-soft" : "hover:bg-elevated"
                        }`}
                      onClick={() => setSelected({ instrument })}
                      type="button"
                    >
                      <span className="min-w-0">
                        <span className="block truncate text-xs font-medium">{instrument.symbol}</span>
                        <span className="block truncate text-3xs text-ink-faint">{instrument.name ?? instrument.token}</span>
                      </span>
                      <span className="shrink-0 text-right">
                        <span className="num block text-xs">{num(quote?.last)}</span>
                        <span className={`num block text-3xs ${toneClass(quote?.change_percent)}`}>
                          {quote ? percent(quote.change_percent) : "—"}
                        </span>
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          </Panel>

          <Panel title="Add instrument" subtitle="Search every NSE and BSE scrip in the master">
            <Field label="Search">
              <input
                className="field-input"
                placeholder="RELIANCE, INFY, HDFCBANK…"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
              />
            </Field>
            {searchError ? <p className="mt-2 text-3xs text-warn">{searchError}</p> : null}
            {searching ? <Loading label="Searching" /> : null}
            {!searching && query.trim().length >= 2 && !results.length && !searchError ? (
              <p className="mt-2 text-3xs text-ink-faint">No matches in the symbol master.</p>
            ) : null}
            {results.length ? (
              <ul className="mt-2 max-h-72 space-y-0.5 overflow-y-auto">
                {results.map((instrument) => (
                  <li key={`${instrument.exchange_segment}:${instrument.token}`}>
                    <button
                      className="flex w-full items-center justify-between gap-2 rounded px-2 py-1.5 text-left hover:bg-elevated"
                      onClick={() => addInstrument(instrument)}
                      type="button"
                    >
                      <span className="min-w-0">
                        <span className="block truncate text-2xs font-medium">{instrument.trading_symbol}</span>
                        <span className="block truncate text-3xs text-ink-faint">
                          {instrument.name ?? instrument.symbol}
                        </span>
                      </span>
                      <span className="shrink-0 text-3xs text-ink-faint">
                        {instrument.option_type ? `${instrument.option_type} ${num(instrument.strike, 0)}` : instrument.exchange_segment}
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            ) : null}
          </Panel>
        </div>

        {/* ---------------- chart ---------------- */}
        <div className="space-y-4 xl:col-span-3">
          <Panel
            title={selected ? `${selected.instrument.symbol} · ${interval}` : "Chart"}
            subtitle={
              live
                ? `last ${num(live.last)} · ${percent(live.change_percent)} · tick ${relative(live.updated_at)} ago`
                : "waiting for a tick"
            }
            actions={
              <div className="flex flex-wrap gap-1">
                {OVERLAY_PRESETS.map((preset) => (
                  <button
                    key={preset.id}
                    className={`btn btn-sm ${overlays.includes(preset.id) ? "btn-primary" : ""}`}
                    onClick={() =>
                      setOverlays((current) =>
                        current.includes(preset.id) ? current.filter((id) => id !== preset.id) : [...current, preset.id],
                      )
                    }
                    type="button"
                  >
                    {preset.label}
                  </button>
                ))}
              </div>
            }
          >
            {loadingChart ? (
              <Loading label="Loading recorded candles" />
            ) : candles.length ? (
              <PriceChart candles={candles} style={style} height={420} overlays={overlaysToDraw} />
            ) : (
              <Empty
                title="No recorded candles for this instrument"
                message={
                  coverage?.note ??
                  "Kotak Neo publishes no historical candles. This platform records the live tape, so history builds from the moment the feed starts recording this instrument."
                }
              />
            )}
          </Panel>

          {coverage ? (
            <Panel title="Data provenance" subtitle="What this chart is actually built from">
              <dl className="grid gap-2 text-2xs sm:grid-cols-4">
                <Stat label="Ticks recorded" value={num(coverage.ticks, 0)} />
                <Stat label="First tick" value={coverage.first_tick ? new Date(coverage.first_tick * 1000).toLocaleDateString("en-IN") : "—"} />
                <Stat label="Last tick" value={coverage.last_tick ? relative(coverage.last_tick) : "—"} />
                <Stat label="Span" value={coverage.span_seconds > 0 ? `${Math.round(coverage.span_seconds / 60)} min` : "—"} />
              </dl>
              <p className="mt-3 border-t border-hairline pt-3 text-3xs leading-relaxed text-ink-faint">
                {coverage.note}
              </p>
            </Panel>
          ) : null}

          {selected ? <QuoteDetail token={selected.instrument.token} segment={selected.instrument.exchange_segment} /> : null}
        </div>
      </div>
    </div>
  );
}

function QuoteDetail({ token, segment }: { token: string; segment: string }) {
  const feed = useLiveFeed();
  const quote = feed.quotes[quoteKey(token, segment)];
  if (!quote) return null;
  return (
    <Panel title="Live quote detail" subtitle={quote.last === null ? "no price yet" : `updated ${relative(quote.updated_at)} ago`}>
      <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-2xs sm:grid-cols-4">
        <Stat label="Open" value={num(quote.open)} />
        <Stat label="High" value={num(quote.high)} />
        <Stat label="Low" value={num(quote.low)} />
        <Stat label="Prev close" value={num(quote.previous_close)} />
        <Stat label="Bid" value={num(quote.bid)} />
        <Stat label="Ask" value={num(quote.ask)} />
        <Stat label="Volume" value={compactInt(quote.volume)} />
        <Stat label="Open interest" value={compactInt(quote.open_interest)} />
        <Stat label="VWAP" value={num(quote.average_price)} />
        <Stat label="IV" value={quote.implied_volatility ? `${num(quote.implied_volatility)}%` : "—"} />
        <Stat label="Upper circuit" value={num(quote.upper_circuit)} />
        <Stat label="Lower circuit" value={num(quote.lower_circuit)} />
      </dl>
    </Panel>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-3xs text-ink-faint">{label}</dt>
      <dd className="num mt-0.5 text-xs">{value}</dd>
    </div>
  );
}

/* ------------------------------------------------------------- indicators */

function ema(values: number[], period: number): (number | null)[] {
  if (values.length < period) return values.map(() => null);
  const multiplier = 2 / (period + 1);
  const out: (number | null)[] = values.map(() => null);
  let current = values.slice(0, period).reduce((sum, value) => sum + value, 0) / period;
  out[period - 1] = current;
  for (let index = period; index < values.length; index += 1) {
    current = (values[index] - current) * multiplier + current;
    out[index] = current;
  }
  return out;
}

function sma(values: number[], period: number): (number | null)[] {
  if (values.length < period) return values.map(() => null);
  const out: (number | null)[] = values.map(() => null);
  let total = values.slice(0, period).reduce((sum, value) => sum + value, 0);
  out[period - 1] = total / period;
  for (let index = period; index < values.length; index += 1) {
    total += values[index] - values[index - period];
    out[index] = total / period;
  }
  return out;
}

export default function MarketPage() {
  return (
    <AppShell>
      <MarketWatch />
    </AppShell>
  );
}

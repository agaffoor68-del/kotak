"use client";

/**
 * Live instrument auto-complete.
 *
 * The user types a symbol ("SBIN", "RELIANCE") and the dropdown answers from
 * the local symbol master — no broker call, no table scan. Every result
 * carries the symbol token the backend quotes by, so selecting one hands the
 * caller a token and a symbol and nothing else.
 */

import { useEffect, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import { api } from "@/lib/api";
import type { Instrument } from "@/lib/api";

interface InstrumentSearchProps {
  onSelect: (instrument: Instrument) => void;
  placeholder?: string;
  disabled?: boolean;
  label?: string;
  /** "equity" searches cash instruments; "all" also searches derivatives. */
  scope?: "equity" | "all";
}

/** The name is only worth showing when it says more than the symbol does. */
function subtitle(instrument: Instrument): string {
  const name = (instrument.name ?? "").trim();
  if (name && name.toUpperCase() !== instrument.symbol.toUpperCase()) return name;
  const exchange = instrument.exchange_segment === "bse_cm" ? "BSE" : "NSE";
  return `${exchange} · token ${instrument.token}`;
}

export function InstrumentSearch({
  onSelect,
  placeholder = "Search a symbol, e.g. SBIN or RELIANCE",
  disabled = false,
  label = "Search",
  scope = "equity",
}: InstrumentSearchProps) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<Instrument[]>([]);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  // Debounced search against the local master.
  useEffect(() => {
    const term = query.trim();
    if (term.length < 1) {
      setResults([]);
      setOpen(false);
      setError(null);
      return;
    }
    let activeRequest = true;
    const timer = window.setTimeout(async () => {
      setLoading(true);
      try {
        const response = await api.search(term, 10, scope);
        if (!activeRequest) return;
        setResults(response.results);
        setActive(0);
        setOpen(true);
        setError(null);
      } catch (failure) {
        if (!activeRequest) return;
        setResults([]);
        setOpen(false);
        setError(failure instanceof Error ? failure.message : "Search failed");
      } finally {
        if (activeRequest) setLoading(false);
      }
    }, 200);
    return () => {
      activeRequest = false;
      window.clearTimeout(timer);
    };
  }, [query, scope]);

  // Close the dropdown when the operator clicks away.
  useEffect(() => {
    function onPointerDown(event: MouseEvent) {
      if (!rootRef.current) return;
      if (!rootRef.current.contains(event.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onPointerDown);
    return () => document.removeEventListener("mousedown", onPointerDown);
  }, []);

  function choose(instrument: Instrument) {
    onSelect(instrument);
    setQuery("");
    setResults([]);
    setOpen(false);
    inputRef.current?.blur();
  }

  function onKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (!open || !results.length) {
      if (event.key === "Escape") setOpen(false);
      return;
    }
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActive((current) => (current + 1) % results.length);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActive((current) => (current - 1 + results.length) % results.length);
    } else if (event.key === "Enter") {
      event.preventDefault();
      const instrument = results[active];
      if (instrument) choose(instrument);
    } else if (event.key === "Escape") {
      setOpen(false);
    }
  }

  return (
    <div className="relative" ref={rootRef}>
      <label className="label-caps" htmlFor="instrument-search">
        {label}
      </label>
      <input
        id="instrument-search"
        ref={inputRef}
        className="field-input mt-1"
        value={query}
        placeholder={placeholder}
        autoComplete="off"
        spellCheck={false}
        disabled={disabled}
        onChange={(event) => setQuery(event.target.value)}
        onFocus={() => {
          if (results.length) setOpen(true);
        }}
        onKeyDown={onKeyDown}
      />

      {open ? (
        <div className="absolute z-30 mt-1 w-full overflow-hidden rounded border border-hairline bg-elevated shadow-xl">
          {loading && !results.length ? (
            <p className="px-3 py-2 text-3xs text-ink-faint">Searching…</p>
          ) : null}
          {!loading && !results.length ? (
            <p className="px-3 py-2 text-3xs text-ink-faint">No instrument matches “{query.trim()}”.</p>
          ) : null}
          <ul className="max-h-72 overflow-y-auto">
            {results.map((instrument, index) => (
              <li key={`${instrument.exchange_segment}:${instrument.token}`}>
                <button
                  type="button"
                  className={`flex w-full items-center justify-between gap-3 px-3 py-2 text-left transition-colors ${
                    index === active ? "bg-accent-soft" : "hover:bg-surface"
                  }`}
                  onMouseEnter={() => setActive(index)}
                  onClick={() => choose(instrument)}
                >
                  <span className="min-w-0">
                    <span className="block truncate text-xs font-medium">{instrument.symbol}</span>
                    <span className="block truncate text-3xs text-ink-faint">{subtitle(instrument)}</span>
                  </span>
                  <span className="shrink-0 text-3xs text-ink-faint">
                    {instrument.exchange_segment === "bse_cm" ? "BSE" : "NSE"} · {instrument.token}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {error ? <p className="mt-1 text-3xs text-warn">{error}</p> : null}
    </div>
  );
}

export default InstrumentSearch;

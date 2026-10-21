"use client";

/**
 * Options strike visualizer.
 *
 * The chain is drawn the way a trading desk reads it: calls on the left, the
 * strike in the middle, puts on the right, and every row tinted by where the
 * strike sits against the live spot — in the money, at the money or out of it.
 * The spot marker tells the operator which side of the money the market is
 * trading at, without reading a single number.
 */

import type { OptionChain } from "@/lib/api";
import { compactInt, num } from "@/lib/format";

/** How a strike sits against the spot, per leg. */
export type Moneyness = "ITM" | "ATM" | "OTM";

const TONE: Record<Moneyness, string> = {
  ITM: "bg-sky-500/10 text-sky-200",
  ATM: "bg-accent/15 text-accent",
  OTM: "bg-elevated/50 text-ink-dim",
};

const LEG_TONE: Record<Moneyness, string> = {
  ITM: "bg-sky-500/10",
  ATM: "bg-accent/15",
  OTM: "bg-elevated/40",
};

export function moneynessFor(strike: number, spot: number | null, kind: "call" | "put", atm: number | null): Moneyness {
  if (atm !== null && Math.abs(strike - atm) < 1e-9) return "ATM";
  if (spot === null || spot <= 0) return "OTM";
  if (kind === "call") return strike < spot ? "ITM" : "OTM";
  return strike > spot ? "ITM" : "OTM";
}

export function OptionChainVisualizer({
  chain,
  spot,
  selectedStrike,
  onSelectStrike,
}: {
  chain: OptionChain;
  spot: number | null;
  selectedStrike: number | null;
  onSelectStrike: (strike: number) => void;
}) {
  const atm = chain.summary.atm_strike;
  const rows = chain.rows;

  // The row that carries the spot marker: the strike closest to the spot.
  const markerRow =
    spot === null
      ? null
      : rows.reduce<{ index: number; distance: number } | null>((best, row, index) => {
          const distance = Math.abs(row.strike - spot);
          if (!best || distance < best.distance) return { index, distance };
          return best;
        }, null);

  return (
    <div className="space-y-2">
      <div className="overflow-auto rounded-md border border-hairline" style={{ maxHeight: "34rem" }}>
        <table className="w-full border-collapse text-xs">
          <thead className="sticky top-0 z-10 bg-surface">
            <tr>
              <th className="px-2 py-1.5 text-right text-3xs font-semibold uppercase tracking-[0.08em] text-up">
                OI
              </th>
              <th className="px-2 py-1.5 text-right text-3xs font-semibold uppercase tracking-[0.08em] text-up">
                Vol
              </th>
              <th className="px-2 py-1.5 text-right text-3xs font-semibold uppercase tracking-[0.08em] text-up">
                Call LTP
              </th>
              <th className="border-x border-hairline px-3 py-1.5 text-center text-3xs font-semibold uppercase tracking-[0.08em] text-ink">
                Strike
              </th>
              <th className="px-2 py-1.5 text-right text-3xs font-semibold uppercase tracking-[0.08em] text-down">
                Put LTP
              </th>
              <th className="px-2 py-1.5 text-right text-3xs font-semibold uppercase tracking-[0.08em] text-down">
                Vol
              </th>
              <th className="px-2 py-1.5 text-right text-3xs font-semibold uppercase tracking-[0.08em] text-down">
                OI
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row, index) => {
              const callState = moneynessFor(row.strike, spot, "call", atm);
              const putState = moneynessFor(row.strike, spot, "put", atm);
              const isSelected = selectedStrike !== null && Math.abs(row.strike - selectedStrike) < 1e-9;
              const isMarker = markerRow?.index === index;
              return (
                <tr
                  key={row.strike}
                  onClick={() => onSelectStrike(row.strike)}
                  className={`cursor-pointer transition-colors ${isSelected ? "outline outline-1 outline-accent" : "hover:bg-elevated/40"}`}
                >
                  <td className={`px-2 py-1.5 text-right num ${LEG_TONE[callState]}`}>{compactInt(row.call.open_interest)}</td>
                  <td className={`px-2 py-1.5 text-right num text-ink-faint ${LEG_TONE[callState]}`}>
                    {compactInt(row.call.volume)}
                  </td>
                  <td className={`px-2 py-1.5 text-right num font-semibold ${TONE[callState]}`}>
                    {num(row.call.ltp)}
                  </td>
                  <td className="relative border-x border-hairline bg-surface px-3 py-1.5 text-center num font-semibold text-ink">
                    {isMarker && spot !== null ? (
                      <span
                        className="absolute -top-px left-1/2 z-10 -translate-x-1/2 rounded-b bg-accent px-1.5 py-0.5 text-3xs font-bold text-canvas"
                        title="Live spot price"
                      >
                        {num(spot)}
                      </span>
                    ) : null}
                    {num(row.strike, 0)}
                  </td>
                  <td className={`px-2 py-1.5 text-right num font-semibold ${TONE[putState]}`}>{num(row.put.ltp)}</td>
                  <td className={`px-2 py-1.5 text-right num text-ink-faint ${LEG_TONE[putState]}`}>
                    {compactInt(row.put.volume)}
                  </td>
                  <td className={`px-2 py-1.5 text-right num ${LEG_TONE[putState]}`}>{compactInt(row.put.open_interest)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 text-3xs text-ink-faint">
        <Legend className="bg-sky-500/20" label="In the money" />
        <Legend className="bg-accent/30" label="At the money" />
        <Legend className="bg-elevated" label="Out of the money" />
        <span className="ml-auto">
          Spot <span className="num text-ink-dim">{spot === null ? "—" : num(spot)}</span> · ATM{" "}
          <span className="num text-ink-dim">{atm === null ? "—" : num(atm, 0)}</span>
        </span>
      </div>
    </div>
  );
}

function Legend({ className, label }: { className: string; label: string }) {
  return (
    <span className="flex items-center gap-1.5">
      <span className={`h-2.5 w-2.5 rounded-sm ${className}`} />
      {label}
    </span>
  );
}

"use client";

/** Live portfolio: holdings, positions, order book and trades from Kotak Neo. */

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { AppShell } from "@/components/app-shell";
import { Empty, ErrorNote, Loading, Panel } from "@/components/ui";
import { clock, inr, num, percent, signedInr, toneClass } from "@/lib/format";

/**
 * Neo returns a different envelope per endpoint and occasionally renames fields,
 * so these helpers walk whatever shape arrived and render a dash for anything
 * missing rather than inventing a value.
 */
function rows(payload: unknown): Record<string, unknown>[] {
  if (Array.isArray(payload)) return payload.filter((row) => typeof row === "object" && row !== null) as Record<string, unknown>[];
  if (payload && typeof payload === "object") {
    for (const key of ["data", "holdings", "positions", "orders", "trades", "items", "net"]) {
      const nested = (payload as Record<string, unknown>)[key];
      if (Array.isArray(nested)) return nested.filter((row) => typeof row === "object" && row !== null) as Record<string, unknown>[];
    }
  }
  return [];
}

function pick(row: Record<string, unknown>, ...keys: string[]): unknown {
  for (const key of keys) {
    const value = row[key];
    if (value !== undefined && value !== null && value !== "" && value !== "-") return value;
  }
  return null;
}

function Portfolio() {
  const [data, setData] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [tab, setTab] = useState<"holdings" | "positions" | "orders" | "trades">("holdings");

  const load = useCallback(async () => {
    try {
      setData(await api.portfolio());
      setError(null);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Could not load the portfolio");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = setInterval(() => void load(), 30_000);
    return () => clearInterval(timer);
  }, [load]);

  const holdings = rows(data?.holdings);
  const positions = rows(data?.positions);
  const orders = rows(data?.orders);
  const trades = rows(data?.trades);

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold tracking-tight">Portfolio</h1>
          <p className="text-2xs text-ink-faint">
            Live from the Kotak Neo trade API
            {data?.as_of ? ` · as of ${clock(Number(data.as_of))}` : ""}
          </p>
        </div>
        <button className="btn btn-sm" onClick={() => void load()} type="button">
          Refresh
        </button>
      </header>

      {error ? <ErrorNote error={error} onRetry={() => void load()} /> : null}
      {loading && !data ? <Loading label="Loading portfolio" /> : null}

      <div className="flex flex-wrap gap-1.5">
        {(["holdings", "positions", "orders", "trades"] as const).map((key) => (
          <button key={key} className={`btn btn-sm ${tab === key ? "btn-primary" : ""}`} onClick={() => setTab(key)} type="button">
            {key} ({rows(data?.[key]).length})
          </button>
        ))}
      </div>

      {tab === "holdings" ? (
        <Panel title="Holdings">
          {holdings.length ? (
            <GenericTable
              rows={holdings}
              columns={[
                { header: "Symbol", render: (row) => String(pick(row, "trading_symbol", "tradingsymbol", "trdSym", "symbol", "sym") ?? "—") },
                { header: "Qty", render: (row) => <span className="num">{num(Number(pick(row, "quantity", "qty", "netqty") ?? NaN), 0)}</span> },
                { header: "Avg", render: (row) => <span className="num">{num(Number(pick(row, "average_price", "averageprice", "avg_price") ?? NaN))}</span> },
                { header: "LTP", render: (row) => <span className="num">{num(Number(pick(row, "last_price", "lastprice", "ltp") ?? NaN))}</span> },
                {
                  header: "P&L",
                  render: (row) => (
                    <span className={`num ${toneClass(Number(pick(row, "profitloss", "pnl", "profit") ?? NaN))}`}>
                      {signedInr(Number(pick(row, "profitloss", "pnl", "profit") ?? NaN))}
                    </span>
                  ),
                },
              ]}
            />
          ) : (
            <Empty title="No holdings" message="Kotak Neo returned no holdings for this account." />
          )}
        </Panel>
      ) : null}

      {tab === "positions" ? (
        <Panel title="Open positions">
          {positions.length ? (
            <GenericTable
              rows={positions}
              columns={[
                { header: "Symbol", render: (row) => String(pick(row, "trading_symbol", "trdSym", "symbol") ?? "—") },
                { header: "Product", render: (row) => String(pick(row, "product", "productCode") ?? "—") },
                { header: "Net qty", render: (row) => <span className="num">{num(Number(pick(row, "netqty", "net_quantity", "quantity") ?? NaN), 0)}</span> },
                { header: "Buy avg", render: (row) => <span className="num">{num(Number(pick(row, "buyprice", "buy_average", "netprice") ?? NaN))}</span> },
                { header: "LTP", render: (row) => <span className="num">{num(Number(pick(row, "last_price", "ltp", "netprice") ?? NaN))}</span> },
                {
                  header: "Unrealised",
                  render: (row) => (
                    <span className={`num ${toneClass(Number(pick(row, "unrealised_profit", "unrealized_profit", "upldmtm", "mto_mtm") ?? NaN))}`}>
                      {signedInr(Number(pick(row, "unrealised_profit", "unrealized_profit", "upldmtm", "mto_mtm") ?? NaN))}
                    </span>
                  ),
                },
              ]}
            />
          ) : (
            <Empty title="No open positions" message="Kotak Neo returned no open positions." />
          )}
        </Panel>
      ) : null}

      {tab === "orders" ? (
        <Panel title="Order book">
          {orders.length ? (
            <GenericTable
              rows={orders}
              columns={[
                { header: "Order", render: (row) => <span className="num text-3xs">{String(pick(row, "orderid", "orderId", "order_no") ?? "—")}</span> },
                { header: "Symbol", render: (row) => String(pick(row, "trading_symbol", "trdSym", "symbol") ?? "—") },
                { header: "Side", render: (row) => <span className={pick(row, "transaction_type", "transtype") === "B" ? "text-up" : "text-down"}>{String(pick(row, "transaction_type", "transtype") ?? "—")}</span> },
                { header: "Type", render: (row) => String(pick(row, "ordertype", "order_type") ?? "—") },
                { header: "Qty", render: (row) => <span className="num">{num(Number(pick(row, "quantity", "qty") ?? NaN), 0)}</span> },
                { header: "Filled", render: (row) => <span className="num">{num(Number(pick(row, "filledqty", "filled_quantity") ?? NaN), 0)}</span> },
                { header: "Price", render: (row) => <span className="num">{num(Number(pick(row, "price", "averageprice") ?? NaN))}</span> },
                { header: "Status", render: (row) => String(pick(row, "status", "orderstatus", "order_status") ?? "—") },
              ]}
            />
          ) : (
            <Empty title="No orders today" message="Kotak Neo returned no orders for the current session." />
          )}
        </Panel>
      ) : null}

      {tab === "trades" ? (
        <Panel title="Trade book">
          {trades.length ? (
            <GenericTable
              rows={trades}
              columns={[
                { header: "Order", render: (row) => <span className="num text-3xs">{String(pick(row, "orderid", "orderId") ?? "—")}</span> },
                { header: "Symbol", render: (row) => String(pick(row, "trading_symbol", "trdSym", "symbol") ?? "—") },
                { header: "Side", render: (row) => <span className={pick(row, "transaction_type", "transtype") === "B" ? "text-up" : "text-down"}>{String(pick(row, "transaction_type", "transtype") ?? "—")}</span> },
                { header: "Qty", render: (row) => <span className="num">{num(Number(pick(row, "quantity", "fillsize") ?? NaN), 0)}</span> },
                { header: "Price", render: (row) => <span className="num">{num(Number(pick(row, "fillprice", "price") ?? NaN))}</span> },
                { header: "Value", render: (row) => <span className="num">{inr(Number(pick(row, "fillvalue", "value") ?? NaN))}</span> },
              ]}
            />
          ) : (
            <Empty title="No trades today" message="Kotak Neo returned no fills for the current session." />
          )}
        </Panel>
      ) : null}
    </div>
  );
}

function GenericTable({
  rows: data,
  columns,
}: {
  rows: Record<string, unknown>[];
  columns: { header: string; render: (row: Record<string, unknown>) => React.ReactNode }[];
}) {
  return (
    <div className="overflow-auto" style={{ maxHeight: "34rem" }}>
      <table className="tbl">
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={column.header}>{column.header}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {data.map((row, index) => (
            <tr key={index}>
              {columns.map((column) => (
                <td key={column.header}>{column.render(row)}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function PortfolioPage() {
  return (
    <AppShell>
      <Portfolio />
    </AppShell>
  );
}

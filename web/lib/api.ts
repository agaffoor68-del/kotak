/**
 * Typed client for the AlphaTradePro backend.
 *
 * The token lives in memory plus `sessionStorage` so a refresh keeps the user
 * signed in without ever writing credentials to disk.
 */

const DEFAULT_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8000";
let base = DEFAULT_BASE;
let accessToken: string | null = null;

/**
 * The deployed API hosts, tried in order when nothing is configured.
 *
 * A free-tier Render instance sleeps after a few minutes of inactivity and
 * takes up to a minute to wake, so the list ends with the known deployment
 * rather than the local development port.
 */
const KNOWN_BACKENDS = [
  "https://kotak-xs4d.onrender.com",
  "http://127.0.0.1:8000",
];

export function setApiBase(next: string) {
  base = next.replace(/\/+$/, "");
}

export function getApiBase() {
  return base;
}

/** In a browser, work out which host actually serves the API. */
export function resolveApiBase() {
  if (typeof window === "undefined") return base;
  const configured = window.localStorage.getItem("alphatrade.api-base");
  if (configured) {
    setApiBase(configured);
    return base;
  }
  return base;
}

/**
 * Work out the API host once, then remember it.
 *
 * In production nginx serves the app and the API on one origin, so same-origin
 * is correct. In development they are separate ports, so the same-origin probe
 * 404s and we fall back to the backend port. Doing this automatically means a
 * phone pointed at a tunnel URL works with no manual configuration.
 */
let resolvedBase: string | null = null;

export async function ensureApiBase(): Promise<string> {
  if (resolvedBase) return resolvedBase;
  if (typeof window === "undefined") return base;

  const candidates: string[] = [];
  const configured = window.localStorage.getItem("alphatrade.api-base");
  if (configured) {
    setApiBase(configured);
    return base;
  }
  // A same-origin deployment needs no probing; only try it when the configured
  // base actually points somewhere else.
  if (!base.startsWith(window.location.origin)) {
    candidates.push(window.location.origin);
  }
  if (base && !candidates.includes(base)) candidates.push(base);
  for (const known of KNOWN_BACKENDS) {
    if (!candidates.includes(known)) candidates.push(known);
  }

  const tried: string[] = [];
  for (const candidate of candidates) {
    tried.push(candidate);
    try {
      // `/health` is deliberately dependency-free: it answers in milliseconds
      // even while the broker sessions are down. The cross-origin backend may
      // be a free-tier host that is asleep and needs 15-50s to wake, so the
      // budget here is deliberately generous. `request()` below uses a plain
      // fetch with no timeout, so once this probe succeeds the real calls ride
      // the now-warm instance.
      const response = await fetch(`${candidate}/health`, {
        cache: "no-store",
        signal: AbortSignal.timeout(60000),
      });
      if (response.ok) {
        resolvedBase = candidate;
        setApiBase(candidate);
        return candidate;
      }
    } catch {
      // Try the next candidate.
    }
  }

  // Nothing answered; keep the configured default so the error is a real
  // "cannot reach the backend" rather than a silent redirect.
  resolvedBase = base;
  tried.push(base);
  return base;
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly detail?: unknown,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/* ----------------------------------------------------------------- session */

const STORAGE_KEY = "alphatrade.token";

export function loadToken(): string | null {
  if (accessToken) return accessToken;
  if (typeof window === "undefined") return null;
  accessToken = window.sessionStorage.getItem(STORAGE_KEY);
  return accessToken;
}

export function setToken(token: string | null) {
  accessToken = token;
  if (typeof window === "undefined") return;
  if (token) window.sessionStorage.setItem(STORAGE_KEY, token);
  else window.sessionStorage.removeItem(STORAGE_KEY);
}

/* ---------------------------------------------------------------- requests */

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  await ensureApiBase();
  const token = loadToken();
  let response: Response;
  try {
    response = await fetch(`${base}${path}`, {
      cache: "no-store",
      ...init,
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(init.headers ?? {}),
      },
    });
  } catch {
    // `fetch` rejects for three very different reasons — the API is not
    // running, the host is wrong, or the browser blocked the response on CORS —
    // and it reports all of them as a bare "Failed to fetch". Say which host was
    // tried so the cause is obvious instead of a dead end.
    throw new ApiError(
      `Cannot reach the backend at ${base}. Check that it is running and that this page's origin is allowed by the API's CORS settings.`,
      0,
    );
  }

  const text = await response.text();
  let payload: unknown = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    throw new ApiError(
      response.ok ? "The server returned a non-JSON response." : `Request failed (${response.status})`,
      response.status,
      text.slice(0, 200),
    );
  }

  if (!response.ok) {
    if (response.status === 401) setToken(null);
    throw new ApiError(extractMessage(payload), response.status, payload);
  }
  return payload as T;
}

/** Pull a readable message out of FastAPI's several error shapes. */
function extractMessage(payload: unknown): string {
  const body = payload as { detail?: unknown; message?: unknown } | null;
  const detail = body?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        const entry = item as { msg?: string; errors?: string[] };
        if (Array.isArray(entry.errors)) return entry.errors.join("; ");
        return entry.msg ?? JSON.stringify(item);
      })
      .join("; ");
  }
  if (detail && typeof detail === "object") {
    const entry = detail as { message?: string; reason?: string; errors?: string[] };
    if (Array.isArray(entry.errors)) return entry.errors.join("; ");
    if (entry.message) return entry.reason ? `${entry.reason}: ${entry.message}` : entry.message;
    if (entry.reason) return entry.reason;
    return JSON.stringify(detail);
  }
  if (typeof body?.message === "string") return body.message;
  return "The request could not be completed.";
}

const get = <T,>(path: string) => request<T>(path);
const post = <T,>(path: string, body?: unknown) =>
  request<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });
const put = <T,>(path: string, body: unknown) =>
  request<T>(path, { method: "PUT", body: JSON.stringify(body) });
const patch = <T,>(path: string, body: unknown) =>
  request<T>(path, { method: "PATCH", body: JSON.stringify(body) });
const del = <T,>(path: string) => request<T>(path, { method: "DELETE" });

/* ------------------------------------------------------------------- types */

export interface User {
  id: string;
  email: string;
  role: "admin" | "trader" | "viewer";
  account_id?: string | null;
  is_active?: boolean;
  whatsapp_number?: string | null;
}

export interface Quote {
  token: string;
  symbol?: string;
  name?: string | null;
  exchange_segment: string;
  last: number | null;
  open: number | null;
  high: number | null;
  low: number | null;
  previous_close: number | null;
  change: number | null;
  change_percent: number | null;
  volume: number | null;
  open_interest: number | null;
  bid: number | null;
  ask: number | null;
  implied_volatility: number | null;
  upper_circuit: number | null;
  lower_circuit: number | null;
  average_price: number | null;
  updated_at: number;
  age_seconds: number;
}

export interface Instrument {
  token: string;
  exchange_segment: string;
  symbol: string;
  trading_symbol: string;
  name?: string;
  series?: string;
  expiry?: string;
  option_type?: string;
  strike?: number;
  lot_size?: number;
  tick_size?: number;
  is_index: number;
  quote?: Quote | null;
  /** The other exchanges the same symbol trades on. */
  listings?: { token: string; exchange_segment: string }[];
}

/** One row of the user's watchlist. */
export interface WatchlistItem {
  token: string;
  exchange_segment: string;
  symbol: string;
  name?: string | null;
  position?: number;
  created_at?: number;
}

export interface Candle {
  // Backend returns timestamp as string, convert to number for chart
  // Keep both fields for backward compatibility during migration
  // Coverage interface for market data availability
// export type Coverage = { ... }  // Removed: duplicate identifier, defined below

  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  trades: number;
}

export interface MarketStatus {
  session: {
    state: string;
    is_open: boolean;
    reason: string;
    ist: string;
    date: string;
    weekday: string;
  };
  seconds_until_open: number;
  /** Every quote and candle here comes from Angel One SmartAPI. */
  data_source: string;
  feed: {
    broker?: string;
    status: string;
    frames_received: number;
    reconnects: number;
    subscribed_instruments: number;
    last_error: string | null;
    cached_quotes: number;
  };
  scrip_master: {
    status: string;
    instruments: number;
    last_sync: number | null;
    last_error: string | null;
  };
  angel_instruments?: AngelInstrumentStatus;
  broker_session?: {
    configured?: boolean;
    authenticated?: boolean;
    status: string;
    last_error: string | null;
  };
  intervals: string[];
  record_ticks: boolean;
  features?: {
    live_trading?: boolean;
    paper_trading?: boolean;
    backtest?: boolean;
    algo_execution?: boolean;
  };
}

export interface FiiDiiData {
  date: string;
  fii_net: string | number;
  dii_net: string | number;
  fii_buy?: string | number;
  fii_sell?: string | number;
  dii_buy?: string | number;
  dii_sell?: string | number;
  updated_at?: number;
  source?: string;
}

export interface OptionLeg {
  token?: string;
  trading_symbol?: string;
  lot_size?: number | null;
  ltp: number | null;
  open_interest: number | null;
  volume: number | null;
  change_percent: number | null;
  bid: number | null;
  ask: number | null;
  iv: number | null;
  delta?: number | null;
  gamma?: number | null;
}

export interface BreadthSnapshot {
  as_of?: number;
  universe_size: number;
  priced: number;
  breadth: {
    priced?: number | null;
    advances?: number | null;
    declines?: number | null;
    unchanged?: number | null;
    limit_up?: number | null;
    limit_down?: number | null;
    advance_decline_ratio?: number | null;
    volume_ratio?: number | null;
  };
  top_gainers: Quote[];
  top_losers: Quote[];
  most_active: Quote[];
}

export interface Coverage {
  ticks: number;
  first_tick: number | null;
  last_tick: number | null;
  span_seconds: number;
  has_history: boolean;
  note?: string;
}

export interface OptionChain {
  underlying: string;
  expiry: string | null;
  expiries: string[];
  rows: { strike: number; call: OptionLeg; put: OptionLeg }[];
  spot: number | null;
  has_data: boolean;
  greeks_available: boolean;
  summary: {
    total_call_oi: number;
    total_put_oi: number;
    pcr: number | null;
    max_pain: number | null;
    atm_strike: number | null;
    support: number[];
    resistance: number[];
    oi_change_bias: string;
  };
  note?: string;
  fetch_error?: string | null;
}

export interface BacktestTrade {
  symbol: string;
  side: string;
  quantity: number;
  entry_time: number;
  entry_price: number;
  exit_time: number | null;
  exit_price: number | null;
  reason: string;
  gross_pnl: number;
  charges: number;
  net_pnl: number;
  pnl_percent: number;
}

export interface BacktestResult {
  status: "ok" | "no_data" | "invalid" | string;
  metrics: Record<string, unknown>;
  trades: BacktestTrade[];
  equity_curve: number[];
  drawdown_curve: number[];
  bars: number;
  note: string;
  warnings: string[];
  error: string | null;
  coverage?: { label: string; bars: number }[];
}

/** What the automatic history step did during a one-click backtest. */
export interface BacktestDownload {
  attempted: boolean;
  performed: boolean;
  bars: number;
  source: string | null;
  error: string | null;
  seconds: number;
  yf_ticker?: string;
}

export interface CoachFinding {
  pattern: string;
  title: string;
  occurrences: number;
  recommendation: string;
  evidence?: unknown[];
}

export interface LiveAnalysis {
  index: string;
  underlying: string;
  symbol: string;
  signal: "BUY CE" | "BUY PE" | "WAIT";
  strike: string;
  strike_price?: number;
  confidence: string;
  reason: string;
  trend: "Bullish" | "Bearish" | "Unknown";
  price?: number;
  vwap?: number;
  rsi?: number;
  supertrend?: number;
  ema20?: number;
  ema50?: number;
  confluence_score?: string;
  avoiding_loss_pattern?: boolean;
  timestamp: number;
  status: string;
}

export interface CoachResponse {
  has_data: boolean;
  disclaimer: string;
  findings: CoachFinding[];
  narrative: string | null;
  note?: string;
  llm_enabled?: boolean;
  mode?: string;
  analysed_trades?: number;
  suggestions?: { reason: string; change: string; rule?: unknown; timeframe?: string }[];
}

export interface IndicatorCatalog {
  indicators: { name: string; input: string; outputs: string }[];
  price_fields: string[];
  comparators: string[];
  cross_comparators: string[];
  logical: string[];
  timeframes: string[];
}

export interface RiskStatus {
  equity: number;
  peak_equity: number;
  current_drawdown: number;
  drawdown_percent: number;
  realised_today: number;
  daily_loss_remaining: number | null;
  daily_limit_breached: boolean;
  drawdown_limit_breached: boolean;
  kill_switch: boolean;
  trading_halted: boolean;
  halt_reason: string | null;
  open_positions: number;
  orders_last_minute: number;
  config: Record<string, number>;
}

export interface AlertRow {
  id: string;
  kind: string;
  symbol: string | null;
  condition: Record<string, unknown>;
  channels: string[];
  is_active: number;
  trigger_count: number;
  triggered_at: number | null;
}


export interface CalculatedIndicatorResult {
  symbol: string;
  token: string;
  timeframe: string;
  indicator_name: string;
  columns: string[];
  values: ({ time: number; close?: number } & Record<string, number | null | undefined>)[];
}

export interface AvailableIndicatorInfo {
  name: string;
  category: string;
  label: string;
  params?: Record<string, unknown>;
}

export interface IndicatorCatalogResponse {
  count: number;
  popular: AvailableIndicatorInfo[];
  indicators: AvailableIndicatorInfo[];
}

/* ------------------------------------------------------------------ routes */

export interface PerformanceSummary {
  final_equity: number;
  total_return: number;
  total_return_percent: number | null;
  cagr_percent: number | null;
  sharpe_ratio: number | null;
  sortino_ratio: number | null;
  profit_factor: number | null;
  recovery_factor: number | null;
  max_drawdown: { absolute: number; percent: number; peak_index: number | null; trough_index: number | null };
  daily_volatility_percent: number;
  trades: {
    count: number;
    wins: number;
    losses: number;
    win_rate_percent: number | null;
    gross_profit: number;
    gross_loss: number;
    net_profit: number;
    average_win: number | null;
    average_loss: number | null;
    largest_win: number | null;
    largest_loss: number | null;
    expectancy: number | null;
  };
  assumptions: Record<string, unknown>;
}

export interface PnlBucket {
  date?: string;
  month?: string;
  trades: number;
  net_pnl: number;
  win_rate: number;
}

export interface PerformancePayload {
  mode?: "paper" | "backtesting";
  has_data: boolean;
  note?: string;
  summary?: PerformanceSummary;
  daily: PnlBucket[];
  monthly: PnlBucket[];
  drawdown_curve: number[];
}

export interface StrategyRow {
  strategy: string;
  trades: number;
  net_pnl: number;
  win_rate_percent: number | null;
  profit_factor: number | null;
  average_pnl: number;
}

export interface RiskEventRow {
  id: string;
  kind: string;
  detail: string | null;
  created_at: number;
}

export interface StrategyRowRecord {
  id: string;
  name: string;
  description?: string;
  definition: Record<string, unknown>;
  updated_at?: number;
  validation?: { valid: boolean; errors: string[] };
}

export interface PineImportResult {
  definition: Record<string, unknown>;
  warnings: string[];
  errors: string[];
  valid: boolean;
}

export interface PineExportResult {
  code: string;
  warnings: string[];
}

export const api = {
  health: () => request<Record<string, unknown>>("/api/v1/system/health"),

  // auth
  login: (email: string, password: string) =>
    post<{ access_token: string; expires_in: number; user: User }>("/api/v1/auth/login", { email, password }),
  me: () => get<User>("/api/v1/auth/me"),
  listUsers: () => get<User[]>("/api/v1/auth/users"),
  createUser: (email: string, password: string, role: string) =>
    post<User>("/api/v1/auth/users", { email, password, role }),
  updateUserRole: (userId: string, role: string) =>
    patch<{ id: string; email: string; role: string; is_active: boolean }>(
      `/api/v1/admin/users/${encodeURIComponent(userId)}/role`,
      { role },
    ),
  updateSettings: (whatsappNumber: string | null) =>
    put<{ id: string; email: string; role: string; whatsapp_number: string | null }>(
      "/api/v1/users/settings",
      { whatsapp_number: whatsappNumber },
    ),
  createApiKey: (name: string) => post<{ api_key: string; warning: string }>("/api/v1/auth/api-keys", { name }),

  // market
  status: () => get<MarketStatus>("/api/v1/market/status"),
  search: (q: string, limit = 25, scope: "equity" | "all" = "equity") =>
    get<{ query: string; count: number; results: Instrument[] }>(
      `/api/v1/market/search?q=${encodeURIComponent(q)}&limit=${limit}&scope=${scope}`,
    ),
  /** The local symbol master: instant, and never calls the broker. */
  searchInstruments: (q: string, limit = 10) =>
    get<{ query: string; count: number; results: Instrument[] }>(
      `/api/v1/market/search?q=${encodeURIComponent(q)}&limit=${limit}`,
    ),
  /** Turn a symbol the user typed into the instrument that carries the token. */
  resolveInstrument: (symbol: string) =>
    get<Instrument>(`/api/v1/market/resolve?symbol=${encodeURIComponent(symbol)}`),
  /** The live quote for one instrument, by symbol token. */
  quote: (token: string, exchangeSegment?: string, refresh = false) => {
    const params = new URLSearchParams();
    if (exchangeSegment) params.set("exchange_segment", exchangeSegment);
    if (refresh) params.set("refresh", "true");
    const query = params.toString();
    return get<{
      token: string;
      exchange_segment: string;
      exchange: string;
      symbol?: string;
      name?: string;
      source: string;
      cached: boolean;
      quote: Quote;
    }>(`/api/v1/market/quote/${encodeURIComponent(token)}${query ? `?${query}` : ""}`);
  },
  watchlist: () => get<{ items: WatchlistItem[]; count: number }>("/api/v1/market/watchlist"),
  addToWatchlist: (items: { token: string; symbol?: string; exchange_segment?: string }[]) =>
    post<{ status: string; count: number; items: WatchlistItem[] }>("/api/v1/market/watchlist", { items }),
  removeFromWatchlist: (token: string, exchangeSegment?: string) =>
    del<{ status: string; removed: number; count: number; items: WatchlistItem[] }>(
      `/api/v1/market/watchlist/${encodeURIComponent(token)}` +
        (exchangeSegment ? `?exchange_segment=${encodeURIComponent(exchangeSegment)}` : ""),
    ),
  indices: () => get<(Instrument & { quote?: Quote | null })[]>("/api/v1/market/indices"),
  underlyings: () => get<string[]>("/api/v1/market/underlyings"),
  futures: (exchangeSegment = "nse_fo") => get<Instrument[]>(`/api/v1/market/futures?exchange_segment=${exchangeSegment}`),
  quotes: (tokens: { instrument_token: string; exchange_segment: string }[], quoteType = "all") =>
    post<{ as_of: number; source: string; count: number; quotes: Quote[] }>("/api/v1/market/quotes", {
      tokens,
      quote_type: quoteType,
    }),
  fiiDii: (refresh = false) =>
    get<FiiDiiData>(`/api/v1/market/fii-dii${refresh ? "?refresh=true" : ""}`),
  breadth: (limit = 200) =>
    get<BreadthSnapshot>(`/api/v1/market/breadth?limit=${limit}`),
  candles: (token: string, exchangeSegment: string, interval: string, limit = 400, days?: number) =>
    get<{ candles: Candle[]; count: number; cached: boolean; coverage: Coverage }>(
      `/api/v1/market/candles/${encodeURIComponent(token)}?exchange_segment=${exchangeSegment}&interval=${interval}&limit=${limit}${
        days ? `&days=${days}` : ""
      }`,
    ),
  syncMaster: (force = false) => post<Record<string, unknown>>(`/api/v1/market/sync-master?force=${force}`),
  subscribe: (tokens: string[], exchangeSegment = "nse_cm") =>
    get<{ subscribed: unknown[]; total: number }>(
      `/api/v1/market/feed/subscribe?tokens=${encodeURIComponent(tokens.join(","))}&exchange_segment=${exchangeSegment}`,
    ),

  // options
  chain: (underlying: string, expiry?: string) =>
    get<OptionChain>(
      `/api/v1/options/chain?underlying=${encodeURIComponent(underlying)}${
        expiry ? `&expiry=${encodeURIComponent(expiry)}` : ""
      }`,
    ),
  optionExpiries: (underlying: string) =>
    get<{ underlying: string; expiries: string[]; near: string | null; count: number }>(
      `/api/v1/options/expiries?underlying=${encodeURIComponent(underlying)}`,
    ),

  // indicators dynamic engine (100+ indicators via pandas_ta)
  calculateIndicator: (body: {
    symbol: string;
    timeframe?: string;
    indicator_name: string;
    params?: Record<string, unknown>;
  }) => post<CalculatedIndicatorResult>("/api/v1/indicators/calculate", body),
  listDynamicIndicators: (category?: string) =>
    get<IndicatorCatalogResponse>(
      `/api/v1/indicators/list${category ? `?category=${encodeURIComponent(category)}` : ""}`,
    ),

  // strategies
  indicators: () => get<IndicatorCatalog>("/api/v1/strategies/indicators"),
  templates: () =>
    get<(Record<string, unknown> & { id: string; name: string; valid: boolean })[]>(
      "/api/v1/strategies/templates",
    ),
  strategies: () => get<StrategyRowRecord[]>("/api/v1/strategies"),
  strategy: (id: string) =>
    get<StrategyRowRecord & { resolved_universe?: { token: string; label: string; exchange_segment: string }[] }>(
      `/api/v1/strategies/${id}`,
    ),
  saveStrategy: (name: string, description: string, definition: unknown) =>
    post<{ id: string }>("/api/v1/strategies", { name, description, definition }),
  updateStrategy: (id: string, name: string, description: string, definition: unknown) =>
    request<{ id: string }>(`/api/v1/strategies/${id}`, {
      method: "PUT",
      body: JSON.stringify({ name, description, definition }),
    }),
  deleteStrategy: (id: string) => del<{ deleted: boolean }>(`/api/v1/strategies/${id}`),
  validateStrategy: (definition: unknown) =>
    post<{ valid: boolean; errors: string[] }>("/api/v1/strategies/validate", definition),
  importPine: (body: { code: string; name?: string; timeframe?: string; token?: string; label?: string }) =>
    post<PineImportResult>("/api/v1/strategies/pine/import", body),
  exportPine: (definition: unknown) =>
    post<PineExportResult>("/api/v1/strategies/pine/export", { definition }),
  downloadHistoricalData: (body: {
    symbol_token: string;
    symbol: string;
    start_date: string;
    end_date: string;
    timeframe: string;
  }) => post<{
    status: string;
    message: string;
    bars: number;
    symbol: string;
    symbol_token: string;
    yf_ticker?: string;
    source?: string;
  }>("/api/v1/backtest/download-data", body),
  backtest: (body: {
    strategy_id?: string;
    definition?: unknown;
    initial_capital?: number;
    slippage?: string;
    timeframe?: string;
    symbol_token?: string;
    start_date?: string;
    end_date?: string;
  }) => post<BacktestResult>("/api/v1/strategies/backtest", body),
  /**
   * The one-click pipeline: resolve the symbol, download whatever history is
   * missing, and run the simulation — all in one request.
   */
  runBacktest: (body: {
    strategy_id?: string;
    definition?: unknown;
    symbol?: string;
    symbol_token?: string;
    exchange_segment?: string;
    initial_capital?: number;
    slippage?: string;
    timeframe?: string;
    start_date?: string;
    end_date?: string;
    history_days?: number;
    auto_download?: boolean;
  }) => post<BacktestResult & { download?: BacktestDownload; symbol?: string }>("/api/v1/backtest/run", body),

  // execution
  portfolio: (broker?: ExecutionBroker) =>
    get<Record<string, unknown>>(`/api/v1/portfolio${broker ? `?broker=${broker}` : ""}`),
  executionBrokers: () => get<ExecutionBrokersResponse>("/api/v1/execution/brokers"),
  charges: (exchangeSegment: string, product: string, quantity: number, price: number, isOption = false) =>
    get<{
      buy: Record<string, number>;
      sell: Record<string, number>;
      round_trip: number;
      break_even_move_percent: number | null;
    }>(
      `/api/v1/charges?exchange_segment=${exchangeSegment}&product=${product}&quantity=${quantity}&price=${price}&is_option=${isOption}`,
    ),
  liveOrder: (order: Record<string, unknown>) => post<Record<string, unknown>>("/api/v1/orders/live", order),
  cancelOrder: (orderId: string, broker?: ExecutionBroker) =>
    post<Record<string, unknown>>("/api/v1/orders/cancel", {
      order_id: orderId,
      broker: broker ?? null,
      confirmation: LIVE_PHRASE,
    }),
  paperOrder: (order: Record<string, unknown>) => post<Record<string, unknown>>("/api/v1/paper/orders", order),
  paperClose: (symbol: string, quantity?: number) =>
    post<Record<string, unknown>>("/api/v1/paper/close", { trading_symbol: symbol, quantity }),
  paperPortfolio: () =>
    get<{
      equity: number;
      starting_capital: number;
      realised_pnl: number;
      unrealised_pnl: number;
      open_positions: {
        symbol: string;
        quantity: number;
        avg_entry: number;
        last_price: number | null;
        unrealised_pnl: number | null;
      }[];
    }>("/api/v1/paper/portfolio"),
  paperJournal: () => get<Record<string, unknown>[]>("/api/v1/paper/journal"),
  algoRuns: () => get<Record<string, unknown>[]>("/api/v1/algo/runs"),
  addJournalNote: (body: {
    symbol: string;
    side: string;
    quantity: number;
    entry_price: number;
    exit_price?: number | null;
    trade_id?: string | null;
    setup?: string;
    emotion?: string;
    followed_plan?: boolean;
    notes?: string;
  }) => post<{ id: string; created: boolean }>("/api/v1/paper/journal", body),
  resetPaper: () => post<Record<string, unknown>>("/api/v1/paper/reset"),
  risk: () => get<RiskStatus>("/api/v1/risk"),
  updateRisk: (changes: Record<string, number | string>) =>
    put<Record<string, number | string>>("/api/v1/risk", changes),
  killSwitch: (reason: string) => post<Record<string, unknown>>("/api/v1/risk/kill-switch", { reason }),
  releaseKillSwitch: () => post<Record<string, unknown>>("/api/v1/risk/release"),
  startAlgo: (strategyId: string, mode: "paper" | "live", broker?: ExecutionBroker) =>
    post<Record<string, unknown>>(
      `/api/v1/algo/runs?strategy_id=${strategyId}&mode=${mode}${broker ? `&broker=${broker}` : ""}`,
    ),
  stopAlgo: (runId: string) => post<Record<string, unknown>>(`/api/v1/algo/runs/${runId}/stop`),
  riskEvents: (limit = 50) => get<RiskEventRow[]>(`/api/v1/risk/events?limit=${limit}`),
  // The header's reconnect button; scoped to the account the caller owns.
  reconnect: () => post<Record<string, unknown>>("/api/v1/broker/session/reconnect"),

  // broker connection panel
  brokerAccounts: () => get<BrokerAccountsResponse>("/api/v1/broker/accounts"),
  brokerSession: () => get<BrokerAccountView>("/api/v1/broker/session"),
  brokerNetwork: () => get<BrokerNetworkResponse>("/api/v1/broker/network"),
  brokerTest: (body: BrokerCredentialsInput) => post<BrokerTestResult>("/api/v1/broker/test", body),
  brokerConnect: (body: BrokerCredentialsInput & { label?: string; test_before_save?: boolean }) =>
    post<BrokerConnectResponse>("/api/v1/broker/accounts", body),
  brokerTestSaved: (accountId: string) =>
    post<BrokerTestResult>(`/api/v1/broker/accounts/${encodeURIComponent(accountId)}/test`),
  brokerReconnect: (accountId: string) =>
    post<BrokerAccountView>(`/api/v1/broker/accounts/${encodeURIComponent(accountId)}/reconnect`),
  brokerDisconnect: (accountId: string) =>
    del<{ removed: boolean; account_id: string }>(`/api/v1/broker/accounts/${encodeURIComponent(accountId)}`),

  // Angel One (market data) connection
  angelTest: (body: AngelCredentialsInput) => post<BrokerTestResult>("/api/v1/broker/angel/test", body),
  angelConnect: (body: AngelCredentialsInput & { label?: string; test_before_save?: boolean }) =>
    post<AngelConnectResponse>("/api/v1/broker/angel/accounts", body),
  angelSession: () => get<ExchangeSessionHealth>("/api/v1/broker/angel/session"),
  angelReconnect: () => post<ExchangeSessionHealth>("/api/v1/broker/angel/session/reconnect"),

  // analytics
  performance: (mode: "paper" | "backtesting" = "paper") => get<PerformancePayload>(`/api/v1/analytics/performance?mode=${mode}`),
  strategyAnalytics: (mode: "paper" | "backtesting" = "paper") => get<StrategyRow[]>(`/api/v1/analytics/strategies?mode=${mode}`),
  performanceHeatmap: (days = 180, mode: "paper" | "backtesting" = "paper") =>
    get<{ grid: Record<string, Record<string, number>>; weekdays: string[]; days: number }>(
      `/api/v1/analytics/heatmap?mode=${mode}&days=${days}`,
    ),
  coach: (mode: "loss_analysis" | "strategy_suggestions" = "loss_analysis") =>
    get<CoachResponse>(`/api/v1/coach?mode=${mode}`),

  // AI
  suggestTrade: () => get<LiveAnalysis>("/api/v1/ai/suggest_trade"),
  liveAnalysis: (underlying?: string) =>
    get<LiveAnalysis[] | LiveAnalysis>(
      underlying ? `/api/v1/ai/live-analysis?underlying=${encodeURIComponent(underlying)}` : "/api/v1/ai/live-analysis"
    ),
  liveAnalysisMulti: () => get<LiveAnalysis[]>("/api/v1/ai/live-analysis"),
  recordAiTrade: (body: {
    index: string;
    signal: string;
    entry_price: number;
    exit_price?: number;
    pnl?: number;
    indicators_at_entry?: Record<string, unknown>;
    result: "WIN" | "LOSS";
  }) => post<{ status: string; id: number }>("/api/v1/ai/trade-history", body),
  aiTradeHistory: (index?: string) =>
    get<Record<string, unknown>[]>(
      index ? `/api/v1/ai/trade-history?index=${encodeURIComponent(index)}` : "/api/v1/ai/trade-history"
    ),
  executeOption: (body: Record<string, unknown>) => post<Record<string, unknown>>("/api/v1/orders/options", body),
  alerts: () => get<{ alerts: AlertRow[]; channels: Record<string, boolean> }>("/api/v1/alerts"),
  createAlert: (body: { kind: string; symbol?: string; condition: Record<string, unknown>; channels: string[] }) =>
    post<AlertRow>("/api/v1/alerts", body),
  deleteAlert: (id: string) => del<{ deleted: boolean }>(`/api/v1/alerts/${id}`),
  testAlert: (id: string) =>
    post<{ message: string; delivery: Record<string, { status: string }> }>(`/api/v1/alerts/${id}/test`),

  // share
  // The browser's own origin is sent so the QR points at the terminal the user is
  // actually looking at, not at the API's port, on split-host deployments.
  share: (symbol?: string) => {
    const params = new URLSearchParams();
    if (typeof window !== "undefined") params.set("origin", window.location.origin);
    if (symbol) params.set("symbol", symbol.toUpperCase());
    const query = params.toString();
    return get<ShareResponse>(`/api/v1/share${query ? `?${query}` : ""}`);
  },

  // deployment: static IP + redirect URL (SEBI compliance)
  deployment: (refresh = false) =>
    get<DeploymentReport>(`/api/v1/system/deployment${refresh ? "?refresh=true" : ""}`),
  refreshDeployment: () => post<DeploymentReport>("/api/v1/system/deployment/refresh"),
};

/* ------------------------------------------------------- broker connection */

/** Exactly what the connect form collects. Secrets never round-trip back. */
export interface BrokerCredentialsInput {
  consumer_key: string;
  totp_key: string;
  mpin: string;
  mobile_number: string;
  ucc: string;
  neo_fin_key?: string | null;
  environment: "prod" | "uat";
}

export interface ExchangeSessionHealth {
  account_id: string | null;
  broker?: string;
  authenticated: boolean;
  status: string;
  last_error?: string | null;
  age_seconds?: number;
  expires_in_seconds?: number;
  configured?: boolean;
  feed_token?: boolean;
  profile?: Record<string, unknown> | null;
}

export type BrokerName = "kotak" | "angel";
/** Where an order goes. `both` fans one signal out to every enabled broker. */
export type ExecutionBroker = BrokerName | "both";

export interface BrokerAccountView {
  account_id: string | null;
  broker?: BrokerName;
  label?: string;
  environment?: string;
  configured?: boolean;
  is_active?: number;
  created_at?: number;
  updated_at?: number;
  session: ExchangeSessionHealth;
  status?: string;
  message?: string;
}

export interface BrokerAccountsResponse {
  is_admin: boolean;
  accounts: {
    account_id: string;
    broker: BrokerName;
    label: string;
    environment: string;
    created_at: number;
    updated_at: number;
    session: ExchangeSessionHealth;
  }[];
  default_account_id: string | null;
  vault_enabled: boolean;
  legacy_env_fallback: boolean;
}

export interface ExecutionBrokersResponse {
  enabled: BrokerName[];
  default: ExecutionBroker;
  available: ExecutionBroker[];
  data_source: string;
}

/* ---------------------------------------------------------- Angel One data */

/** Exactly what the Angel One connect form collects. */
export interface AngelCredentialsInput {
  api_key: string;
  client_code: string;
  pin: string;
  totp_key: string;
  environment: "prod" | "uat";
}

export interface AngelConnectResponse {
  account: BrokerAccountView;
  /** Redacted view: API key shown as `••••xxxx`, secrets never echoed. */
  masked: Record<string, string>;
  verified: boolean;
  bound_to_user: string;
}

export interface AngelInstrumentStatus {
  source: string;
  status: string;
  last_sync: number | null;
  last_error: string | null;
  counts: Record<string, number>;
  instruments: number;
  stale: boolean;
}

export interface BrokerTestResult {
  ok: boolean;
  error: string | null;
  seconds: number;
  state?: Record<string, unknown>;
}

export interface BrokerConnectResponse {
  account: BrokerAccountView;
  /** Redacted view: consumer key shown as `••••xxxx`, secrets never echoed. */
  masked: Record<string, string>;
  verified: boolean;
  bound_to_user: string;
}

export interface BrokerAddressProbe {
  address: string;
  family: "ipv4" | "ipv6";
  reachable: boolean;
  latency_ms: number | null;
  error: string | null;
}

export interface BrokerNetworkHost {
  label: string;
  host: string;
  required: boolean;
  dns_error: string | null;
  ipv4: string[];
  ipv6: string[];
  results: BrokerAddressProbe[];
}

export interface BrokerNetworkResponse {
  checked_at: number;
  verdict: "dual_stack" | "ipv4_only" | "ipv6_unreachable" | "degraded" | "unreachable";
  hosts_down: string[];
  required_hosts_down: string[];
  summary: {
    ipv4_resolved: number;
    ipv4_reachable: number;
    ipv6_resolved: number;
    ipv6_reachable: number;
    timeout_seconds: number;
    local_ipv6: boolean;
  };
  hosts: BrokerNetworkHost[];
  note: string;
}

/* ------------------------------------------------------- deployment facts */

/** Static IP + redirect URL: what a broker must be told before live orders. */
export interface StaticIpReport {
  registered_ip: string | null;
  detected_ip: string | null;
  detection_source: "override" | "remote" | "disabled" | "unavailable";
  detection_error: string | null;
  checked_at: number;
  reserved: boolean;
  status: "verified" | "mismatch" | "unregistered" | "unknown";
  message: string;
  private_address: string | null;
  request_origin: string | null;
  note: string;
}

export interface RedirectReport {
  path: string;
  production: string;
  development: string;
  allowlist: string[];
  scheme: string;
  host: string;
  https: boolean;
  configured: boolean;
}

export interface DeploymentCheck {
  id: string;
  label: string;
  ok: boolean;
  detail: string;
}

export interface DeploymentReport {
  static_ip: StaticIpReport;
  redirect: RedirectReport;
  checklist: DeploymentCheck[];
  ready_for_live: boolean;
  environment: string;
  generated_at: number;
}

export interface ShareResponse {
  base_url: string;
  api_base_url: string;
  links: Record<string, string>;
  query: Record<string, string>;
  qr_payload: string;
  qr_endpoint: string;
  configured_from: "browser" | "env" | "request";
  note: string;
}

export const LIVE_PHRASE = "PLACE LIVE ORDER";

/** WebSocket URL for the live tick stream, carrying the JWT as a query param. */
export function marketSocketUrl(): string {
  const resolved = resolvedBase ?? resolveApiBase();
  const url = resolved.replace(/^http/, "ws");
  return `${url}/ws/market?token=${encodeURIComponent(loadToken() ?? "")}`;
}

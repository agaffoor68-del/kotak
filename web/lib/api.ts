/**
 * Typed client for the AlphaTradePro backend.
 *
 * The token lives in memory plus `sessionStorage` so a refresh keeps the user
 * signed in without ever writing credentials to disk.
 */

const DEFAULT_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8000";
let base = DEFAULT_BASE;
let accessToken: string | null = null;

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

  const candidates = [base];
  // A same-origin deployment needs no probing; only try it when the configured
  // base actually points somewhere else.
  if (!base.startsWith(window.location.origin)) {
    candidates.unshift(window.location.origin);
  }

  for (const candidate of candidates) {
    try {
      const response = await fetch(`${candidate}/api/v1/system/health`, {
        cache: "no-store",
        signal: AbortSignal.timeout(4000),
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
  const response = await fetch(`${base}${path}`, {
    cache: "no-store",
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(init.headers ?? {}),
    },
  });

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
const del = <T,>(path: string) => request<T>(path, { method: "DELETE" });

/* ------------------------------------------------------------------- types */

export interface User {
  id: string;
  email: string;
  role: "admin" | "trader" | "viewer";
  account_id?: string | null;
  is_active?: boolean;
}

export interface Quote {
  token: string;
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
}

export interface Candle {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  trades: number;
}

export interface Coverage {
  ticks: number;
  first_tick: number | null;
  last_tick: number | null;
  span_seconds: number;
  has_history: boolean;
  note: string;
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
  feed: {
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
  broker_session: {
    configured?: boolean;
    authenticated?: boolean;
    status: string;
    last_error: string | null;
  };
  intervals: string[];
  record_ticks: boolean;
}

export interface OptionLeg {
  token?: string;
  trading_symbol?: string;
  ltp: number | null;
  open_interest: number | null;
  volume: number | null;
  change_percent: number | null;
  bid: number | null;
  ask: number | null;
  iv: number | null;
  delta?: number | null;
  gamma?: number | null;
  theta?: number | null;
  vega?: number | null;
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

export interface CoachFinding {
  pattern: string;
  title: string;
  occurrences: number;
  recommendation: string;
  evidence?: unknown[];
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

export const api = {
  health: () => request<Record<string, unknown>>("/api/v1/system/health"),

  // auth
  login: (email: string, password: string) =>
    post<{ access_token: string; expires_in: number; user: User }>("/api/v1/auth/login", { email, password }),
  me: () => get<User>("/api/v1/auth/me"),
  listUsers: () => get<User[]>("/api/v1/auth/users"),
  createUser: (email: string, password: string, role: string) =>
    post<User>("/api/v1/auth/users", { email, password, role }),
  createApiKey: (name: string) => post<{ api_key: string; warning: string }>("/api/v1/auth/api-keys", { name }),

  // market
  status: () => get<MarketStatus>("/api/v1/market/status"),
  search: (q: string, limit = 25) =>
    get<{ query: string; count: number; results: Instrument[] }>(
      `/api/v1/market/search?q=${encodeURIComponent(q)}&limit=${limit}`,
    ),
  indices: () => get<(Instrument & { quote?: Quote | null })[]>("/api/v1/market/indices"),
  underlyings: () => get<string[]>("/api/v1/market/underlyings"),
  futures: (exchangeSegment = "nse_fo") => get<Instrument[]>(`/api/v1/market/futures?exchange_segment=${exchangeSegment}`),
  quotes: (tokens: { instrument_token: string; exchange_segment: string }[], quoteType = "all") =>
    post<{ as_of: number; count: number; quotes: Quote[] }>("/api/v1/market/quotes", { tokens, quote_type: quoteType }),
  breadth: (limit = 200) =>
    get<{
      universe_size: number;
      priced: number;
      breadth: Record<string, number | null>;
      top_gainers: Quote[];
      top_losers: Quote[];
      most_active: Quote[];
    }>(`/api/v1/market/breadth?limit=${limit}`),
  candles: (token: string, exchangeSegment: string, interval: string, limit = 400) =>
    get<{ candles: Candle[]; count: number; coverage: Coverage }>(
      `/api/v1/market/candles/${encodeURIComponent(token)}?exchange_segment=${exchangeSegment}&interval=${interval}&limit=${limit}`,
    ),
  syncMaster: (force = false) => post<Record<string, unknown>>(`/api/v1/market/sync-master?force=${force}`),
  subscribe: (tokens: string[], exchangeSegment = "nse_cm") =>
    get<{ subscribed: unknown[]; total: number }>(
      `/api/v1/market/feed/subscribe?tokens=${encodeURIComponent(tokens.join(","))}&exchange_segment=${exchangeSegment}`,
    ),

  // options
  chain: (underlying: string) =>
    get<OptionChain>(`/api/v1/options/chain?underlying=${encodeURIComponent(underlying)}`),

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
  backtest: (body: {
    strategy_id?: string;
    definition?: unknown;
    initial_capital?: number;
    slippage?: string;
    timeframe?: string;
  }) => post<BacktestResult>("/api/v1/strategies/backtest", body),

  // execution
  portfolio: () => get<Record<string, unknown>>("/api/v1/portfolio"),
  charges: (exchangeSegment: string, product: string, quantity: number, price: number) =>
    get<{
      buy: Record<string, number>;
      sell: Record<string, number>;
      round_trip: number;
      break_even_move_percent: number | null;
    }>(`/api/v1/charges?exchange_segment=${exchangeSegment}&product=${product}&quantity=${quantity}&price=${price}`),
  liveOrder: (order: Record<string, unknown>) => post<Record<string, unknown>>("/api/v1/orders/live", order),
  cancelOrder: (orderId: string) =>
    post<Record<string, unknown>>("/api/v1/orders/cancel", { order_id: orderId, confirmation: LIVE_PHRASE }),
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
  startAlgo: (strategyId: string, mode: "paper" | "live") =>
    post<Record<string, unknown>>(`/api/v1/algo/runs?strategy_id=${strategyId}&mode=${mode}`),
  stopAlgo: (runId: string) => post<Record<string, unknown>>(`/api/v1/algo/runs/${runId}/stop`),
  riskEvents: (limit = 50) => get<RiskEventRow[]>(`/api/v1/risk/events?limit=${limit}`),
  reconnect: () => post<Record<string, unknown>>("/api/v1/session/reconnect"),

  // analytics
  performance: () => get<PerformancePayload>("/api/v1/analytics/performance"),
  strategyAnalytics: () => get<StrategyRow[]>("/api/v1/analytics/strategies"),
  performanceHeatmap: (days = 180) =>
    get<{ grid: Record<string, Record<string, number>>; weekdays: string[]; days: number }>(
      `/api/v1/analytics/heatmap?days=${days}`,
    ),
  coach: (mode: "loss_analysis" | "strategy_suggestions" = "loss_analysis") =>
    get<CoachResponse>(`/api/v1/coach?mode=${mode}`),

  // alerts
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
};

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

"use client";

/**
 * Live market data over the backend WebSocket.
 *
 * Opens one socket for the whole app, subscribes to whatever the current page
 * needs, and reconnects with backoff. Quotes are cached in a ref so a
 * re-render with the same watchlist does not re-subscribe.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, ensureApiBase, loadToken, marketSocketUrl } from "@/lib/api";
import type { Quote } from "@/lib/api";

export type FeedState = "connecting" | "live" | "reconnecting" | "offline";

export interface LiveFeed {
  quotes: Record<string, Quote>;
  state: FeedState;
  lastTick: number | null;
  frames: number;
  subscribe: (instruments: { instrument_token: string; exchange_segment: string }[]) => void;
  clear: () => void;
}

const key = (token: string, segment: string) => `${segment}:${token}`;
const RETRY_STEPS_MS = [1000, 2000, 5000, 10000, 20000, 30000];

export function useLiveFeed(): LiveFeed {
  const [quotes, setQuotes] = useState<Record<string, Quote>>({});
  const [state, setState] = useState<FeedState>("connecting");
  const [lastTick, setLastTick] = useState<number | null>(null);
  const [frames, setFrames] = useState(0);

  const socketRef = useRef<WebSocket | null>(null);
  const queueRef = useRef<{ instrument_token: string; exchange_segment: string }[]>([]);
  const wantedRef = useRef(new Set<string>());
  const retryRef = useRef(0);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const closedRef = useRef(false);

  const flush = useCallback((socket: WebSocket) => {
    const pending = queueRef.current;
    if (!pending.length) return;
    queueRef.current = [];
    socket.send(JSON.stringify({ action: "subscribe", tokens: pending }));
  }, []);

  useEffect(() => {
    closedRef.current = false;

    // Resolve the API host before opening the socket, and wait for a token to
    // exist: the hook is mounted by the app shell, which may render once before
    // the user has signed in.
    let started = false;

    const connect = async () => {
      if (closedRef.current || started) return;
      await ensureApiBase();
      if (closedRef.current) return;
      if (!loadToken()) {
        // Not signed in yet. Retry shortly rather than opening an unauthenticated
        // socket that the server would reject.
        timerRef.current = setTimeout(() => void connect(), 2000);
        return;
      }
      started = true;
      openSocket();
    };

    const openSocket = () => {
      let socket: WebSocket;
      try {
        socket = new WebSocket(marketSocketUrl());
      } catch {
        schedule();
        return;
      }
      socketRef.current = socket;

      socket.onopen = () => {
        retryRef.current = 0;
        setState("live");
        // Re-subscribe everything this client wanted before the drop.
        const tokens = [...wantedRef.current];
        if (tokens.length) {
          queueRef.current = tokens.map((entry) => {
            const [segment, token] = entry.split(":");
            return { instrument_token: token, exchange_segment: segment };
          });
        }
        flush(socket);
      };

      socket.onmessage = (event) => {
        let payload: { type?: string; data?: Quote };
        try {
          payload = JSON.parse(event.data);
        } catch {
          return;
        }
        if (payload.type !== "quote" || !payload.data) return;
        const quote = payload.data;
        setQuotes((current) => ({ ...current, [key(quote.token, quote.exchange_segment)]: quote }));
        setLastTick(Date.now());
        setFrames((count) => count + 1);
      };

      socket.onclose = () => {
        if (closedRef.current) return;
        setState("reconnecting");
        schedule();
      };

      socket.onerror = () => socket.close();
    };

    const schedule = () => {
      if (closedRef.current) return;
      const delay = RETRY_STEPS_MS[Math.min(retryRef.current, RETRY_STEPS_MS.length - 1)];
      retryRef.current += 1;
      timerRef.current = setTimeout(() => void connect(), delay);
    };

    void connect();

    return () => {
      closedRef.current = true;
      if (timerRef.current) clearTimeout(timerRef.current);
      socketRef.current?.close();
    };
  }, [flush]);

  const subscribe = useCallback(
    (instruments: { instrument_token: string; exchange_segment: string }[]) => {
      const fresh = instruments.filter(
        (item) => !wantedRef.current.has(key(item.instrument_token, item.exchange_segment)),
      );
      for (const item of instruments) {
        wantedRef.current.add(key(item.instrument_token, item.exchange_segment));
      }
      if (!fresh.length) return;

      const socket = socketRef.current;
      if (socket && socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ action: "subscribe", tokens: fresh }));
      } else {
        queueRef.current.push(...fresh);
      }
    },
    [],
  );

  const clear = useCallback(() => {
    wantedRef.current.clear();
    queueRef.current = [];
    setQuotes({});
  }, []);

  return useMemo(
    () => ({ quotes, state, lastTick, frames, subscribe, clear }),
    [quotes, state, lastTick, frames, subscribe, clear],
  );
}

/** Fetch a REST snapshot. Used on load and to backfill before the socket ticks. */
export async function snapshot(
  instruments: { instrument_token: string; exchange_segment: string }[],
  quoteType = "all",
): Promise<Record<string, Quote>> {
  await ensureApiBase();
  if (!instruments.length) return {};
  const response = await api.quotes(instruments, quoteType);
  const out: Record<string, Quote> = {};
  for (const quote of response.quotes) out[key(quote.token, quote.exchange_segment)] = quote;
  return out;
}

export { key as quoteKey };

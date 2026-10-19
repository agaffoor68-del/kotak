"use client";

import { useSyncExternalStore } from "react";
import type { OptionChain } from "@/lib/api";

interface State { chains: Record<string, OptionChain>; }
let state: State = { chains: {} };
const listeners = new Set<() => void>();
const serverState = state;

function emit() { listeners.forEach((listener) => listener()); }
export const marketStore = {
  getSnapshot: () => state,
  getServerSnapshot: () => serverState,
  subscribe: (listener: () => void) => { listeners.add(listener); return () => listeners.delete(listener); },
  setChain: (underlying: string, chain: OptionChain) => { state = { ...state, chains: { ...state.chains, [underlying]: chain } }; emit(); },
};

export function useMarketStore() {
  return useSyncExternalStore(marketStore.subscribe, marketStore.getSnapshot, marketStore.getServerSnapshot);
}

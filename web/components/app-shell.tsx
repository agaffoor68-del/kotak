"use client";

/**
 * Application Shell: TradingView Mobile Web Design System.
 * - Header: Minimal top bar with Search, Notifications, Upgrade button, and Mobile Drawer trigger.
 * - Ticker Tape: Horizontal scrolling ribbon for NIFTY, BANKNIFTY, SENSEX, FINNIFTY.
 * - Mobile Drawer Sidebar: Off-canvas navigation with dark overlay and TradingView styling.
 * - Palette: #131722 (Background), #1e222d (Panels), #2a2e39 (Borders), #2962ff (Blue).
 */

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import type { LucideIcon } from "lucide-react";
import {
  Activity,
  AlertTriangle,
  ArrowDownRight,
  ArrowUpRight,
  BarChart3,
  Bell,
  Bot,
  BriefcaseBusiness,
  ChevronRight,
  ClipboardList,
  Layers,
  LayoutDashboard,
  LogOut,
  Menu,
  Network,
  RefreshCw,
  Search,
  Settings,
  SlidersHorizontal,
  Sparkles,
  TrendingUp,
  X,
  Zap,
} from "lucide-react";
import { api } from "@/lib/api";
import type { MarketStatus } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useLiveFeed, quoteKey } from "@/lib/useLiveFeed";
import { clock, compact, num, percent } from "@/lib/format";
import { BootScreen } from "./boot-screen";
import { SessionChip, StatusChip } from "./ui";

interface NavItem {
  href: string;
  label: string;
  icon: LucideIcon;
}

const NAV: { group: string; items: NavItem[] }[] = [
  {
    group: "Markets",
    items: [
      { href: "/dashboard", label: "Overview", icon: LayoutDashboard },
      { href: "/market", label: "Market Watch", icon: TrendingUp },
      { href: "/options", label: "Option Chain", icon: SlidersHorizontal },
      { href: "/portfolio", label: "Portfolio", icon: BriefcaseBusiness },
    ],
  },
  {
    group: "Trading",
    items: [
      { href: "/trade", label: "Order Ticket", icon: ClipboardList },
      { href: "/risk", label: "Risk Monitor", icon: AlertTriangle },
      { href: "/paper", label: "Paper Trading", icon: Activity },
    ],
  },
  {
    group: "Quant Lab",
    items: [
      { href: "/strategies", label: "Strategies", icon: Layers },
      { href: "/backtest", label: "Backtest", icon: RefreshCw },
      { href: "/analytics", label: "Analytics", icon: BarChart3 },
      { href: "/coach", label: "AI Coach", icon: Bot },
    ],
  },
  {
    group: "Settings",
    items: [
      { href: "/connect", label: "Broker Connect", icon: Network },
      { href: "/alerts", label: "Alerts", icon: Bell },
      { href: "/settings", label: "Settings", icon: Settings },
    ],
  },
];

const TICKER_ITEMS = [
  { token: "99926000", segment: "nse_cm", symbol: "NIFTY 50", defaultBase: 24350 },
  { token: "99926009", segment: "nse_cm", symbol: "BANKNIFTY", defaultBase: 51800 },
  { token: "99919000", segment: "bse_cm", symbol: "SENSEX", defaultBase: 79800 },
  { token: "99926037", segment: "nse_cm", symbol: "FINNIFTY", defaultBase: 23900 },
];

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const { user, ready, signOut } = useAuth();
  const [status, setStatus] = useState<MarketStatus | null>(null);
  const [open, setOpen] = useState(false);
  const [now, setNow] = useState(() => Date.now() / 1000);
  const [searchOpen, setSearchOpen] = useState(false);
  const feed = useLiveFeed();

  // Authentication Guard
  useEffect(() => {
    if (!ready) return;
    if (!user) {
      router.replace("/login");
      return;
    }
  }, [ready, user, router]);

  // Subscribe Ticker Feed
  useEffect(() => {
    feed.subscribe(
      TICKER_ITEMS.map((item) => ({
        instrument_token: item.token,
        exchange_segment: item.segment,
      }))
    );
  }, [feed]);

  // Status Polling
  useEffect(() => {
    if (!user) return;
    let active = true;
    const load = async () => {
      try {
        const payload = await api.status();
        if (active) setStatus(payload);
      } catch {
        // Silently tolerate
      }
    };
    void load();
    const timer = setInterval(load, 30_000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [user]);

  // Clock
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(timer);
  }, []);

  // Close drawer on route change
  useEffect(() => {
    setOpen(false);
  }, [pathname]);

  if (!ready || !user) {
    return <BootScreen />;
  }

  return (
    <div className="min-h-screen bg-[#131722] text-[#d1d4dc] flex flex-col font-sans selection:bg-[#2962ff]/30">
      {/* ========================================================================= */}
      {/* 1. TICKER TAPE (Very Top) */}
      {/* ========================================================================= */}
      <div className="bg-[#0e111a] border-b border-[#2a2e39] h-7 px-3 flex items-center justify-between text-[11px] overflow-hidden select-none z-50">
        <div className="flex items-center space-x-6 overflow-x-auto no-scrollbar scroll-smooth">
          {TICKER_ITEMS.map((item) => {
            const q = feed.quotes[quoteKey(item.token, item.segment)];
            const ltp = q?.last ?? item.defaultBase;
            const chgPct = q?.change_percent ?? 0.35;
            const isUp = chgPct >= 0;

            return (
              <div
                key={item.token}
                onClick={() => router.push(`/trade?symbol=${encodeURIComponent(item.symbol)}`)}
                className="flex items-center space-x-1.5 shrink-0 cursor-pointer hover:opacity-80 transition-opacity"
              >
                <span className="font-semibold text-[#787b86] text-[10px]">{item.symbol}</span>
                <span className="font-mono text-[#d1d4dc] font-medium text-[11px]">{num(ltp)}</span>
                <span
                  style={{ color: isUp ? "#089981" : "#f23645" }}
                  className="font-mono text-[10px] font-medium flex items-center"
                >
                  {isUp ? "+" : ""}
                  {percent(chgPct)}
                </span>
              </div>
            );
          })}
        </div>

        <div className="hidden sm:flex items-center space-x-2 text-[10px] text-[#787b86] shrink-0 pl-3 border-l border-[#2a2e39]">
          <span className="h-1.5 w-1.5 rounded-full bg-[#089981] animate-pulse" />
          <span className="font-mono">{clock(now)} IST</span>
        </div>
      </div>

      {/* ========================================================================= */}
      {/* 2. MINIMAL TOP BAR */}
      {/* ========================================================================= */}
      <header className="sticky top-0 z-40 bg-[#1e222d] border-b border-[#2a2e39] h-11 px-3 flex items-center justify-between">
        {/* Left: Mobile Drawer Button + Brand Logo */}
        <div className="flex items-center space-x-3">
          <button
            type="button"
            onClick={() => setOpen(true)}
            aria-label="Open Navigation Drawer"
            className="p-1.5 rounded hover:bg-[#2a2e39] text-[#d1d4dc] transition-colors"
          >
            <Menu className="w-4 h-4" />
          </button>

          <Link href="/dashboard" className="flex items-center space-x-2">
            <span className="w-6 h-6 rounded bg-[#2962ff] flex items-center justify-center font-bold text-xs text-white">
              TV
            </span>
            <span className="font-semibold text-xs text-white tracking-tight hidden xs:inline">
              TradingDesk
            </span>
          </Link>
        </div>

        {/* Center: Quick Search Trigger */}
        <div
          onClick={() => router.push("/market")}
          className="flex items-center space-x-2 bg-[#131722] hover:bg-[#2a2e39] border border-[#2a2e39] rounded px-3 py-1 cursor-pointer transition-colors max-w-xs w-full mx-4 text-xs text-[#787b86]"
        >
          <Search className="w-3.5 h-3.5" />
          <span className="truncate text-[11px]">Symbol, chart or tool...</span>
          <kbd className="hidden md:inline-block ml-auto text-[9px] bg-[#2a2e39] px-1 rounded text-[#d1d4dc]">
            /
          </kbd>
        </div>

        {/* Right Actions: Upgrade + Notifications + Profile */}
        <div className="flex items-center space-x-2">
          {/* Upgrade Button */}
          <button
            type="button"
            onClick={() => router.push("/settings")}
            className="bg-[#2962ff] hover:bg-[#1e4bd8] text-white text-[11px] font-semibold px-2.5 py-1 rounded transition-colors flex items-center space-x-1"
          >
            <Zap className="w-3 h-3 fill-white" />
            <span className="hidden sm:inline">Upgrade</span>
          </button>

          {/* Notifications */}
          <button
            type="button"
            onClick={() => router.push("/alerts")}
            className="p-1.5 rounded hover:bg-[#2a2e39] text-[#787b86] hover:text-[#d1d4dc] transition-colors relative"
            aria-label="Alerts"
          >
            <Bell className="w-4 h-4" />
            <span className="absolute top-1 right-1 w-1.5 h-1.5 bg-[#2962ff] rounded-full" />
          </button>

          {/* Feed State Pill */}
          <div className="hidden lg:block">
            <StatusChip
              status={feed.state === "live" ? "live" : feed.state === "offline" ? "error" : "warn"}
              label={feed.state === "live" ? "LIVE" : feed.state.toUpperCase()}
            />
          </div>
        </div>
      </header>

      {/* ========================================================================= */}
      {/* 3. MOBILE DRAWER SIDEBAR (Off-canvas with Dark Overlay) */}
      {/* ========================================================================= */}
      {open && (
        <div className="fixed inset-0 z-50 flex">
          {/* Dark Backdrop Overlay */}
          <div
            className="fixed inset-0 bg-black/75 backdrop-blur-sm transition-opacity"
            onClick={() => setOpen(false)}
          />

          {/* Drawer Content */}
          <div className="relative w-72 max-w-[85vw] bg-[#1e222d] border-r border-[#2a2e39] h-full flex flex-col z-10 shadow-2xl animate-fade-in">
            {/* Drawer Header */}
            <div className="h-12 border-b border-[#2a2e39] px-4 flex items-center justify-between">
              <div className="flex items-center space-x-2">
                <span className="w-5 h-5 rounded bg-[#2962ff] flex items-center justify-center font-bold text-[10px] text-white">
                  TV
                </span>
                <span className="font-semibold text-xs text-white">Navigation</span>
              </div>
              <button
                type="button"
                onClick={() => setOpen(false)}
                className="p-1 rounded hover:bg-[#2a2e39] text-[#787b86] hover:text-white transition-colors"
              >
                <X className="w-4 h-4" />
              </button>
            </div>

            {/* Navigation Links */}
            <div className="flex-1 overflow-y-auto p-3 space-y-4 text-xs">
              {NAV.map((section) => (
                <div key={section.group}>
                  <div className="text-[10px] font-semibold uppercase tracking-wider text-[#787b86] px-2 mb-1">
                    {section.group}
                  </div>
                  <div className="space-y-0.5">
                    {section.items.map((item) => {
                      const active = pathname === item.href || pathname.startsWith(`${item.href}/`);
                      return (
                        <Link
                          key={item.href}
                          href={item.href}
                          onClick={() => setOpen(false)}
                          className={`flex items-center justify-between px-2.5 py-2 rounded text-[12px] font-medium transition-colors ${
                            active
                              ? "bg-[#2962ff]/15 text-[#2962ff]"
                              : "text-[#d1d4dc] hover:bg-[#2a2e39]"
                          }`}
                        >
                          <div className="flex items-center space-x-2.5">
                            <item.icon className={`w-4 h-4 ${active ? "text-[#2962ff]" : "text-[#787b86]"}`} />
                            <span>{item.label}</span>
                          </div>
                          {active && <ChevronRight className="w-3.5 h-3.5 text-[#2962ff]" />}
                        </Link>
                      );
                    })}
                  </div>
                </div>
              ))}
            </div>

            {/* Drawer Footer with User & Sign Out */}
            <div className="border-t border-[#2a2e39] p-3 bg-[#171b26] flex items-center justify-between text-xs">
              <div className="min-w-0 pr-2">
                <div className="font-semibold text-[#d1d4dc] truncate text-[11px]">{user.email}</div>
                <div className="text-[10px] text-[#787b86] uppercase font-mono">{user.role}</div>
              </div>
              <button
                type="button"
                onClick={signOut}
                className="p-1.5 rounded hover:bg-[#2a2e39] text-[#787b86] hover:text-[#f23645] transition-colors"
                title="Sign out"
              >
                <LogOut className="w-4 h-4" />
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ========================================================================= */}
      {/* 4. MAIN PAGE CONTENT CONTAINER (Dense Spacing: p-2 or p-3) */}
      {/* ========================================================================= */}
      <main className="flex-1 w-full max-w-[1720px] mx-auto p-2 sm:p-3 overflow-x-hidden">
        {children}
      </main>
    </div>
  );
}

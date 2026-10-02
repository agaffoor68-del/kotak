"use client";

/** Application shell: sidebar navigation, connection status and the sign-out. */

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { MarketStatus } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useLiveFeed } from "@/lib/useLiveFeed";
import { clock, compact } from "@/lib/format";
import { BootScreen } from "./boot-screen";
import { SessionChip, StatusChip } from "./ui";

interface NavItem {
  href: string;
  label: string;
  icon: string;
}

const NAV: { group: string; items: NavItem[] }[] = [
  {
    group: "Desk",
    items: [
      { href: "/dashboard", label: "Overview", icon: "▦" },
      { href: "/market", label: "Market watch", icon: "◫" },
      { href: "/options", label: "Option chain", icon: "⋔" },
      { href: "/portfolio", label: "Portfolio", icon: "◧" },
    ],
  },
  {
    group: "Trading",
    items: [
      { href: "/trade", label: "Order ticket", icon: "⇅" },
      { href: "/risk", label: "Risk", icon: "⚠" },
      { href: "/paper", label: "Paper trading", icon: "◇" },
    ],
  },
  {
    group: "Quant",
    items: [
      { href: "/strategies", label: "Strategies", icon: "⚙" },
      { href: "/backtest", label: "Backtest", icon: "⟲" },
      { href: "/analytics", label: "Analytics", icon: "◪" },
      { href: "/coach", label: "AI coach", icon: "✦" },
    ],
  },
  {
    group: "System",
    items: [
      { href: "/alerts", label: "Alerts", icon: "◔" },
      { href: "/settings", label: "Settings", icon: "⚒" },
    ],
  },
];

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const { user, ready, signOut } = useAuth();
  const [status, setStatus] = useState<MarketStatus | null>(null);
  const [open, setOpen] = useState(false);
  const [now, setNow] = useState(() => Date.now() / 1000);
  const feed = useLiveFeed();

  useEffect(() => {
    if (!ready) return;
    if (!user) {
      router.replace("/login");
      return;
    }
  }, [ready, user, router]);

  useEffect(() => {
    if (!user) return;
    let active = true;
    const load = async () => {
      try {
        const payload = await api.status();
        if (active) setStatus(payload);
      } catch {
        // The header chip already reflects the socket state; a failed poll is
        // not worth an error banner on every page.
      }
    };
    void load();
    const timer = setInterval(load, 30_000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [user]);

  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    setOpen(false);
  }, [pathname]);

  if (!ready || !user) {
    return <BootScreen />;
  }

  const brokerLive = status?.broker_session?.authenticated === true;

  const navigation = (
    <nav className="space-y-5">
      {NAV.map((section) => (
        <div key={section.group}>
          <p className="label-caps px-3 pb-1.5">{section.group}</p>
          <ul className="space-y-0.5">
            {section.items.map((item) => {
              const active = pathname === item.href || pathname.startsWith(`${item.href}/`);
              return (
                <li key={item.href}>
                  <Link
                    href={item.href}
                    aria-current={active ? "page" : undefined}
                    className={`flex items-center gap-2.5 rounded-md px-3 py-2 text-xs transition-colors ${active
                        ? "bg-accent-soft font-semibold text-accent"
                        : "text-ink-dim hover:bg-elevated hover:text-ink"
                      }`}
                  >
                    <span aria-hidden className="w-4 text-center text-sm">
                      {item.icon}
                    </span>
                    {item.label}
                  </Link>
                </li>
              );
            })}
          </ul>
        </div>
      ))}
    </nav>
  );

  return (
    <div className="min-h-screen">
      {/* ---------- top bar ---------- */}
      <header className="sticky top-0 z-40 border-b border-hairline bg-surface/95 backdrop-blur">
        <div className="flex items-center gap-3 px-4 py-2.5">
          <button
            className="btn btn-sm btn-ghost lg:hidden"
            onClick={() => setOpen((value) => !value)}
            aria-label="Toggle navigation"
            aria-expanded={open}
            type="button"
          >
            ☰
          </button>

          <Link href="/dashboard" className="flex items-center gap-2">
            <span className="grid h-7 w-7 place-items-center rounded bg-accent text-sm font-bold text-white">A</span>
            <span className="hidden text-sm font-semibold tracking-tight sm:inline">AlphaTradePro</span>
          </Link>

          <div className="ml-auto flex flex-wrap items-center gap-2">
            {status ? <SessionChip state={status.session.state} isOpen={status.session.is_open} /> : null}
            <StatusChip
              status={feed.state === "live" ? "live" : feed.state === "offline" ? "error" : "warn"}
              label={feed.state === "live" ? `${compact(feed.frames)} ticks` : feed.state}
            />
            {status ? (
              <span className="hidden text-3xs text-ink-faint md:inline">
                {compact(status.scrip_master.instruments)} instruments
              </span>
            ) : null}
            <span className="num hidden text-3xs text-ink-faint lg:inline">{clock(now)} IST</span>
            {!brokerLive ? (
              <button
                className="btn btn-sm"
                type="button"
                onClick={() => api.reconnect().catch(() => undefined)}
                title={status?.broker_session?.last_error ?? "Kotak Neo is not authenticated"}
              >
                Reconnect broker
              </button>
            ) : null}
            <div className="flex items-center gap-2 border-l border-hairline pl-2">
              <span className="hidden text-2xs text-ink-dim sm:inline">{user.email}</span>
              <span className="chip chip-idle">{user.role}</span>
              <button className="btn btn-sm btn-ghost" type="button" onClick={signOut}>
                Sign out
              </button>
            </div>
          </div>
        </div>
      </header>

      <div className="flex">
        {/* ---------- desktop sidebar ---------- */}
        <aside className="sticky top-[49px] hidden h-[calc(100vh-49px)] w-56 shrink-0 overflow-y-auto border-r border-hairline bg-surface/50 p-3 lg:block">
          {navigation}
        </aside>

        {/* ---------- mobile drawer ---------- */}
        {open ? (
          <>
            <div
              className="fixed inset-0 z-40 bg-black/60 lg:hidden"
              onClick={() => setOpen(false)}
              aria-hidden
            />
            <aside className="fixed bottom-0 left-0 top-[49px] z-50 w-64 overflow-y-auto border-r border-hairline bg-surface p-3 lg:hidden">
              {navigation}
            </aside>
          </>
        ) : null}

        <main className="min-w-0 flex-1 p-4 lg:p-6">{children}</main>
      </div>
    </div>
  );
}

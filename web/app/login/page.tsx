import type { Metadata } from "next";
import { LoginGate } from "./login-gate";

export const metadata: Metadata = { title: "Sign in — AlphaTradePro" };

export default function LoginPage() {
  return (
    <div className="grid min-h-screen lg:grid-cols-2">
      {/* Brand panel — hidden on small screens so the form owns the viewport. */}
      <section className="relative hidden flex-col justify-between overflow-hidden border-r border-hairline bg-surface p-10 lg:flex">
        <div
          aria-hidden
          className="pointer-events-none absolute -left-24 top-1/4 h-96 w-96 rounded-full bg-accent/10 blur-3xl"
        />
        <div className="relative">
          <div className="flex items-center gap-2.5">
            <span className="grid h-9 w-9 place-items-center rounded-lg bg-accent text-lg font-bold text-white">A</span>
            <span className="text-lg font-semibold tracking-tight">AlphaTradePro</span>
          </div>
        </div>

        <div className="relative max-w-md space-y-5">
          <h1 className="text-3xl font-semibold leading-tight">
            Institutional trading,
            <br />
            wired to Kotak Neo.
          </h1>
          <p className="text-sm leading-relaxed text-ink-dim">
            Live ticks over the Neo WebSocket, real orders through the Neo trade API, and an option chain
            with live open interest and Greeks.
          </p>
          <ul className="space-y-2 text-xs text-ink-faint">
            {[
              "No simulated prices — every figure comes from Kotak Neo",
              "Risk engine and emergency kill switch gate every order",
              "Record the tape as it trades, so charts and backtests use real data",
            ].map((line) => (
              <li key={line} className="flex items-start gap-2">
                <span className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-accent" />
                {line}
              </li>
            ))}
          </ul>
        </div>

        <p className="relative text-3xs text-ink-faint">
          Trading involves risk. This platform does not provide investment advice.
        </p>
      </section>

      <section className="flex items-center justify-center p-6">
        <div className="w-full max-w-sm">
          <div className="mb-6 lg:hidden">
            <div className="flex items-center gap-2.5">
              <span className="grid h-8 w-8 place-items-center rounded-lg bg-accent text-base font-bold text-white">A</span>
              <span className="text-base font-semibold">AlphaTradePro</span>
            </div>
          </div>
          <LoginGate />
        </div>
      </section>
    </div>
  );
}

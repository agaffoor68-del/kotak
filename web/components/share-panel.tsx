"use client";

/**
 * "Open on your phone" panel.
 *
 * Renders a QR code the user scans with the phone camera, which opens the
 * terminal in the phone's own browser. Because the app is a PWA, the browser
 * can then install it to the home screen and it runs full-screen.
 *
 * The link is resolved by the *server*, not the browser, so it points at the
 * public address even when the page was reached over a tunnel.
 */

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { ShareResponse } from "@/lib/api";
import { ErrorNote, Loading, Panel } from "./ui";

const FALLBACK = "Install the app, then Add to Home Screen from your phone's browser menu.";

export function SharePanel() {
  const [share, setShare] = useState<ShareResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);
  const [canNativeShare, setCanNativeShare] = useState(false);

  useEffect(() => {
    let active = true;
    api
      .share()
      .then((value) => active && setShare(value))
      .catch((failure) => active && setError(failure instanceof Error ? failure.message : "Share link unavailable"));
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    setCanNativeShare(typeof navigator !== "undefined" && typeof navigator.share === "function");
  }, []);

  const copy = useCallback(async (label: string, value: string) => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(label);
      setTimeout(() => setCopied(null), 2000);
    } catch {
      setError("Clipboard is blocked in this browser — long-press the link to copy it.");
    }
  }, []);

  if (error && !share) {
    return (
      <Panel title="Open on your phone">
        <ErrorNote error={error} />
      </Panel>
    );
  }

  if (!share) {
    return (
      <Panel title="Open on your phone">
        <Loading label="Resolving the public address" />
      </Panel>
    );
  }

  const primary = share.links.terminal ?? share.base_url;
  const signIn = share.links.sign_in ?? primary;
  // A short URL scans far more reliably than a long one.
  const qrSrc = share.configured_from === "browser" && share.qr_endpoint
    ? `${share.qr_endpoint}?origin=${encodeURIComponent(window.location.origin)}&svg=1`
    : `${share.qr_endpoint}?svg=1`;

  return (
    <Panel
      title="Open on your phone"
      subtitle="Scan the code, then install to the home screen for a full-screen app"
      actions={
        <span className={`chip ${share.configured_from === "request" ? "chip-warn" : "chip-live"}`}>
          {share.configured_from === "browser"
            ? "this device"
            : share.configured_from === "env"
              ? "canonical URL"
              : "server-detected"}
        </span>
      }
    >
      <div className="flex flex-col gap-5 sm:flex-row sm:items-start">
        <div className="shrink-0">
          <div className="border border-hairline bg-white p-2">
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src={qrSrc}
              alt={`QR code linking to ${primary}`}
              width={190}
              height={190}
              className="block"
              style={{ imageRendering: "pixelated" }}
            />
          </div>
          <p className="mt-1.5 max-w-[190px] text-center text-3xs leading-relaxed text-ink-faint">
            Point the phone camera at this
          </p>
        </div>

        <div className="min-w-0 flex-1 space-y-3">
          <div className="rounded border border-hairline bg-canvas p-3">
            <p className="label-caps">Sign in on your phone</p>
            <code className="mt-1 block break-all font-mono text-2xs text-ink">{signIn}</code>
            <div className="mt-2 flex flex-wrap gap-1.5">
              <button className="btn btn-sm" type="button" onClick={() => void copy("sign in", signIn)}>
                {copied === "sign in" ? "Copied" : "Copy link"}
              </button>
              {canNativeShare ? (
                <button
                  className="btn btn-sm btn-primary"
                  type="button"
                  onClick={() =>
                    navigator
                      .share({ title: "AlphaTradePro", text: "Sign in to the trading desk", url: signIn })
                      .catch(() => void copy("sign in", signIn))
                  }
                >
                  Share
                </button>
              ) : null}
              <a className="btn btn-sm" href={share.qr_endpoint} download={`alphatrade-qr.png`}>
                Download QR
              </a>
            </div>
          </div>

          <div>
            <p className="label-caps">Deep links</p>
            <ul className="mt-1.5 space-y-1">
              {Object.entries(share.links)
                .filter(([key]) => key !== "api_health")
                .map(([key, value]) => (
                  <li key={key} className="flex items-center gap-2">
                    <span className="w-20 shrink-0 text-3xs capitalize text-ink-faint">{key.replace(/_/g, " ")}</span>
                    <a
                      className="min-w-0 flex-1 truncate font-mono text-3xs text-accent underline-offset-2 hover:underline"
                      href={value}
                      target="_blank"
                      rel="noreferrer"
                    >
                      {value}
                    </a>
                  </li>
                ))}
            </ul>
          </div>

          <ol className="space-y-1 rounded border border-hairline p-3 text-2xs leading-relaxed text-ink-faint">
            <li>1. Open the phone camera and scan the code.</li>
            <li>2. Sign in with your AlphaTradePro account.</li>
            <li>3. In the browser menu choose <span className="text-ink-dim">Add to Home Screen</span>.</li>
            <li>4. It then launches full-screen, like a native app, and keeps working offline for the shell.</li>
          </ol>

          <p className="text-3xs leading-relaxed text-ink-faint">{share.note ?? FALLBACK}</p>
          {error ? <p className="text-3xs text-warn">{error}</p> : null}
        </div>
      </div>
    </Panel>
  );
}

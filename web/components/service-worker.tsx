"use client";

/**
 * Registers the service worker so the desk installs as a PWA.
 *
 * Deliberately deferred until after load, so it never competes with the first
 * paint on a phone.
 */

import { useEffect } from "react";

export function ServiceWorker() {
  useEffect(() => {
    if (!("serviceWorker" in navigator)) return;
    if (window.location.protocol !== "https:" && window.location.hostname !== "localhost") return;
    const register = () => {
      navigator.serviceWorker.register("/sw.js").catch(() => {
        // A failed registration is not fatal; the app still works online.
      });
    };
    if (document.readyState === "complete") register();
    else window.addEventListener("load", register, { once: true });
  }, []);

  return null;
}

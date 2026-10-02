import type { Metadata, Viewport } from "next";
import "./globals.css";
import { AuthProvider } from "@/lib/auth";
import { ServiceWorker } from "@/components/service-worker";

export const metadata: Metadata = {
  title: "AlphaTradePro — Kotak Neo trading platform",
  description:
    "Institutional algorithmic trading for Indian markets on Kotak Neo. Live data, real orders, option chain analytics, backtesting and risk controls.",
  applicationName: "AlphaTradePro",
  manifest: "/manifest.webmanifest",
  appleWebApp: { capable: true, title: "AlphaTradePro", statusBarStyle: "black-translucent" },
  icons: {
    icon: [{ url: "/icon.svg", type: "image/svg+xml" }],
    apple: [{ url: "/icon.svg" }],
  },
};

export const viewport: Viewport = {
  themeColor: "#080B12",
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
};

export default function Layout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <AuthProvider>
          {children}
          <ServiceWorker />
        </AuthProvider>
      </body>
    </html>
  );
}

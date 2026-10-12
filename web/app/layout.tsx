import type { Metadata, Viewport } from "next";
import "./globals.css";
import { AuthProvider } from "@/lib/auth";
import { ServiceWorker } from "@/components/service-worker";

export const metadata: Metadata = {
  title: "AlphaTradePro — Angel One data, dual-broker execution",
  description:
    "Institutional algorithmic trading for Indian markets. Market data from Angel One SmartAPI; live orders routed to Kotak Neo, Angel One, or both.",
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

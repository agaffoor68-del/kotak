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
      <head>
        <meta httpEquiv="Content-Security-Policy" content="default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self' https://kotak-xs4d.onrender.com wss://kotak-xs4d.onrender.com;" />
        <link rel="manifest" href="/manifest.webmanifest" crossOrigin="use-credentials" />
      </head>
        <AuthProvider>
          {children}
          <ServiceWorker />
        </AuthProvider>
      </body>
    </html>
  );
}

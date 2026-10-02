"use client";

/**
 * Entry point.
 *
 * The session token lives in `sessionStorage`, so only the browser can decide
 * where a visitor belongs. Waiting for `AuthProvider` to resolve it here means
 * an anonymous visitor lands on the sign-in page directly, instead of being
 * bounced through the dashboard shell and its loading spinner first.
 */

import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { BootScreen } from "@/components/boot-screen";
import { useAuth } from "@/lib/auth";

export default function Page() {
  const { user, ready } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (!ready) return;
    router.replace(user ? "/dashboard" : "/login");
  }, [ready, user, router]);

  return <BootScreen />;
}

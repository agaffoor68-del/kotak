"use client";

/**
 * Sign-in gate.
 *
 * The previous check read the token during a server render, where `window` does
 * not exist and `loadToken()` always returns null — so a signed-in user could
 * still see the form. This waits for the client session to resolve instead: a
 * live session is forwarded to the desk, otherwise the form renders.
 */

import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { BootScreen } from "@/components/boot-screen";
import { useAuth } from "@/lib/auth";
import { LoginForm } from "./login-form";

export function LoginGate() {
  const { user, ready } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (ready && user) router.replace("/dashboard");
  }, [ready, user, router]);

  if (!ready || user) return <BootScreen />;
  return <LoginForm />;
}
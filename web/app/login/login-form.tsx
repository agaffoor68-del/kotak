"use client";

import { useState } from "react";
import { useAuth } from "@/lib/auth";
import { resolveApiBase } from "@/lib/api";
import { Field } from "@/components/ui";

export function LoginForm() {
  const { signIn } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [show, setShow] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await signIn(email.trim(), password);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Sign in failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="panel p-6">
      <h2 className="text-sm font-semibold uppercase tracking-[0.14em]">Sign in</h2>
      <p className="mt-1 text-2xs text-ink-faint">
        Use the account your administrator created.
      </p>

      <div className="mt-5 space-y-4">
        <Field label="Email">
          <input
            className="field-input"
            id="login-email"
            name="email"
            type="email"
            autoComplete="username"
            required
            placeholder="you@example.com"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
          />
        </Field>

        <Field label="Password">
          <div className="relative">
            <input
              className="field-input pr-16"
              id="login-password"
              name="password"
              type={show ? "text" : "password"}
              autoComplete="current-password"
              required
              placeholder="••••••••••"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
            />
            <button
              className="absolute right-1 top-1/2 -translate-y-1/2 rounded px-2 py-1 text-3xs uppercase text-ink-faint hover:text-ink"
              type="button"
              onClick={() => setShow((value) => !value)}
              aria-label={show ? "Reveal password" : "Hide password"}
              aria-pressed={show}
            >
              {show ? "Hide" : "Show"}
            </button>
          </div>
        </Field>

        {error ? (
          <p className="rounded-md border border-down/40 bg-down-soft px-3 py-2 text-2xs text-down" role="alert">
            {error}
          </p>
        ) : null}

        <button className="btn btn-primary btn-lg w-full" type="submit" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </div>

      <p className="mt-5 border-t border-hairline pt-4 text-3xs leading-relaxed text-ink-faint">
        First run? Set <code className="text-ink-dim">BOOTSTRAP_ADMIN_EMAIL</code> and{" "}
        <code className="text-ink-dim">BOOTSTRAP_ADMIN_PASSWORD</code> in the server environment, then
        restart. The first account created becomes the administrator.
      </p>
      <ApiBaseHint />
    </form>
  );
}

/** Lets an operator point a misconfigured build at the right API host. */
function ApiBaseHint() {
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState("");

  function save(event: React.FormEvent) {
    event.preventDefault();
    window.localStorage.setItem("alphatrade.api-base", value.trim());
    window.location.reload();
  }

  return (
    <div className="mt-3">
      <button className="text-3xs text-ink-faint underline-offset-2 hover:underline" type="button" onClick={() => setOpen((v) => !v)}>
        Backend not reachable?
      </button>
      {open ? (
        <form className="mt-2 flex gap-2" onSubmit={save}>
          <input
            className="field-input"
            placeholder={resolveApiBase()}
            value={value}
            onChange={(event) => setValue(event.target.value)}
          />
          <button className="btn btn-sm" type="submit">
            Set
          </button>
        </form>
      ) : null}
    </div>
  );
}

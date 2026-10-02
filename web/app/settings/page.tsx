"use client";

/** Settings: system health, users, API keys and deployment diagnostics. */

import { useCallback, useEffect, useState } from "react";
import { api, getApiBase, loadToken } from "@/lib/api";
import type { User } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { AppShell } from "@/components/app-shell";
import { ErrorNote, Field, Loading, Panel, StatusChip } from "@/components/ui";
import { clock, num, relative } from "@/lib/format";

interface Health {
  status: string;
  environment: string;
  broker: string;
  features: Record<string, boolean>;
  feed: { status: string; frames_received: number; reconnects: number; subscribed_instruments: number; last_error: string | null; cached_quotes: number };
  scrip_master: { status: string; instruments: number; last_sync: number | null; last_error: string | null; stale: boolean };
  quotes: { cached_quotes: number; last_error: string | null };
}

function Settings() {
  const { user, signOut } = useAuth();
  const [health, setHealth] = useState<Health | null>(null);
  const [users, setUsers] = useState<User[]>([]);
  const [apiKey, setApiKey] = useState<{ api_key: string; warning: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [newUser, setNewUser] = useState({ email: "", password: "", role: "trader" });
  const [baseUrl, setBaseUrl] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const healthResponse = await fetch(`${getApiBase()}/api/v1/system/health`, { cache: "no-store" });
      setHealth(await healthResponse.json());
      if (user?.role === "admin") setUsers(await api.listUsers());
      setError(null);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Could not reach the backend");
    }
  }, [user?.role]);

  useEffect(() => {
    setBaseUrl(getApiBase());
    void load();
  }, [load]);

  async function mintKey() {
    setBusy(true);
    try {
      setApiKey(await api.createApiKey(`key-${Date.now()}`));
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Could not create an API key");
    } finally {
      setBusy(false);
    }
  }

  async function addUser() {
    setBusy(true);
    try {
      await api.createUser(newUser.email, newUser.password, newUser.role);
      setNewUser({ email: "", password: "", role: "trader" });
      setUsers(await api.listUsers());
      setError(null);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Could not create the user");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      <header>
        <h1 className="text-lg font-semibold tracking-tight">Settings</h1>
        <p className="text-2xs text-ink-faint">System health, access control and connection settings</p>
      </header>

      {error ? <ErrorNote error={error} onRetry={() => void load()} /> : null}
      {!health ? <Loading label="Loading system health" /> : null}

      {health ? (
        <>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <div className="panel px-4 py-3">
              <p className="label-caps">Backend</p>
              <p className="num mt-1.5 text-sm">{health.status}</p>
              <p className="mt-0.5 text-3xs text-ink-faint">{health.environment}</p>
            </div>
            <div className="panel px-4 py-3">
              <p className="label-caps">Broker</p>
              <p className="mt-1.5 text-sm font-semibold">{health.broker}</p>
              <p className="mt-0.5 text-3xs text-ink-faint">exclusive</p>
            </div>
            <div className="panel px-4 py-3">
              <p className="label-caps">Feed</p>
              <p className="mt-1.5 text-sm">{health.feed.status}</p>
              <p className="mt-0.5 text-3xs text-ink-faint">
                {num(health.feed.frames_received, 0)} frames · {num(health.feed.reconnects, 0)} reconnects
              </p>
            </div>
            <div className="panel px-4 py-3">
              <p className="label-caps">Symbol master</p>
              <p className="mt-1.5 text-sm">{num(health.scrip_master.instruments, 0)}</p>
              <p className="mt-0.5 text-3xs text-ink-faint">
                {health.scrip_master.last_sync ? `synced ${relative(health.scrip_master.last_sync)}` : "never synced"}
              </p>
            </div>
          </div>

          <Panel title="Feature gates" subtitle="Server-side switches that control real-money capability">
            <div className="flex flex-wrap gap-2">
              {Object.entries(health.features).map(([key, enabled]) => (
                <span key={key} className={`chip ${enabled ? "chip-live" : "chip-idle"}`}>
                  {key.replace(/_/g, " ")} {enabled ? "on" : "off"}
                </span>
              ))}
            </div>
            <p className="mt-3 border-t border-hairline pt-3 text-3xs leading-relaxed text-ink-faint">
              Live trading and algo execution are off by default. Turning them on requires setting
              <code className="text-ink-dim"> ENABLE_LIVE_TRADING=1</code> and
              <code className="text-ink-dim"> ENABLE_ALGO_EXECUTION=1</code> in the server environment, plus a
              passing risk check on every order.
            </p>
          </Panel>

          {health.feed.last_error ? (
            <Panel title="Feed error">
              <p className="text-2xs text-down">{health.feed.last_error}</p>
            </Panel>
          ) : null}
          {health.scrip_master.last_error ? (
            <Panel title="Symbol master error">
              <p className="text-2xs text-warn">{health.scrip_master.last_error}</p>
            </Panel>
          ) : null}
        </>
      ) : null}

      <div className="grid gap-4 lg:grid-cols-2">
        <Panel title="API key" subtitle="For automation and the WebSocket feed">
          <p className="text-2xs leading-relaxed text-ink-faint">
            An API key bypasses the browser session. It carries your role, so a read-only key cannot trade.
          </p>
          <button className="btn btn-primary mt-3" onClick={() => void mintKey()} disabled={busy} type="button">
            {busy ? "Creating…" : "Create API key"}
          </button>
          {apiKey ? (
            <div className="mt-3 rounded border border-warn/40 bg-warn-soft p-3">
              <p className="text-3xs font-semibold text-warn">{apiKey.warning}</p>
              <code className="mt-1.5 block break-all font-mono text-2xs text-ink">{apiKey.api_key}</code>
            </div>
          ) : null}
        </Panel>

        <Panel title="Backend address" subtitle="Where this client sends its API calls">
          <Field label="API base URL">
            <input className="field-input" value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} />
          </Field>
          <button
            className="btn mt-2"
            onClick={() => {
              window.localStorage.setItem("alphatrade.api-base", baseUrl.replace(/\/+$/, ""));
              window.location.reload();
            }}
            type="button"
          >
            Save and reload
          </button>
          <p className="mt-2 text-3xs text-ink-faint">
            Token present: {loadToken() ? "yes" : "no"} · {clock(Date.now() / 1000)} IST
          </p>
        </Panel>
      </div>

      {user?.role === "admin" ? (
        <Panel title="Users" subtitle="Role-based access control">
          <table className="tbl">
            <thead>
              <tr>
                <th>Email</th>
                <th>Role</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {users.map((row) => (
                <tr key={row.id}>
                  <td className="text-xs">{row.email}</td>
                  <td>
                    <span className="chip chip-idle">{row.role}</span>
                  </td>
                  <td>{row.is_active ? <StatusChip status="live" label="active" /> : <StatusChip status="idle" label="disabled" />}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="mt-4 grid gap-3 border-t border-hairline pt-4 sm:grid-cols-4">
            <Field label="Email">
              <input
                className="field-input"
                type="email"
                value={newUser.email}
                onChange={(event) => setNewUser({ ...newUser, email: event.target.value })}
              />
            </Field>
            <Field label="Password" hint="min 10 characters">
              <input
                className="field-input"
                type="password"
                value={newUser.password}
                onChange={(event) => setNewUser({ ...newUser, password: event.target.value })}
              />
            </Field>
            <Field label="Role">
              <select className="field-select" value={newUser.role} onChange={(event) => setNewUser({ ...newUser, role: event.target.value })}>
                {["admin", "trader", "viewer"].map((role) => (
                  <option key={role} value={role}>
                    {role}
                  </option>
                ))}
              </select>
            </Field>
            <div className="flex items-end">
              <button
                className="btn btn-primary w-full"
                onClick={() => void addUser()}
                disabled={busy || !newUser.email || newUser.password.length < 10}
                type="button"
              >
                Add user
              </button>
            </div>
          </div>
        </Panel>
      ) : null}

      <Panel title="Deployment">
        <p className="text-2xs leading-relaxed text-ink-faint">
          See <code className="text-ink-dim">DEPLOY.md</code> in the repository for Docker, systemd and nginx
          setup, the environment variables each feature needs, and the reverse-proxy configuration that keeps
          the API off the public internet.
        </p>
        <button className="btn btn-ghost mt-3" onClick={signOut} type="button">
          Sign out of this device
        </button>
      </Panel>
    </div>
  );
}

export default function SettingsPage() {
  return (
    <AppShell>
      <Settings />
    </AppShell>
  );
}

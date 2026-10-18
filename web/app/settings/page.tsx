"use client";

/** Settings: system health, users, API keys and deployment (static IP + redirect). */

import { useCallback, useEffect, useMemo, useState } from "react";
import { api, getApiBase, loadToken } from "@/lib/api";
import type { DeploymentReport, User } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { AppShell } from "@/components/app-shell";
import { ErrorNote, Field, Loading, Modal, Panel, StatusChip } from "@/components/ui";
import { clock, num, relative } from "@/lib/format";

type Toast = { id: string; kind: "success" | "error"; message: string };

interface Health {
  status: string;
  environment: string;
  data_source?: string;
  execution_brokers?: string[];
  deployment?: {
    static_ip: string | null;
    redirect_url: string | null;
    redirect_path: string;
  };
  features: Record<string, boolean>;
  feed: { status: string; frames_received: number; reconnects: number; subscribed_instruments: number; last_error: string | null; cached_quotes: number };
  scrip_master: { status: string; instruments: number; last_sync: number | null; last_error: string | null; stale: boolean };
  quotes: { cached_quotes: number; last_error: string | null };
}

/** Copy-to-clipboard row: the value a user must paste into a broker portal. */
function CopyRow({ label, value, hint }: { label: string; value: string; hint?: string }) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false);
    }
  }

  return (
    <div className="rounded-md border border-hairline px-3 py-2">
      <p className="label-caps">{label}</p>
      <div className="mt-1 flex items-center gap-2">
        <code className="min-w-0 flex-1 break-all font-mono text-2xs text-ink">{value}</code>
        <button className="btn btn-sm btn-ghost shrink-0" onClick={() => void copy()} type="button">
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      {hint ? <p className="mt-1 text-3xs leading-relaxed text-ink-faint">{hint}</p> : null}
    </div>
  );
}

function DeploymentPanel() {
  const [report, setReport] = useState<DeploymentReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setReport(await api.deployment());
      setError(null);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Could not read the deployment report");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function refresh() {
    setBusy(true);
    setError(null);
    try {
      setReport(await api.refreshDeployment());
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Could not re-detect the public IP");
    } finally {
      setBusy(false);
    }
  }

  const ip = report?.static_ip;
  const ipTone =
    ip?.status === "verified" ? "live" : ip?.status === "mismatch" ? "error" : "warn";

  return (
    <Panel
      title="Deployment: static IP & redirect URL"
      subtitle="Register these with the broker before enabling live order APIs (SEBI)"
      actions={
        <button className="btn btn-sm btn-ghost" onClick={() => void refresh()} disabled={busy} type="button">
          {busy ? "Re-detecting…" : "Re-detect IP"}
        </button>
      }
    >
      {error ? <ErrorNote error={error} onRetry={() => void load()} /> : null}
      {!report && !error ? <Loading label="Reading the deployment report" /> : null}

      {report ? (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center gap-2">
            <StatusChip
              status={report.ready_for_live ? "live" : "warn"}
              label={report.ready_for_live ? "Ready for live order APIs" : "Not ready for live orders"}
            />
            {ip ? <StatusChip status={ipTone} label={`static IP ${ip.status}`} /> : null}
            <span className="text-3xs text-ink-faint">{report.environment}</span>
          </div>

          <div className="grid gap-3 sm:grid-cols-2">
            <CopyRow
              label="Public IP (register on the broker portal)"
              value={ip?.detected_ip ?? ip?.registered_ip ?? "not detected"}
              hint={
                ip?.detected_ip && ip?.registered_ip && ip.detected_ip !== ip.registered_ip
                  ? `Recorded as ${ip.registered_ip}, which no longer matches — orders will be rejected.`
                  : "This is the address the host egresses from right now."
              }
            />
            <CopyRow
              label="Redirect URL (production)"
              value={report.redirect.production}
              hint={
                report.redirect.configured
                  ? "Paste this into the broker's redirect/callback field."
                  : "Derived from the request — set PUBLIC_URL or REDIRECT_URL to pin it."
              }
            />
            <CopyRow
              label="Redirect URL (development)"
              value={report.redirect.development}
              hint="For a broker that also accepts a loopback callback."
            />
            <div className="rounded-md border border-hairline px-3 py-2">
              <p className="label-caps">Local (private) address</p>
              <code className="mt-1 block font-mono text-2xs text-ink-dim">
                {ip?.private_address ?? "unknown"}
              </code>
              <p className="mt-1 text-3xs leading-relaxed text-ink-faint">
                {ip?.detection_source === "override"
                  ? "From PUBLIC_IP_OVERRIDE."
                  : ip?.detection_source === "disabled"
                    ? "Detection is off (PUBLIC_IP_CHECK=0)."
                    : "Detected from the egress path."}
              </p>
            </div>
          </div>

          {ip?.message ? (
            <p
              className={`rounded-md border px-3 py-2 text-2xs ${
                ip.status === "verified"
                  ? "border-up/40 bg-up/10 text-up"
                  : ip.status === "mismatch"
                    ? "border-down/40 bg-down/10 text-down"
                    : "border-warn/40 bg-warn-soft text-warn"
              }`}
            >
              {ip.message}
            </p>
          ) : null}

          <div>
            <p className="label-caps mb-2">Compliance checklist</p>
            <ul className="space-y-1.5">
              {report.checklist.map((item) => (
                <li key={item.id} className="flex items-start gap-2 text-2xs">
                  <span className={item.ok ? "text-up" : "text-down"} aria-hidden>
                    {item.ok ? "✓" : "✕"}
                  </span>
                  <span className="min-w-0">
                    <span className={item.ok ? "text-ink-dim" : "text-ink"}>{item.label}</span>
                    {!item.ok ? (
                      <span className="mt-0.5 block text-3xs leading-relaxed text-ink-faint">{item.detail}</span>
                    ) : null}
                  </span>
                </li>
              ))}
            </ul>
          </div>

          <p className="border-t border-hairline pt-3 text-3xs leading-relaxed text-ink-faint">{ip?.note}</p>
        </div>
      ) : null}
    </Panel>
  );
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
  const [whatsappNumber, setWhatsappNumber] = useState(user?.whatsapp_number ?? "");
  const [revokeConfirm, setRevokeConfirm] = useState<string | null>(null);
  const [toasts, setToasts] = useState<Toast[]>([]);

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

  const toast = useMemo(() => (kind: "success" | "error", message: string) => {
    const id = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    setToasts((prev) => [...prev, { id, kind, message }]);
    setTimeout(() => setToasts((prev) => prev.filter((t) => t.id !== id)), 5000);
  }, []);

  const dismissToast = useCallback((id: string) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

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

  async function saveSettings() {
    setBusy(true);
    try {
      await api.updateSettings(whatsappNumber || null);
      toast("success", "Settings saved");
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Could not save settings");
      toast("error", failure instanceof Error ? failure.message : "Could not save settings");
    } finally {
      setBusy(false);
    }
  }

  async function revokeAccess(targetUserId: string) {
    setRevokeConfirm(null);
    setBusy(true);
    try {
      await api.updateUserRole(targetUserId, "viewer");
      setUsers(await api.listUsers());
      toast("success", "Access revoked — user is now a viewer");
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Could not revoke access");
      toast("error", failure instanceof Error ? failure.message : "Could not revoke access");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      {/* Toast notifications */}
      <div className="fixed top-4 right-4 z-50 flex flex-col gap-2 pointer-events-none">
        {toasts.map((t) => (
          <div
            key={t.id}
            className={`pointer-events-auto px-4 py-3 rounded-md shadow-pop text-sm min-w-[280px] ${
              t.kind === "success" ? "border-up/40 bg-up/10 text-up" : "border-down/40 bg-down/10 text-down"
            }`}
          >
            <div className="flex items-start gap-2">
              <span className="mt-0.5">{t.kind === "success" ? "✓" : "✕"}</span>
              <p className="leading-relaxed">{t.message}</p>
              <button
                className="ml-2 opacity-60 hover:opacity-100"
                onClick={() => dismissToast(t.id)}
                type="button"
                aria-label="Dismiss"
              >
                ✕
              </button>
            </div>
          </div>
        ))}
      </div>

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
              <p className="label-caps">Data source</p>
              <p className="mt-1.5 text-sm font-semibold">
                {health.data_source === "angel_one" ? "Angel One" : health.data_source ?? "—"}
              </p>
              <p className="mt-0.5 text-3xs text-ink-faint">
                execution: {(health.execution_brokers ?? []).join(" + ") || "—"}
              </p>
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

      {/* Personal settings: WhatsApp number */}
      <Panel title="Your settings" subtitle="Notification preferences">
        <Field label="WhatsApp number" hint="Include country code, e.g. +919876543210">
          <input
            className="field-input"
            type="tel"
            placeholder="+919876543210"
            value={whatsappNumber}
            onChange={(event) => setWhatsappNumber(event.target.value)}
          />
        </Field>
        <div className="flex items-end">
          <button
            className="btn btn-primary"
            onClick={() => void saveSettings()}
            disabled={busy}
            type="button"
          >
            {busy ? "Saving…" : "Save settings"}
          </button>
        </div>
      </Panel>

      {user?.role === "admin" ? (
        <Panel title="Users" subtitle="Role-based access control">
          <table className="tbl">
            <thead>
              <tr>
                <th>Email</th>
                <th>Role</th>
                <th>Status</th>
                <th>Actions</th>
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
                  <td>
                    {row.role === "trader" && row.id !== user.id ? (
                      <button
                        className="btn btn-sm btn-ghost text-down"
                        onClick={() => setRevokeConfirm(row.id)}
                        type="button"
                        disabled={busy}
                      >
                        Revoke access
                      </button>
                    ) : null}
                  </td>
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

      {user?.role === "admin" || user?.role === "trader" ? <DeploymentPanel /> : null}

      <Panel title="Deployment">
        <p className="text-2xs leading-relaxed text-ink-faint">
          See <code className="text-ink-dim">DEPLOY.md</code> in the repository for Docker, systemd and nginx
          setup, the static IP and redirect URL steps a broker needs, the environment variables each feature
          needs, and the reverse-proxy configuration that keeps the API off the public internet.
        </p>
        <button className="btn btn-ghost mt-3" onClick={signOut} type="button">
          Sign out of this device
        </button>
      </Panel>

      {/* Revoke access confirmation modal */}
      <Modal
        open={revokeConfirm !== null}
        title="Revoke access"
        onClose={() => setRevokeConfirm(null)}
      >
        <p className="text-2xs leading-relaxed text-ink">
          Downgrading this user to <strong>"viewer"</strong> removes their ability to place orders or
          create API keys. They will still be able to view market data and portfolios. This cannot be
          undone from this screen — an admin must re-assign a role.
        </p>
        <div className="flex justify-end gap-2 border-t border-hairline px-4 py-3">
          <button
            className="btn btn-sm btn-ghost"
            onClick={() => setRevokeConfirm(null)}
            type="button"
          >
            Cancel
          </button>
          <button
            className="btn btn-sm btn-primary"
            onClick={() => void revokeAccess(revokeConfirm!)}
            disabled={busy}
            type="button"
          >
            {busy ? "Revoking…" : "Confirm revoke"}
          </button>
        </div>
      </Modal>
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

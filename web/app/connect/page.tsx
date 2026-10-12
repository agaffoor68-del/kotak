"use client";

/**
 * Broker connection panel.
 *
 * A separate surface from Settings on purpose: this is where a user pastes
 * their own Kotak credentials, verifies them against the live API before they
 * are stored, and sees which address — IPv4 or IPv6 — the endpoints resolve to.
 * Secrets are write-only from here; the backend only ever returns masked views.
 */

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import type {
  BrokerAccountsResponse,
  BrokerNetworkResponse,
  BrokerTestResult,
} from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { AppShell } from "@/components/app-shell";
import { Empty, ErrorNote, Field, Loading, Panel, StatusChip } from "@/components/ui";
import { relative } from "@/lib/format";

const EMPTY_FORM = {
  label: "My Kotak account",
  consumer_key: "",
  totp_key: "",
  mpin: "",
  mobile_number: "",
  ucc: "",
  neo_fin_key: "",
  environment: "prod" as "prod" | "uat",
};

type Form = typeof EMPTY_FORM;

function verdictChip(verdict: BrokerNetworkResponse["verdict"]) {
  switch (verdict) {
    case "dual_stack":
      return <StatusChip status="live" label="Dual stack (IPv4 + IPv6)" />;
    case "ipv4_only":
      return <StatusChip status="warn" label="IPv4 only — no reachable IPv6" />;
    case "ipv6_unreachable":
      return <StatusChip status="error" label="IPv6 resolves but is unreachable" />;
    case "degraded":
      return <StatusChip status="error" label="Some Kotak hosts are down" />;
    default:
      return <StatusChip status="error" label="No Kotak host reachable" />;
  }
}

function FyersSetupPanel() {
  const [accepted, setAccepted] = useState(false);
  const [showSecret, setShowSecret] = useState(false);

  return (
    <Panel
      title="Connect FYERS terminal"
      subtitle="Use the App ID, App Secret and access token from your FYERS API app"
      actions={<StatusChip status="warn" label="Setup required" />}
    >
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="App ID" hint="FYERS API app ID">
          <input className="field-input font-mono" autoComplete="off" spellCheck={false} placeholder="e.g. ABCD1234-100" />
        </Field>
        <Field label="App Secret" hint="Keep this private">
          <input className="field-input font-mono" type="password" autoComplete="off" spellCheck={false} placeholder="Paste your app secret" />
        </Field>
        <Field label="Access token" hint="Generated after the FYERS OAuth login">
          <div className="flex gap-2">
            <input className="field-input min-w-0 flex-1 font-mono" type={showSecret ? "text" : "password"} autoComplete="off" spellCheck={false} placeholder="Paste access token" />
            <button className="btn btn-sm btn-ghost" type="button" onClick={() => setShowSecret((value) => !value)}>
              {showSecret ? "Hide" : "Show"}
            </button>
          </div>
        </Field>
        <Field label="Redirect URL" hint="Register this exact URL in FYERS">
          <input className="field-input font-mono" defaultValue="https://alpha-trade-pro.vercel.app/api/v1/broker/fyers/callback" readOnly />
        </Field>
      </div>

      <div className="mt-4 rounded-md border border-accent/30 bg-accent-soft px-3 py-3 text-2xs text-ink-dim">
        <p className="font-semibold text-ink">Required FYERS API permissions</p>
        <p className="mt-1">Profile details, transaction info, historical data, quotes and market data, and order placements.</p>
      </div>
      <label className="mt-4 flex cursor-pointer items-start gap-2 text-2xs text-ink-dim">
        <input className="mt-0.5 size-4 accent-[var(--accent)]" type="checkbox" checked={accepted} onChange={(event) => setAccepted(event.target.checked)} />
        <span>I accept the FYERS API terms and conditions.</span>
      </label>
      <div className="mt-4 flex flex-wrap items-center gap-2">
        <button className="btn btn-primary" type="button" disabled={!accepted}>
          Save and activate FYERS
        </button>
        <span className="text-3xs text-ink-faint">Credentials will be encrypted and never shown again.</span>
      </div>
      <p className="mt-3 rounded border border-hairline px-3 py-2 text-3xs leading-relaxed text-ink-faint">
        Real-time quotes require the FYERS WebSocket session after activation. This form is ready for the production credentials; no demo market data is displayed.
      </p>
    </Panel>
  );
}

function ConnectPanel({ onConnected }: { onConnected: () => void }) {
  const [form, setForm] = useState<Form>(EMPTY_FORM);
  const [result, setResult] = useState<BrokerTestResult | null>(null);
  const [busy, setBusy] = useState<null | "test" | "connect">(null);
  const [error, setError] = useState<string | null>(null);

  const set = <K extends keyof Form>(key: K, value: Form[K]) =>
    setForm((current) => ({ ...current, [key]: value }));

  const credentials = () => ({
    consumer_key: form.consumer_key.trim(),
    totp_key: form.totp_key.trim(),
    mpin: form.mpin.trim(),
    mobile_number: form.mobile_number.trim(),
    ucc: form.ucc.trim(),
    neo_fin_key: form.neo_fin_key.trim() || null,
    environment: form.environment,
  });

  const ready = Boolean(
    form.consumer_key.trim() && form.totp_key.trim() && form.mpin.trim() &&
    form.mobile_number.trim() && form.ucc.trim(),
  );

  async function test() {
    setBusy("test");
    setError(null);
    setResult(null);
    try {
      setResult(await api.brokerTest(credentials()));
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Could not reach Kotak");
    } finally {
      setBusy(null);
    }
  }

  async function connect() {
    setBusy("connect");
    setError(null);
    setResult(null);
    try {
      const saved = await api.brokerConnect({
        ...credentials(),
        label: form.label.trim() || "My Kotak account",
        test_before_save: true,
      });
      setForm({ ...EMPTY_FORM });
      setResult({ ok: true, error: `Connected ${saved.account.account_id}`, seconds: 0 });
      onConnected();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Could not connect the account");
    } finally {
      setBusy(null);
    }
  }

  return (
    <Panel
      title="Connect a Kotak Neo account"
      subtitle="Credentials are encrypted before storage and are never returned to the browser"
      actions={
        <>
          <button className="btn btn-sm btn-ghost" type="button" disabled={busy !== null || !ready}
            onClick={() => void test()}>
            {busy === "test" ? "Testing…" : "Test only"}
          </button>
          <button className="btn btn-sm btn-primary" type="button" disabled={busy !== null || !ready}
            onClick={() => void connect()}>
            {busy === "connect" ? "Connecting…" : "Verify and connect"}
          </button>
        </>
      }
    >
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Label" hint="Shown in the account list">
          <input className="field-input" value={form.label}
            onChange={(event) => set("label", event.target.value)} />
        </Field>
        <Field label="Environment">
          <select className="field-select" value={form.environment}
            onChange={(event) => set("environment", event.target.value as Form["environment"])}>
            <option value="prod">Production (prod)</option>
            <option value="uat">UAT / sandbox</option>
          </select>
        </Field>
        <Field label="Consumer key" hint="Kotak Neo app → Invest → Trade API">
          <input className="field-input font-mono" autoComplete="off" spellCheck={false}
            value={form.consumer_key}
            onChange={(event) => set("consumer_key", event.target.value)} />
        </Field>
        <Field label="UCC" hint="Your Kotak client code">
          <input className="field-input font-mono" autoComplete="off" spellCheck={false}
            value={form.ucc} onChange={(event) => set("ucc", event.target.value)} />
        </Field>
        <Field label="Mobile number" hint="Registered with Kotak">
          <input className="field-input" autoComplete="off" value={form.mobile_number}
            onChange={(event) => set("mobile_number", event.target.value)} />
        </Field>
        <Field label="TOTP secret" hint="Base32 key from your authenticator app">
          <input className="field-input font-mono" type="password" autoComplete="off" spellCheck={false}
            value={form.totp_key} onChange={(event) => set("totp_key", event.target.value)} />
        </Field>
        <Field label="MPIN">
          <input className="field-input" type="password" autoComplete="off" value={form.mpin}
            onChange={(event) => set("mpin", event.target.value)} />
        </Field>
        <Field label="NeoFinKey" hint="Only if Kotak issued one; leave blank otherwise">
          <input className="field-input font-mono" autoComplete="off" spellCheck={false}
            value={form.neo_fin_key} onChange={(event) => set("neo_fin_key", event.target.value)} />
        </Field>
      </div>

      {error ? <div className="mt-3"><ErrorNote error={error} /></div> : null}
      {result ? (
        <div className={`mt-3 rounded-md border px-3 py-2 text-2xs break-words ${
          result.ok ? "border-up/40 bg-up/10 text-up" : "border-down/40 bg-down/10 text-down"}`}>
          <p className="font-semibold">
            {result.ok ? "Kotak accepted these credentials" : "Login rejected"}
          </p>
          <p className="mt-0.5">{result.error}</p>
        </div>
      ) : null}
    </Panel>
  );
}



function AccountsPanel({
  data,
  onChanged,
}: {
  data: BrokerAccountsResponse | null;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function act(id: string, run: () => Promise<unknown>) {
    setBusy(id);
    setError(null);
    try {
      await run();
      onChanged();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Action failed");
    } finally {
      setBusy(null);
    }
  }

  if (!data) return <Loading label="Loading connected accounts" />;

  return (
    <Panel
      title="Connected accounts"
      subtitle={
        data.accounts.length
          ? `${data.accounts.length} account(s) visible to you`
          : "No account connected yet"
      }
      actions={
        <span className="text-3xs text-ink-faint">
          Vault {data.vault_enabled ? "enabled" : "disabled"} ·{" "}
          {data.legacy_env_fallback ? "env fallback on" : "env fallback off"}
        </span>
      }
    >
      {error ? <div className="mb-3"><ErrorNote error={error} /></div> : null}
      {!data.accounts.length ? (
        <Empty
          title="No Kotak account connected"
          message="Add your Consumer Key, TOTP secret, MPIN and UCC below. Each user connects their own account, and only that user can use it."
        />
      ) : (
        <table className="tbl">
          <thead>
            <tr>
              <th>Account</th>
              <th>Environment</th>
              <th>Session</th>
              <th>Updated</th>
              <th aria-label="Actions" />
            </tr>
          </thead>
          <tbody>
            {data.accounts.map((row) => {
              const live = row.session.authenticated === true;
              return (
                <tr key={row.account_id}>
                  <td className="text-xs">
                    <span className="font-mono">{row.account_id}</span>
                    <span className="ml-2 text-3xs text-ink-faint">{row.label}</span>
                  </td>
                  <td>
                    <span className="chip chip-idle">{row.environment}</span>
                  </td>
                  <td>
                    <StatusChip
                      status={live ? "live" : row.session.last_error ? "error" : "idle"}
                      label={live ? "authenticated" : row.session.status}
                    />
                    {row.session.last_error ? (
                      <p className="mt-1 max-w-xs truncate text-3xs text-down" title={row.session.last_error}>
                        {row.session.last_error}
                      </p>
                    ) : null}
                  </td>
                  <td className="text-3xs text-ink-faint">{relative(row.updated_at)}</td>
                  <td>
                    <div className="flex justify-end gap-1.5">
                      <button className="btn btn-sm btn-ghost" type="button" disabled={busy === row.account_id}
                        onClick={() => void act(row.account_id, () => api.brokerTestSaved(row.account_id))}>
                        Test
                      </button>
                      <button className="btn btn-sm btn-ghost" type="button" disabled={busy === row.account_id}
                        onClick={() => void act(row.account_id, () => api.brokerReconnect(row.account_id))}>
                        Reconnect
                      </button>
                      <button className="btn btn-sm btn-ghost text-down" type="button"
                        disabled={busy === row.account_id}
                        onClick={() => void act(row.account_id, () => api.brokerDisconnect(row.account_id))}>
                        Remove
                      </button>
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </Panel>
  );
}

function NetworkPanel() {
  const [report, setReport] = useState<BrokerNetworkResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      setReport(await api.brokerNetwork());
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Network check failed");
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <Panel
      title="Kotak network path"
      subtitle="Resolved IPv4 and IPv6 addresses, reachability and latency"
      actions={
        <button className="btn btn-sm btn-ghost" type="button" onClick={() => void load()} disabled={busy}>
          {busy ? "Checking…" : "Re-check"}
        </button>
      }
    >
      {error ? <ErrorNote error={error} onRetry={() => void load()} /> : null}
      {!report && !error ? <Loading label="Probing Kotak endpoints" /> : null}
      {!report ? null : (
        <>
          <div className="flex flex-wrap items-center gap-2">
            {verdictChip(report.verdict)}
            <span className="text-3xs text-ink-faint">
              {report.summary.ipv4_reachable}/{report.summary.ipv4_resolved} IPv4 ·{" "}
              {report.summary.ipv6_reachable}/{report.summary.ipv6_resolved} IPv6 reachable ·{" "}
              {report.summary.timeout_seconds}s timeout
            </span>
          </div>
          {report.required_hosts_down.length ? (
            <p className="mt-2 text-2xs text-down">
              Not answering: {report.required_hosts_down.join(", ")}
            </p>
          ) : null}

          <div className="mt-3 overflow-auto">
            <table className="tbl">
              <thead>
                <tr>
                  <th>Endpoint</th>
                  <th>IPv4 address</th>
                  <th>IPv6 address</th>
                  <th>Latency</th>
                </tr>
              </thead>
              <tbody>
                {report.hosts.map((host) => {
                  const probe = (address: string) =>
                    host.results.find((entry) => entry.address === address);
                  const rows = host.ipv4.length || host.ipv6.length
                    ? Math.max(host.ipv4.length, host.ipv6.length)
                    : 0;
                  return Array.from({ length: rows || 1 }).map((_, index) => {
                    const v4 = host.ipv4[index];
                    const v6 = host.ipv6[index];
                    const v4Probe = v4 ? probe(v4) : undefined;
                    const v6Probe = v6 ? probe(v6) : undefined;
                    return (
                      <tr key={`${host.host}-${index}`}>
                        <td className="text-2xs">
                          {index === 0 ? (
                            <>
                              <span className="text-ink">{host.label}</span>
                              <br />
                              <span className="font-mono text-3xs text-ink-faint">{host.host}</span>
                            </>
                          ) : null}
                        </td>
                        <td className="font-mono text-2xs">
                          {v4 ? (
                            <span className={v4Probe?.reachable ? "text-up" : "text-down"}>
                              {v4}
                              {v4Probe && !v4Probe.reachable ? " ✕" : ""}
                            </span>
                          ) : (
                            <span className="text-ink-faint">—</span>
                          )}
                        </td>
                        <td className="font-mono text-2xs">
                          {v6 ? (
                            <span className={v6Probe?.reachable ? "text-up" : "text-down"}>
                              {v6}
                              {v6Probe && !v6Probe.reachable ? " ✕" : ""}
                            </span>
                          ) : (
                            <span className="text-ink-faint">—</span>
                          )}
                        </td>
                        <td className="num text-2xs">
                          {v4Probe?.reachable && v4Probe.latency_ms !== null
                            ? `${v4Probe.latency_ms}ms`
                            : v6Probe?.reachable && v6Probe.latency_ms !== null
                              ? `${v6Probe.latency_ms}ms`
                              : host.dns_error
                                ? "DNS failed"
                                : "timeout"}
                        </td>
                      </tr>
                    );
                  });
                })}
              </tbody>
            </table>
          </div>
          <p className="mt-2 text-3xs leading-relaxed text-ink-faint">{report.note}</p>
        </>
      )}
    </Panel>
  );
}

function Connect() {
  const { user } = useAuth();
  const [accounts, setAccounts] = useState<BrokerAccountsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setAccounts(await api.brokerAccounts());
      setError(null);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Could not load broker accounts");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const canConnect = user?.role === "admin" || user?.role === "trader";

  return (
    <div className="space-y-4">
      <header>
        <h1 className="text-lg font-semibold tracking-tight">Broker connection</h1>
        <p className="text-2xs text-ink-faint">
          Connect both Kotak Neo and FYERS terminals for one professional live trading desk
        </p>
      </header>

      {error ? <ErrorNote error={error} onRetry={() => void load()} /> : null}

      <AccountsPanel data={accounts} onChanged={() => void load()} />

      <FyersSetupPanel />

      {canConnect ? (
        <ConnectPanel onConnected={() => void load()} />
      ) : (
        <Panel title="Connect an account" subtitle="Requires the trader or admin role">
          <p className="text-2xs text-ink-faint">
            Your account is <span className="font-mono">{user?.account_id ?? "not bound"}</span>. Ask an
            administrator to connect credentials for you.
          </p>
        </Panel>
      )}

      <NetworkPanel />
    </div>
  );
}

export default function ConnectPage() {
  return (
    <AppShell>
      <Connect />
    </AppShell>
  );
}


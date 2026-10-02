/**
 * End-to-end verification for AlphaTradePro.
 *
 * Signs in, then visits every route and records what actually mounted. Pages that
 * legitimately have no data (empty symbol master, no recorded tape) are reported
 * as such rather than treated as failures — the platform is designed to say "no
 * data" instead of inventing numbers.
 *
 * Run against a live stack:
 *   node browser.mjs <url> --script ./verify.mjs
 */

const ROUTES = [
  "/dashboard", "/market", "/options", "/portfolio", "/trade",
  "/risk", "/paper", "/strategies", "/backtest",
  "/analytics", "/coach", "/alerts", "/settings",
];

/** Text that only a real page would contain; used to prove the page mounted. */
const SIGNALS = /kotak|recorded|history|no records|not configured|unavailable|master|warm-up|no holdings|entry/i;

export default async function run(page, ui) {
  const report = { consoleErrors: [], failedRequests: [], pages: {} };

  page.on("console", (message) => {
    if (message.type() === "error") report.consoleErrors.push(message.text().slice(0, 180));
  });
  page.on("requestfailed", (request) => {
    report.failedRequests.push(`${request.url().slice(0, 120)} ${request.failure()?.errorText ?? ""}`);
  });
  page.on("response", (response) => {
    if (response.status() >= 500) report.failedRequests.push(`HTTP ${response.status()} ${response.url().slice(0, 120)}`);
  });

  // ---- sign in ----
  await page.waitForSelector("#login-email", { timeout: 25000 });
  await page.locator("#login-email").fill(process.env.AT_EMAIL ?? "admin@alphatrade.local");
  await page.locator("#login-password").fill(process.env.AT_PASSWORD ?? "LocalDev-Admin-99");
  await page.getByRole("button", { name: /^sign in$/i }).click();

  await page.waitForURL("**/dashboard", { timeout: 30000 });
  await page.waitForFunction(() => document.querySelector("h1")?.textContent?.trim().length > 0, { timeout: 25000 });
  report.landedOn = new URL(page.url()).pathname;

  // ---- every route ----
  for (const route of ROUTES) {
    const before = report.consoleErrors.length;
    await page.goto(`${process.env.AT_ORIGIN ?? "http://127.0.0.1:3000"}${route}`, {
      waitUntil: "domcontentloaded",
    });
    // Wait for the page to actually mount rather than a fixed delay.
    await page.waitForFunction(
      () => {
        const heading = document.querySelector("h1")?.textContent?.trim();
        return Boolean(heading && heading.length > 0);
      },
      { timeout: 25000 },
    );
    await page.waitForTimeout(1400);

    report.pages[route] = await page.evaluate((signalSource) => {
      const signal = new RegExp(signalSource);
      const heading = document.querySelector("h1")?.textContent?.trim() ?? null;
      const text = document.body.innerText;
      return {
        heading,
        chars: text.length,
        navLinks: document.querySelectorAll("aside a, nav a").length,
        // A mounted page that says nothing about data is a hollow shell.
        saysSomethingAboutData: signal.test(text),
        // Horizontal overflow is the classic mobile-layout failure.
        horizontalOverflow: document.documentElement.scrollWidth > window.innerWidth + 2,
      };
    }, SIGNALS.source);

    report.pages[route].newConsoleErrors = report.consoleErrors.length - before;
  }

  // ---- mobile layout check ----
  const desktop = page.viewportSize();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`${process.env.AT_ORIGIN ?? "http://127.0.0.1:3000"}/dashboard`, { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(2200);
  report.mobile = await page.evaluate(() => ({
    width: window.innerWidth,
    horizontalOverflow: document.documentElement.scrollWidth > window.innerWidth + 2,
    hamburgerVisible: Boolean(
      [...document.querySelectorAll("button")].find(
        (b) => b.getAttribute("aria-label") === "Toggle navigation" && b.offsetParent !== null,
      ),
    ),
    sessionChipPresent: /Market open|Market closed|Pre-open|Post-close/.test(document.body.innerText),
  }));
  await page.setViewportSize(desktop);

  // ---- PWA installability ----
  const manifest = await page.evaluate(async () => {
    const link = document.querySelector('link[rel="manifest"]');
    if (!link) return null;
    const response = await fetch(link.getAttribute("href"));
    return response.ok ? await response.json() : null;
  });
  report.pwa = {
    manifestLoaded: Boolean(manifest),
    name: manifest?.name ?? null,
    display: manifest?.display ?? null,
    icons: manifest?.icons?.length ?? 0,
    serviceWorkerRegistered: await page.evaluate(() => Boolean(navigator.serviceWorker?.controller)),
  };

  return report;
bd}

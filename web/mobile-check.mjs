/**
 * Mobile verification: sign in, confirm the QR panel renders, and check the
 * 390x844 layout for horizontal overflow (the classic phone-layout failure).
 */
export default async function run(page) {
  // iPhone-class viewport from the very start, so first paint is judged as the
  // phone would see it.
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("http://127.0.0.1:3000/login", { waitUntil: "domcontentloaded" });
  await page.waitForSelector("#login-email", { timeout: 25000 });

  await page.locator("#login-email").fill("admin@alphatrade.local");
  await page.locator("#login-password").fill("LocalDev-Admin-99");
  await page.getByRole("button", { name: /^sign in$/i }).click();
  await page.waitForURL("**/dashboard", { timeout: 30000 });

  // Wait for the QR code image to actually load, not just appear.
  await page.waitForFunction(
    () => {
      const img = document.querySelector('img[alt*="QR code"]');
      return Boolean(img && img.complete && img.naturalWidth > 0);
    },
    { timeout: 30000 },
  );

  return await page.evaluate(() => {
    const qr = document.querySelector('img[alt*="QR code"]');
    const overflow = document.documentElement.scrollWidth - window.innerWidth;
    // Find any element wider than the viewport, so a layout break is named.
    const offenders = [...document.querySelectorAll("*")]
      .filter((el) => el.getBoundingClientRect().width > window.innerWidth + 2)
      .slice(0, 5)
      .map((el) => `${el.tagName.toLowerCase()}.${(el.className || "").toString().split(" ")[0]}`);

    return {
      viewport: { width: window.innerWidth, height: window.innerHeight },
      path: location.pathname,
      horizontalOverflowPx: overflow,
      overflowOffenders: offenders,
      qrLoaded: Boolean(qr && qr.complete && qr.naturalWidth > 0),
      qrPixels: qr ? `${qr.naturalWidth}x${qr.naturalHeight}` : null,
      signInLinkVisible: document.body.innerText.includes("Sign in on your phone"),
      shareChip: document.body.innerText.match(/canonical URL|detected from request/)?.[0] ?? null,
      hamburgerVisible: Boolean(
        [...document.querySelectorAll("button")].find(
          (b) => b.getAttribute("aria-label") === "Toggle navigation" && b.offsetParent !== null,
        ),
      ),
      indexCards: document.querySelectorAll(".panel").length,
    };
  });
}

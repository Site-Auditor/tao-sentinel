// Layout regression harness: asserts the page never scrolls horizontally and
// the table sits flush in its card, at the widths that matter.
// Usage: node scripts/measure-layout.mjs [baseUrl]
import puppeteer from "puppeteer-core";

const base = process.argv[2] ?? "http://127.0.0.1:8787";
const browser = await puppeteer.launch({
  executablePath: "/usr/bin/chromium-browser",
  args: ["--no-sandbox", "--disable-gpu"],
});
let failed = false;
for (const width of [360, 390, 768, 1024, 1100, 1280, 1360]) {
  const page = await browser.newPage();
  await page.setViewport({ width, height: 900 });
  await page.emulateMediaFeatures([
    { name: "prefers-reduced-motion", value: "reduce" },
  ]);
  await page.goto(`${base}/`, { waitUntil: "networkidle0", timeout: 120000 });
  await page.waitForSelector("tbody tr", { timeout: 60000 });
  const m = await page.evaluate(() => {
    const table = document.querySelector("table");
    const card = table.closest(".card");
    const cardRect = card.getBoundingClientRect();
    return {
      pageScrollW: document.documentElement.scrollWidth,
      innerW: window.innerWidth,
      tableW: Math.round(table.getBoundingClientRect().width),
      cardW: Math.round(cardRect.width),
      cardRight: Math.round(cardRect.right),
      cols: document.querySelectorAll("thead th").length,
    };
  });
  const overflow = m.pageScrollW > m.innerW;
  const tableSpills = m.tableW > m.cardW;
  const cardSpills = m.cardRight > m.innerW;
  const bad = overflow || tableSpills || cardSpills;
  failed ||= bad;
  console.log(
    `${bad ? "FAIL" : " ok "} ${width}px cols=${m.cols} page=${m.pageScrollW}/${m.innerW} table=${m.tableW}/card=${m.cardW} cardRight=${m.cardRight}`,
  );
  await page.close();
}
await browser.close();
process.exit(failed ? 1 : 0);

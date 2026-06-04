import puppeteer from "puppeteer-core";
const browser = await puppeteer.launch({ executablePath: "/usr/bin/chromium-browser", args: ["--no-sandbox", "--disable-gpu"] });
const results = {};
for (const width of [390, 768, 1100, 1360]) {
  const page = await browser.newPage();
  await page.setViewport({ width, height: 900 });
  await page.goto("http://127.0.0.1:8801/", { waitUntil: "networkidle0", timeout: 60000 });
  await page.waitForSelector("tbody tr", { timeout: 30000 });
  results[width] = await page.evaluate(() => {
    const table = document.querySelector("table");
    return {
      pageScrollW: document.documentElement.scrollWidth,
      innerW: window.innerWidth,
      tableW: Math.round(table.getBoundingClientRect().width),
      cardW: Math.round(table.parentElement.getBoundingClientRect().width),
      cols: [...document.querySelectorAll("thead th")].map((t) => t.textContent.trim().replace(/[▲▼]/g, "")),
      nameCellW: Math.round(document.querySelector("tbody tr td:nth-child(2)").getBoundingClientRect().width),
      overflows: document.documentElement.scrollWidth > window.innerWidth,
    };
  });
  await page.close();
}
console.log(JSON.stringify(results, null, 1));
await browser.close();

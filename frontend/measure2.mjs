import puppeteer from "puppeteer-core";
const browser = await puppeteer.launch({ executablePath: "/usr/bin/chromium-browser", args: ["--no-sandbox", "--disable-gpu"] });
const page = await browser.newPage();
await page.setViewport({ width: 390, height: 844 });
await page.goto("https://tao.insightfulbytes.com/", { waitUntil: "networkidle0", timeout: 120000 });
await page.waitForSelector("tbody tr", { timeout: 60000 });
const m = await page.evaluate(() => {
  const ths = [...document.querySelectorAll("thead th")];
  const tds = [...document.querySelector("tbody tr").children];
  return {
    thCount: ths.length,
    tdCount: tds.length,
    thLabels: ths.map((t) => t.textContent.trim().replace("▲", "")),
    tdTexts: tds.map((t) => t.textContent.trim().slice(0, 12)),
    innerW: window.innerWidth,
  };
});
console.log(JSON.stringify(m));
await browser.close();

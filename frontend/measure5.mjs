import puppeteer from "puppeteer-core";
const browser = await puppeteer.launch({ executablePath: "/usr/bin/chromium-browser", args: ["--no-sandbox", "--disable-gpu"] });
const page = await browser.newPage();
await page.setViewport({ width: 1360, height: 900 });
await page.goto("http://127.0.0.1:8801/", { waitUntil: "networkidle0", timeout: 60000 });
await page.waitForSelector("tbody tr", { timeout: 30000 });
const m = await page.evaluate(() => {
  const cg = document.querySelector("colgroup");
  const ths = [...document.querySelectorAll("thead th")];
  return {
    colgroupHTML: cg ? cg.outerHTML : "MISSING",
    colCount: cg ? cg.children.length : 0,
    thWidths: ths.map((t) => ({ l: t.textContent.trim().replace(/[▲▼]/g, ""), w: Math.round(t.getBoundingClientRect().width) })),
  };
});
console.log(JSON.stringify(m, null, 1));
await browser.close();

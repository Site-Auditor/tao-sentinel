import puppeteer from "puppeteer-core";
const browser = await puppeteer.launch({ executablePath: "/usr/bin/chromium-browser", args: ["--no-sandbox", "--disable-gpu"] });
const page = await browser.newPage();
await page.setViewport({ width: 390, height: 844 });
await page.goto("https://tao.insightfulbytes.com/", { waitUntil: "networkidle0", timeout: 120000 });
await page.waitForSelector("tbody tr", { timeout: 60000 });
const m = await page.evaluate(() => {
  const table = document.querySelector("table");
  const cs = getComputedStyle(table);
  const rows = [...table.querySelectorAll("tr")];
  const cellCounts = {};
  rows.forEach((r) => { const n = r.children.length; cellCounts[n] = (cellCounts[n] || 0) + 1; });
  const wideRow = rows.find((r) => r.getBoundingClientRect().width > 360);
  return {
    tableCSSWidth: cs.width, tableLayout: cs.tableLayout,
    rectW: Math.round(table.getBoundingClientRect().width),
    rowCellCounts: cellCounts,
    head300: table.outerHTML.slice(0, 300),
    wideRowHTML: wideRow ? wideRow.outerHTML.slice(0, 400) : null,
    widestCellTexts: (() => {
      let worst = null, max = 0;
      rows.forEach((r) => [...r.children].forEach((c) => {
        const w = c.getBoundingClientRect().width;
        if (w > max) { max = w; worst = c.textContent.trim().slice(0, 30); }
      }));
      return { max: Math.round(max), worst };
    })(),
  };
});
console.log(JSON.stringify(m, null, 1));
await browser.close();

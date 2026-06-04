import puppeteer from "puppeteer-core";
const browser = await puppeteer.launch({ executablePath: "/usr/bin/chromium-browser", args: ["--no-sandbox", "--disable-gpu"] });
const page = await browser.newPage();
await page.setViewport({ width: 1360, height: 900 });
await page.goto("http://127.0.0.1:8801/", { waitUntil: "networkidle0", timeout: 60000 });
await page.waitForSelector("tbody tr", { timeout: 30000 });
const m = await page.evaluate(() => {
  const tables = [...document.querySelectorAll("table")];
  return tables.map((t) => {
    const tr = t.querySelector("tbody tr");
    return {
      w: Math.round(t.getBoundingClientRect().width),
      ths: t.querySelectorAll("thead th").length,
      cols: t.querySelectorAll("colgroup col").length,
      firstRowText: tr ? tr.textContent.trim().slice(0, 40) : null,
      nameTdW: tr && tr.children[1] ? Math.round(tr.children[1].getBoundingClientRect().width) : null,
    };
  });
});
console.log(JSON.stringify(m, null, 1));
await browser.close();

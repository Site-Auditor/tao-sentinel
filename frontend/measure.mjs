import puppeteer from "puppeteer-core";
const browser = await puppeteer.launch({
  executablePath: "/usr/bin/chromium-browser",
  args: ["--no-sandbox", "--disable-gpu"],
});
const page = await browser.newPage();
await page.setViewport({ width: 390, height: 844 });
await page.emulateMediaFeatures([{ name: "prefers-reduced-motion", value: "reduce" }]);
await page.goto("https://tao.insightfulbytes.com/", { waitUntil: "networkidle0", timeout: 120000 });
await page.waitForSelector("tbody tr", { timeout: 60000 });
const m = await page.evaluate(() => {
  const doc = document.documentElement;
  const table = document.querySelector("table");
  const card = table.closest(".card");
  const section = card.parentElement;
  const ths = [...document.querySelectorAll("thead th")].map((th) => ({
    id: th.textContent.trim(), w: Math.round(th.getBoundingClientRect().width),
  }));
  const firstRowTds = [...document.querySelector("tbody tr").children].map((td) => Math.round(td.getBoundingClientRect().width));
  return {
    docScrollW: doc.scrollWidth, viewportW: window.innerWidth,
    tableW: Math.round(table.getBoundingClientRect().width),
    cardW: Math.round(card.getBoundingClientRect().width),
    sectionW: Math.round(section.getBoundingClientRect().width),
    ths, firstRowTds,
  };
});
console.log(JSON.stringify(m, null, 1));
await browser.close();

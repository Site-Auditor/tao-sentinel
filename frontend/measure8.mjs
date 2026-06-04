import puppeteer from "puppeteer-core";
const b = await puppeteer.launch({ executablePath: "/usr/bin/chromium-browser", args: ["--no-sandbox", "--disable-gpu"] });
const p = await b.newPage();
await p.setViewport({ width: 1360, height: 900 });
await p.goto("http://127.0.0.1:8801/", { waitUntil: "networkidle0", timeout: 60000 });
await p.waitForSelector("tbody tr");
const m = await p.evaluate(() => {
  const table = document.querySelector("table");
  const cols = [...table.querySelectorAll("col")];
  const tr = table.querySelector("tbody tr");
  const th = table.querySelectorAll("thead th")[1];
  const td = tr.children[1];
  return {
    tableLayout: getComputedStyle(table).tableLayout,
    tableW: table.getBoundingClientRect().width,
    col1Style: cols[1].getAttribute("style"),
    col1Computed: getComputedStyle(cols[1]).width,
    nameThW: th.getBoundingClientRect().width,
    nameTdW: td.getBoundingClientRect().width,
    nameTdHTML: td.outerHTML.slice(0, 200),
    sameTable: th.closest("table") === td.closest("table"),
    trChildren: tr.children.length,
    theadRows: table.tHead.rows.length,
  };
});
console.log(JSON.stringify(m, null, 1));
await b.close();

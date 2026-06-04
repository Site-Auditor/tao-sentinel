import puppeteer from "puppeteer-core";
const b = await puppeteer.launch({ executablePath: "/usr/bin/chromium-browser", args: ["--no-sandbox", "--disable-gpu"] });
const p = await b.newPage();
await p.setViewport({ width: 1360, height: 900 });
await p.goto("http://127.0.0.1:8801/", { waitUntil: "networkidle0", timeout: 60000 });
await p.waitForSelector("tbody tr");
console.log(await p.evaluate(() => getComputedStyle(document.querySelector("table")).tableLayout));
await b.close();

// HTML → PDF via headless Chromium (puppeteer-core driving a system Chromium).
//
// Why a browser at all: the rest of this service is Konva + node-canvas, which
// is a 2D drawing surface, not an HTML layout engine. The board report is real
// HTML — CSS grid (.score/.chart-grid/.ins), @media print, break-inside,
// thead{display:table-header-group}, clamp() — so only a browser engine lays it
// out faithfully. cairo/pango/rsvg in the image are node-canvas's C deps and
// cannot parse HTML.
//
// The documents this serves are fully self-contained: no <script>, no <link>,
// no <img>, no url(), no external anything (charts are inline SVG). We hold the
// renderer to that contract rather than trusting it:
//   * JavaScript is DISABLED in the page.
//   * Every subresource request is aborted.
// That leaves almost no attack surface even though the HTML is machine-built
// from spreadsheet data. Blocked-request counts are reported back in a response
// header so a future report that quietly grows an external dependency is
// visible instead of silently rendering wrong.
import puppeteer from "puppeteer-core";

const DEFAULT_CHROME = process.platform === "win32"
  ? "C:/Program Files/Google/Chrome/Application/chrome.exe"
  : "/usr/bin/chromium";

export const chromePath = () =>
  process.env.CHROME_PATH?.trim() || DEFAULT_CHROME;

export const pdfTimeoutMs = () => Number(process.env.PDF_TIMEOUT_MS ?? 60000);

// Chromium is memory-heavy; a handful of reports a week never needs more than a
// couple in flight, and an unbounded queue is just an OOM with extra steps.
const maxConcurrent = () => Math.max(1, Number(process.env.PDF_CONCURRENCY ?? 2));

let browserPromise = null;

// Lazy: /health and /render must not pay for a browser launch, and with
// min-instances=0 a report request is a cold start anyway.
async function getBrowser() {
  const existing = await browserPromise?.catch(() => null);
  if (existing?.connected) return existing;
  browserPromise = puppeteer.launch({
    executablePath: chromePath(),
    // `true` = the modern headless mode built into every current Chromium.
    // Not "shell": that selects old headless, which recent Chromium builds no
    // longer ship, and the container tracks Debian's chromium package.
    headless: true,
    args: [
      "--no-sandbox",                 // Cloud Run disallows Chromium's setuid sandbox
      "--disable-dev-shm-usage",      // Cloud Run /dev/shm is 64MB — without this, crashes
      "--disable-gpu",
      "--font-render-hinting=none",   // deterministic text metrics across hosts
    ],
  });
  return browserPromise;
}

export async function closeBrowser() {
  const b = await browserPromise?.catch(() => null);
  browserPromise = null;
  if (b?.connected) await b.close();
}

let inFlight = 0;
const waiting = [];
const acquire = () => (inFlight < maxConcurrent()
  ? ((inFlight += 1), Promise.resolve())
  : new Promise((r) => waiting.push(r)));
const release = () => {
  const next = waiting.shift();
  if (next) next();
  else inFlight -= 1;
};

/** Render a self-contained HTML document to a PDF buffer. */
export async function renderPdf(html) {
  await acquire();
  let page;
  try {
    const browser = await getBrowser();
    page = await browser.newPage();
    let blocked = 0;

    // No JS: the contract says the document has none, and this makes injected
    // content inert rather than merely unlikely to be harmful.
    await page.setJavaScriptEnabled(false);

    await page.setRequestInterception(true);
    page.on("request", (req) => {
      const url = req.url();
      if (url.startsWith("data:") || url === "about:blank") return void req.continue();
      blocked += 1;
      req.abort("blockedbyclient").catch(() => {});
    });

    await page.emulateMediaType("print");
    await page.setContent(html, { waitUntil: "load", timeout: pdfTimeoutMs() });

    const pdf = await page.pdf({
      printBackground: true,    // MANDATORY: the identity IS the background colour —
                                // navy cover, gold cells, navy group bands. Without
                                // this the report prints as white paper.
      preferCSSPageSize: true,  // honour the document's own @page{size:A4;margin:13mm 11mm}
      displayHeaderFooter: false,
      timeout: pdfTimeoutMs(),
    });
    return { pdf: Buffer.from(pdf), blocked };
  } finally {
    await page?.close().catch(() => {});
    release();
  }
}

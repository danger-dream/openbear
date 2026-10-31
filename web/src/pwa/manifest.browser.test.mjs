import test from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { MANIFEST_PATH, DARK_MANIFEST_PATH } from "./manifest.js";

const require = createRequire(import.meta.url);
let chromium;
try { ({ chromium } = require(process.env.OPENBEAR_PLAYWRIGHT_MODULE || "/opt/openclaw/node_modules/playwright-core")); } catch {}
const webRoot = new URL("../../", import.meta.url);
const html = readFileSync(new URL("index.html", webRoot), "utf8");
const firstPaint = html.match(/<script>\s*([\s\S]*?)<\/script>/)[1];
const PREFIX = "/_i/family-a/";

// Exercise Chromium's manifest parser with the real first-paint script and theme controller.
test("Chromium loads matching PWA colors on startup and theme changes without changing app identity", { skip: !chromium }, async t => {
  const server = createServer((request, response) => {
    const path = new URL(request.url, "http://localhost").pathname;
    if (path === "/" || path === "/chat") {
      // /chat stands for a gateway instance page whose server injected its prefix.
      const script = path === "/" ? firstPaint : firstPaint.replace('"[[asset-base]]"', JSON.stringify(PREFIX));
      response.setHeader("Content-Type", "text/html");
      response.end(`<!doctype html><html><head><link rel="manifest" href="${MANIFEST_PATH}"><meta name="theme-color" content="#f8f8f8"><meta name="color-scheme" content="light"><script>${script}</script></head><body><script type="module">import {setThemeMode} from '/src/theme.js'; window.setThemeMode = setThemeMode;</script></body></html>`);
      return;
    }
    const file = path.startsWith(PREFIX) ? path.slice(PREFIX.length - 1) : path;
    const script = ["/src/theme.js", "/src/pwa/manifest.js", "/src/assetBase.js"].includes(file);
    const asset = [MANIFEST_PATH, DARK_MANIFEST_PATH, "/icons/openbear-192.png", "/icons/openbear-512.png"].includes(file);
    if (!script && !asset) { response.writeHead(404).end(); return; }
    response.setHeader("Content-Type", script ? "text/javascript" : file.endsWith(".webmanifest") ? "application/manifest+json" : "image/png");
    response.end(readFileSync(new URL(`${script ? "" : "public/"}${file.slice(1)}`, webRoot)));
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise(resolve => server.close(resolve)));
  const browser = await chromium.launch({ executablePath: process.env.OPENBEAR_CHROME || "/usr/bin/google-chrome", headless: true, args: ["--no-sandbox"] });
  t.after(() => browser.close());
  const origin = `http://127.0.0.1:${server.address().port}`;
  for (const [width, entry, base] of [[390, "/", "/"], [1280, "/", "/"], [390, "/chat", PREFIX]]) {
    const dark = `${base}assets/pwa/manifest-dark.webmanifest`;
    const context = await browser.newContext({ viewport: { width, height: 844 }, colorScheme: "light" });
    try {
      await context.addInitScript(() => localStorage.setItem("openbear.theme.v1", "dark"));
      const page = await context.newPage();
      const errors = [];
      page.on("pageerror", error => errors.push(error.message));
      await page.goto(origin + entry);
      await page.waitForFunction(() => typeof window.setThemeMode === "function");
      const cdp = await context.newCDPSession(page);
      const verify = async (theme, path, color) => {
        await page.waitForFunction(expected => document.documentElement.dataset.theme === expected, theme);
        const result = await cdp.send("Page.getAppManifest");
        assert.equal(result.url, origin + path);
        assert.deepEqual(result.errors, []);
        const manifest = JSON.parse(result.data);
        assert.equal(manifest.theme_color, color);
        assert.equal(manifest.background_color, color);
        for (const key of ["id", "start_url", "scope"]) assert.equal(new URL(manifest[key], result.url).href, origin + "/");
        assert.equal(await page.locator('meta[name="theme-color"]').getAttribute("content"), color);
      };
      await verify("dark", dark, "#161616");
      await page.evaluate(() => window.setThemeMode("light"));
      await verify("light", MANIFEST_PATH, "#f8f8f8");
      await page.emulateMedia({ colorScheme: "dark" });
      await verify("light", MANIFEST_PATH, "#f8f8f8");
      await page.evaluate(() => window.setThemeMode("auto"));
      await verify("dark", dark, "#161616");
      await page.emulateMedia({ colorScheme: "light" });
      await verify("light", MANIFEST_PATH, "#f8f8f8");
      assert.deepEqual(errors, []);
      t.diagnostic(`Chromium manifest parser verified at ${width}px with asset base ${base}`);
    } finally { await context.close(); }
  }
});

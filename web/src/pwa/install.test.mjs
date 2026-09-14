import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { compileScript, compileTemplate, parse } from "@vue/compiler-sfc";
import { computed, ref, shallowRef } from "vue";
import { createInstallController, checkInstallResources, httpsOrigin, installPresentation, INSTALL_ICONS } from "./install.js";

const publicRoot = new URL("../../public/", import.meta.url);
const manifest = JSON.parse(readFileSync(new URL("manifest.webmanifest", publicRoot), "utf8"));
class FakeWindow extends EventTarget {
  constructor({ origin = "https://bear.example", secure = true, ua = "Chrome", standalone = false, touch = 0 } = {}) {
    super();
    this.location = new URL(`${origin}/settings?session=never-copy#private`);
    this.isSecureContext = secure;
    this.navigator = { userAgent: ua, maxTouchPoints: touch };
    this.mode = Object.assign(new EventTarget(), { matches: standalone });
    this.matchMedia = () => this.mode;
    this.fetchCalls = [];
    this.fetch = async (url, options) => {
      this.fetchCalls.push({ url, options });
      const path = new URL(url).pathname;
      return new Response(readFileSync(new URL(path.slice(1), publicRoot)), { headers: { "content-type": path.endsWith("webmanifest") ? "application/manifest+json" : "image/png" } });
    };
  }
}
function installEvent({ outcome = "accepted", failure = null, choice = null } = {}) {
  const event = new Event("beforeinstallprompt", { cancelable: true });
  event.calls = 0;
  event.prompt = () => {
    event.calls += 1;
    if (failure) throw failure;
    return Promise.resolve({ outcome });
  };
  if (choice) event.userChoice = choice;
  return event;
}
const kind = (controller) => installPresentation(controller.getState()).kind;

// Run the actual startup module, and fire the event BEFORE any settings subscriber exists.
test("startup bootstrap captures early install events without fetch, auto-prompt or UI", async () => {
  const win = new FakeWindow();
  globalThis.window = win;
  const { installController: controller } = await import(`./bootstrap.js?test=early`);
  try {
    const event = installEvent();
    win.dispatchEvent(event);
    assert.equal(event.defaultPrevented, true);
    assert.equal(event.calls, 0);
    assert.equal(win.fetchCalls.length, 0);
    let state;
    const unsubscribe = controller.subscribe((next) => { state = next; });
    assert.equal(state.canPrompt, true);
    unsubscribe();
    await controller.check();
    assert.equal(kind(controller), "available");
    assert.equal(await controller.promptInstall(), false, "no implicit gesture");
    assert.equal(event.calls, 0);
    const pending = controller.promptInstall({ userGesture: true });
    assert.equal(event.calls, 1, "prompt invoked synchronously within gesture");
    assert.equal(kind(controller), "prompting");
    assert.equal(await pending, true);
    assert.equal(kind(controller), "accepted", "acceptance is NOT completed installation");
    assert.equal(await controller.promptInstall({ userGesture: true }), false);
    win.dispatchEvent(new Event("appinstalled"));
    assert.equal(kind(controller), "installed");
  } finally { controller.destroy(); delete globalThis.window; }
});

for (const [label, options, expected] of [
  ["dismissal", { outcome: "dismissed" }, "dismissed"],
  ["expired prompt", { failure: new Error("InvalidStateError") }, "expired"],
  ["unknown result", { outcome: "unknown" }, "expired"],
]) test(`${label} consumes the event; only a new browser event enables another attempt`, async () => {
  const win = new FakeWindow();
  const controller = createInstallController(win);
  await controller.check();
  const event = installEvent(options);
  win.dispatchEvent(event);
  assert.equal(await controller.promptInstall({ userGesture: true }), false);
  assert.equal(controller.getState().promptState, expected);
  assert.equal(kind(controller), "manual");
  assert.equal(await controller.promptInstall({ userGesture: true }), false);
  assert.equal(event.calls, 1);
  const next = installEvent();
  win.dispatchEvent(next);
  assert.equal(kind(controller), "available");
  await controller.promptInstall({ userGesture: true });
  assert.equal(next.calls, 1);
  controller.destroy();
});

test("late userChoice cannot overwrite appinstalled, standalone or a replacement event", async () => {
  for (const change of ["installed", "standalone", "new-event"]) {
    const win = new FakeWindow();
    const controller = createInstallController(win);
    await controller.check();
    let resolve;
    win.dispatchEvent(installEvent({ choice: new Promise((done) => { resolve = done; }) }));
    const pending = controller.promptInstall({ userGesture: true });
    if (change === "installed") win.dispatchEvent(new Event("appinstalled"));
    if (change === "standalone") {
      win.mode.matches = true;
      win.mode.dispatchEvent(new Event("change"));
    }
    if (change === "new-event") win.dispatchEvent(installEvent());
    resolve({ outcome: "dismissed" });
    await pending;
    assert.equal(kind(controller), { installed: "installed", standalone: "standalone", "new-event": "available" }[change]);
    controller.destroy();
  }
});

test("standalone/iOS manual/in-app/unsupported states never infer global installation", async () => {
  for (const [environment, expected] of [
    [{ standalone: true }, "standalone"],
    [{ ua: "iPhone Safari" }, "manual-ios"],
    [{ ua: "Macintosh Safari", touch: 5 }, "manual-ios"],
    [{ ua: "iPhone MicroMessenger" }, "embedded"],
    [{ ua: "Firefox" }, "manual"],
    [{ origin: "http://bear.example", secure: false }, "insecure"],
    [{ origin: "http://localhost", secure: true }, "manual"],
  ]) {
    const controller = createInstallController(new FakeWindow(environment));
    await controller.check();
    assert.equal(kind(controller), expected);
    assert.equal(controller.getState().installedEvent, false);
    assert.equal(await controller.promptInstall({ userGesture: true }), false);
    controller.destroy();
  }
  const win = new FakeWindow({ ua: "iPhone" });
  win.navigator.standalone = true;
  const controller = createInstallController(win);
  assert.equal(kind(controller), "standalone");
  controller.destroy();
});

test("insecure HTTP cannot prompt even with a synthetic available event", async () => {
  const win = new FakeWindow({ origin: "http://bear.example", secure: false });
  const controller = createInstallController(win);
  const event = installEvent();
  win.dispatchEvent(event);
  await controller.check();
  assert.equal(kind(controller), "insecure");
  assert.equal(await controller.promptInstall({ userGesture: true }), false);
  assert.equal(event.calls, 0);
  controller.destroy();
});

test("resource checks read real manifest/PNG assets with anonymous same-origin no-store requests", async () => {
  const win = new FakeWindow();
  await checkInstallResources(win);
  assert.deepEqual(win.fetchCalls.map((item) => new URL(item.url).pathname).sort(), ["/manifest.webmanifest", ...INSTALL_ICONS.map((item) => item.path)].sort());
  for (const { url, options } of win.fetchCalls) {
    assert.equal(new URL(url).origin, win.location.origin);
    assert.equal(new URL(url).search, "");
    assert.equal(options.credentials, "omit");
    assert.equal(options.cache, "no-store");
    assert.equal(options.redirect, "error");
    assert.equal(options.referrerPolicy, "no-referrer");
  }
});

for (const failure of ["404", "html", "network", "bad-json", "external-start", "session-start", "external-icon", "missing-icon", "bad-png", "truncated-png", "corrupt-png", "wrong-size", "redirect"]) {
  test(`resource failure (${failure}) does not advertise install availability; retry can recover`, async () => {
    const win = new FakeWindow();
    const realFetch = win.fetch;
    win.fetch = async (url, options) => {
      if (failure === "network") throw new Error("offline");
      if (failure === "404") return new Response("missing", { status: 404 });
      if (failure === "html") return new Response("<html>login</html>", { headers: { "content-type": "text/html" } });
      if (failure === "redirect") return { ok: true, redirected: true, headers: new Headers({ "content-type": "application/manifest+json" }) };
      if (url.endsWith("webmanifest")) {
        if (failure === "bad-json") return new Response("{", { headers: { "content-type": "application/json" } });
        const changed = structuredClone(manifest);
        if (failure === "external-start") changed.start_url = "https://other.example/";
        if (failure === "session-start") changed.start_url = "./?session=private";
        if (failure === "external-icon") changed.icons[0].src = "https://other.example/icon.png";
        if (failure === "missing-icon") changed.icons.pop();
        return new Response(JSON.stringify(changed), { headers: { "content-type": "application/manifest+json" } });
      }
      if (failure === "bad-png") return new Response("not png", { headers: { "content-type": "image/png" } });
      if (failure === "truncated-png" || failure === "corrupt-png") {
        const data = readFileSync(new URL("icons/openbear-192.png", publicRoot));
        if (failure === "corrupt-png") data[data.length - 20] ^= 255;
        return new Response(failure === "truncated-png" ? data.subarray(0, 33) : data, { headers: { "content-type": "image/png" } });
      }
      if (failure === "wrong-size") return realFetch(`${win.location.origin}/icons/apple-touch-icon.png`, options);
      return realFetch(url, options);
    };
    const controller = createInstallController(win);
    const event = installEvent();
    win.dispatchEvent(event);
    assert.equal(await controller.check(), false);
    assert.equal(kind(controller), "resource-error");
    assert.equal(await controller.promptInstall({ userGesture: true }), false);
    assert.equal(event.calls, 0);
    assert.ok(win.fetchCalls.every(({ url }) => new URL(url).origin === win.location.origin));
    win.fetch = realFetch;
    assert.equal(await controller.check(), true);
    assert.equal(kind(controller), "available");
    controller.destroy();
  });
}

test("resource checks deduplicate and time out without refreshing", async () => {
  const win = new FakeWindow();
  win.fetch = (_url, { signal }) => new Promise((_resolve, reject) => {
    signal.addEventListener("abort", () => reject(Object.assign(new Error("aborted"), { name: "AbortError" })));
  });
  const controller = createInstallController(win, { checkResources: (window) => checkInstallResources(window, { timeoutMs: 10 }) });
  const first = controller.check();
  assert.equal(controller.check(), first);
  assert.equal(await first, false);
  assert.equal(kind(controller), "resource-error");
  assert.match(controller.getState().resourceError, /超时/);
  controller.destroy();
});

test("HTTPS entry accepts only explicit safe HTTPS origins and discards contextual suffixes", () => {
  for (const [input, expected] of [
    ["https://bear.example/settings?secret=input#fragment", "https://bear.example"],
    ["  HTTPS://Bear.Example:8443/path?token=x#hash  ", "https://bear.example:8443"],
    ["https://[::1]:8443/path", "https://[::1]:8443"],
    ["https://bear.example:443/", "https://bear.example"],
  ]) assert.equal(httpsOrigin(input), expected);
  for (const input of ["", "bear.example", "/login", "//bear.example", "http://bear.example", "javascript:alert(1)", "data:text/html,x", "https://user:secret@bear.example", "https://@bear.example", "https://bear.example\\@evil.example", "https://bear.example\n.evil", "https:///", "https://bear.example:99999"])
    assert.throws(() => httpsOrigin(input), undefined, input);
});

test("real InstallAppView setup parses locally, requires separate navigation confirmation, supports cancel", async () => {
  const source = readFileSync(new URL("../views/InstallAppView.vue", import.meta.url), "utf8");
  const { descriptor } = parse(source);
  const script = compileScript(descriptor, { id: "pwa-install-test" });
  const code = script.content.replace(/^import .*;\n/gm, "").replace("export default", "return");
  const win = new FakeWindow({ origin: "http://bear.example", secure: false });
  const controller = createInstallController(win);
  const cleanup = [];
  // This setup test injects the presentation-only logo; its real render is covered by bearBranding.test.mjs.
  const component = new Function("computed", "onBeforeUnmount", "ref", "shallowRef", "installController", "httpsOrigin", "installPresentation", "BearLogo", code)(computed, (fn) => cleanup.push(fn), ref, shallowRef, controller, httpsOrigin, installPresentation, {});
  const view = component.setup({}, { expose() {} });
  await controller.check();
  const before = win.fetchCalls.length;
  view.httpsInput.value = "https://own.example:8443/private?secret=x#session";
  assert.equal(view.destination.value, "", "typing has no navigation effect");
  view.prepareAddress();
  assert.equal(view.destination.value, "https://own.example:8443");
  assert.equal(win.fetchCalls.length, before, "input is never fetched");
  view.cancelAddress();
  assert.equal(view.destination.value, "");
  assert.equal(view.httpsInput.value, "");
  view.httpsInput.value = "javascript:alert(1)";
  view.prepareAddress();
  assert.equal(view.destination.value, "");
  assert.ok(view.addressError.value);
  const template = compileTemplate({ source: descriptor.template.content, filename: "InstallAppView.vue", id: "pwa-install-test", compilerOptions: { bindingMetadata: script.bindings } });
  assert.deepEqual(template.errors, []);
  assert.match(template.code, /referrerpolicy: "no-referrer"/);
  assert.match(template.code, /rel: "noreferrer noopener"/);
  cleanup.forEach((fn) => fn());
  controller.destroy();
});

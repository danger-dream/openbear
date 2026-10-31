// The serving process writes a gateway instance's own prefix into index.html;
// an untouched marker (controller, standalone instance, dev server) is the root.
export function assetBase(browser = globalThis) {
  const value = browser.__OPENBEAR_ASSET_BASE__;
  return typeof value === "string" && /^\/_i\/[A-Za-z0-9_-]{1,128}\/$/.test(value) ? value : "/";
}

// Public build files referenced by fixed path, e.g. "assets/brand/x.png".
export function assetUrl(path, browser) {
  return assetBase(browser) + path;
}

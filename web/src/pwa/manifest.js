import { assetUrl } from "../assetBase.js";

export const MANIFEST_PATH = "/manifest.webmanifest";
export const DARK_MANIFEST_PATH = assetUrl("assets/pwa/manifest-dark.webmanifest");

export function themeManifestPath(theme) {
  return theme === "dark" ? DARK_MANIFEST_PATH : MANIFEST_PATH;
}

export function activeManifestPath(doc) {
  const href = doc?.querySelector?.('link[rel="manifest"]')?.getAttribute?.("href");
  return href === DARK_MANIFEST_PATH ? DARK_MANIFEST_PATH : MANIFEST_PATH;
}

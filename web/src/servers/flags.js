// Windows has no flag glyphs; the bundled font is only loaded when needed.
let flagsReady = false;
export function ensureFlagFont() {
  if (flagsReady) return;
  flagsReady = true;
  import('./flagFont.js').then(({ loadFlagFont }) => loadFlagFont(), () => {});
}
export function flag(code) {
  const iso = String(code || '').toUpperCase();
  return /^[A-Z]{2}$/.test(iso) ? String.fromCodePoint(...[...iso].map(c => 0x1F1E6 + c.charCodeAt(0) - 65)) : '';
}

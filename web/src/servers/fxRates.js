// Exchange rates for adding up bills in CNY, fetched by the browser from free
// public sources (both allow cross-origin reads) and kept for twelve hours.
const SOURCES = ['https://open.er-api.com/v6/latest/CNY', 'https://api.frankfurter.dev/v1/latest?base=CNY'];
const KEY = 'openbear.servers.fx';
const FRESH = 12 * 3600 * 1000;

// Both sources give units of each currency per CNY; the page wants CNY per unit.
export function parseRates(body) {
  if (!body || typeof body !== 'object' || (body.result && body.result !== 'success') || !body.rates || typeof body.rates !== 'object') return null;
  const rates = {};
  for (const [code, value] of Object.entries(body.rates)) {
    if (/^[A-Z]{3}$/.test(code) && typeof value === 'number' && value > 0) rates[code] = Number((1 / value).toPrecision(6));
  }
  if (!Object.keys(rates).length) return null;
  rates.CNY = 1;
  const stamp = body.time_last_update_unix;
  const date = typeof stamp === 'number' ? new Date(stamp * 1000).toISOString().slice(0, 10) : String(body.date || '').slice(0, 10);
  return {date, rates};
}

export function storedRates() {
  try {
    const value = JSON.parse(localStorage.getItem(KEY) || 'null');
    return value?.rates ? value : null;
  } catch { return null; }
}

let pending = null;
// The stored rates when fresh; otherwise fetched, falling back to the stored ones.
export function loadRates() {
  const stored = storedRates();
  if (stored && Date.now() - stored.fetchedAt < FRESH) return Promise.resolve(stored);
  pending ||= (async () => {
    for (const url of SOURCES) {
      try {
        const response = await fetch(url, {credentials: 'omit', signal: AbortSignal.timeout?.(15000)});
        const parsed = response.ok ? parseRates(await response.json()) : null;
        if (parsed) {
          const value = {...parsed, fetchedAt: Date.now()};
          try { localStorage.setItem(KEY, JSON.stringify(value)); } catch { /* storage may be blocked */ }
          return value;
        }
      } catch { /* try the next source */ }
    }
    return stored;
  })().finally(() => { pending = null; });
  return pending;
}

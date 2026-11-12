import { COUNTRIES, LIMIT, PLACES, SIZE } from '../worldMap.js';

// Where an address is, on the square of worldMap.js (Web Mercator, SIZE x SIZE): its pin at its coordinates
// (without them, at the country's label point), and the box of its country to show around it; for a country
// spread across the globe, the piece the pin is on (the United States without Alaska, France without Guiana).
// Wider than SPREAD (a sixth of the globe) a country is taken as spread out.
const SPREAD = 170;

export const project = (lon, lat) => {
  const l = Math.max(-LIMIT, Math.min(LIMIT, lat)) * Math.PI / 180;
  return [(lon + 180) / 360 * SIZE, (1 - Math.log(Math.tan(Math.PI / 4 + l / 2)) / Math.PI) / 2 * SIZE];
};
export const unproject = ([x, y]) => [(2 * Math.atan(Math.exp(Math.PI * (1 - 2 * y / SIZE))) - Math.PI / 2) * 180 / Math.PI, x / SIZE * 360 - 180];

export const pinOf = (lat, lon, country) => lat == null || lon == null ? PLACES[country] || null : project(lon, lat);

function boxes(path) {
  return path.split('M').filter(Boolean).map(part => {
    const n = part.match(/-?\d+(?:\.\d+)?/g).map(Number);
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (let i = 0; i + 1 < n.length; i += 2) {
      x0 = Math.min(x0, n[i]); x1 = Math.max(x1, n[i]); y0 = Math.min(y0, n[i + 1]); y1 = Math.max(y1, n[i + 1]);
    }
    return {x0, y0, x1, y1, area: (x1 - x0) * (y1 - y0)};
  });
}

// The country's box ({x0, y0, x1, y1}), or null for a country without an outline (Singapore, Hong Kong).
export function homeBox(country, pin) {
  const list = COUNTRIES[country] ? boxes(COUNTRIES[country]) : [];
  if (!list.length) return null;
  const whole = {x0: Math.min(...list.map(b => b.x0)), y0: Math.min(...list.map(b => b.y0)), x1: Math.max(...list.map(b => b.x1)), y1: Math.max(...list.map(b => b.y1))};
  if (whole.x1 - whole.x0 <= SPREAD) return whole;
  const p = pin;
  const inside = p && list.filter(b => p[0] >= b.x0 && p[0] <= b.x1 && p[1] >= b.y0 && p[1] <= b.y1).sort((a, b) => a.area - b.area)[0];
  const near = p && [...list].sort((a, b) => Math.hypot((a.x0 + a.x1) / 2 - p[0], (a.y0 + a.y1) / 2 - p[1]) - Math.hypot((b.x0 + b.x1) / 2 - p[0], (b.y0 + b.y1) / 2 - p[1]))[0];
  return inside || near || [...list].sort((a, b) => b.area - a.area)[0];
}

// The `display` and `visibility` rules of the app's own CSS, for happy-dom tests: happy-dom's
// getComputedStyle is far too slow for whole pages, and the tests only need to know which
// elements are hidden (display: none, at the current viewport) and which are block-level
// (for innerText). Media queries are evaluated with window.matchMedia at lookup time.
// testing/vueLoader.mjs feeds every CSS file and <style> block a component imports into addStyles().
import postcss from 'postcss';

const rules = [];       // {selector, specificity, order, media: [params], important, prop, value}
const byKey = new Map(); // rightmost class / id / tag / '*' -> rules
let order = 0;
const seen = new Set();

function specificity(selector) {
  const noStrings = selector.replace(/"[^"]*"|'[^']*'/g, '');
  const ids = (noStrings.match(/#[\w-]+/g) || []).length;
  const classes = (noStrings.match(/\.[\w-]+|\[[^\]]+\]|:(?!not|is|where|has)[\w-]+(\([^)]*\))?/g) || []).filter(x => !x.startsWith('::')).length;
  const tags = (noStrings.replace(/\[[^\]]*\]|\([^)]*\)/g, '').match(/(^|[\s>+~(])[a-zA-Z][\w-]*/g) || []).length;
  return ids * 10000 + classes * 100 + tags;
}
function keyOf(selector) {
  // The rightmost compound selector decides which elements a rule can match.
  const compound = selector.replace(/\([^)]*\)/g, '').trim().split(/\s*[\s>+~]\s*/).pop() || '';
  const id = compound.match(/#([\w-]+)/);
  if (id) return '#' + id[1];
  const cls = compound.match(/\.([\w-]+)/g);
  if (cls) return cls.at(-1);
  const tag = compound.match(/^([a-zA-Z][\w-]*)/);
  if (tag) return tag[1].toUpperCase();
  return '*';
}

export function addStyles(css, file = '') {
  if (file && seen.has(file)) return;
  if (file) seen.add(file);
  let root;
  try { root = postcss.parse(css); } catch { return; }
  root.walkDecls(/^(display|visibility)$/, decl => {
    const rule = decl.parent;
    if (rule?.type !== 'rule') return;
    const media = [];
    for (let p = rule.parent; p; p = p.parent) {
      if (p.type === 'atrule' && p.name === 'media') media.push(p.params);
      else if (p.type === 'atrule' && p.name !== 'layer') return; // @supports, @container…: skip
    }
    for (const raw of rule.selectors) {
      // Pseudo-elements and state pseudo-classes say nothing about the element at rest.
      if (/::|:(hover|focus|focus-visible|focus-within|active|visited|before|after|placeholder|-webkit-)/.test(raw)) continue;
      const selector = raw.replace(/:root\b/g, 'html');
      const entry = {selector, specificity: specificity(selector), order: order++, media, important: decl.important, prop: decl.prop, value: decl.value.trim()};
      rules.push(entry);
      const key = keyOf(selector);
      if (!byKey.has(key)) byKey.set(key, []);
      byKey.get(key).push(entry);
    }
  });
}

const mediaCache = new Map();
function mediaOk(list, window) {
  for (const params of list) {
    let hit = mediaCache.get(params);
    if (hit === undefined) { try { hit = window.matchMedia(params).matches; } catch { hit = false; } mediaCache.set(params, hit); }
    if (!hit) return false;
  }
  return true;
}
// matchMedia answers change with the viewport and the colour scheme.
export function viewportChanged() { mediaCache.clear(); }

// The cascaded value of display / visibility from the app's stylesheets, or '' if none applies.
export function cssValue(el, prop, window) {
  const keys = ['*', el.tagName];
  if (el.id) keys.push('#' + el.id);
  for (const c of el.classList || []) keys.push('.' + c);
  let best = null;
  for (const key of keys) {
    const list = byKey.get(key);
    if (!list) continue;
    for (const r of list) {
      if (r.prop !== prop) continue;
      if (best && (best.important && !r.important || (best.important === r.important && (best.specificity > r.specificity || (best.specificity === r.specificity && best.order > r.order))))) continue;
      if (r.media.length && !mediaOk(r.media, window)) continue;
      let ok = false;
      try { ok = el.matches(r.selector); } catch { ok = false; }
      if (ok) best = r;
    }
  }
  return best ? best.value : '';
}

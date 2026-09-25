import {readFileSync} from 'node:fs';
import postcss from 'postcss';

const palette = postcss.parse(readFileSync(new URL('../theme-tokens.css', import.meta.url), 'utf8'));
export function themeValues(dark = false) {
  const values = new Map();
  palette.walkRules(rule => {
    if (!rule.selectors.includes(':root') && !(dark && rule.selectors.includes('html.dark'))) return;
    rule.walkDecls(decl => values.set(decl.prop, decl.value));
  });
  return values;
}
export function resolveThemeValue(value, values) {
  for (let depth = 0; value.includes('var(') && depth < 12; depth++) {
    value = value.replace(/var\((--[\w-]+)\)/g, (_, key) => {
      if (!values.has(key)) throw new Error(`Missing theme variable: ${key}`);
      return values.get(key);
    });
  }
  if (value.includes('var(')) throw new Error(`Unresolved theme value: ${value}`);
  return value;
}
export function themeColor(value, values, backdrop = [255, 255, 255]) {
  value = resolveThemeValue(value, values);
  let channels;
  if (value.startsWith('#')) {
    const hex = value.slice(1);
    channels = (hex.length === 3 ? [...hex].map(c => c + c) : hex.match(/../g)).map(c => parseInt(c, 16));
  } else {
    if (!/^rgba?\(/.test(value)) throw new Error(`Not an RGB color: ${value}`);
    channels = value.match(/[\d.]+/g).map(Number);
  }
  const alpha = channels[3] ?? 1;
  return channels.slice(0, 3).map((c, i) => c * alpha + backdrop[i] * (1 - alpha));
}
export function contrast(first, second) {
  const luminance = rgb => {
    const c = rgb.map(v => v / 255).map(v => v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4);
    return c[0] * .2126 + c[1] * .7152 + c[2] * .0722;
  };
  const values = [luminance(first), luminance(second)].sort((a, b) => b - a);
  return (values[0] + .05) / (values[1] + .05);
}

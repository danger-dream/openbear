// CIDR intersections for permission summaries. BigInt keeps IPv6 host/subprefix
// boundaries exact; address families never overlap. This does not validate edits.
function address(text) {
  if (!text.includes(':')) {
    const p = text.split('.');
    if (p.length !== 4 || p.some(x => !/^(0|[1-9]\d{0,2})$/.test(x) || +x > 255)) return null;
    return {bits: 32, value: p.reduce((n, x) => n * 256n + BigInt(x), 0n)};
  }
  let ip = text.toLowerCase();
  if (ip.includes('.')) {
    const at = ip.lastIndexOf(':'), tail = address(ip.slice(at + 1));
    if (!tail) return null;
    ip = ip.slice(0, at + 1) + (tail.value >> 16n).toString(16) + ':' + (tail.value & 65535n).toString(16);
  }
  const halves = ip.split('::');
  if (halves.length > 2) return null;
  const left = halves[0] ? halves[0].split(':') : [], right = halves[1] ? halves[1].split(':') : [];
  const missing = 8 - left.length - right.length;
  if (halves.length === 1 ? missing !== 0 : missing < 1) return null;
  const parts = [...left, ...Array(missing).fill('0'), ...right];
  if (parts.some(x => !/^[\da-f]{1,4}$/.test(x))) return null;
  return {bits: 128, value: parts.reduce((n, x) => n * 65536n + BigInt(parseInt(x, 16)), 0n)};
}
export function network(text) {
  const parts = String(text || '').split('/');
  if (parts.length !== 2 || !/^(0|[1-9]\d{0,2})$/.test(parts[1])) return null;
  const a = address(parts[0]), prefix = +parts[1];
  if (!a || prefix > a.bits) return null;
  const size = 1n << BigInt(a.bits - prefix), base = a.value / size * size;
  return {family: a.bits, base, end: base + size, text};
}
const overlap = (a, b) => a.family === b.family && a.base < b.end && b.base < a.end;
const contains = (a, b) => a.family === b.family && a.base <= b.base && a.end >= b.end;
export function lanAccess(side, nets, dst = true) {
  if (side?.allSubnets) return {hit: true, part: []};
  const lans = nets.map(network).filter(Boolean);
  const targets = (side?.[dst ? 'subnets' : 'prefixes'] || []).map(network).filter(Boolean);
  const ranges = new Set();
  for (const n of targets) for (const lan of lans) if (overlap(n, lan)) ranges.add(contains(n, lan) ? lan.text : n.text);
  const full = lans.length > 0 && lans.every(l => targets.some(n => contains(n, l)));
  return {hit: ranges.size > 0, part: full ? [] : [...ranges]};
}

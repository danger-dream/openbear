// The lines page with the optimizer at work, for fakeMesh's init: five more relays and every pair of the seven linked,
// Berlin ⇄ LAX made automatically, Berlin ⇄ R1 set by hand to TCP and +100 ms, the measured lines, the automatic values,
// and autoLinks as the controller answers it (mesh_autolinks.view_of): the last change, a candidate watched, what links give.
import {member} from './meshBackend.mjs';

export const GAINS = [{src: 'home', dst: 'r2', before: 300, after: 200}, {src: 'home', dst: 'r3', before: 280, after: 230}];

export function linesAtWork(mesh) {
  const more = ['r1', 'r2', 'r3', 'r4', 'r5'];
  mesh.doc.members.push(...more.map((id, i) => member(id, `R${i + 1}`, 'relay', {address: `100.96.0.${20 + i}`, region: 'JP'})));
  const all = ['berlin', 'lax', ...more], pairs = [];
  for (let i = 0; i < all.length; i++) for (let j = i + 1; j < all.length; j++) pairs.push({a: all[i], b: all[j], transport: 'udp', cost: 1, dialer: 'a', auto: ['transport', 'dialer', 'bias']});
  Object.assign(pairs[0], {origin: 'auto', reason: '让 Home → R2 从 300ms 降到 200ms 等 2 对', since: Date.now() - 600000});
  Object.assign(pairs[1], {transport: 'tcp', bias: 100, auto: ['dialer']});
  mesh.doc.relayLinks = {mode: 'custom', pairs};
  const handle = mesh.handle;
  mesh.handle = async (...args) => {
    const answer = await handle(...args), body = answer[1];
    if (body?.autoLinks) {
      const links = mesh.doc.relayLinks.pairs;
      body.lines = [...(body.lines || []).filter(l => l.kind !== 'relay'), ...links.map((p, i) => ({id: `relay:${p.a}|${p.b}`, kind: 'relay', a: p.a, b: p.b, transport: p.transport, state: 'up', role: 'active', rttMs: 20 + i * 5, lossRate: 0}))];
      body.auto.pairs = Object.fromEntries(links.map(p => [`${p.a}|${p.b}`, {transport: {value: 'udp', reason: '两端都开着 UDP'}, dialer: {value: 'a', reason: ''}, bias: {value: 0, reason: ''}, rules: []}]));
      body.autoLinks = {holdRounds: 3, dropAfterS: 3600, idleSince: {}, benefits: {'berlin|lax': {kind: 'relay', count: 2, pairs: GAINS}},
        lastChange: {at: Date.now() - 300000, reason: '自动：新建 Berlin ↔ LAX', added: [{a: 'berlin', b: 'lax', kind: 'relay', transport: 'udp', reason: pairs[0].reason, count: 2, pairs: GAINS}],
          dropped: [{a: 'lax', b: 'r5', kind: 'relay', why: '连续无改善'}]},
        suggestions: [{key: 'home|r1', a: 'home', b: 'r1', kind: 'entry', transport: 'udp', reason: '', count: 1, pairs: [{src: 'home', dst: 'r1', before: 250, after: 150}], rounds: 2, ready: false}]};
    }
    return answer;
  };
}

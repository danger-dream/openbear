import { ElMessage } from 'element-plus';
import { customRelayLinks } from './meshGraph.js';

// Edits the pages share: which editor a link opens with, and taking one out of the draft.
const same = (p, a, b) => (p.a === a && p.b === b) || (p.a === b && p.b === a);

// The editor id of the relay link between a and b: a listed pair, else (every pair linked) the pair itself; '' without one.
export function relayEdgeId(work, a, b) {
  const i = (work?.relayLinks?.pairs || []).findIndex(p => same(p, a, b));
  if (i >= 0) return `pair:${i}`;
  return (work?.relayLinks?.mode || 'full') === 'full' ? `relay:${a}~${b}` : '';
}
export function directEdgeId(work, a, b) {
  const i = (work?.directLinks || []).findIndex(p => same(p, a, b));
  return i >= 0 ? `direct:${i}` : '';
}
export function entryEdgeId(work, member, relay) {
  const c = (work?.members || []).find(m => m.serverId === member)?.entry?.candidates?.find(x => x.relay === relay);
  return c ? `entry:${member}:${c.id}` : '';
}

// Takes the link out of the draft; the caller saves.
export function removeEdge(work, id) {
  const [kind, ...rest] = String(id).split(':'), key = rest.join(':');
  if (kind === 'entry') {
    const [memberId, candId] = key.split(':'), m = (work.members || []).find(x => x.serverId === memberId), i = m?.entry?.candidates?.findIndex(c => c.id === candId) ?? -1;
    if (i >= 0) m.entry.candidates.splice(i, 1);
  } else if (kind === 'relay') {
    customRelayLinks(work, key.split('~'));
    ElMessage.info('已改为逐对设置中转连接，并删掉了这一对');
  } else if (kind === 'pair') work.relayLinks.pairs.splice(Number(key), 1);
  else if (kind === 'direct') work.directLinks.splice(Number(key), 1);
}

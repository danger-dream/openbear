import { contextFetch } from '../../sessionBoundary.js';
import { loginUrlForLocation } from '../../loginRedirect.js';

// Behaves like the axios client in api.js: resolves with the JSON body and
// rejects with {response: {status, data}}, so safeError and errors[].path work.
async function call(method, path, body, timeout = 30000) {
  const controller = new AbortController(), timer = setTimeout(() => controller.abort(), timeout);
  try {
    const response = await contextFetch(`/api/servers/mesh${path}`, {method, signal: controller.signal,
      headers: body === undefined ? {} : {'content-type': 'application/json'}, body: body === undefined ? undefined : JSON.stringify(body)});
    let data = null;
    try { data = await response.json(); } catch { /* empty or non-JSON body */ }
    if (response.status === 401 && window.location.pathname !== '/login') window.location.replace(loginUrlForLocation(window.location));
    if (!response.ok || data?.ok === false) throw Object.assign(new Error(data?.error || `HTTP ${response.status}`), {response: {status: response.status, data: data || {}}});
    return data;
  } catch (error) {
    if (error.response || !controller.signal.aborted) throw error;
    throw Object.assign(new Error('timeout'), {code: 'ECONNABORTED'});
  } finally { clearTimeout(timer); }
}

export const MeshApi = {
  // fresh: the nodes are asked for their interfaces and routes first (the page coming on screen); polls go by what they reported last.
  state: (fresh = false) => call('GET', fresh ? '?fresh=1' : ''),
  saveDraft: draft => call('PUT', '/draft', draft),
  // Installs run while the request is open; progress also shows in state().nodes.
  addMembers: serverIds => call('POST', '/members', {serverIds}, 300000),
  removeMember: id => call('DELETE', `/members/${encodeURIComponent(id)}`),
  // Puts back a member's missing mesh service while the request is open, then rolls out its new keys in the background.
  reinstall: id => call('POST', `/members/${encodeURIComponent(id)}/reinstall`, undefined, 300000),
  apply: () => call('POST', '/apply', {}, 60000),
  discard: () => call('POST', '/discard', {}),
  // Without a revision, the previous one; with one, that kept revision, applied as a new one.
  rollback: (revision = null) => call('POST', '/rollback', revision == null ? {} : {revision}, 60000),
  // The kept revisions, newest first: {revisions: [{revision, at, reason, status, current, changes}]}.
  revisions: () => call('GET', '/revisions'),
  // A server about to join measures every relay: {results: [{relay, rttMs, loss, udp, tcp, tls}]}, each ok|timeout|refused|closed|punch|unknown.
  probe: serverId => call('POST', '/probe', {serverId}, 40000),
  member: id => call('GET', `/members/${encodeURIComponent(id)}`),
  // Where nodes sit on the board; not part of the draft. null removes a position.
  saveLayout: positions => call('PUT', '/layout', {positions}),
  trend: (id, range) => call('GET', `/members/${encodeURIComponent(id)}/trend?range=${encodeURIComponent(range)}`),
  // One line's long-term record (lines[].id): {summary, series: [{at, rttMs, lossRate, upRatio, downs}]}; null where there was no sample.
  lineTrend: (lineId, range) => call('GET', `/lines/${encodeURIComponent(lineId)}/trend?range=${encodeURIComponent(range)}`),
  // The current path from one node to another, from what the nodes last reported: {hops: [{node, via}], rttMs, stale}.
  path: (src, dst) => call('GET', `/path?src=${encodeURIComponent(src)}&dst=${encodeURIComponent(dst)}`),
  // The k best paths by the latest reports and the pair's own routes in the draft: {routes: [{nodes, segments, rttMs, own}], own: [{id, rank, on, nodes, segments, rttMs, place}]}.
  pathOptions: (src, dst, k = 10) => call('GET', `/path/options?src=${encodeURIComponent(src)}&dst=${encodeURIComponent(dst)}&k=${k}`),
  // A route laid out in the editor, link by link: reported, or measured now from one end: {segments: [{a, b, exists, rttMs, loss, how, why?}], rttMs}.
  measure: nodes => call('POST', '/path/measure', {nodes}, 40000),
  // Every route from one node: {routes: [{dst, hops, rttMs, stale}], subnets: [{subnet, gateway, hops, rttMs, stale}]}.
  routes: src => call('GET', `/routes?src=${encodeURIComponent(src)}`),
};

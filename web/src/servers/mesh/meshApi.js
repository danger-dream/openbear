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
  state: () => call('GET', ''),
  saveDraft: draft => call('PUT', '/draft', draft),
  // Installs run while the request is open; progress also shows in state().nodes.
  addMembers: serverIds => call('POST', '/members', {serverIds}, 300000),
  removeMember: id => call('DELETE', `/members/${encodeURIComponent(id)}`),
  // Puts back a member's missing mesh service while the request is open, then rolls out its new keys in the background.
  reinstall: id => call('POST', `/members/${encodeURIComponent(id)}/reinstall`, undefined, 300000),
  apply: () => call('POST', '/apply', {}, 60000),
  discard: () => call('POST', '/discard', {}),
  rollback: () => call('POST', '/rollback', {}, 60000),
  member: id => call('GET', `/members/${encodeURIComponent(id)}`),
  // Where nodes sit on the board; not part of the draft. null removes a position.
  saveLayout: positions => call('PUT', '/layout', {positions}),
  trend: (id, range) => call('GET', `/members/${encodeURIComponent(id)}/trend?range=${encodeURIComponent(range)}`),
  // One line's long-term record (lines[].id): {summary, series: [{at, rttMs, lossRate, upRatio, downs}]}; null where there was no sample.
  lineTrend: (lineId, range) => call('GET', `/lines/${encodeURIComponent(lineId)}/trend?range=${encodeURIComponent(range)}`),
  // The current path from one node to another, from what the nodes last reported: {hops: [{node, via}], rttMs, stale}.
  path: (src, dst) => call('GET', `/path?src=${encodeURIComponent(src)}&dst=${encodeURIComponent(dst)}`),
  // Every route from one node: {routes: [{dst, hops, rttMs, stale}], subnets: [{subnet, gateway, hops, rttMs, stale}]}.
  routes: src => call('GET', `/routes?src=${encodeURIComponent(src)}`),
};

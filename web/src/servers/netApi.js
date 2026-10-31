import { contextFetch } from '../sessionBoundary.js';
import { loginUrlForLocation } from '../loginRedirect.js';

// A server's network: the addresses on its interfaces, and ping / tcping / trace run from it.
// Behaves like the axios client in api.js: resolves with the JSON body and rejects with
// {response: {status, data}}, so safeError works on it.
async function call(method, path, body, {timeout = 30000, keepalive = false} = {}) {
  const controller = new AbortController(), timer = setTimeout(() => controller.abort(), timeout);
  try {
    const response = await contextFetch(`/api/servers${path}`, {method, signal: controller.signal, keepalive,
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
const at = id => `/${encodeURIComponent(id)}/net`;

export const NetApi = {
  // {interfaces: [{name, up, mac, kind, addrs: [{ip, prefixLen, family, scope, egress}]}], publicIPv4, publicIPv6, meshAddress}
  addresses: id => call('GET', `${at(id)}/addresses`),
  // {mode, target, port?, family, count?, intervalMs?, timeoutMs?} → {id}
  start: (id, body) => call('POST', `${at(id)}/probes`, body),
  status: (id, pid) => call('GET', `${at(id)}/probes/${encodeURIComponent(pid)}`),
  // keepalive lets a stop go out while the page is being closed.
  stop: (id, pid, keepalive = false) => call('DELETE', `${at(id)}/probes/${encodeURIComponent(pid)}`, undefined, {keepalive}),
};

import { contextFetch } from '../sessionBoundary.js';
import { loginUrlForLocation } from '../loginRedirect.js';

// The controller's 动态域名 endpoints (contract §4). Resolves with the JSON body; rejects with
// {response: {status, data: {error, message}}} like the axios client, so errorMessage works on it.
async function call(method, path, body, {timeout = 30000} = {}) {
  const controller = new AbortController(), timer = setTimeout(() => controller.abort(), timeout);
  try {
    const response = await contextFetch(`/api/ddns${path}`, {method, signal: controller.signal,
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
const id = value => encodeURIComponent(value);

export const DdnsApi = {
  // {accounts, records, providers, nodes, issues}
  list: () => call('GET', ''),
  testAccount: body => call('POST', '/accounts/test', body, {timeout: 60000}),
  addAccount: body => call('POST', '/accounts', body, {timeout: 60000}),
  updateAccount: (accountId, body) => call('PATCH', `/accounts/${id(accountId)}`, body, {timeout: 60000}),
  deleteAccount: (accountId, force = false) => call('DELETE', `/accounts/${id(accountId)}${force ? '?force=1' : ''}`),
  checkAccount: (accountId, settings) => call('POST', `/accounts/${id(accountId)}/check`, settings ? {settings} : {}, {timeout: 60000}),
  // Probes the node and reads the provider: a few seconds is normal.
  preview: body => call('POST', '/preview', body, {timeout: 60000}),
  addRecords: body => call('POST', '/records', body, {timeout: 60000}),
  updateRecord: (recordId, body) => call('PATCH', `/records/${id(recordId)}`, body),
  deleteRecord: (recordId, deleteRemote = false) => call('DELETE', `/records/${id(recordId)}${deleteRemote ? '?deleteRemote=1' : ''}`, undefined, {timeout: 60000}),
  // Returns once the sync finished, 20 seconds at most.
  syncRecord: recordId => call('POST', `/records/${id(recordId)}/sync`, {}, {timeout: 30000}),
  events: (recordId, limit = 50) => call('GET', `/records/${id(recordId)}/events?limit=${limit}`),
  diagnose: body => call('POST', '/diagnose', body, {timeout: 60000}),
};

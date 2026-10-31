// Test-only network for happy-dom component tests: the axios client in api.js, fetch and
// WebSocket all go to one handler instead of a server. Import after '../domTestEnv.mjs' and
// before anything imports api.js (axios.create copies the default adapter when it runs).
//
// handler(request) gets {url: URL, path, method, query, headers, body, raw} and returns
// {status = 200, json} or {status, headers, body} (body: string | Buffer | Uint8Array).
// Every request is recorded in network.calls as {path, method, body, query}, like the
// browser fixtures record theirs.
import axios from 'axios';
import {window} from '../domTestEnv.mjs';

// happy-dom's Blob / File / FormData have no toStringTag, so axios (which tells them apart by
// Object.prototype.toString, as in a browser) would send a File as JSON.
for (const name of ['Blob', 'File', 'FormData']) {
  const proto = window[name]?.prototype;
  if (proto && !Object.prototype.hasOwnProperty.call(proto, Symbol.toStringTag)) Object.defineProperty(proto, Symbol.toStringTag, {value: name, configurable: true});
}

const listeners = new Set();
export const network = {
  handler: null, external: null, calls: [], sockets: [], onSocket: null,
  reset() { this.handler = null; this.external = null; this.calls = []; this.sockets = []; this.onSocket = null; listeners.clear(); },
  // Like page.waitForResponse / waitForRequest: predicate gets {url(), status(), request()}.
  waitFor(kind, predicate, {timeout = 5000} = {}) {
    return new Promise((resolve, reject) => {
      const entry = {kind, predicate, resolve};
      listeners.add(entry);
      setTimeoutReal(() => { if (listeners.delete(entry)) reject(new Error(`waitFor${kind}: timed out`)); }, timeout);
    });
  },
};
const setTimeoutReal = globalThis.setTimeout;
function emit(kind, info) {
  for (const entry of [...listeners]) {
    let hit = false;
    try { hit = typeof entry.predicate === 'string' ? info.url().includes(entry.predicate) : entry.predicate instanceof RegExp ? entry.predicate.test(info.url()) : entry.predicate(info); } catch { hit = false; }
    if (hit && entry.kind === kind) { listeners.delete(entry); entry.resolve(info); }
  }
}

function bytesOf(data) {
  if (data == null) return null;
  if (typeof data === 'string') return Buffer.from(data);
  if (data instanceof Uint8Array) return Buffer.from(data);
  if (data instanceof ArrayBuffer) return Buffer.from(new Uint8Array(data));
  return null;
}
async function rawOf(data) {
  if (data == null) return null;
  if (typeof data?.arrayBuffer === 'function') return Buffer.from(await data.arrayBuffer());
  return bytesOf(data);
}

async function dispatch({href, method, headers, raw}) {
  const url = new URL(href, window.location.href);
  const contentType = headers['content-type'] || '';
  let body = null;
  if (raw && contentType.includes('json')) { try { body = JSON.parse(raw.toString()); } catch { body = null; } }
  const request = {url, path: url.pathname, method: method.toUpperCase(), query: Object.fromEntries(url.searchParams), headers, body, raw};
  const isApi = url.origin === window.location.origin && url.pathname.startsWith('/api');
  if (isApi) network.calls.push({path: request.path, method: request.method, body, query: request.query});
  const info = {url: () => url.href, method: () => request.method, postDataJSON: () => body, request: () => info};
  emit('request', info);
  const handler = isApi ? network.handler : network.external;
  let result = handler ? await handler(request) : null;
  if (!result) result = isApi ? {status: 404, json: {ok: false, error: 'not_found'}} : {status: 503, body: ''};
  const status = result.status ?? 200;
  const out = {status, headers: {...(result.json !== undefined ? {'content-type': 'application/json'} : {}), ...(result.headers || {})},
    body: result.json !== undefined ? JSON.stringify(result.json) : result.body ?? ''};
  emit('response', {...info, status: () => status, ok: () => status >= 200 && status < 300, request: () => info, json: async () => JSON.parse(out.body)});
  return out;
}

axios.defaults.adapter = async config => {
  const base = config.baseURL || '';
  let href = /^https?:/.test(config.url) ? config.url : base.replace(/\/$/, '') + (config.url.startsWith('/') ? config.url : '/' + config.url);
  const params = config.params ? Object.entries(config.params).filter(([, v]) => v !== undefined && v !== null) : [];
  if (params.length) href += (href.includes('?') ? '&' : '?') + new URLSearchParams(params.map(([k, v]) => [k, String(v)])).toString();
  const headers = Object.fromEntries(Object.entries(config.headers?.toJSON?.() || config.headers || {}).map(([k, v]) => [k.toLowerCase(), String(v)]));
  if (config.signal?.aborted) throw new axios.CanceledError(null, null, config);
  const raw = await rawOf(config.data);
  if (raw && config.onUploadProgress) config.onUploadProgress({loaded: raw.length, total: raw.length, progress: 1, bytes: raw.length, lengthComputable: true});
  if (raw && !headers['content-type'] && typeof config.data === 'string') headers['content-type'] = 'application/json';
  const res = await dispatch({href, method: config.method || 'get', headers, raw});
  if (config.signal?.aborted) throw new axios.CanceledError(null, null, config);
  let data = res.body;
  if (config.responseType === 'blob') data = new window.Blob([bytesOf(data)], {type: res.headers['content-type'] || ''});
  else if (config.responseType === 'arraybuffer') data = new Uint8Array(bytesOf(data)).buffer;
  else if (typeof data !== 'string') data = bytesOf(data).toString();
  const response = {data, status: res.status, statusText: String(res.status), headers: new axios.AxiosHeaders(res.headers), config, request: {}};
  const validate = config.validateStatus || (s => s >= 200 && s < 300);
  if (!validate(res.status)) {
    // axios parses error.response.data with transformResponse, as for a real response.
    throw new axios.AxiosError(`Request failed with status code ${res.status}`, res.status >= 500 ? 'ERR_BAD_RESPONSE' : 'ERR_BAD_REQUEST', config, {}, response);
  }
  return response;
};

globalThis.fetch = async (input, init = {}) => {
  const href = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
  const method = init.method || input?.method || 'GET';
  const headers = Object.fromEntries([...new Headers(init.headers || input?.headers || {})].map(([k, v]) => [k.toLowerCase(), v]));
  if (init.signal?.aborted) throw new DOMException('The operation was aborted.', 'AbortError');
  const raw = await rawOf(init.body ?? null);
  const res = await dispatch({href, method, headers, raw});
  if (init.signal?.aborted) throw new DOMException('The operation was aborted.', 'AbortError');
  const body = [204, 304].includes(res.status) ? null : bytesOf(res.body);
  return new Response(body, {status: res.status, headers: res.headers});
};
window.fetch = globalThis.fetch;

// A WebSocket the test drives: network.onSocket(socket) runs when a page opens one.
// socket.receive(data) delivers a message to the page; socket.sent holds what the page sent.
export class FakeWebSocket extends window.EventTarget {
  static CONNECTING = 0; static OPEN = 1; static CLOSING = 2; static CLOSED = 3;
  constructor(url) {
    super();
    this.url = new URL(url, window.location.href.replace(/^http/, 'ws')).href;
    this.readyState = 0; this.binaryType = 'blob'; this.sent = []; this.onopen = this.onmessage = this.onclose = this.onerror = null;
    network.sockets.push(this);
    queueMicrotask(() => {
      if (this.readyState !== 0) return;
      this.readyState = 1; this._fire('open', new window.Event('open'));
      network.onSocket?.(this);
    });
  }
  // happy-dom's dispatchEvent also calls the on<type> property, as a browser does.
  _fire(type, event) { this.dispatchEvent(event); }
  send(data) { this.sent.push(typeof data === 'string' ? data : Buffer.from(data instanceof ArrayBuffer ? new Uint8Array(data) : data)); this.onSend?.(this.sent.at(-1)); }
  receive(data) {
    if (this.readyState !== 1) return;
    const value = typeof data === 'string' ? data : this.binaryType === 'arraybuffer' ? new Uint8Array(Buffer.from(data)).buffer : new window.Blob([Buffer.from(data)]);
    this._fire('message', new window.MessageEvent('message', {data: value}));
  }
  close(code = 1000, reason = '') {
    if (this.readyState >= 2) return;
    this.readyState = 3; this._fire('close', Object.assign(new window.Event('close'), {code, reason, wasClean: true}));
  }
}
globalThis.WebSocket = window.WebSocket = FakeWebSocket;

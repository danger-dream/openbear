// Invoked by tests/test_web_uploads.py against its isolated aiohttp TestServer.
// Execute the production upload state machine over real HTTP, without a browser.
import assert from "node:assert/strict";
import {File} from "node:buffer";
import {uploadFilesViaHttp} from "../src/uploads.js";

let input = "";
for await (const chunk of process.stdin) input += chunk;
const {origin, cookie, conversationUuid} = JSON.parse(input);
const chunks = [];
async function request(method, path, body, options = {}) {
  const url = new URL('/api' + path, origin);
  for (const [key, value] of Object.entries(options.params || {})) url.searchParams.set(key, value);
  const binary = body instanceof Blob;
  const headers = {Cookie: `openbear_web_session=${cookie}`, ...(options.headers || {})};
  if (body !== undefined && !binary) headers['Content-Type'] = 'application/json';
  if (method === 'PUT') chunks.push({type: headers['Content-Type'], size: body.size});
  const response = await fetch(url, {method, headers, signal: options.signal,
    body: body === undefined ? undefined : binary ? body : JSON.stringify(body)});
  const data = await response.json();
  assert.equal(response.ok, true, JSON.stringify({status: response.status, data}));
  return {data};
}
const api = {
  post: (path, body, options) => request('POST', path, body, options),
  put: (path, body, options) => request('PUT', path, body, options),
  delete: (path, options) => request('DELETE', path, undefined, options),
};
const png = Buffer.from('89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000b49444154789c636000020000050001a5f645400000000049454e44ae426082', 'hex');
const files = [
  new File([new Uint8Array(70 * 1024 * 1024).fill(37)], 'node70.bin', {type: 'application/octet-stream'}),
  new File([png], 'node.png', {type: 'image/png'}),
  new File([], 'empty.txt', {type: 'text/plain'}),
];
let progress;
const refs = await uploadFilesViaHttp(api, conversationUuid, files, {onProgress: event => {progress = event;}});
const totalBytes = files.reduce((sum, file) => sum + file.size, 0);
assert.equal(refs.length, 3);
assert.equal(progress.loaded, totalBytes);
assert.equal(chunks.length, 141);
assert.ok(chunks.every(chunk => chunk.type === 'application/octet-stream' && chunk.size <= 512 * 1024));
assert.ok(refs.every(file => Object.keys(file).join() === 'uploadId'));
console.log(JSON.stringify({ok: true, bytes: totalBytes, chunks: chunks.length, refs}));

import test from "node:test";
import assert from "node:assert/strict";
import {uploadFilesViaHttp, UPLOAD_CHUNK_BYTES} from "./uploads.js";

function server({failChunk = false} = {}) {
  const calls = [];
  const uploads = new Map();
  const api = {
    async post(url, body, options) {
      calls.push({method: "POST", url, body, options});
      if (url.endsWith("/complete")) {
        const uploadId = url.split("/").at(-2);
        return {data: {ok: true, uploadId}};
      }
      const uploadId = `id-${uploads.size + 1}`;
      uploads.set(uploadId, {size: body.size, offset: 0});
      return {data: {ok: true, uploadId, offset: 0}};
    },
    async put(url, body, options) {
      calls.push({method: "PUT", url, body, options});
      if (failChunk) throw new Error("chunk_failed");
      const upload = uploads.get(url.split("/").at(-1));
      assert.equal(options.params.offset, upload.offset);
      assert.ok(body.size <= UPLOAD_CHUNK_BYTES);
      assert.equal(options.headers["Content-Type"], "application/octet-stream");
      assert.equal(options.timeout, 0);
      upload.offset += body.size;
      options.onUploadProgress({loaded: body.size});
      return {data: {ok: true, offset: upload.offset}};
    },
    async delete(url, options) {calls.push({method: "DELETE", url, options}); return {data: {ok: true}};},
  };
  return {api, calls};
}

test("files and images are sliced as binary Blobs, finalized before returning only upload IDs", async () => {
  const {api, calls} = server();
  const file = new File([new Uint8Array(UPLOAD_CHUNK_BYTES + 7)], "大附件.png", {type: "image/png"});
  const events = [];
  const controller = new AbortController();
  const refs = await uploadFilesViaHttp(api, "conv /1", [file], {signal: controller.signal, onProgress: event => events.push(event)});
  assert.deepEqual(refs, [{uploadId: "id-1"}]);
  assert.equal(calls[0].url, "/conversations/conv%20%2F1/uploads");
  assert.deepEqual(calls[0].body, {name: "大附件.png", type: "image/png", size: file.size});
  assert.deepEqual(calls.filter(c => c.method === "PUT").map(c => c.body.size), [UPLOAD_CHUNK_BYTES, 7]);
  assert.ok(calls.filter(c => c.method === "PUT").every(c => c.body instanceof Blob));
  assert.equal(calls.at(-1).options.timeout, 0);
  assert.equal(events.at(-1).loaded, file.size);
  assert.equal(events.at(-1).total, file.size);
  assert.ok(calls.every(c => c.options.signal === controller.signal));
  const count = calls.length;
  assert.deepEqual(await uploadFilesViaHttp(api, "conv /1", [file]), refs);
  assert.equal(calls.length, count, "lost-ACK/manual retry can reuse the completed upload");
  await uploadFilesViaHttp(api, "other-conversation", [file]);
  assert.ok(calls.length > count, "references must not cross conversations");
});

test("multi-GB sizes have no cap or 32-bit truncation and never require a whole-file read", async () => {
  const {api, calls} = server();
  const size = 5 * 1024 ** 3 + 7;
  const file = {name: "large.zip", type: "application/zip", size, slice(start, end) { return {size: end - start}; }};
  assert.deepEqual(await uploadFilesViaHttp(api, "large", [file]), [{uploadId: "id-1"}]);
  const chunks = calls.filter(c => c.method === "PUT");
  assert.equal(chunks.length, Math.ceil(size / UPLOAD_CHUNK_BYTES));
  assert.equal(chunks.reduce((sum, c) => sum + c.body.size, 0), size);
  assert.equal(chunks.at(-1).body.size, 7);
});

test("empty files finalize without a spurious chunk", async () => {
  const {api, calls} = server();
  const result = await uploadFilesViaHttp(api, "empty", [new File([], "empty.txt")]);
  assert.deepEqual(result, [{uploadId: "id-1"}]);
  assert.deepEqual(calls.map(c => c.method), ["POST", "POST"]);
});

test("chunk failures clean up the incomplete upload and never finalize or retry content", async () => {
  const {api, calls} = server({failChunk: true});
  await assert.rejects(uploadFilesViaHttp(api, "fail", [new File(["data"], "file.txt")]), /chunk_failed/);
  assert.deepEqual(calls.map(c => c.method), ["POST", "PUT", "DELETE"]);
});

test("abort between chunks discards only pending content and sends no later chunk", async () => {
  const {api, calls} = server();
  const controller = new AbortController();
  const put = api.put;
  api.put = async (...args) => { const result = await put(...args); controller.abort(); return result; };
  await assert.rejects(uploadFilesViaHttp(api, "cancel", [new File([new Uint8Array(UPLOAD_CHUNK_BYTES + 1)], "file.bin")], {signal: controller.signal}), {name: "AbortError"});
  assert.deepEqual(calls.map(c => c.method), ["POST", "PUT", "DELETE"]);
  assert.equal(calls.at(-1).options.signal, undefined, "cleanup must not inherit the canceled signal");
});

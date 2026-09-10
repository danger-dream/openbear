// Keep requests below common reverse-proxy body limits. This is a transport
// chunk size, not a file-size cap; even multi-GB files are never read as a whole.
export const UPLOAD_CHUNK_BYTES = 512 * 1024;
const completedUploads = new WeakMap();

export async function uploadFilesViaHttp(api, conversationUuid, files = [], {signal, onProgress} = {}) {
  const base = `/conversations/${encodeURIComponent(conversationUuid)}/uploads`;
  const rows = [];
  const list = Array.from(files || []);
  const total = list.reduce((sum, file) => sum + file.size, 0);
  let loaded = 0;
  for (const [index, file] of list.entries()) {
    signal?.throwIfAborted();
    const progress = (bytes) => onProgress?.({loaded: loaded + bytes, total, fileIndex: index, fileCount: list.length});
    const cached = completedUploads.get(file)?.get(conversationUuid);
    if (cached) {
      rows.push({uploadId: cached});
      progress(file.size);
      loaded += file.size;
      continue;
    }
    let uploadId = "";
    try {
      const created = await api.post(base, {name: file.name || "upload.bin", type: file.type || "application/octet-stream", size: file.size}, {signal});
      uploadId = created.data?.uploadId || "";
      if (!uploadId) throw new Error("attachment_upload_invalid_response");
      const url = `${base}/${encodeURIComponent(uploadId)}`;
      for (let offset = 0; offset < file.size;) {
        signal?.throwIfAborted();
        const end = Math.min(file.size, offset + UPLOAD_CHUNK_BYTES);
        const result = await api.put(url, file.slice(offset, end), {
          params: {offset}, signal, timeout: 0,
          headers: {"Content-Type": "application/octet-stream"},
          onUploadProgress: (event) => progress(offset + Math.min(Number(event.loaded) || 0, end - offset)),
        });
        if (result.data?.offset !== end) throw new Error("attachment_upload_offset_mismatch");
        offset = end;
        progress(offset);
      }
      signal?.throwIfAborted();
      // Finalizing may hash/copy a large file. Neither this nor the upload shares
      // the short WS acknowledgement/ordinary JSON request timeout.
      const completed = await api.post(`${url}/complete`, {}, {signal, timeout: 0});
      if (completed.data?.uploadId !== uploadId) throw new Error("attachment_upload_invalid_response");
      let cache = completedUploads.get(file);
      if (!cache) completedUploads.set(file, cache = new Map());
      cache.set(conversationUuid, uploadId);
      rows.push({uploadId});
      progress(file.size);
      loaded += file.size;
    } catch (error) {
      // Never retry a chunk/message after an uncertain response. Only unfinished
      // uploads are discardable; the server protects finalized/referenced files.
      if (uploadId) {
        try { await api.delete(`${base}/${encodeURIComponent(uploadId)}`, {timeout: 10000}); } catch { /* best effort */ }
      }
      throw error;
    }
  }
  signal?.throwIfAborted();
  return rows;
}

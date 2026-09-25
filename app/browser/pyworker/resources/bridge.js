// OpenBear adapter. Executed ONLY inside a Chromium isolated world, after creation of h.
// The licensed injected.js algorithms are kept unmodified.
h.__renderSnapshot = data => renderAriaSnapshotAsYaml(data);
h.__query = selector => h.querySelector(h.parseSelector(selector), h.document, true);
h.__point = node => {
  const element = h.retarget(node, 'button-link');
  if (!element || !element.isConnected) return 'error:notconnected';
  const rects = [...element.getClientRects()];
  const width = h.window.innerWidth, height = h.window.innerHeight;
  for (const rect of rects) {
    const left = Math.max(0, rect.left), right = Math.min(width, rect.right);
    const top = Math.max(0, rect.top), bottom = Math.min(height, rect.bottom);
    if (right - left > 1 && bottom - top > 1)
      return {x:(left+right)/2, y:(top+bottom)/2};
  }
  return 'error:notvisible';
};
h.__fileTransfers = new Map();
h.__startFiles = id => h.__fileTransfers.set(id, []);
h.__startFile = (id, meta) => h.__fileTransfers.get(id).push({meta, chunks:[]});
h.__fileChunk = (id, chunk) => {
  const files = h.__fileTransfers.get(id);
  files[files.length-1].chunks.push(Uint8Array.from(atob(chunk), c => c.charCodeAt(0)));
};
h.__finishFiles = (node, args) => {
  const files = h.__fileTransfers.get(args.id);
  if (!files) throw new Error('file_transfer_expired');
  const dt = new DataTransfer();
  for (const file of files)
    dt.items.add(new File(file.chunks, file.meta.name, {type:file.meta.mimeType, lastModified:file.meta.lastModified}));
  if (args.drop) {
    for (const [key,value] of Object.entries(args.data || {})) dt.setData(key,value);
    for (const type of ['dragenter','dragover','drop'])
      node.dispatchEvent(new DragEvent(type,{bubbles:true,cancelable:true,dataTransfer:dt}));
  } else {
    const input = h.retarget(node, 'follow-label');
    if (!input || input.tagName !== 'INPUT' || input.type !== 'file') throw new Error('not_file_input');
    if (input.webkitdirectory) throw new Error('directory_upload_not_supported');
    if (!input.multiple && files.length > 1) throw new Error('file_input_not_multiple');
    input.files = dt.files;
    input.dispatchEvent(new Event('input',{bubbles:true,composed:true}));
    input.dispatchEvent(new Event('change',{bubbles:true}));
  }
  h.__fileTransfers.delete(args.id);
};

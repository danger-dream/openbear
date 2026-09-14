// Retain frames crossing an HTTP snapshot boundary. A missing base stops replay
// without acknowledging that frame; overflow requires a newer snapshot rather
// than silently dropping events or growing memory without a bound.
export function createOperationFrameBuffer({limit = 4096} = {}) {
  const frames = new Map();
  let blocked = false, requiredBaseline = 0;
  function requireSnapshotThrough(seq = 0) {
    requiredBaseline = Math.max(requiredBaseline, Number(seq) || 0);
    blocked = true;
  }
  function add(frame) {
    const seq = Number(frame?.frameSeq) || 0;
    if (!seq) return;
    frames.set(seq, frame);
    if (frames.size > limit) {
      requireSnapshotThrough(Math.max(...frames.keys()));
      frames.clear();
    }
  }
  function block(frame) { add(frame); blocked = true; }
  function replayAfter(cursor, apply) {
    const baseline = Number(cursor) || 0;
    for (const seq of frames.keys()) if (seq <= baseline) frames.delete(seq);
    if (requiredBaseline > baseline) { blocked = true; return false; }
    requiredBaseline = 0;
    blocked = false;
    for (const [seq, frame] of [...frames].sort((a, b) => a[0] - b[0])) {
      const result = apply(frame);
      if (result?.needsResync) { blocked = true; return false; }
      frames.delete(seq);
    }
    return true;
  }
  function reset() { frames.clear(); blocked = false; requiredBaseline = 0; }
  return {add, block, requireSnapshotThrough, replayAfter, reset,
    get blocked() { return blocked; }, get size() { return frames.size; }};
}

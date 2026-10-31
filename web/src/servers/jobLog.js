// The job log panel holds only the newest output: a job may print far more than a page should keep.
export const LOG_LIMIT = 1024 * 1024;
// Never ask for more sooner than this, whatever the node says is still to come.
export const FOLLOW_GAP = 500;

// log ({data, start: byte offset of data, ...}) cut to its last `limit` characters, from a line start when one is near.
export function keepRecent(log, limit = LOG_LIMIT) {
  if (log.data.length <= limit) return log;
  let cut = log.data.length - limit;
  const line = log.data.indexOf('\n', cut);
  if (line >= 0 && line - cut < 4096 && line + 1 < log.data.length) cut = line + 1;
  return {...log, data: log.data.slice(cut), start: log.start + new TextEncoder().encode(log.data.slice(0, cut)).length, trimmed: true};
}

// How long to wait before reading on: soon while the node still has output past what was read, else the usual poll.
// A reply that does not move forward counts as nothing more to read.
export function followDelay(part, previousNext) {
  const behind = part.next < part.size && (previousNext == null || part.next > previousNext);
  return behind ? FOLLOW_GAP : 2000;
}

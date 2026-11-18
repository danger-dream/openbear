// The job log panel holds only the newest output: a job may print far more than a page should keep.
export const LOG_LIMIT = 1024 * 1024;
// Never ask for more sooner than this, whatever the node says is still to come.
export const FOLLOW_GAP = 500;

// Retry transient transport/node errors at most five times (1, 2, 4, 8, 16s).
// An explicit retry starts a new budget but never resets the byte cursor.
export function retryDelay(error, failures) {
  const status = error?.response?.status ?? error?.status;
  const code = error?.response?.data?.error ?? error?.error ?? error?.message;
  if (failures > 5 || ['job_not_found', 'job_unreadable', 'forbidden', 'unauthorized', 'invalid_offset', 'node_update_required'].includes(code)) return null;
  const transient = ['node_offline', 'node_connection_lost', 'node_busy', 'rpc_timeout'].includes(code);
  if (!transient && status >= 400 && status < 500 && status !== 408 && status !== 429) return null;
  return Math.min(16000, 1000 * 2 ** Math.max(0, failures - 1));
}

export function canStopJob(job) {
  // N-1 nodes do not report canStop: retain running-job controls, never infer an orphan.
  return job?.canStop === true || (job?.state === 'running' && job?.canStop == null);
}

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

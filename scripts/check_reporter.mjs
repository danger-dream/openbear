// node --test reporter for scripts/check.py: one JSON line per finished test.
export default async function* checkReporter(source) {
  for await (const event of source) {
    if (event.type !== 'test:pass' && event.type !== 'test:fail') continue;
    const {name, nesting, file, details} = event.data;
    const error = details?.error;
    // A parent fails with its subtests; those are reported themselves.
    if (error?.failureType === 'subtestsFailed') continue;
    const cause = error?.cause ?? error;
    const frame = String(cause?.stack ?? '').split('\n').find(line => /\.test\.[cm]?js:\d+/.test(line));
    yield JSON.stringify({
      ok: event.type === 'test:pass', name, nesting, file, ms: details?.duration_ms,
      error: error ? String(cause?.message ?? cause).slice(0, 4000) : undefined,
      at: frame ? frame.trim().replace(/^at\s+/, '') : undefined,
    }) + '\n';
  }
}

// One transport read per conversation visit; mutation refreshes queue at most
// one newer read. Callers still own their response/scroll generation guards.
export function createConversationStateRequests(fetchState) {
  let scope = null;
  const cancelled = () => Object.assign(new Error('conversation_request_cancelled'), {name: 'AbortError'});
  function entry() {
    let resolve, reject;
    const promise = new Promise((a, b) => { resolve = a; reject = b; });
    return {promise, resolve, reject, controller: new AbortController()};
  }
  function start(owner, item) {
    owner.active = item;
    let result;
    try { result = fetchState(owner.uuid, item.controller.signal); }
    catch (error) { result = Promise.reject(error); }
    Promise.resolve(result).then(item.resolve, item.reject).finally(() => {
      if (scope !== owner || owner.active !== item) return;
      owner.active = null;
      const queued = owner.queued;
      owner.queued = null;
      if (queued) start(owner, queued);
    });
    return item.promise;
  }
  function invalidate() {
    const old = scope;
    scope = null;
    for (const item of [old?.active, old?.queued]) {
      if (!item) continue;
      item.controller.abort();
      item.reject(cancelled());
    }
  }
  function request(uuid, {fresh = false} = {}) {
    if (scope?.uuid !== uuid) { invalidate(); scope = {uuid, active: null, queued: null}; }
    if (!scope.active) return start(scope, entry());
    if (fresh) scope.queued ||= entry();
    return (scope.queued || scope.active).promise;
  }
  return {request, invalidate, get pending() { return Boolean(scope?.active || scope?.queued); }};
}

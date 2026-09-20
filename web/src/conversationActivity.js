// One completion version is acknowledged at a time; read receipts never cover future results.
export function withActivityReadVersion(item, readVersion = 0) {
  const version = Number(item?.activityVersion || 0);
  const read = Math.max(Number(item?.activityReadVersion || 0), Number(readVersion || 0));
  return {...item, activityReadVersion: read, activityUnread: version > read};
}

export function applyActivityReadVersions(status = {}, versions = new Map()) {
  const adjust = item => withActivityReadVersion(item, versions.get(item.conversationUuid));
  return {...status,
    items: (status.items || []).map(adjust),
    ...(Array.isArray(status.activityItems) ? {
      activityItems: status.activityItems.map(adjust).filter(item => item.running || item.activityUnread),
    } : {}),
    ...(Array.isArray(status.recentItems) ? {
      recentItems: status.recentItems.map(adjust),
    } : {}),
  };
}

export function activityState(item = {}) {
  if (item.running) return item.activityState === "waiting" ? "waiting" : "running";
  if (item.readWhileSelected) return "read";
  return item.activityState || item.activityResult?.status || "completed";
}
export function activityInteractionTarget(item) {
  if (activityState(item) !== "waiting" || !item?.conversationUuid) return null;
  const pending = item.activityPending?.find(request => request?.interactionId);
  return pending ? {conversationUuid: item.conversationUuid, interactionId: pending.interactionId} : null;
}
export function activityLabel(item) {
  if (activityState(item) === "waiting" && item?.activityPending?.length) {
    const label = ({confirm: "待确认", select: "待选择", prompt: "待填写", questionnaire: "待填写问卷"})[item.activityPending[0].action] || "待处理";
    return item.activityPending.length > 1 ? `${label} · ${item.activityPending.length}项` : label;
  }
  return ({waiting: "待处理", running: "运行中", completed: "已完成", failed: "失败", error: "失败",
    cancelled: "已取消", interrupted: "已中断", partial: "部分完成", read: "已读"})[activityState(item)] || "已完成";
}
export function activityGroup(item) {
  const state = activityState(item);
  if (["waiting", "failed", "error", "partial", "interrupted"].includes(state)) return "attention";
  return item.running ? "running" : "unread";
}
export function groupActivityItems(items = []) {
  const unique = new Map(items.filter(item => item?.conversationUuid && !item.archived).map(item => [item.conversationUuid, item]));
  const sorted = [...unique.values()].sort((a, b) => Number(activityState(b) === "waiting") - Number(activityState(a) === "waiting")
    || Number(b.activityAtMs || 0) - Number(a.activityAtMs || 0) || a.conversationUuid.localeCompare(b.conversationUuid));
  return [{key: "attention", label: "需要处理"}, {key: "running", label: "运行中"}, {key: "unread", label: "完成待查看"}]
    .map(group => ({...group, items: sorted.filter(item => activityGroup(item) === group.key)})).filter(group => group.items.length);
}
export function activityReadRequests(items = []) {
  return items.filter(item => item.activityUnread && Number(item.activityVersion) > 0)
    .map(item => ({conversationUuid: item.conversationUuid, version: Number(item.activityVersion)}));
}

export function eligibleActivityRead(snapshot = {}) {
  const {item, conversationUuid, loadedConversationUuid, visible, focused, atLatest, ready, running, operation} = snapshot;
  if (!item?.activityUnread || item.running || running || !ready || !visible || !focused || !atLatest) return null;
  if (!conversationUuid || conversationUuid !== loadedConversationUuid || item.conversationUuid !== conversationUuid) return null;
  const result = item.activityResult || {};
  // The global tree can arrive before this tab's timeline. Do not acknowledge
  // it until the exact terminal operation revision is loaded and rendered here.
  if (!result.opId || operation?.opId !== result.opId || Number(operation.revision || 0) < Number(result.opRevision || 1)) return null;
  return {conversationUuid, version: Number(item.activityVersion)};
}

export function createActivityReadTracker({snapshot, send, accepted, setTimer = setTimeout, clearTimer = clearTimeout, dwellMs = 450}) {
  let timer = null, stopped = false, inFlight = false;
  const acknowledged = new Map();
  function candidate() {
    const value = eligibleActivityRead(snapshot());
    return value && value.version > (acknowledged.get(value.conversationUuid) || 0) ? value : null;
  }
  function schedule(delay = dwellMs) {
    clearTimer(timer); timer = null;
    if (stopped || inFlight) return;
    const pending = candidate();
    if (!pending) return;
    timer = setTimer(async () => {
      timer = null;
      const current = candidate();
      if (stopped || !current || current.conversationUuid !== pending.conversationUuid || current.version !== pending.version) return;
      inFlight = true;
      let failed = false;
      try {
        const receipt = await send([current]);
        acknowledged.set(current.conversationUuid, current.version);
        accepted(receipt);
      } catch { failed = true; /* Keep unread on failure; retry only while still viewing. */ }
      finally { inFlight = false; if (!stopped) schedule(failed ? 5000 : dwellMs); }
    }, delay);
  }
  return {schedule: () => schedule(), dispose() { stopped = true; clearTimer(timer); timer = null; }};
}

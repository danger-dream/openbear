// Retry wait completion is not proof that the next model request succeeded.
export function retryWaitLabel(retry = {}) {
  const seconds = Math.max(0, Math.ceil(Number(retry.waitMs || 0) / 1000));
  if (!seconds || !Number.isFinite(seconds)) return "";
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return `等待 ${minutes ? `${minutes} 分钟${remainder ? ` ${remainder} 秒` : ""}` : `${seconds} 秒`}`;
}

export function retryStatusView(retry = {}) {
  if (retry.active) return {label: "等待重试", tone: "waiting"};
  const status = String(retry.status || "");
  if (["completed", "complete"].includes(status)) return {label: "重试成功", tone: "success"};
  if (["cancelled", "canceled", "stopped"].includes(status)) return {label: "已取消", tone: "cancelled"};
  if (status === "failed") return {label: "重试失败", tone: "failed"};
  return {label: status && status !== "resumed" ? status : "已发起重试", tone: "neutral"};
}

export function formatArgumentBytes(value) {
  const bytes = Math.max(0, Number(value) || 0);
  if (bytes < 1000) return `${Math.round(bytes)} B`;
  if (bytes < 1000000) return `${(bytes / 1000).toFixed(2)} kB`;
  return `${(bytes / 1000000).toFixed(2)} MB`;
}

export function modelOutputView(progress = {}, nowMs = Date.now()) {
  const names = Array.isArray(progress.toolNames) ? progress.toolNames.slice(0, 3) : [];
  const ready = progress.phase === "ready";
  const updatedAt = Number(progress.updatedAtMs) || nowMs;
  const elapsedMs = Math.max(0, Number(progress.elapsedMs) || 0) + Math.max(0, nowMs - updatedAt);
  const seconds = Math.floor(elapsedMs / 1000);
  const elapsed = seconds >= 60 ? `${Math.floor(seconds / 60)}分${String(seconds % 60).padStart(2, '0')}秒` : `${seconds}秒`;
  const idleSeconds = Math.max(0, Math.floor((nowMs - updatedAt) / 1000));
  let label = "正在生成工具参数";
  if (ready) label = "参数已接收，等待模型结束";
  else if (names.length === 1 && names[0] === "Write") label = "正在生成文件内容";
  else if (names.length && names.every(name => ["Edit", "EditBatch"].includes(name))) label = "正在生成修改内容";
  return {
    label,
    tools: names.join('、'),
    bytes: formatArgumentBytes(progress.receivedBytes),
    elapsed,
    idle: idleSeconds >= 15 ? `${idleSeconds}秒未收到新参数` : "",
    ready,
    quiet: ready || idleSeconds >= 15,
    hint: ready ? "工具尚未执行，正在等待模型响应结束。" : "模型正在生成工具参数；收齐并确认后才会执行，不代表已经写入文件。",
  };
}

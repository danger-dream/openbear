const formatter = new Intl.DateTimeFormat("zh-CN", {
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

export function formatAssetTime(value, { unknown = "历史数据，时间不可追溯" } = {}) {
  const seconds = Number(value || 0);
  if (!Number.isFinite(seconds) || seconds <= 0) return unknown;
  return formatter.format(new Date(seconds * 1000)).replaceAll("/", "-");
}

export function assetTimeLine(item) {
  const created = Number(item?.createdAt ?? item?.created_at ?? 0);
  const updated = Number(item?.updatedAt ?? item?.updated_at ?? 0);
  return `创建 ${formatAssetTime(created)} · 修改 ${formatAssetTime(updated)}`;
}

// The server preview is authoritative. Older servers only expose a catalog.
export function taskMemoryInjectionPreview(data = {}) {
  const nonnegative = (value, fallback = 0) => {
    const number = Number(value);
    return Number.isFinite(number) ? Math.max(0, Math.trunc(number)) : fallback;
  };
  return {
    text: String(data?.runtimeSnapshot ?? data?.catalogXml ?? ""),
    tokens: nonnegative(data?.estimatedRuntimeTokens),
    maxTokens: nonnegative(data?.maxRuntimeTokens, 1500) || 1500,
    shortBodyMaxChars: nonnegative(data?.shortBodyMaxChars),
    includedCount: data?.includedCount == null ? null : nonnegative(data.includedCount),
    omittedCount: nonnegative(data?.omittedCount),
  };
}

export function taskMemoryInjectionUsage(item, shortBodyMaxChars = 0) {
  if (!item?.autoReinjectCatalog) return "名称与正文均按需读取";
  if (!(shortBodyMaxChars > 0)) return "自动提供名称与说明，正文按需读取";
  if (typeof item?.body !== "string") return "短正文自动提供，长资料按需读取；以模型可见内容为准";
  const body = item.body;
  if (!body) return "自动提供名称与说明";
  // Match Python's Unicode-character limit, not UTF-16 code units.
  if (Array.from(body).length <= shortBodyMaxChars) return "短正文自动提供；是否纳入以模型可见内容为准";
  return "正文较长，自动提供名称与说明，正文按需读取";
}

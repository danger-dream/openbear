export function buildCompressionOrderItems(models = [], candidates = []) {
  const details = new Map(
    (Array.isArray(candidates) ? candidates : [])
      .filter((item) => item && typeof item === "object" && item.fullname)
      .map((item) => [String(item.fullname), item]),
  );
  const seen = new Set();
  return (Array.isArray(models) ? models : []).flatMap((raw) => {
    const fullname = String(raw || "").trim();
    if (!fullname || seen.has(fullname)) return [];
    seen.add(fullname);
    const detail = details.get(fullname) || {};
    const slash = fullname.indexOf("/");
    const provider = String(detail.provider || (slash >= 0 ? fullname.slice(0, slash) : ""));
    const id = String(detail.id || (slash >= 0 ? fullname.slice(slash + 1) : fullname));
    return [{
      fullname,
      provider,
      id,
      name: String(detail.name || id),
    }];
  });
}

export function compressionOrderFullnames(items = []) {
  return (Array.isArray(items) ? items : [])
    .map((item) => String(item?.fullname || "").trim())
    .filter(Boolean);
}

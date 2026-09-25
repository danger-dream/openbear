// Presentation units only: configuration and settings API retain storage values.
export function settingRangeLabel(spec) {
  if (!(spec.displayScale > 1)) return `${spec.min ?? "—"} ～ ${spec.max ?? "—"}`;
  const label = value => {
    if (value == null) return "—";
    if (spec.unit === "MB" && value < spec.displayScale) return `${value / 1024} KB`;
    return `${settingDisplayValue(spec, value)} ${spec.unit}`;
  };
  return `${label(spec.min)} ～ ${label(spec.max)}`;
}
export function settingDisplayValue(spec, value) {
  if (value === null || value === undefined || value === "") return value;
  const scale = spec?.displayScale || 1;
  return scale === 1 ? value : Number(value) / scale;
}

export function settingStorageValue(spec, value) {
  const scale = spec?.displayScale || 1;
  if (scale === 1) return value;
  const number = typeof value === "string" && !value.trim() ? NaN : Number(value);
  if (!Number.isFinite(number)) throw new Error(`请输入数字（${spec.unit}）`);
  const min = spec.min == null ? null : spec.min / scale;
  const max = spec.max == null ? null : spec.max / scale;
  if (min !== null && number < min) throw new Error(`不能小于 ${min} ${spec.unit}`);
  if (max !== null && number > max) throw new Error(`不能大于 ${max} ${spec.unit}`);
  return Math.round(number * scale);
}

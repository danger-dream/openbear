export const RUN_DEFAULT_FIELDS = Object.freeze([
  "mainModel",
  "mainThinkingLevel",
  "mainFastMode",
  "agentModel",
  "agentThinkLevel",
  "agentFastMode",
]);

export const RUN_DEFAULT_INHERIT = "inherit";
const RUN_DEFAULT_VALUE_PREFIX = "value:";
const own = (value, field) => Object.prototype.hasOwnProperty.call(value || {}, field);

export function hasRunDefault(value, field) {
  return RUN_DEFAULT_FIELDS.includes(field) && own(value, field);
}

export function sparseRunDefaults(value) {
  const result = {};
  for (const field of RUN_DEFAULT_FIELDS) {
    if (own(value, field)) result[field] = value[field];
  }
  return result;
}

export function resolvedRunDefaults(local, inherited, fallback, resolved = {}) {
  const result = {};
  for (const field of RUN_DEFAULT_FIELDS) {
    for (const layer of [local, inherited, fallback, resolved]) {
      if (own(layer, field)) {
        result[field] = layer[field];
        break;
      }
    }
  }
  return result;
}

export function normalizedRunDefaults({local, inherited, fallback, resolved} = {}, models = []) {
  const byKey = new Map((Array.isArray(models) ? models : []).map(model => [String(model?.key || ""), model]).filter(([key]) => key));
  const selected = sparseRunDefaults(fallback);
  const overrides = {...sparseRunDefaults(inherited), ...sparseRunDefaults(local)};
  for (const [field, value] of Object.entries(overrides)) {
    if (["mainModel", "agentModel"].includes(field) && value && !byKey.has(String(value))) continue;
    selected[field] = value;
  }

  const resolvedValues = sparseRunDefaults(resolved);
  let mainModel = String(selected.mainModel || "");
  if (!byKey.has(mainModel) && byKey.has(String(resolvedValues.mainModel || ""))) mainModel = String(resolvedValues.mainModel);
  const main = byKey.get(mainModel) || null;
  const mainLevels = Array.isArray(main?.thinkingLevels) ? main.thinkingLevels.filter(Boolean) : [];
  const mainDefault = main?.defaultThinkingLevel || mainLevels[mainLevels.length - 1] || "";
  const requestedMainThinking = String(selected.mainThinkingLevel || "");
  const mainThinkingLevel = mainLevels.length
    ? (mainLevels.includes(requestedMainThinking) ? requestedMainThinking : mainDefault)
    : "off";
  const mainFastMode = selected.mainFastMode === true && Boolean(main?.supportsFast);

  let agentModel = String(selected.agentModel || "");
  if (agentModel && !byKey.has(agentModel)) {
    const resolvedAgentModel = String(resolvedValues.agentModel || "");
    agentModel = byKey.has(resolvedAgentModel) ? resolvedAgentModel : "";
  }
  const effectiveAgentModel = agentModel || mainModel;
  const agent = byKey.get(effectiveAgentModel) || null;
  const agentLevels = Array.isArray(agent?.thinkingLevels) ? agent.thinkingLevels.filter(Boolean) : [];
  const requestedAgentThinking = String(selected.agentThinkLevel || "");
  const agentThinkLevel = requestedAgentThinking && agentLevels.includes(requestedAgentThinking) ? requestedAgentThinking : "";
  const agentFastMode = selected.agentFastMode === false
    ? false
    : selected.agentFastMode === true && agent?.supportsFast ? true : null;

  return {mainModel, mainThinkingLevel: mainThinkingLevel || "off", mainFastMode, agentModel, agentThinkLevel, agentFastMode};
}

export function runDefaultSelection(value, field) {
  return own(value, field) ? `${RUN_DEFAULT_VALUE_PREFIX}${JSON.stringify(value[field])}` : RUN_DEFAULT_INHERIT;
}

export function runDefaultOption(value) {
  return `${RUN_DEFAULT_VALUE_PREFIX}${JSON.stringify(value)}`;
}

export function updateRunDefault(value, field, selection) {
  const result = sparseRunDefaults(value);
  if (!RUN_DEFAULT_FIELDS.includes(field)) return result;
  if (selection === RUN_DEFAULT_INHERIT) {
    delete result[field];
    return result;
  }
  if (typeof selection !== "string" || !selection.startsWith(RUN_DEFAULT_VALUE_PREFIX)) return result;
  result[field] = JSON.parse(selection.slice(RUN_DEFAULT_VALUE_PREFIX.length));
  return result;
}

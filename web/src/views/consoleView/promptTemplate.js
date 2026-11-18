// 人设: the system prompt template a conversation renders. '' is the active main
// template; otherwise a template id marked as a persona. Mirrors app/context/prompt_template.py.
import {reactive} from "vue";
import {Api} from "../../api.js";
import {relativeSessionTime} from "../../sessionPresentation.js";

export function normalizePromptTemplate(value) {
  const text = String(value ?? "").trim();
  return /^[1-9][0-9]{0,18}$/.test(text) && (text.length < 19 || text <= "9223372036854775807") ? text : "";
}

export const promptTemplateChoices = reactive({active: choice(), items: [], existingIds: null, loaded: false, error: ""});

function choice(item = {}) {
  return {id: String(item?.id || ""), name: String(item?.name || ""), chars: Math.max(0, Number(item?.chars) || 0), updatedAt: Math.max(0, Number(item?.updatedAt) || 0)};
}

let pending = null;
export function loadPromptTemplateChoices({force = false} = {}) {
  if (pending) return pending;
  if (promptTemplateChoices.loaded && !force) return Promise.resolve(promptTemplateChoices);
  pending = Api.promptTemplateChoices().then(data => {
    promptTemplateChoices.active = choice(data?.active);
    promptTemplateChoices.items = (Array.isArray(data?.items) ? data.items : []).map(choice).filter(item => item.id);
    promptTemplateChoices.existingIds = Array.isArray(data?.existingIds) ? data.existingIds.map(String) : null;
    promptTemplateChoices.loaded = true;
    promptTemplateChoices.error = "";
    return promptTemplateChoices;
  }).catch(error => {
    promptTemplateChoices.error = String(error?.message || error || "读取失败");
    throw error;
  }).finally(() => { pending = null; });
  return pending;
}

export function defaultPromptTemplateName(choices = promptTemplateChoices) {
  return choices.active?.name ? `系统默认（${choices.active.name}）` : "系统默认";
}

export function promptTemplateName(value, choices = promptTemplateChoices) {
  const id = normalizePromptTemplate(value);
  if (!id) return defaultPromptTemplateName(choices);
  if (id === choices.active?.id) return choices.active.name || `模板 #${id}`;
  return choices.items.find(item => item.id === id)?.name || `模板 #${id}`;
}

export function promptTemplateLabel(value, choices = promptTemplateChoices) {
  return `人设：${promptTemplateName(value, choices)}`;
}

// "3,532 字 · 2 小时前更新": what a persona row says beneath its name.
export function promptTemplateMeta(item, nowMs = Date.now()) {
  const parts = [];
  if (item?.chars) parts.push(`${Number(item.chars).toLocaleString("en-US")} 字`);
  if (item?.updatedAt) parts.push(`${relativeSessionTime(item.updatedAt, nowMs)}更新`);
  return parts.join(" · ");
}

// The first character of a persona's name, shown in its row; Latin letters in capitals.
export function promptTemplateInitial(name) {
  const first = Array.from(String(name || "").trim())[0] || "?";
  return first.toUpperCase();
}

const TINTS = ["blue", "violet", "success", "orange", "danger", "info"];
export function promptTemplateTint(id) {
  const text = String(id || "");
  let hash = 0;
  for (const char of text) hash = (hash * 31 + char.codePointAt(0)) >>> 0;
  return TINTS[hash % TINTS.length];
}

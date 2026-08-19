export const APP_NAME = "OpenBear";

const PAGE_TITLES = Object.freeze({
  login: "登录",
  memory: "记忆管理",
  secrets: "凭证库",
  docs: "文档库",
  skills: "Skills",
  mcp: "MCP 管理",
});

const SETTINGS_SECTION_TITLES = Object.freeze({
  channels: "渠道管理",
  templates: "提示词模板",
  agents: "Agent Presets",
  "system-settings": "系统设置",
  logs: "系统日志",
});

function text(value) {
  return String(value ?? "").trim();
}

export function currentPageTitle({page = "", conversationTitle = "", settingsSection = ""} = {}) {
  const pageKey = text(page);
  if (pageKey === "console") return text(conversationTitle) || "新会话";
  if (pageKey === "settings") return SETTINGS_SECTION_TITLES[text(settingsSection)] || "设置";
  return PAGE_TITLES[pageKey] || APP_NAME;
}

export function documentTitle(state = {}) {
  const title = currentPageTitle(state);
  return title === APP_NAME ? APP_NAME : `${title} - ${APP_NAME}`;
}

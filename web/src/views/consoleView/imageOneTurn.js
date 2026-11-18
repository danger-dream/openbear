// 图片只看一轮: images reach the model only in the round they arrive; later rounds
// see their local file paths. Mirrors app/context/image_one_turn.py.
export const IMAGE_ONE_TURN_DEFAULT = Object.freeze({enabled: false, includeUser: true, agents: true});

export function normalizeImageOneTurn(value) {
  const raw = value && typeof value === "object" ? value : {};
  return Object.fromEntries(Object.entries(IMAGE_ONE_TURN_DEFAULT)
    .map(([key, fallback]) => [key, typeof raw[key] === "boolean" ? raw[key] : fallback]));
}

export function imageOneTurnLabel(value) {
  return `图片只看一轮：${normalizeImageOneTurn(value).enabled ? "已开启" : "关闭"}`;
}

export const IMAGE_ONE_TURN_ROWS = Object.freeze([
  {key: "enabled", label: "图片只看一轮", hint: "图片只在出现的那一轮给模型看，之后换成文件路径"},
  {key: "includeUser", label: "包括我发送的图片", hint: "关闭时只处理 AI 读取的图片和浏览器截图", sub: true},
  {key: "agents", label: "Agent 也跟随", hint: "派出的 Agent 开始新一轮时，同样换掉之前的图片", sub: true},
]);

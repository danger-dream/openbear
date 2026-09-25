export const CONVERSATION_TITLE_MAX_UNITS = 12;

export function titleGraphemes(value = "") {
  const text = String(value || "");
  if (typeof Intl !== "undefined" && typeof Intl.Segmenter === "function") {
    const segmenter = new Intl.Segmenter(undefined, {granularity: "grapheme"});
    return Array.from(segmenter.segment(text), item => item.segment);
  }
  return Array.from(text);
}

function graphemeHalfUnits(grapheme) {
  if (!grapheme) return 0;
  const base = Array.from(grapheme).find(character => {
    const codepoint = character.codePointAt(0);
    return character !== "\u200d" && character !== "\ufe0e" && character !== "\ufe0f"
      && character !== "\u20e3" && !(codepoint >= 0x1f3fb && codepoint <= 0x1f3ff)
      && !/\p{Mark}/u.test(character);
  }) || grapheme;
  return base.codePointAt(0) <= 0x7f ? 1 : 2;
}

export function conversationTitleHalfUnits(value = "") {
  return titleGraphemes(value).reduce((total, grapheme) => total + graphemeHalfUnits(grapheme), 0);
}

export function truncateConversationTitle(value = "", maxUnits = CONVERSATION_TITLE_MAX_UNITS) {
  const limit = Math.max(0, Number(maxUnits) || 0) * 2;
  let used = 0;
  let output = "";
  for (const grapheme of titleGraphemes(value)) {
    const width = graphemeHalfUnits(grapheme);
    if (width && used + width > limit) break;
    output += grapheme;
    used += width;
  }
  return output.trimEnd();
}

export function initialConversationTitle(value = "") {
  const normalized = String(value || "").replace(/\s+/g, " ").trim();
  return truncateConversationTitle(normalized) || "新会话";
}

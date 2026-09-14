import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import postcss from "postcss";
import {parse} from "@vue/compiler-sfc";

const source = readFileSync(new URL("./TaskMemoryDrawer.vue", import.meta.url), "utf8");
const consoleSource = readFileSync(new URL("./ConsoleView.vue", import.meta.url), "utf8");
const minimapSource = readFileSync(new URL("./TurnMinimap.vue", import.meta.url), "utf8");
const composerSource = readFileSync(new URL("./ConsoleComposer.vue", import.meta.url), "utf8");
const apiSource = readFileSync(new URL("../../api.js", import.meta.url), "utf8");

test("phone memory and other tools are in the header menu, not a permanent rail", () => {
  const mobile = (text, selector) => {
    const declarations = {};
    postcss.parse(parse(text).descriptor.styles.map(style => style.content).join("\n")).walkRules(rule => {
      if (!rule.selectors.includes(selector) || rule.parent.name !== "media" || rule.parent.params !== "(max-width: 760px)") return;
      rule.walkDecls(decl => { declarations[decl.prop] = decl.value; });
    });
    return declarations;
  };
  assert.equal(mobile(consoleSource, ".console-controls").display, "none");
  assert.equal(mobile(consoleSource, ".conversation-column")["grid-template-columns"], undefined);
  assert.equal(mobile(minimapSource, ".turn-minimap").display, "none");
  assert.match(consoleSource, /<MobileConversationTools/);
  assert.match(consoleSource, /@open-memory="taskMemoryDrawer\?\.open\(\)"/);
  assert.match(source, /defineExpose\(\{open: openDrawer\}\)/);
  assert.match(source, /async function openDrawer\(\) \{\s*if \(!usableConversationUuid\.value\) return;/);
});

test("desktop floating rail keeps task memory, work details, and quick navigation separate", () => {
  assert.match(consoleSource, /--console-float-minimap-top:\s*calc\(\s*var\(--console-float-rail-top\)\s*\+ var\(--console-float-control-size\)\s*\+ var\(--console-float-control-gap\)\s*\+ var\(--console-float-control-size\)\s*\+ var\(--console-float-control-gap\)\s*\)/);
  assert.match(consoleSource, /@media \(min-width: 761px\) \{[\s\S]*?\.work-detail-toggle\s*\{[\s\S]*?top:\s*calc\(var\(--console-float-rail-top\) \+ var\(--console-float-control-size\) \+ var\(--console-float-control-gap\)\)/);
  assert.match(minimapSource, /top:\s*var\(--console-float-minimap-top/);

  const rem = 16;
  const size = 2.15 * rem;
  const gap = 0.75 * rem;
  const memoryTop = 0;
  const workTop = memoryTop + size + gap;
  const minimapTop = workTop + size + gap;
  assert.equal(workTop - (memoryTop + size), gap, "work-detail button follows task memory with the rail gap");
  assert.equal(minimapTop - (workTop + size), gap, "quick navigation follows work details without overlap");
});

test("floating and composer controls use Element Plus tooltips instead of native titles", () => {
  assert.match(source, /<el-tooltip content="任务记忆" placement="left"/);
  assert.match(consoleSource, /<el-tooltip[\s\S]*?:content="activeTurnWorking \? '当前轮次正在工作，点击查看详情'/);
  assert.match(consoleSource, /<el-tooltip[\s\S]*?:content="autoScrollLocked \? '滚动已锁定到底部，点击解锁'/);
  assert.doesNotMatch(consoleSource, /:title="(?:activeTurnWorking|autoScrollLocked)/);
  assert.match(minimapSource, /<el-tooltip[\s\S]*?:content="turnNavLabel\(turn, idx\)"[\s\S]*?placement="left"/);
  assert.doesNotMatch(minimapSource, /:title=/);
  assert.doesNotMatch(composerSource, /\btitle=/);
  for (const label of ["移除附件", "新话题（Ctrl+N）", "上传图片或附件", "清空草稿", "停止生成", "发送消息（Enter）"]) {
    assert.ok(composerSource.includes(`content="${label}"`), `missing Element Plus tooltip: ${label}`);
  }
  assert.match(composerSource, /aria-label="运行配置"/);
});

test("work-detail tooltip closes before the reference moves with the panel transition", () => {
  const tooltipMarkup = consoleSource.match(/<el-tooltip[\s\S]*?ref="workDetailTooltip"[\s\S]*?<\/el-tooltip>/)?.[0] || "";
  assert.match(tooltipMarkup, /:disabled="workDetailTooltipSuppressed"/);
  assert.match(tooltipMarkup, /:popper-style="workDetailTooltipSuppressed \? \{display: 'none'\} : undefined"/);
  assert.match(tooltipMarkup, /@click\.stop="toggleWorkDetailPanel"/);

  const toggleBody = consoleSource.match(/async function toggleWorkDetailPanel\(\) \{[\s\S]*?\n\}/)?.[0] || "";
  const suppressedAt = toggleBody.indexOf("workDetailTooltipSuppressed.value = true");
  const hideAt = toggleBody.indexOf("workDetailTooltip.value?.hide?.()");
  const flushAt = toggleBody.indexOf("await nextTick()");
  const layoutAt = toggleBody.indexOf("workDetailOpen.value = !workDetailOpen.value");
  assert.ok(suppressedAt >= 0 && suppressedAt < hideAt && hideAt < flushAt && flushAt < layoutAt);
  assert.match(toggleBody, /window\.setTimeout\([\s\S]*?workDetailTooltipSuppressed\.value = false;[\s\S]*?, 300\)/);
  assert.match(consoleSource, /onBeforeUnmount\(\(\) => \{[\s\S]*?window\.clearTimeout\(workDetailTooltipReleaseTimer\)/);
});

test("drawer keeps both scopes and actual catalog while separating reading from technical metadata", () => {
  assert.match(source, /label="会话记忆" name="conversation"/);
  assert.match(source, /label="Agent 记忆" name="agent"/);
  assert.match(source, /taskMemoryTasks/);
  assert.doesNotMatch(source, /task\.taskShortId/);
  assert.match(source, /:label="taskSearchLabel\(task\)"/);
  assert.match(source, /statusLabel\(task\.status\)/);
  assert.match(source, /Api\.taskMemories\(/);
  assert.match(source, /async function editMemory[\s\S]*Api\.taskMemory\(/);
  assert.match(source, /async function viewMemory[\s\S]*Api\.taskMemory\(/);
  assert.match(source, /<InteractionMarkdown[^>]*:text="detail\.body"/);
  assert.match(source, /<details class="memory-technical">[\s\S]*detail\.memoryUuid/);
  assert.doesNotMatch(source, /v\{\{ item\.revision \}\}/);
  assert.match(source, /formatDate\(item\.updatedAt\)/);
  assert.match(source, /formatBytes\(detail\.sizeBytes\)/);
  assert.match(source, /sourceLabel\(item\)/);
  assert.match(source, /自动提供给模型/);
  assert.match(source, /Agent 可读取/);
  assert.match(source, /模型可见内容/);
  assert.match(source, /previewOmittedCount/);
  assert.match(source, /previewShortBodyMaxChars/);
  assert.doesNotMatch(source, /保存重要决定和工作进展|保留重要决定与进展|记录决定、约束、重要发现或后续工作/);
  assert.match(source, /v-if="previewOpen"/);
  assert.match(source, /async function loadPreview[\s\S]*Api\.taskMemoryPreview\(token\.conversationUuid, requestScopeParams\(token\)\)/);
  assert.doesNotMatch(source, /function buildCatalogPreview|\.slice\(0,\s*20\)/);
  assert.doesNotMatch(source.match(/async function loadPreview[\s\S]*?\n\}/)?.[0] || "", /query/);
  assert.match(source, /setInterval[\s\S]*5000/);
  assert.match(source, /window\.addEventListener\("focus"/);
});

test("drawer requests use full identity generations and switching/unmount reset stale state", () => {
  assert.match(source, /createTaskMemoryRequestGate/);
  for (const channel of ["preview", "count", "tasks", "items", "detail", "mutation"]) {
    assert.match(source, new RegExp(`beginRequest\\("${channel}"\\)`));
  }
  assert.match(source, /requestIsCurrent\(token\)/);
  assert.match(source, /watch\(\(\) => props\.conversationUuid[\s\S]*invalidateRequests\(\)[\s\S]*resetContextState/);
  assert.match(source, /watch\(activeTab[\s\S]*invalidateRequests\(\)[\s\S]*resetContextState/);
  assert.match(source, /watch\(selectedTaskUuid[\s\S]*invalidateRequests\(\)[\s\S]*resetContextState/);
  assert.match(source, /function resetContextState[\s\S]*loading\.value = false;\s*tasksLoading\.value = false;\s*if \(resetTasks\)/);
  assert.match(source, /onBeforeUnmount[\s\S]*requestGate\.dispose\(\)[\s\S]*resetContextState/);
});

test("drawer and editor controls expose accessible names and keyboard focus contracts", () => {
  assert.match(source, /:aria-label="badgeCount \? `打开任务记忆，共 \$\{badgeCount\} 条`/);
  assert.match(source, /aria-haspopup="dialog"/);
  assert.match(source, /:aria-expanded=/);
  assert.match(source, /aria-labelledby="task-memory-drawer-title"/);
  assert.match(source, /<h2 id="task-memory-drawer-title">任务记忆<\/h2>/);
  assert.match(source, /aria-label="关闭任务记忆"/);
  for (const field of ["name", "description", "body", "auto-reinject", "visible-agents", "show-deleted"]) {
    assert.match(source, new RegExp(`id="task-memory-${field}"`));
    assert.match(source, new RegExp(`for="task-memory-${field}"`));
    assert.match(source, new RegExp(`aria-labelledby="task-memory-${field}-label"`));
  }
  assert.match(source, /:focus-visible/);
  assert.match(source, /:with-header="false"/);
});

test("API client covers paginated CRUD and restore without global Memory endpoints", () => {
  for (const method of [
    "taskMemories", "taskMemoryTasks", "taskMemoryPreview", "taskMemory", "createTaskMemory",
    "updateTaskMemory", "deleteTaskMemory", "restoreTaskMemory",
  ]) {
    assert.match(apiSource, new RegExp(`${method}:`));
  }
  assert.match(apiSource, /\/conversations\/\$\{encodeURIComponent\(uuid\)\}\/task-memories/);
});

test("drawer, editor, and scoped popups keep desktop typography readable", () => {
  const desktopStyles = source.slice(source.indexOf("<style scoped>"), source.indexOf("@media (max-width: 760px)"));
  assert.doesNotMatch(desktopStyles, /font-size:\s*(?:8(?:\.5)?|9(?:\.5)?)px/);
  assert.match(source, /\.memory-name-line strong[^}]*font-size:\s*14px/);
  assert.match(source, /\.memory-description[^}]*font-size:\s*13px/);
  assert.match(source, /\.memory-meta[^}]*font-size:\s*12px/);
  assert.match(source, /\.memory-form > label[^}]*font-size:\s*13px/);
  assert.match(source, /popper-class="task-memory-task-select-popper"/);
  assert.match(source, /customClass: "task-memory-confirm"/);
  assert.match(source, /task-memory-editor \.el-textarea__inner[^}]*font-size:\s*14px/);
});

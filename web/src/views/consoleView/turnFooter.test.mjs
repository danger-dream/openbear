import { contextResourceUrl } from '../../sessionBoundary.js';
import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {writeFile, unlink} from "node:fs/promises";
import vm from "node:vm";
import {compile, createSSRApp, h, ref} from "vue";
import {renderToString} from "vue/server-renderer";
import {parse} from "@vue/compiler-sfc";
import {Clock, Timer, Top, Bottom, Coin} from "@element-plus/icons-vue";
import {Undo2, Play, StickyNote} from "@lucide/vue";
import {artifactFormat, artifactFromUrl} from "../../artifacts/artifactFiles.js";
import {projectOperationMessages, eventDisplayTimeMs, eventStartedAtMs, eventUpdatedAtMs} from "../../timelineProjection.js";
import {conversationTimeParts} from '../../conversationTime.js';
import {conversationTimelineEntries, shouldRenderAssistantDivider} from "./conversationTimeline.js";
import {replyMarkdownText} from "./replyMarkdownShare.js";
import {visibilitySelectionClasses, selectVisibilityRow} from './messageVisibility.js';
import {isInlineProcess} from './conversationWork.js';
import {formatTime, isClosed, leaves, topNumber, turnTaskNumbers} from './workItemsPresentation.js';

// Use the actual display/usage functions, omitting only browser-only icon/markdown imports.
const displayUrl = new URL(`./.turn-footer-display-${process.pid}.mjs`, import.meta.url);
let display;
try {
  const source = readFileSync(new URL("./display.js", import.meta.url), "utf8")
    .replace('import ContextCompactionIcon from "./legacy/ContextCompactionIcon.vue";', "const ContextCompactionIcon = {};")
    .replace('import {plainText} from "./markdown.js";', 'const plainText = value => String(value || "");');
  await writeFile(displayUrl, source);
  display = await import(displayUrl.href);
} finally {
  await unlink(displayUrl).catch(() => {});
}
const {descriptor} = parse(readFileSync(new URL("./TurnList.vue", import.meta.url), "utf8"));
const script = descriptor.scriptSetup.content.replace(/^import[\s\S]*?;\n/gm, "");
const render = compile(descriptor.template.content);
const setupNames = [...script.matchAll(/^(?:async )?function (\w+)\(/gm)].map(match => match[1]);
const slot = {inheritAttrs: false, setup: (_, {slots}) => () => slots.default?.()};
function turnList(turns, running = false, hiddenIds = [], runState = {}) {
  const copies = [], emitted = [], webhookMessages = [], cronMessages = [];
  const sampleDate = new Date(1789043900 * 1000);
  const day = ref(`${sampleDate.getFullYear()}-${String(sampleDate.getMonth()+1).padStart(2,'0')}-${String(sampleDate.getDate()).padStart(2,'0')}`);
  const props = {turns, running, controllerRunning: running, activeTurnIndex: -1, ...runState,
    conversationUuid: "test-conversation", detailKey: () => "detail", isDetailOpen: () => false, activeToolResultIndex: () => 0};
  const hidden = new Set(hiddenIds);
  const visibility = {hiddenIds: ref(hidden), selecting: ref(false), selected: ref(new Set()), busy: ref(false), canTarget: () => true, isHidden: value => hidden.has(value?.operation?.opId || value?.opId || value?.eventKey || value?.id), assistantTargets: turn => (turn.events || []).filter(event => !hidden.has(event.id))};
  const previews = [];
  const context = vm.createContext({contextResourceUrl,ref, artifactFormat, artifactFromUrl: url => artifactFromUrl(url, "https://openbear.test"), openArtifactPreview: (identity, label) => previews.push({identity, label}), conversationTimeParts, useConversationDay: () => day, ...display, isInlineProcess, conversationTimelineEntries, shouldRenderAssistantDivider, replyMarkdownText, visibilitySelectionClasses, selectVisibilityRow,
    useMessageVisibility: () => visibility,
    useWorkItems: () => null, formatTime, isClosed, leaves, topNumber, turnTaskNumbers,
    projectedEventDisplayTimeMs: eventDisplayTimeMs, projectedEventStartedAtMs: eventStartedAtMs, projectedEventUpdatedAtMs: eventUpdatedAtMs,
    defineProps: () => props, defineEmits: () => (...args) => emitted.push(args), referenceDisplayText: value => value,
    copyTextToClipboard: async text => copies.push(text), ElMessage: {success() {}, error() {}},
    window: {clearTimeout() {}, setTimeout() { return 1; }},
  });
  vm.runInContext(script, context);
  const app = createSSRApp({render, setup: () => vm.runInContext(`({props, emit, visibility, visibilitySelectionClasses, selectVisibilityRow, copiedMessageKey, notedTurnKey, fmtTokens, cachePct, ${setupNames.join(", ")}})`, context)});
  for (const name of ["el-tooltip", "el-icon", "el-image", "Check", "CopyDocument", "Link", "Hide"]) app.component(name, slot);
  for (const [name, icon] of Object.entries({Clock, Timer, Top, Bottom, Coin, Undo2, Play, StickyNote})) app.component(name, icon);
  app.component("MessageVisibilityAction", {render: () => null});
  app.component("PromptSuggestions", {props: ['items', 'collapsed'], setup: p => () => h('section', {class: 'prompt-suggestions', 'data-items': p.items.join('|'), 'data-collapsed': String(p.collapsed)})});
  app.component("AudioPlayer", {props: ['src', 'label'], setup: p => () => h('audio', {src: p.src, 'data-label': p.label})});
  app.component("ReplyMarkdownShare", {props: ['content'], setup: p => () => h('button', {'aria-label': '分享回复 Markdown', 'data-share-content': p.content})});
  // This suite isolates footer/copy contracts. Work grouping itself is mounted
  // with its real lifecycle in conversationWork.integration.test.mjs.
  app.component('ConversationWorkBlock', {props: ['entries'], setup: (p, {slots}) => () => p.entries.map((entry, conversationIndex) => slots.default({entry, conversationIndex}))});
  app.component('ConversationProcessEvent', {props: ['event'], setup: p => () => h('div', {'data-event-id': p.event.id}, p.event.message?.reasoning || '上下文压缩')});
  // The real card's time/details contracts are covered in webhookMessage.integration.test.mjs.
  app.component("WebhookMessageCard", {props: ['message'], setup: p => {
    webhookMessages.push(p.message);
    return () => h('div', {'data-webhook-op-id': p.message.opId}, p.message.eventCard?.name);
  }});
  app.component("CronMessageCard", {props: ['message'], setup: p => {
    cronMessages.push(p.message);
    return () => h('div', {'data-cron-op-id': p.message.opId}, p.message.cronCard?.name);
  }});
  app.component("ConsoleMarkdown", {props: ["text"], setup: props => () => h("p", props.text)});
  app.component("TurnEvent", {props: ["event"], setup: props => () => h("div", {"data-event-id": props.event.id}, props.event.message?.content || "上下文压缩")});
  return {props, day, copies, emitted, hidden, webhookMessages, cronMessages, previews, html: () => renderToString(app), run: code => vm.runInContext(code, context)};
}
const answer = (id = "answer", content = "已完成") => ({kind: "answer", id, message: {content, createdAt: 1789044000, live: false}});
const savedStats = () => ({live: false, durationMs: 128664,
  usage: {inputTokens: 496777, outputTokens: 2593, cacheReadTokens: 824448, cacheWriteTokens: 0}});
const compact = strategy => ({kind: "tool", id: `compress-${strategy}`, toolName: "ContextCompaction", operation: {
  opId: `context-${strategy}`, opType: "context_compaction", status: "completed", lifecycle: "terminal",
  createdAtMs: 1789044001000, updatedAtMs: 1789044002000,
  payload: {scope: "root", strategy, summary: "摘要不属于复制正文", durationMs: 1000},
}});
function footer(html) { return html.match(/<div class="assistant-message-meta">[\s\S]*?<\/div>/)?.[0] || ""; }

const footerTurn = source => ({id: 'footer-turn', user: {
  source, opId: 'saved-user', turnUuid: 'footer-turn', deleteTraceable: true,
  content: '事件来信 webhook 原文', createdAt: 1789043900,
  cronCard: {name: '定时验收', runId: 'cron-run', trigger: 'scheduled'},
  eventCard: {name: '事件来信', receivedAtMs: 1789043900000, events: [{eventId: 'event-one', summary: '入库完成'}]},
}, events: [answer()], stats: savedStats()});

for (const source of ['webhook', 'cron', 'user', undefined]) {
  test(`compiled TurnList renders ${source || 'legacy'} user footer policy without changing the assistant footer`, async () => {
    const turn = footerTurn(source);
    const original = structuredClone(turn);
    const component = turnList([turn]);
    const html = await component.html();
    const userRow = html.split('<div class="assistant-row">')[0];
    if (['webhook', 'cron'].includes(source)) {
      assert.ok(userRow.includes(`data-${source}-op-id="saved-user"`));
      assert.match(userRow, source === 'cron' ? /定时验收/ : /事件来信/);
      assert.equal((source === 'cron' ? component.cronMessages : component.webhookMessages)[0], turn.user, 'the card receives the complete original message');
      assert.doesNotMatch(userRow, /user-message-meta|user-message-time|message-icon-action|message-visibility-action/);
      assert.doesNotMatch(userRow, /aria-label="(?:复制消息|引用本轮问答|从此处重来|隐藏消息)"/);
    } else {
      assert.equal(component.webhookMessages.length, 0);
      assert.equal(component.cronMessages.length, 0);
      assert.match(userRow, /class="message-user"/);
      assert.match(userRow, /class="user-message-meta"/);
      assert.match(userRow, /class="user-message-time"[^>]*>(?:<!--.*?-->)?\d{2}:\d{2}:\d{2}<\/time>/);
      for (const label of ['复制消息', '引用本轮问答', '从此处重来', '隐藏消息']) {
        assert.ok(userRow.includes(`aria-label="${label}"`), `${label} remains available`);
      }
    }
    const assistantFooter = footer(html);
    assert.match(assistantFooter, /assistant-message-time/);
    assert.match(assistantFooter, /2m 9s/);
    assert.match(assistantFooter, /turn-token-usage/);
    for (const label of ['复制消息', '分享回复 Markdown', '隐藏回复']) {
      assert.ok(assistantFooter.includes(`aria-label="${label}"`), `${label} remains available on assistant replies`);
    }
    assert.deepEqual(turn, original, 'rendering does not alter content, event metadata or traceability');
  });
}

for (const source of ['webhook', 'cron']) test(`${source} rows still participate in global visibility selection, hiding and restoration`, async () => {
  const turn = footerTurn(source);
  const original = structuredClone(turn);
  const component = turnList([turn]);
  component.run("visibility.selecting.value = true; visibility.selected.value.add('saved-user')");
  const selected = await component.html();
  assert.match(selected, /class="[^"]*timed-row-user[^"]*visibility-selectable[^"]*visibility-selected/);
  assert.ok(selected.includes(`data-${source}-op-id="saved-user"`));
  assert.doesNotMatch(selected, /user-message-meta/);
  const hidden = await turnList([turn], false, ['saved-user']).html();
  assert.doesNotMatch(hidden, /timed-row-user|data-(?:webhook|cron)-op-id/);
  assert.match(hidden, /assistant-message-meta/);
  const allHidden = await turnList([turn], false, ['saved-user', 'answer']).html();
  assert.doesNotMatch(allHidden, /turn-block|assistant-message-meta|data-(?:webhook|cron)-op-id/);
  const restored = await turnList([turn]).html();
  assert.ok(restored.includes(`data-${source}-op-id="saved-user"`));
  assert.doesNotMatch(restored, /user-message-meta/);
  assert.deepEqual(turn, original);
});

test('restart action renders a non-circular Undo2 icon while retaining its label and guarded delete-suffix event', async () => {
  const turn = {id:'rewind', user:{turnUuid:'rewind', opId:'saved-user', content:'从这一轮重新开始', deleteTraceable:true}, events:[]};
  const component = turnList([turn]);
  const button = html => html.match(/<button[^>]*class="message-icon-action restart-action"[\s\S]*?<\/button>/)?.[0] || '';
  let markup = button(await component.html());
  assert.match(markup, /aria-label="从此处重来"/);
  assert.match(markup, /<svg[^>]*fill="none"[^>]*stroke="currentColor"/);
  assert.match(markup, /aria-hidden="true"/);assert.doesNotMatch(markup, /disabled/);
  assert.match(descriptor.scriptSetup.content, /import \{Undo2\} from "@lucide\/vue"/);
  component.run('deleteTurnSuffix(props.turns[0])');
  assert.equal(component.emitted.length, 1);assert.equal(component.emitted[0][0], 'delete-suffix');assert.equal(component.emitted[0][1].user.turnUuid, 'rewind');
  for (const state of [{running:true,deletingTurnUuid:''},{running:false,deletingTurnUuid:'rewind'}]) {
    Object.assign(component.props,state);markup=button(await component.html());assert.match(markup,/disabled/);
    component.run('deleteTurnSuffix(props.turns[0])');assert.equal(component.emitted.length,1);
  }
});

// Stats and ordering mirror the values established in the user's referenced investigation.
// Projection inputs below are deterministic fixtures, not a second production DB read.
for (const strategy of ["historical", "sliding_window", "model_summary"]) {
  test(`completed reply followed by ${strategy} compression retains one turn footer after the card`, async () => {
    const operation = strategy === "historical" ? {
      opId: "tool:context-compaction:77", opType: "tool", source: "context_compaction",
      payload: {scope: "root", toolName: "ContextCompaction", compactionId: "context-compaction:77", summaryId: 77},
    } : compact(strategy).operation;
    const projected = projectOperationMessages([
      {opId: "user", opType: "user_message", turnId: "turn-a", displaySeq: 1, createdAtMs: 1789043900000, payload: {text: "请处理"}},
      {opId: "reply", opType: "assistant_message", turnId: "turn-a", displaySeq: 2, status: "completed", lifecycle: "terminal", createdAtMs: 1789044000000, payload: {text: "已完成", complete: true}},
      {...operation, turnId: "turn-a", displaySeq: 3, status: "completed", lifecycle: "terminal", createdAtMs: 1789044001000, updatedAtMs: 1789044002000},
      {opId: "stats", opType: "stats", turnId: "turn-a", displaySeq: 4, payload: savedStats()},
    ]);
    const assistant = projected.find(message => message.role === "assistant");
    const turn = {id: "turn-a", user: projected.find(message => message.role === "user"), events: assistant.localTimeline, stats: assistant.localStats};
    assert.deepEqual(turn.events.map(event => event.kind), ["answer", "tool"]);
    const component = turnList([turn]);
    const html = await component.html();
    assert.equal((html.match(/class="assistant-message-meta"/g) || []).length, 1);
    assert.ok(html.indexOf('class="assistant-message-meta"') > html.lastIndexOf('data-event-id='));
    assert.match(footer(html), /2m 9s/);
    assert.match(footer(html), /↑1\.32M · ↓2\.6K · 缓存 824\.4K（62\.4%）/);
    assert.match(footer(html), /aria-label="复制消息"/);
    await component.run("copyMessage(assistantTurnRawContent(props.turns[0]), 'turn-copy')");
    assert.deepEqual(component.copies, ["已完成"]);
    assert.equal(turn.stats.durationMs, 128664);
  });
}

test("footer adds five decorative metric icons while retaining time, duration, token values and cache percentage", async () => {
  const html = footer(await turnList([{id:'icons',events:[answer()],stats:savedStats()}]).html());
  assert.equal((html.match(/class="footer-meta-icon"/g) || []).length, 5);
  assert.equal((html.match(/aria-hidden="true"/g) || []).length, 5, 'decorative icons do not replace accessible metric names');
  assert.match(html, /aria-label="输入 Tokens：1\.32M"/);
  assert.match(html, /aria-label="输出 Tokens：2\.6K"/);
  assert.match(html, /aria-label="缓存 Tokens：824\.4K，命中率 62\.4%"/);
  const text = html.replace(/<[^>]*>/g, '');
  assert.match(text, /\d{2}:\d{2}:\d{2}/);
  assert.match(text, /2m 9s/);
  assert.match(text, /↑1\.32M · ↓2\.6K · 缓存 824\.4K（62\.4%）/);
  assert.match(html, /aria-label="复制消息"/);assert.match(html, /aria-label="隐藏回复"/);
});

test("multiple replies and trailing compression produce one footer and copy only answer content", async () => {
  const turn = {id: "many", events: [answer("a1", "进度"), compact("model_summary"), answer("a2", "最终结果"), compact("sliding_window")], stats: savedStats()};
  const component = turnList([turn]);
  const html = await component.html();
  assert.equal((html.match(/class="assistant-message-meta"/g) || []).length, 1);
  assert.ok(html.indexOf('class="assistant-message-meta"') > html.indexOf('data-event-id="compress-sliding_window"'));
  assert.equal(component.run("assistantTurnRawContent(props.turns[0])"), "进度\n\n最终结果");
});

test("running, streaming, reasoning and live statistics do not expose a completed footer", async () => {
  const base = {id: "active", events: [answer(), compact("model_summary")], stats: savedStats()};
  for (const [turn, running] of [
    [base, true],
    [{...base, stats: {...base.stats, live: true}}, false],
    [{...base, events: [{...answer(), message: {...answer().message, live: true}}, compact("sliding_window")]}, false],
    [{...base, events: [{...answer(), reasoningActive: true}, compact("sliding_window")]}, false],
  ]) {
    assert.equal(footer(await turnList([turn], running).html()), "");
  }
  const prior = {...base, id: "prior"};
  const next = {id: "next", events: [answer("next-answer")], stats: {...savedStats(), live: true}};
  const html = await turnList([prior, next], true).html();
  assert.equal((html.match(/class="assistant-message-meta"/g) || []).length, 1, "a later running turn must not hide prior completed stats");
});

test("next-message suggestions follow only the latest reply's footer", async () => {
  const first = {id: "first", events: [answer("first-answer", "上一轮")], stats: savedStats()};
  const last = {id: "last", events: [answer("last-answer", "最新回复")], stats: savedStats()};
  const html = await turnList([first, last], false, [], {promptSuggestions: ["继续", "换个方案"], promptSuggestionsCollapsed: true}).html();
  assert.equal((html.match(/class="prompt-suggestions"/g) || []).length, 1);
  assert.ok(html.indexOf('class="prompt-suggestions"') > html.lastIndexOf('class="assistant-message-meta"'));
  assert.ok(html.indexOf('class="prompt-suggestions"') > html.indexOf('data-event-id="last-answer"'));
  assert.match(html, /data-items="继续\|换个方案" data-collapsed="true"/);
  assert.doesNotMatch(await turnList([first, last]).html(), /prompt-suggestions/);
});

test("a turn whose controller ended shows its footer while only its Agents keep running", async () => {
  const failed = {id: "failed", user: {turnUuid: "root-failed"}, events: [answer("failed-answer", "本轮执行失败")], stats: savedStats()};
  // The controller failed; its Agents still run in the background.
  const background = await turnList([failed], true, [], {controllerRunning: false}).html();
  assert.notEqual(footer(background), "", "background Agents must not keep a finished turn's footer hidden");
  // A notification can resume an earlier root: that turn, not the latest one, is in progress.
  const resumed = {...failed, id: "resumed"};
  const later = {id: "later", user: {turnUuid: "root-later"}, events: [answer("later-answer")], stats: savedStats()};
  const html = await turnList([resumed, later], true, [], {controllerRunning: true, activeTurnIndex: 0}).html();
  assert.equal((html.match(/class="assistant-message-meta"/g) || []).length, 1);
  assert.ok(html.indexOf('class="assistant-message-meta"') > html.indexOf('data-event-id="later-answer"'),
    "only the resumed turn hides its footer");
});

test("a continuation block names the turn it resumes and links back to its question", async () => {
  const continuation = {rootTurnUuid: "root-1", runIds: ["exec-2"], source: "task_notification",
    rootUserOpId: "msg:root-1", rootStartedAt: 1792720919, rootPreview: "审计这套 Java"};
  const turn = {id: "c", user: {id: "op-continuation", role: "user", content: "", syntheticPlaceholder: true, continuation},
    events: [answer("cont-answer", "审计结论")], stats: savedStats()};
  const html = await turnList([turn]).html();
  assert.match(html, /class="turn-continuation"[\s\S]*后台任务完成后继续[\s\S]*<button[^>]*class="turn-continuation-root"[^>]*>接续 [^<]*「审计这套 Java」<\/button>/);
  assert.equal((html.match(/class="assistant-message-meta"/g) || []).length, 1, "a finished continuation shows its own footer");
  const orphan = await turnList([{...turn, user: {...turn.user, continuation: {...continuation, rootUserOpId: ""}}}]).html();
  assert.match(orphan, /<span class="turn-continuation-root">接续 /, "without a loaded root question the label is plain text");
});

test("stats remain visible without answer text while copy stays absent and empty turns stay empty", async () => {
  for (const events of [[compact("model_summary")], []]) {
    const html = await turnList([{id: "stats-only", events, stats: savedStats()}]).html();
    assert.match(footer(html), /2m 9s/);
    assert.match(footer(html), /↑1\.32M/);
    assert.doesNotMatch(footer(html), /aria-label="复制消息"/);
  }
  assert.equal(footer(await turnList([{id: "empty", events: []}]).html()), "");
});

test("share control receives only visible answer Markdown, never reasoning or hidden answers", async () => {
  const turn = {id: 'share', user: {opId: 'u', content: '用户私密提问'}, events: [
    {...answer('a', '不公开'), message: {content: '不公开', reasoning: '私密思考'}},
    {...answer('b', '  # 原文\n'), message: {content: '  # 原文\n', reasoning: '其它思考'}},
    compact('sliding_window'),
  ], stats: savedStats()};
  const hidden = await turnList([turn], false, ['a']).html();
  assert.match(hidden, /aria-label="分享回复 Markdown"/);
  assert.match(hidden, /data-share-content="  # 原文\n"/);
  assert.doesNotMatch(hidden, /data-share-content="[^"]*(?:不公开|私密思考|其它思考)/);
  const allHidden = await turnList([turn], false, ['a', 'b']).html();
  assert.doesNotMatch(allHidden, /aria-label="分享回复 Markdown"/);
  const running = await turnList([turn], true).html();
  assert.doesNotMatch(running, /aria-label="分享回复 Markdown"/);
});

test('message and process timestamps add their date when the local day changes', async () => {
  const turn = {...footerTurn('user'), events: [answer('first', '进度'), answer('last', '结果')]};
  const component = turnList([turn]);
  const sameDay = await component.html();
  assert.doesNotMatch(sameDay, /class="message-time-date"|class="time-float-date"/);
  const originalDay = component.day.value;
  component.day.value = '2030-01-01';
  const older = await component.html();
  assert.match(older, new RegExp(`class="user-message-time"[^>]*><span class="message-time-date">${originalDay}</span>\\d{2}:\\d{2}:\\d{2}`));
  assert.match(footer(older), new RegExp(`class="message-time-date">${originalDay}</span>\\d{2}:\\d{2}:\\d{2}`));
  assert.match(older, new RegExp(`class="time-float-date">${originalDay}</span><span>\\d{2}:\\d{2}:\\d{2}`));
  assert.match(footer(older), /2m 9s/, 'duration stays distinct from the timestamp');
});

test("ordinary and legacy answers retain the same footer and intermediate hover timestamps", async () => {
  const html = await turnList([{id: "legacy", events: [answer("a1", "进度"), answer("a2", "结果")]}]).html();
  assert.equal((html.match(/class="assistant-message-meta"/g) || []).length, 1);
  assert.equal((html.match(/class="time-float time-float-left"/g) || []).length, 1);
  assert.match(footer(html), /assistant-message-time/);
  assert.match(footer(html), /aria-label="复制消息"/);
  assert.doesNotMatch(footer(html), /turn-token-usage/);
});

test('uploaded audio plays in the bubble and uploaded video opens the shared preview; other attachments keep their tiles', async () => {
  const base = '/api/conversations/39a541d4-4d9c-4a58-87d6-1b276779954a/artifacts';
  const attachment = (id, fileName, mimeType) => ({id, fileName, mimeType, contentUrl: `${base}/${id}/content`, previewUrl: `${base}/${id}/content?preview=1`});
  const ids = ['adfead18-e6d1-40df-8475-391e1245aa01', 'adfead18-e6d1-40df-8475-391e1245aa02', 'adfead18-e6d1-40df-8475-391e1245aa03', 'adfead18-e6d1-40df-8475-391e1245aa04'];
  const turn = {id: 'media', user: {opId: 'u', content: '', attachments: [attachment(ids[0], '录音.m4a', 'audio/mp4'), attachment(ids[1], 'clip.mp4', 'video/mp4'), attachment(ids[2], 'movie.mkv', 'video/x-matroska'), {fileName: 'loose.mp3', mimeType: 'audio/mpeg', contentUrl: '/elsewhere/loose.mp3'}]}, events: [answer()], stats: savedStats()};
  const component = turnList([turn]);
  const html = await component.html();
  assert.match(html, new RegExp(`<div class="user-attachment audio"[^>]*>.*?<audio src="${base}/${ids[0]}/content" data-label="录音.m4a"`));
  assert.match(html, new RegExp(`<button type="button" class="user-attachment video"[^>]*aria-label="播放视频：clip.mp4"[^>]*><video src="${base}/${ids[1]}/content#t=0.1" preload="metadata"[^>]*muted[^>]*playsinline`));
  assert.match(html, /<a class="user-attachment" href="[^"]*aa03[^"]*"[^>]*><span class="file-tile">movie.mkv<\/span>/);
  assert.match(html, /<a class="user-attachment" href="\/elsewhere\/loose.mp3"[^>]*><span class="file-tile">loose.mp3<\/span>/);
  assert.equal((html.match(/<audio /g) || []).length, 1); assert.equal((html.match(/<video /g) || []).length, 1);
  component.run('openMediaAttachment({currentTarget: null}, props.turns[0].user.attachments[1])');
  assert.equal(component.previews.at(-1).identity.artifactUuid, ids[1]); assert.equal(component.previews.at(-1).label, 'clip.mp4');
  component.run('openMediaAttachment({currentTarget: null}, props.turns[0].user.attachments[2])');
  assert.equal(component.previews.length, 1);
});

test('hidden user content and attachments leave no DOM; copied answer excludes hidden segments', async () => {
  const turn = {id: 'hidden', user: {opId: 'u', content: 'private user', attachments: [{fileName: 'private.pdf', contentUrl: '/private.pdf'}]}, events: [answer('a', 'private answer'), answer('b', 'public answer')], stats: savedStats()};
  const component = turnList([turn], false, ['u', 'a']);
  const html = await component.html();
  assert.doesNotMatch(html, /private user|private answer|private.pdf/);
  assert.match(html, /public answer/);
  assert.equal(component.run('assistantTurnRawContent(props.turns[0])'), 'public answer');
  assert.equal(turn.user.content, 'private user');
  const restored = await turnList([turn]).html();
  assert.ok(restored.indexOf('private user') < restored.indexOf('private answer'));
  assert.ok(restored.indexOf('private answer') < restored.indexOf('public answer'));
  const allHidden = await turnList([turn], false, ['u', 'a', 'b']).html();
  assert.doesNotMatch(allHidden, /class="turn-block"|assistant-message-meta|private|public answer/);
});

test('assistant footer note button sits immediately next to copy when notesEnabled is true', async () => {
  const turn = {id: 'note-turn-1', turnUuid: 't-uuid-1', user: {opId: 'u-1', content: '你好'}, events: [answer('ans-op-9', '这是重点结论')], stats: savedStats()};
  const disabledHtml = await turnList([turn]).html();
  assert.doesNotMatch(disabledHtml, /aria-label="记入便签"/);

  const enabled = turnList([turn], false, [], {notesEnabled: true});
  const html = footer(await enabled.html());
  const copyIdx = html.indexOf('aria-label="复制消息"');
  const noteIdx = html.indexOf('aria-label="记入便签"');
  const shareIdx = html.indexOf('aria-label="分享回复 Markdown"');
  assert.ok(copyIdx > 0 && noteIdx > copyIdx && shareIdx > noteIdx);

  enabled.run('captureTurnNote(props.turns[0], 0)');
  assert.deepEqual(JSON.parse(JSON.stringify(enabled.emitted.at(-1))), ['note-turn', {
    opId: 'ans-op-9',
    turnUuid: 't-uuid-1',
    turnNumber: 1,
    quote: '这是重点结论',
  }]);
});

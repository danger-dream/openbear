<script setup>
import { ref, onMounted, onBeforeUnmount, watch } from "vue";
import * as monaco from "monaco-editor";
// 注:Monaco worker 配置在 main.js 第一个 import 的 ./monaco-worker.js 里(必须早于 monaco-editor 求值)

const props = defineProps({
  modelValue: { type: String, default: "" },
  language: { type: String, default: "markdown" },
  square: { type: Boolean, default: false },
  // completionMode:
  // - memory: 记忆/文档正文编辑,补 @mem/@secret/@doc 引用
  // - template: 提示词模板编辑,补 [[变量/函数]] + @if/@each 等模板指令
  // - none: 关闭自定义补全
  completionMode: { type: String, default: "memory" },
  // 引用补全数据:{ mem:[{key,name,note}], secret:[{key,note}], doc:[{key,title}] }
  refData: { type: Object, default: () => ({ mem: [], secret: [], doc: [] }) },
});
const emit = defineEmits(["update:modelValue"]);

const el = ref(null);
let editor = null;
let suppress = false;

if (typeof window !== "undefined") {
  window.__mdCompletionContext = window.__mdCompletionContext || {
    mode: "memory",
    refData: { mem: [], secret: [], doc: [] },
  };
}

function syncCompletionContext() {
  if (typeof window === "undefined") return;
  window.__mdCompletionContext = {
    mode: props.completionMode || "memory",
    refData: props.refData || { mem: [], secret: [], doc: [] },
  };
}

const TEMPLATE_EXPRESSIONS = [
  // 当前 build_system_prompt_params() 提供的运行时变量
  { expr: "workspaceDir", detail: "当前工作目录" },
  { expr: "toolNames", detail: "当前全部可用工具名列表(内置 + MCP)" },
  { expr: "toolSummaries", detail: "全部工具说明映射" },
  { expr: "builtinToolNames", detail: "内置工具名列表" },
  { expr: "builtinToolSummaries", detail: "内置工具说明映射" },
  { expr: "mcpToolNames", detail: "MCP 工具名列表" },
  { expr: "mcpToolSummaries", detail: "MCP 工具说明映射" },
  { expr: "mcpToolGroups", detail: "按 MCP server 聚合的紧凑工具目录" },
  { expr: "mcpServerInstructions", detail: "MCP server instructions 数组" },
  { expr: "tools.allowlist", detail: "工具允许清单(与 toolNames 同源)" },
  { expr: "tools.summaries", detail: "工具说明映射(嵌套结构)" },
  { expr: "tools.builtin.allowlist", detail: "内置工具允许清单" },
  { expr: "tools.builtin.summaries", detail: "内置工具说明映射" },
  { expr: "tools.mcp.allowlist", detail: "MCP 工具允许清单" },
  { expr: "tools.mcp.summaries", detail: "MCP 工具说明映射" },
  { expr: "tools.mcp.groups", detail: "按 MCP server 聚合的紧凑工具目录" },
  { expr: "tools.mcp.serverInstructions", detail: "MCP server instructions 数组" },
  { expr: "skillsPrompt", detail: "Skills 注入文本" },
  { expr: "defaultThinkLevel", detail: "默认 thinking 等级" },
  { expr: "reasoningLevel", detail: "Reasoning 当前等级" },

  // host / runtimeInfo
  { expr: "host.hostname", detail: "宿主机名" },
  { expr: "host.os", detail: "宿主 OS" },
  { expr: "host.arch", detail: "宿主架构" },
  { expr: "host.platform", detail: "平台详情" },
  { expr: "runtimeInfo.channel", detail: "当前渠道" },
  { expr: "runtimeInfo.primaryInterface", detail: "主交互界面" },
  { expr: "runtimeInfo.outputFormat", detail: "用户输出格式" },
  { expr: "runtimeInfo.web.primaryInterface", detail: "Web 主界面标识" },
  { expr: "runtimeInfo.web.outputFormat", detail: "Web 输出格式" },
  { expr: "runtimeInfo.web.rendering", detail: "Web 渲染方式" },
  { expr: "runtimeInfo.model", detail: "当前模型" },
  { expr: "runtimeInfo.host", detail: "Runtime 主机名(兼容别名)" },
  { expr: "runtimeInfo.hostname", detail: "Runtime 主机名" },
  { expr: "runtimeInfo.os", detail: "Runtime OS" },
  { expr: "runtimeInfo.arch", detail: "Runtime 架构" },
  { expr: "runtimeInfo.shell", detail: "默认 shell" },
  { expr: "outputFormat", detail: "顶层输出格式别名" },
  { expr: "templateEngine.name", detail: "模板引擎名称" },
  { expr: "templateEngine.supportedSyntax", detail: "模板引擎支持语法" },
  { expr: "availableAgents", detail: "当前可用 Agent 数组" },
  { expr: "agents.available", detail: "当前可用 Agent 数组别名" },

  // 常用 helper
  { expr: "helpers.toolLines(toolNames, toolSummaries)", detail: "渲染全部工具清单" },
  { expr: "helpers.toolLines(builtinToolNames, builtinToolSummaries)", detail: "渲染内置工具清单" },
  { expr: "helpers.toolLines(mcpToolNames, mcpToolSummaries)", detail: "渲染 MCP 工具完整清单（兼容）" },
  { expr: "helpers.mcpGroupLines(mcpToolGroups)", detail: "渲染 MCP 紧凑分组目录" },
  { expr: "helpers.literalBlock(item.instructions)", detail: "安全包裹动态多行文本，避免 Markdown 边界冲突" },
  { expr: "helpers.runtimeLine(runtimeInfo, defaultThinkLevel)", detail: "渲染 Runtime 行" },
  { expr: "helpers.has(toolNames,'OpenBearControl')", detail: "判断某个工具是否可用" },
  { expr: "helpers.join(toolNames, ', ')", detail: "数组 join 输出" },

  // memory 根结构
  { expr: "memory.expandedEntries", detail: "每轮展开到提示词的完整记忆条目" },
  { expr: "memory.byCat.memory", detail: "长期记忆条目列表" },
  { expr: "memory.byCat.tools", detail: "工具说明条目列表" },
  { expr: "memory.groupsByCat.memory", detail: "环境事实按 grp 分组" },
  { expr: "memory.groupsByCat.tools", detail: "工具分类按 grp 分组" },
  { expr: "memory.secretNames", detail: "凭证索引列表(name,note)" },
  { expr: "memory.docNames", detail: "文档索引列表(name,title)" },

  // 循环变量(模板里常见命名)
  { expr: "e.title", detail: "当前记忆条目标题" },
  { expr: "e.ref", detail: "当前记忆条目引用 key" },
  { expr: "e.body", detail: "当前记忆条目 Markdown 正文" },
  { expr: "e.fields", detail: "当前记忆条目结构化字段" },
  { expr: "e.grp", detail: "当前记忆条目分组" },
  { expr: "e.note", detail: "当前记忆条目备注" },
  { expr: "a.id", detail: "当前 Agent id" },
  { expr: "a.name", detail: "当前 Agent 名称" },
  { expr: "a.scenario", detail: "当前 Agent 适用场景" },
  { expr: "g.name", detail: "当前分组名" },
  { expr: "g.entries", detail: "当前分组内条目列表" },
  { expr: "s.name", detail: "凭证 key" },
  { expr: "s.note", detail: "凭证备注" },
  { expr: "d.name", detail: "文档 key" },
  { expr: "d.title", detail: "文档标题" },
  { expr: "item.server", detail: "当前 MCP server instructions 的服务器名" },
  { expr: "item.instructions", detail: "当前 MCP server instructions 正文" },
  { expr: "group.server", detail: "当前 MCP 工具分组的 server 名" },
  { expr: "group.toolCount", detail: "当前 MCP 工具分组的工具数" },
  { expr: "group.namespacePrefix", detail: "多工具 MCP 的公共命名空间前缀" },
  { expr: "group.exactToolName", detail: "单工具 MCP 的准确工具名" },
];

const TEMPLATE_DIRECTIVES = [
  { label: "@if", detail: "条件块", insert: "@if ${1:condition}\n  $0\n@endif" },
  { label: "@else", detail: "否则分支", insert: "@else" },
  { label: "@endif", detail: "结束条件块", insert: "@endif" },
  { label: "@each expanded memory", detail: "循环每轮展开的完整记忆", insert: "@if memory.expandedEntries.length\n@each ${1:e} in memory.expandedEntries\n### [[ ${1:e}.title ]] -- @mem/[[ ${1:e}.ref ]]\n[[ ${1:e}.body ]]\n@endeach\n@endif" },
  { label: "@each memory entry", detail: "循环记忆条目", insert: "@each ${1:e} in ${2:memory.byCat.memory}\n### [[ ${1:e}.title ]] -- @mem/[[ ${1:e}.ref ]]\n[[ ${1:e}.body ]]\n@endeach" },
  { label: "@each group", detail: "循环分组 + 组内条目", insert: "@each ${1:g} in ${2:memory.groupsByCat.memory}\n@if ${1:g}.name\n### [[ ${1:g}.name ]]\n@endif\n@each ${3:e} in ${1:g}.entries\n#### [[ ${3:e}.title ]] -- @mem/[[ ${3:e}.ref ]]\n[[ ${3:e}.body ]]\n@endeach\n@endeach" },
  { label: "@each secret index", detail: "循环凭证索引", insert: "@each ${1:s} in memory.secretNames\n- 🔑 @secret/[[ ${1:s}.name ]] — [[ ${1:s}.note ]]\n@endeach" },
  { label: "@each doc index", detail: "循环文档索引", insert: "@each ${1:d} in memory.docNames\n- 📄 @doc/[[ ${1:d}.name ]] — [[ ${1:d}.title ]]\n@endeach" },
  { label: "@endeach", detail: "结束循环块", insert: "@endeach" },
  { label: "@raw", detail: "原样输出块,不解析模板语法", insert: "@raw\n$0\n@endraw" },
  { label: "@endraw", detail: "结束原样输出块", insert: "@endraw" },
];

function getCompletionContext() {
  return (typeof window !== "undefined" && window.__mdCompletionContext) || {
    mode: "memory",
    refData: { mem: [], secret: [], doc: [] },
  };
}

function buildTemplateExpressionSuggestions(monacoApi, position, line) {
  const open = line.lastIndexOf("[[");
  if (open < 0) return null;

  // 只处理当前正在输入的 [[ ...。
  // Monaco 会把 [[ 自动补成 [[]]，某些输入路径下光标会落在闭合 ]] 后面，
  // 所以额外兼容“空表达式已闭合”的形态；否则用户刚打 [[ 就会看到 No suggestions。
  let exprPart = line.slice(open + 2);
  const closeAt = exprPart.indexOf("]]" );
  if (closeAt >= 0) {
    const beforeClose = exprPart.slice(0, closeAt);
    const afterClose = exprPart.slice(closeAt + 2);
    if (beforeClose.trim() || afterClose.trim()) return null;
    exprPart = beforeClose;
  }

  const typed = exprPart.trim().toLowerCase();
  const range = {
    startLineNumber: position.lineNumber,
    endLineNumber: position.lineNumber,
    startColumn: open + 1,
    endColumn: position.column,
  };
  const suggestions = TEMPLATE_EXPRESSIONS
    .filter((it) => !typed || it.expr.toLowerCase().includes(typed))
    .map((it, idx) => ({
      label: `[[ ${it.expr} ]]`,
      kind: it.expr.includes("(") ? monacoApi.languages.CompletionItemKind.Function : monacoApi.languages.CompletionItemKind.Variable,
      insertText: `[[ ${it.expr} ]]`,
      range,
      detail: it.detail,
      filterText: `[[ ${it.expr} ]] ${it.expr}`,
      sortText: String(idx).padStart(4, "0"),
    }));
  return { suggestions, incomplete: false };
}

function buildTemplateDirectiveSuggestions(monacoApi, position, line) {
  const m = line.match(/^(\s*)@(\S*)$/);
  if (!m) return null;
  const indent = m[1] || "";
  const typed = (m[2] || "").toLowerCase();
  const range = {
    startLineNumber: position.lineNumber,
    endLineNumber: position.lineNumber,
    startColumn: indent.length + 1,
    endColumn: position.column,
  };
  const suggestions = TEMPLATE_DIRECTIVES
    .filter((it) => !typed || it.label.toLowerCase().replace(/^@/, "").startsWith(typed))
    .map((it, idx) => ({
      label: it.label,
      kind: monacoApi.languages.CompletionItemKind.Keyword,
      insertText: it.insert,
      insertTextRules: monacoApi.languages.CompletionItemInsertTextRule.InsertAsSnippet,
      range,
      detail: it.detail,
      filterText: it.label,
      sortText: String(idx).padStart(4, "0"),
    }));
  return { suggestions, incomplete: false };
}

function buildMemoryRefSuggestions(monacoApi, position, line, data) {
  // ① @类型/<key> — 列出具体 key 候选
  const mref = line.match(/@(mem|secret|doc)\/([^\s）)，。、]*)$/);
  if (mref) {
    const type = mref[1];
    const typed = mref[2] || "";
    // 替换范围:从 @type/ 之后的已输入部分到光标
    const slashCol = position.column - typed.length;
    const refRange = { startLineNumber: position.lineNumber, endLineNumber: position.lineNumber, startColumn: slashCol, endColumn: position.column };
    const list = data[type] || [];
    const suggestions = list.map((it, idx) => {
      const label = it.key;
      const desc = type === "mem" ? (it.name || "") : (type === "secret" ? (it.note || "") : (it.title || ""));
      return {
        label: { label, description: desc },
        kind: monacoApi.languages.CompletionItemKind.Value,
        insertText: it.key,
        range: refRange,
        filterText: it.key + " " + desc,
        sortText: String(idx).padStart(4, "0"),
        detail: (type === "mem" ? "记忆" : type === "secret" ? "凭证" : "文档") + (desc ? " · " + desc : ""),
      };
    });
    return { suggestions, incomplete: false };
  }

  // ② 刚输入 @ / @m / @secret — 提示三种引用前缀
  // 注意:range 必须包含 @ 本身,insertText 也写完整 @mem/。
  // Monaco 0.55 对 markdown 里的 @ 不按普通 word 处理;如果 range 是 @ 后面的空位,
  // 候选会被内部过滤掉,界面就只剩 “No suggestions.”。
  const atPrefix = line.match(/[@＠]([A-Za-z]*)$/);
  if (atPrefix && !/[@＠](mem|secret|doc)\//.test(line)) {
    const typed = (atPrefix[1] || "").toLowerCase();
    const atRange = {
      startLineNumber: position.lineNumber,
      endLineNumber: position.lineNumber,
      startColumn: position.column - typed.length - 1,
      endColumn: position.column,
    };
    const refs = [
      { key: "mem", label: "@mem/", detail: "引用另一条记忆", filterText: "@mem/ mem memory 记忆", sortText: "0000" },
      { key: "secret", label: "@secret/", detail: "引用凭证(按需取)", filterText: "@secret/ secret credential 凭证 密码", sortText: "0001" },
      { key: "doc", label: "@doc/", detail: "引用长文档(按需取)", filterText: "@doc/ doc document 文档", sortText: "0002" },
    ].filter((it) => !typed || it.key.startsWith(typed));
    const suggestions = refs.map((it) => ({
      label: it.label,
      kind: monacoApi.languages.CompletionItemKind.Reference,
      insertText: it.label,
      range: atRange,
      filterText: it.filterText,
      sortText: it.sortText,
      detail: it.detail,
      command: { id: "editor.action.triggerSuggest", title: "继续补全 key" },
    }));
    return { suggestions, incomplete: false };
  }
  return null;
}

function registerCompletion() {
  if (window.__mdCompletionRegistered) return;
  window.__mdCompletionRegistered = true;
  monaco.languages.registerCompletionItemProvider("markdown", {
    triggerCharacters: ["[", "@", "＠", "/", "."],
    provideCompletionItems(model, position) {
      const line = model.getValueInRange({ startLineNumber: position.lineNumber, startColumn: 1, endLineNumber: position.lineNumber, endColumn: position.column });
      const { mode, refData } = getCompletionContext();
      if (mode === "none") return { suggestions: [] };

      if (mode === "template") {
        return buildTemplateExpressionSuggestions(monaco, position, line)
          || buildTemplateDirectiveSuggestions(monaco, position, line)
          || { suggestions: [] };
      }

      return buildMemoryRefSuggestions(monaco, position, line, refData || { mem: [], secret: [], doc: [] })
        || { suggestions: [] };
    },
  });
}

onMounted(() => {
  syncCompletionContext();
  registerCompletion();
  editor = monaco.editor.create(el.value, {
    value: props.modelValue,
    language: props.language,
    theme: "vs",
    fontSize: 13,
    lineHeight: 22,
    minimap: { enabled: false },
    wordWrap: "on",
    scrollBeyondLastLine: false,
    automaticLayout: true,
    padding: { top: 10, bottom: 10 },
    fontFamily: "'SF Mono', Menlo, Consolas, monospace",
    renderLineHighlight: "none",
    overviewRulerLanes: 0,
    // 关键:补全/hover 等 overflow widget 渲染到 body 顶层,
    // 否则被 Element 弹窗的 overflow-hidden 裁掉 + z-index(2002) 压住(放 modal 里的经典坑)
    fixedOverflowWidgets: true,
    // markdown 默认压制自动补全,显式打开:让 @ / [[ 触发建议
    quickSuggestions: { other: true, comments: true, strings: true },
    suggestOnTriggerCharacters: true,
    // 0.55 用字符串枚举(不是布尔!),"off" 才真正关掉"拿文档里的词当补全";
    // 否则 @mem/ 后会弹一堆正文单词(Creature/Emoji...)淹没我们的 key 列表
    wordBasedSuggestions: "off",
    suggest: { showWords: false, filterGraceful: true, snippetsPreventQuickSuggestions: false },
    tabCompletion: "on",
  });
  editor.onDidFocusEditorWidget(syncCompletionContext);
  editor.onDidChangeModelContent(() => {
    syncCompletionContext();
    suppress = true;
    emit("update:modelValue", editor.getValue());
    suppress = false;
    // Monaco 在 markdown 下非字母字符(@ / [[)不会总是自动触发补全,手动触发一把。
    try {
      const pos = editor.getPosition();
      if (pos) {
        const lineText = editor.getModel().getLineContent(pos.lineNumber);
        const charBefore = lineText.charAt(pos.column - 2); // 光标前一个字符
        const charBefore2 = lineText.slice(Math.max(0, pos.column - 3), pos.column - 1);
        if (charBefore === "@" || charBefore === "＠" || charBefore === "/" || charBefore === "[" || charBefore === "." || charBefore2 === "[[") {
          setTimeout(() => { try { editor.trigger("mdsuggest", "editor.action.triggerSuggest", {}); } catch (_) {} }, 0);
        }
      }
    } catch (_) { /* ignore */ }
  });
});

watch(() => [props.refData, props.completionMode], syncCompletionContext, { deep: true });

watch(() => props.modelValue, (v) => {
  if (!suppress && editor && v !== editor.getValue()) editor.setValue(v || "");
});

onBeforeUnmount(() => { editor?.dispose(); });
</script>

<template>
  <div ref="el" class="w-full h-full border border-macborder overflow-hidden" :class="square ? 'rounded-none' : 'rounded-lg'"></div>
</template>

<style>
/* fixedOverflowWidgets 把补全/hover widget 渲染到 body 顶层的 fixed 容器,
   z-index 必须盖过 Element 弹窗(2002),否则被压在弹窗底下看不见 */
.monaco-editor .overflowingContentWidgets,
.editor-widget.suggest-widget,
.monaco-editor .suggest-widget {
  z-index: 3000 !important;
}
</style>

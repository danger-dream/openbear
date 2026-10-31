import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import postcss from "postcss";
import {parse} from "@vue/compiler-sfc";

const source = readFileSync(new URL("./TaskMemoryDrawer.vue", import.meta.url), "utf8");
const consoleSource = readFileSync(new URL("./ConsoleView.vue", import.meta.url), "utf8");
const minimapSource = readFileSync(new URL("./TurnMinimap.vue", import.meta.url), "utf8");

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
  assert.match(consoleSource, /@open-context="contextEditor\?\.open\(\)"/);
  const template = parse(consoleSource).descriptor.template.content;
  assert.ok(template.indexOf('<ContextEditor') < template.indexOf('class="console-controls"'), 'editor overlay must not be inside the mobile-hidden rail');
  const editorStyles = readFileSync(new URL('./contextEditor/style.css', import.meta.url), 'utf8');
  assert.match(editorStyles, /@media\s*\(max-width:\s*760px\)\s*\{\s*\.context-editor-entry\s*\{\s*display:\s*none;/);
  const menu = readFileSync(new URL('./MobileConversationTools.vue', import.meta.url), 'utf8');
  assert.match(menu, /chooseAction\('open-context'\)/);
  assert.match(source, /defineExpose\(\{open: openDrawer\}\)/);
  assert.match(source, /async function openDrawer\(\) \{\s*if \(!usableConversationUuid\.value\) return;/);
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

import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";

const panelSource = readFileSync(new URL("./TurnWorkDetailPanel.vue", import.meta.url), "utf8");
const turnEventSource = readFileSync(new URL("./TurnEvent.vue", import.meta.url), "utf8");
const agentCardSource = readFileSync(new URL("./AgentEventCard.vue", import.meta.url), "utf8");

test("work detail requests Agent preview mode and TurnEvent forwards it to every Agent card", () => {
	const workEventMarkup = panelSource.match(/<TurnEvent[\s\S]*?\/>/)?.[0] || "";
	assert.match(workEventMarkup, /:agent-preview-only="true"/);
	assert.match(workEventMarkup, /:compact="true"/);
	assert.match(turnEventSource, /:compact="props\.compact"/);

	const agentCardMarkups = [...turnEventSource.matchAll(/<AgentEventCard[\s\S]*?\/>/g)].map((match) => match[0]);
	assert.equal(agentCardMarkups.length, 2);
	for (const markup of agentCardMarkups) {
		assert.match(markup, /:preview-only="props\.agentPreviewOnly"/);
	}
});

test("Agent preview renders the existing summary as a non-interactive non-details row", () => {
	const previewStart = agentCardSource.indexOf('v-if="props.previewOnly"');
	const interactiveStart = agentCardSource.indexOf("<details\n\t\tv-else", previewStart);
	assert.notEqual(previewStart, -1);
	assert.notEqual(interactiveStart, -1);
	const previewMarkup = agentCardSource.slice(previewStart, interactiveStart);

	assert.match(previewMarkup, /class="agent-summary-row agent-preview-row"/);
	assert.match(previewMarkup, /class="agent-icon"/);
	assert.match(previewMarkup, /class="agent-summary-title"/);
	assert.match(previewMarkup, /class="agent-summary-preview"/);
	assert.match(previewMarkup, /class="agent-summary-status-icon"/);
	assert.doesNotMatch(previewMarkup, /<details\b|<summary\b|@toggle\b|@click\b|disclosure-icon/);
	assert.match(agentCardSource, /\.agent-preview-row\s*\{[\s\S]*?display:\s*grid;[\s\S]*?grid-template-columns:\s*\.92rem max-content minmax\(0,\s*1fr\) \.9rem;[\s\S]*?min-height:\s*1\.45rem;[\s\S]*?align-items:\s*center;[\s\S]*?padding:\s*0;[\s\S]*?cursor:\s*default;/);
	assert.match(agentCardSource, /\.agent-preview-row \.agent-summary-status-icon\s*\{\s*justify-self:\s*end;/);

	const interactiveMarkup = agentCardSource.slice(interactiveStart, agentCardSource.indexOf("</template>", interactiveStart));
	assert.match(interactiveMarkup, /<details[\s\S]*?:open="isOpen"[\s\S]*?@toggle="onToggle"/);
	assert.match(interactiveMarkup, /<summary class="agent-summary-row">/);
	assert.match(interactiveMarkup, /class="disclosure-icon"/);
});

test("Agent preview forces closed state and cannot enter detail loading paths", () => {
	assert.match(agentCardSource, /previewOnly:\s*\{type:\s*Boolean,\s*default:\s*false\}/);
	assert.match(agentCardSource, /const isOpen = computed\(\(\) => !props\.previewOnly && props\.isDetailOpen\(detailId\.value\)\);/);
	assert.match(agentCardSource, /function prepareOpenPanel\([^)]*\)\s*\{\s*if \(props\.previewOnly\) return;/);
	assert.match(agentCardSource, /function onToggle\(event\)\s*\{\s*if \(props\.previewOnly\) return;/);
	assert.match(agentCardSource, /watch\(isOpen, \(open\) => \{\s*if \(!props\.previewOnly && open\) prepareOpenPanel/);
});

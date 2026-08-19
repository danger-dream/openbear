<script setup>
import {computed} from "vue";
import AgentActivityList from "./AgentActivityList.vue";
import {compactAgentStepActivityLines} from "./agentPlanPresentation.js";

const props = defineProps({
	sourceLines: {type: Array, default: () => []},
	modelLabel: {type: String, default: ""},
	thinkLevel: {type: String, default: ""},
	fastMode: {type: Boolean, default: false},
	limit: {type: Number, default: 0},
	emptyText: {type: String, default: "暂无过程记录。"},
});

const displayLines = computed(() => {
	const lines = compactAgentStepActivityLines(props.sourceLines, {
		modelLabel: props.modelLabel,
		thinkLevel: props.thinkLevel,
		fastMode: props.fastMode,
	});
	return props.limit > 0 ? lines.slice(-props.limit) : lines;
});
</script>

<template>
	<AgentActivityList :lines="displayLines" :empty-text="emptyText" compact/>
</template>

<script setup>
import {computed} from "vue";
import {fmtTokens} from "../views/consoleView/display.js";

// The extensions a folder gives the conversations created in it from now on.
const props = defineProps({
	modelValue: {type: Object, required: true},
	catalog: {type: Array, default: () => []},
	inherited: {type: Object, default: null},
	inheritedFrom: {type: String, default: ""},
	// A top-level folder or the temporary node has nothing above it: it only has its own setting.
	canInherit: {type: Boolean, default: true},
	disabled: {type: Boolean, default: false},
});
const emit = defineEmits(["update:modelValue"]);
const value = computed(() => props.modelValue);
const update = patch => emit("update:modelValue", {...value.value, ...patch});
const byKey = computed(() => Object.fromEntries(props.catalog.map(item => [item.key, item])));
const tokensOf = keys => keys.reduce((sum, key) => sum + Number(byKey.value[key]?.tokens || 0), 0);
// While inheriting, the list shows what comes from above and cannot be changed.
const inheriting = computed(() => props.canInherit && value.value.inherit);
const shown = computed(() => inheriting.value
	? {enabled: props.inherited?.enabled || [], autoAllow: !!props.inherited?.autoAllow} : value.value);
const source = computed(() => props.inherited?.explicit ? `「${props.inheritedFrom || "上级目录"}」` : "");
const inheritedText = computed(() => source.value ? `沿用${source.value}的设置` : "上级目录都没有设置，新会话的扩展全部关闭");
const selectedTokens = computed(() => tokensOf(shown.value.enabled));
function toggle(key, on) {
	const enabled = new Set(value.value.enabled);
	const requires = Object.fromEntries(props.catalog.map(item => [item.key, item.requires || []]));
	// An extension comes with what it requires and goes with it, as conversations store it.
	if (on) [key, ...requires[key] || []].forEach(k => enabled.add(k));
	else {
		enabled.delete(key);
		for (const [other, needs] of Object.entries(requires)) if (needs.includes(key)) enabled.delete(other);
	}
	update({enabled: props.catalog.map(item => item.key).filter(k => enabled.has(k))});
}
</script>

<template>
	<section class="property-section folder-extension-defaults" aria-labelledby="folder-extension-defaults-title">
		<header class="property-section-head">
			<h3 id="folder-extension-defaults-title">新会话默认扩展</h3>
			<p>只影响以后新建的会话；已有会话在输入框的“扩展”里单独开关。</p>
		</header>
		<label v-if="props.canInherit" class="property-switch">
			<span class="property-switch-text">
				<span>继承上级目录</span>
				<small>{{ inheritedText }}</small>
			</span>
			<el-switch :model-value="value.inherit" :disabled="props.disabled" aria-label="继承上级目录" @update:model-value="update({inherit: $event})"/>
		</label>
		<div class="folder-extension-list" :class="{'is-inherited': inheriting}" role="group" aria-labelledby="folder-extension-list-title">
			<div class="folder-extension-list-head">
				<span id="folder-extension-list-title">扩展工具</span>
				<small v-if="inheriting">继承{{ source || '上级目录' }}，不能单独修改；要单独设置，先关闭“继承上级目录”。</small>
				<small v-else>勾选的扩展会出现在新会话的工具列表里，右侧是开启后每次请求大约多占的 token。</small>
			</div>
			<label v-for="item in props.catalog" :key="item.key" class="folder-extension-row">
				<el-checkbox :model-value="shown.enabled.includes(item.key)" :disabled="props.disabled || inheriting" @update:model-value="toggle(item.key, $event)"/>
				<span class="folder-extension-text"><span>{{ item.label }}</span><small :title="item.hint">{{ item.hint }}</small></span>
				<span class="folder-extension-tokens" :title="`开启后每次请求约多 ${item.tokens} token`">≈{{ fmtTokens(item.tokens) }}</span>
			</label>
			<div class="folder-extension-total">
				<span>已选 {{ shown.enabled.length }} 项</span>
				<span>每次请求约多 {{ fmtTokens(selectedTokens) }} token</span>
			</div>
		</div>
		<label class="property-switch folder-extension-auto" :class="{'is-inherited': inheriting}">
			<span class="property-switch-text">
				<span>自动允许模型开启扩展</span>
				<small>打开后，模型申请开启未开启的扩展时直接通过，不再询问。</small>
			</span>
			<el-switch :model-value="shown.autoAllow" :disabled="props.disabled || inheriting" aria-label="自动允许模型开启扩展" @update:model-value="update({autoAllow: $event})"/>
		</label>
	</section>
</template>

<style>
.folder-extension-list { display: flex; flex-direction: column; gap: 2px; padding-top: 10px; border-top: 1px solid var(--ob-border-soft); }
.folder-extension-list-head { display: flex; flex-direction: column; gap: 2px; margin-bottom: 4px; }
.folder-extension-list-head > span { font-size: 14px; font-weight: 550; line-height: 1.5; color: var(--ob-text); }
.folder-extension-list-head small { font-size: 13px; line-height: 1.5; color: var(--ob-text-subtle); }
.folder-extension-row { display: flex; align-items: flex-start; gap: 10px; padding: 7px 6px; border-radius: 8px; cursor: pointer; }
.folder-extension-row:hover { background: var(--ob-hover); }
.folder-extension-list.is-inherited .folder-extension-row, .folder-extension-auto.is-inherited { cursor: default; }
.folder-extension-list.is-inherited .folder-extension-row:hover { background: none; }
.folder-extension-row .el-checkbox { flex: none; height: 20px; }
.folder-extension-text { display: flex; flex: 1; flex-direction: column; min-width: 0; font-size: 14px; line-height: 20px; color: var(--ob-text); }
.folder-extension-text small { display: -webkit-box; overflow: hidden; -webkit-box-orient: vertical; -webkit-line-clamp: 2; font-size: 12.5px; line-height: 1.45; color: var(--ob-text-subtle); }
.folder-extension-tokens { flex: none; font-size: 12.5px; line-height: 20px; color: var(--ob-text-subtle); font-variant-numeric: tabular-nums; }
.folder-extension-total { display: flex; justify-content: space-between; gap: 12px; padding: 6px 6px 0; font-size: 12.5px; color: var(--ob-text-subtle); font-variant-numeric: tabular-nums; }
.folder-extension-auto { padding-top: 12px; border-top: 1px solid var(--ob-border-soft); }
</style>

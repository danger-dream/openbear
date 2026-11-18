<script setup>
import {computed, onMounted, ref} from "vue";
import {Blocks} from "@lucide/vue";
import {Api} from "../api.js";
import {fmtTokens} from "../views/consoleView/display.js";

// The system default: what a new conversation starts with when neither its folder nor one above it
// (nor the temporary node) has its own setting. Each switch is saved at once, as the settings page does.
const SYSTEM = "__system";
const state = ref(null), loading = ref(true), saving = ref(""), error = ref("");
const items = computed(() => state.value?.extensions || []);
const enabled = computed(() => items.value.filter(item => item.enabled).map(item => item.key));
const autoAllow = computed(() => Boolean(state.value?.default?.autoAllow));
const explicit = computed(() => Boolean(state.value?.default?.explicit));
const selectedTokens = computed(() => items.value.filter(item => item.enabled).reduce((sum, item) => sum + Number(item.tokens || 0), 0));
const busy = computed(() => loading.value || Boolean(saving.value));

async function load() {
	loading.value = true; error.value = "";
	try { state.value = await Api.folderExtensions(SYSTEM); }
	catch (e) { error.value = e?.response?.data?.message || "扩展默认设置读取失败"; }
	finally { loading.value = false; }
}
async function save(value, key) {
	if (busy.value) return;
	saving.value = key; error.value = "";
	try { state.value = await Api.updateFolderExtensions(SYSTEM, value); }
	catch (e) { error.value = e?.response?.data?.message || "保存失败"; }
	finally { saving.value = ""; }
}
function toggle(item, on) {
	const next = new Set(enabled.value);
	const requires = Object.fromEntries(items.value.map(x => [x.key, x.requires || []]));
	// An extension comes with what it requires and goes with it, as conversations store it.
	if (on) [item.key, ...requires[item.key] || []].forEach(k => next.add(k));
	else {
		next.delete(item.key);
		for (const [other, needs] of Object.entries(requires)) if (needs.includes(item.key)) next.delete(other);
	}
	void save({enabled: items.value.map(x => x.key).filter(k => next.has(k)), autoAllow: autoAllow.value}, item.key);
}
const setAutoAllow = on => save({enabled: enabled.value, autoAllow: on}, "autoAllow");
onMounted(load);
</script>

<template>
	<section class="system-extensions" aria-labelledby="system-extensions-title" :aria-busy="busy">
		<header class="system-extensions__heading">
			<h3 id="system-extensions-title"><Blocks :size="16" aria-hidden="true" />新会话默认扩展</h3>
			<span v-if="state" class="system-extensions__state">{{ explicit ? `已开启 ${enabled.length} 项` : '未设置' }}</span>
		</header>
		<p class="system-extensions__desc">目录及其上级目录、临时会话都没有单独设置时，新会话按这里开启扩展；定时任务和 Webhook 没指定扩展时也用这里。只影响以后新建的会话，已有会话在输入框的“扩展”里单独开关。</p>
		<div v-if="loading && !state" class="system-extensions__empty">正在读取…</div>
		<template v-else-if="state">
			<ul class="system-extensions__list">
				<li v-for="item in items" :key="item.key" class="system-extensions__row">
					<div class="system-extensions__text"><span>{{ item.label }}</span><small :title="item.hint">{{ item.hint }}</small></div>
					<span class="system-extensions__tokens" :title="`开启后每次请求约多 ${item.tokens} token`">≈{{ fmtTokens(item.tokens) }}</span>
					<el-switch :model-value="item.enabled" :loading="saving === item.key" :disabled="busy" :aria-label="`${item.label}扩展`" @update:model-value="toggle(item, $event)"/>
				</li>
			</ul>
			<div class="system-extensions__row system-extensions__auto">
				<div class="system-extensions__text"><span>自动允许模型开启扩展</span><small>打开后，模型申请开启未开启的扩展时直接通过，不再询问。</small></div>
				<el-switch :model-value="autoAllow" :loading="saving === 'autoAllow'" :disabled="busy" aria-label="自动允许模型开启扩展" @update:model-value="setAutoAllow"/>
			</div>
		</template>
		<p v-if="error" class="system-extensions__error" role="alert">{{ error }}<button v-if="!state" type="button" @click="load">重新读取</button></p>
		<footer v-if="state" class="system-extensions__foot">
			<span>{{ explicit ? `已选 ${enabled.length} 项` : '还没有设置，新会话的扩展全部关闭' }}</span>
			<span>每次请求约多 {{ fmtTokens(selectedTokens) }} token</span>
		</footer>
	</section>
</template>

<style>
.system-extensions { overflow: hidden; margin: 0 0 14px; border: 1px solid var(--ob-border); border-radius: 12px; background: var(--ob-surface); }
.system-extensions__heading { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px; padding: 12px 16px; border-bottom: 1px solid var(--ob-border); background: var(--ob-hover); }
.system-extensions__heading h3 { display: flex; align-items: center; gap: 8px; margin: 0; color: var(--ob-text-strong); font-size: 13px; font-weight: 600; }
.system-extensions__state { flex-shrink: 0; padding: 3px 8px; border: 1px solid var(--ob-border); border-radius: 4px; color: var(--ob-text-subtle); background: var(--ob-surface-soft); font-size: 11px; line-height: 1.4; }
.system-extensions__desc { margin: 0; padding: 12px 16px 6px; color: var(--ob-text-subtle); font-size: 12px; line-height: 1.65; overflow-wrap: anywhere; }
.system-extensions__list { margin: 0; padding: 0 8px; list-style: none; }
.system-extensions__row { display: flex; align-items: center; gap: 12px; min-height: 44px; padding: 7px 8px; border-radius: 8px; }
.system-extensions__list .system-extensions__row:hover { background: var(--ob-hover); }
.system-extensions__text { display: flex; flex: 1; flex-direction: column; min-width: 0; }
.system-extensions__text > span { color: var(--ob-text); font-size: 13px; line-height: 1.5; }
.system-extensions__text small { display: -webkit-box; overflow: hidden; -webkit-box-orient: vertical; -webkit-line-clamp: 2; color: var(--ob-text-subtle); font-size: 12px; line-height: 1.45; }
.system-extensions__tokens { flex: none; color: var(--ob-text-subtle); font-size: 12px; font-variant-numeric: tabular-nums; }
.system-extensions__row .el-switch { flex: none; }
.system-extensions__auto { margin: 4px 8px 0; padding: 10px 8px 7px; border-top: 1px solid var(--ob-border-soft); border-radius: 0; }
.system-extensions__empty { padding: 10px 16px 14px; color: var(--ob-text-muted); font-size: 12px; }
.system-extensions__error { display: flex; align-items: center; gap: 10px; margin: 6px 16px 0; color: var(--ob-danger); font-size: 12px; }
.system-extensions__error button { border: 0; background: transparent; color: var(--ob-blue); font: inherit; cursor: pointer; }
.system-extensions__foot { display: flex; justify-content: space-between; flex-wrap: wrap; gap: 4px 12px; margin-top: 8px; padding: 10px 16px 12px; border-top: 1px solid var(--ob-border); background: rgb(var(--ob-surface-soft-rgb) / 0.35); color: var(--ob-text-subtle); font-size: 11px; font-variant-numeric: tabular-nums; }
@media (max-width: 760px) {
	.system-extensions__heading { padding: 12px 13px; }
	.system-extensions__desc { padding: 11px 13px 4px; }
	.system-extensions__list { padding: 0 5px; }
	.system-extensions__row { min-height: 52px; }
	.system-extensions__auto { margin: 4px 5px 0; }
	.system-extensions__text > span { font-size: 14px; }
	.system-extensions__foot { padding: 10px 13px 12px; }
	.system-extensions__error { margin: 6px 13px 0; }
}
</style>

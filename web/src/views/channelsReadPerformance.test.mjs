import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const appSource = readFileSync(new URL("../App.vue", import.meta.url), "utf8");
const channelsSource = readFileSync(new URL("./ChannelsView.vue", import.meta.url), "utf8");

function sourceBetween(source, startText, endText) {
  const start = source.indexOf(startText);
  const end = source.indexOf(endText, start);
  assert.ok(start >= 0 && end > start, `source block ${startText} exists`);
  return source.slice(start, end);
}

test("App channel count uses config-only settings and preserves fallback text", async () => {
  const block = sourceBetween(appSource, "async function refreshChannelStats()", "const memoryType",);
  assert.match(block, /Api\.settings\(\)/);
  assert.doesNotMatch(block, /Api\.channels\(\)/);

  async function renderWith(response) {
    const context = {
      channelStatsText: { value: "系统就绪" },
      Api: { settings: async () => {
        if (response instanceof Error) throw response;
        return response;
      } },
    };
    const refresh = vm.runInNewContext(`${block}; refreshChannelStats`, context);
    await refresh();
    return context.channelStatsText.value;
  }

  assert.equal(await renderWith({ providerCount: 3 }), "3 渠道就绪");
  assert.equal(await renderWith({ providerCount: 0 }), "系统就绪");
  assert.equal(await renderWith(new Error("offline")), "系统就绪");
});

test("ChannelsView reuses full list rows for first detail and channel switches", async () => {
  const helperBlock = sourceBetween(channelsSource, "function listedProviderDetail(name)", "function okOrThrow",);
  const loadBlock = sourceBetween(channelsSource, "async function loadProvider(name)", "function openCreateProvider",);
  let detailRequests = 0;
  const first = { name: "first", models: [{ id: "one" }] };
  const second = { name: "second", models: [{ id: "two" }] };
  const context = {
    providers: { value: [first, second] },
    primaryModel: { value: "first/one" },
    modelsDev: { value: { available: true } },
    selectedName: { value: "" },
    detail: { value: null },
    detailLoading: { value: false },
    Api: { channel: async () => { detailRequests += 1; throw new Error("detail request should not run"); } },
    okOrThrow: (value) => value,
    ElMessage: { error: () => {} },
    apiError: (error) => String(error),
  };
  const functions = vm.runInNewContext(
    `${helperBlock}\n${loadBlock}; ({ listedProviderDetail, loadProvider })`,
    context,
  );

  await functions.loadProvider("first");
  assert.equal(context.detail.value.provider, first);
  await functions.loadProvider("second");
  assert.equal(context.detail.value.provider, second);
  assert.equal(context.selectedName.value, "second");
  assert.equal(detailRequests, 0);
});

test("ChannelsView retains detail endpoint fallback for an older list response", async () => {
  const helperBlock = sourceBetween(channelsSource, "function listedProviderDetail(name)", "function okOrThrow",);
  const loadBlock = sourceBetween(channelsSource, "async function loadProvider(name)", "function openCreateProvider",);
  let detailRequests = 0;
  const response = { ok: true, primaryModel: "legacy/model", provider: { name: "legacy", models: [] } };
  const context = {
    providers: { value: [{ name: "legacy" }] },
    primaryModel: { value: "" },
    modelsDev: { value: {} },
    selectedName: { value: "" },
    detail: { value: null },
    detailLoading: { value: false },
    Api: { channel: async () => { detailRequests += 1; return response; } },
    okOrThrow: (value) => value,
    ElMessage: { error: () => {} },
    apiError: (error) => String(error),
  };
  const { loadProvider } = vm.runInNewContext(
    `${helperBlock}\n${loadBlock}; ({ listedProviderDetail, loadProvider })`,
    context,
  );

  await loadProvider("legacy");
  assert.equal(detailRequests, 1);
  assert.equal(context.detail.value, response);
  assert.equal(context.primaryModel.value, "legacy/model");
  assert.equal(context.detailLoading.value, false);
});

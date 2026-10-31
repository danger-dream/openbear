import {computed, reactive, ref} from 'vue';
import {clone, configErrors, defaultConfig, mergeConfig, serializableDraft, submittedConfig} from './webhookConfig.js';

// The key component reads credentials separately; editor drafts and snapshots never hold them.
export function createWebhookEditor(api, {requestId = () => crypto.randomUUID(), defaultName = () => ''} = {}) {
  const draft = reactive({name: '', description: '', enabled: false, config: defaultConfig()});
  const endpoint = ref(null), environment = ref({}), loading = ref(false), saving = ref(false);
  const error = ref(''), conflict = ref(null), errors = ref([]);
  const scope = ref({}), baseline = ref(''), loaded = ref(false);
  let generation = 0, abort = null, write = null, disposed = false;
  const dirty = computed(() => loaded.value && serializableDraft(draft) !== baseline.value);
  const message = exception => exception?.response?.data?.message || exception?.response?.data?.error || exception?.message || '请求失败，请重试';
  function adopt(value) {
    // Explicit projection avoids a credential accidentally becoming a reactive ordinary field.
    const {key: _key, credentialKey: _credentialKey, ...visible} = value || {};
    if (visible.credential) { const {key: _secret, ...summary} = visible.credential; visible.credential = summary; }
    endpoint.value = value ? visible : null;
    draft.name = value?.name || defaultName(); draft.description = value?.description || ''; draft.enabled = Boolean(value?.enabled);
    draft.config = mergeConfig(defaultConfig(scope.value), value?.config || {});
    if (!value?.config?.target && value?.target) draft.config.target = mergeConfig(defaultConfig(scope.value).target,value.target);
    baseline.value = serializableDraft(draft); errors.value = []; conflict.value = null; write = null; loaded.value = true;
  }
  async function load(nextScope) {
    if (saving.value) return false;
    const current = ++generation; abort?.abort(); abort = new AbortController();
    scope.value = clone(nextScope); loading.value = true; loaded.value = false; error.value = ''; endpoint.value = null;
    try {
      const [data, env] = await Promise.all([
        api.webhooks({scopeType: nextScope.type, scopeId: nextScope.id}, {signal: abort.signal}),
        api.webhookEnvironment({signal: abort.signal}),
      ]);
      if (disposed || current !== generation) return false;
      const found = data.items?.[0];
      const detail = found ? await api.webhook(found.id, {signal: abort.signal}) : null;
      if (disposed || current !== generation) return false;
      environment.value = env; adopt(detail?.endpoint || null); return true;
    } catch (exception) {
      if (current === generation && !disposed && !abort.signal.aborted) error.value = message(exception);
      return false;
    } finally { if (current === generation && !disposed) loading.value = false; }
  }
  async function save(enable, {confirm} = {}) {
    if (saving.value || loading.value || !loaded.value) return false;
    const enabled = enable === undefined ? draft.enabled : Boolean(enable);
    errors.value = configErrors(draft.config, {enabled, environment: environment.value, scope: scope.value});
    if (errors.value.length) return false;
    const payload = {name: draft.name.trim() || defaultName(), description: draft.description, config: submittedConfig(draft.config), enabled,
      ...(endpoint.value ? {expectedRevision: endpoint.value.revision, expectedControlRevision: endpoint.value.controlRevision} : {scope: clone(scope.value)})};
    const submittedDraft = serializableDraft(draft), current = generation, id = endpoint.value?.id;
    const fingerprint = JSON.stringify(payload);
    if (!write || write.fingerprint !== fingerprint) write = {fingerprint, requestId: requestId()};
    payload.requestId = write.requestId;
    saving.value = true; error.value = ''; conflict.value = null;
    try {
      if (confirm && !await confirm({id,config:clone(payload.config)})) return false;
      if (disposed || current !== generation) return false;
      const data = id ? await api.updateWebhook(id, payload) : await api.createWebhook(payload);
      if (data.ok === false) throw new Error(data.message || data.error || '保存失败');
      if (disposed || current !== generation) return false;
      const laterDraft = serializableDraft(draft) !== submittedDraft ? clone(draft) : null;
      adopt(data.endpoint);
      if (laterDraft) Object.assign(draft, laterDraft);
      return true;
    } catch (exception) {
      if (disposed || current !== generation) return false;
      error.value = message(exception);
      const detail = exception?.response?.data?.details;
      if (detail?.field && exception?.response?.data?.code === 'system_limit_exceeded') {
        const path = detail.field;
        errors.value = [{path, tab: path.startsWith('statistics.') ? 'statistics' : path.startsWith('pre.') || path.startsWith('post.') ? 'scripts' : path.startsWith('limits.') ? 'advanced' : 'batching',
          message: `此项超过当前系统上限 ${path.endsWith('Bytes') ? detail.maximum / (1024 * 1024) + ' MB' : detail.maximum}，草稿已保留，请调整后保存`}];
      }
      if (exception?.response?.status === 409) conflict.value = exception.response.data;
      return false; // Keep both the full draft and request ID for a safe retry after response loss.
    } finally { if (!disposed && current === generation) saving.value = false; }
  }
  async function prepareMerge() {
    if (!endpoint.value || saving.value || loading.value) return false;
    const current = generation, id = endpoint.value.id;
    saving.value = true; error.value = '';
    try {
      const data = await api.webhook(id);
      if (disposed || current !== generation) return false;
      if (!data.endpoint) throw new Error('入口已不存在，请保留草稿后重新加载');
      const local = clone(draft);
      adopt(data.endpoint); Object.assign(draft, local);
      conflict.value = {currentRevision: endpoint.value.revision, mergeReady: true, details: {current: clone(endpoint.value)}};
      return true; // Explicit read only; the user must review and save the merged draft separately.
    } catch (exception) { if (!disposed && current === generation) error.value = message(exception); return false; }
    finally { if (!disposed && current === generation) saving.value = false; }
  }
  async function control(action, extra = {}) {
    if (!endpoint.value || saving.value) return false;
    saving.value = true; error.value = '';
    try {
      const data = await api.controlWebhook(endpoint.value.id, {action, requestId: requestId(), expectedControlRevision: endpoint.value.controlRevision, ...extra});
      const value = data.endpoint;
      if (value) {
        // Immediate controls must neither overwrite rule edits nor become reversible by discarding them.
        endpoint.value = value;
        const previous = JSON.parse(baseline.value); previous.enabled = value.enabled;
        baseline.value = JSON.stringify(previous); draft.enabled = value.enabled;
      }
      return data;
    } catch (exception) { error.value = message(exception); return false; }
    finally { saving.value = false; }
  }
  function discard() { if (baseline.value) Object.assign(draft, clone(JSON.parse(baseline.value))); errors.value = []; conflict.value = null; }
  function dispose() { disposed = true; generation++; abort?.abort(); }
  return {draft, endpoint, scope, environment, loading, loaded, saving, dirty, error, errors, conflict,
    load, save, prepareMerge, control, discard, dispose, adopt, message};
}

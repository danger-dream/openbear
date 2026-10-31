import {computed, reactive, ref} from 'vue';
import {clone, draftOf, message, schedulePayload, scheduleErrors, validateDraft, parseTokenBudget} from './cronConfig.js';

// Retry the same user intent with the same requestId, including a lost response.
export function createIntents(requestId = () => crypto.randomUUID()) {
  const entries = new Map();
  return {
    body(key, payload) {
      const fingerprint = JSON.stringify(payload), previous = entries.get(key);
      if (!previous || previous.fingerprint !== fingerprint) entries.set(key,{fingerprint,id:requestId()});
      return {...payload,requestId:entries.get(key).id};
    },
    clear(key) { entries.delete(key); },
  };
}
export function createQuery(fetcher) {
  const data = ref(null), loading = ref(false), error = ref(''); let generation = 0;
  async function load(params, {keepData = false} = {}) {
    const current = ++generation; loading.value = true; error.value = ''; if (!keepData) data.value = null;
    try { const result = await fetcher(params); if (current !== generation) return false; data.value = result; return true; }
    catch (exception) { if (current === generation) error.value = message(exception); return false; }
    finally { if (current === generation) loading.value = false; }
  }
  function clear() { generation++;data.value=null;error.value='';loading.value=false; }
  return {data,loading,error,load,clear,dispose:clear};
}
export function createCronEditor(api, {requestId} = {}) {
  const draft = reactive(draftOf(null)), job = ref(null), loaded = ref(false), loading = ref(false), saving = ref(false);
  const environment = ref({}), models = ref([]), folder = ref({}), folders = ref([]), inheritanceLoading = ref(false), inheritanceError = ref('');
  const errors = ref([]), error = ref(''), conflict = ref(null), baseline = ref('');
  const dirty = computed(() => loaded.value && JSON.stringify(draft) !== baseline.value);
  const intents = createIntents(requestId); let generation = 0, folderGeneration = 0, disposed = false;
  const preview = createQuery(schedule => api.cronPreview({schedule: schedulePayload(schedule),count:5}));
  function adopt(value, folderId = '') {
    job.value = value; Object.assign(draft,draftOf(value,folderId)); baseline.value=JSON.stringify(draft); loaded.value=true;
    errors.value=[];conflict.value=null;intents.clear('save');
  }
  async function loadInheritance(id = draft.folderId) {
    const current = ++folderGeneration; folder.value={};inheritanceError.value='';inheritanceLoading.value=Boolean(id);
    if (!id) return;
    try { const data = await api.conversationFolderProperties(id); if (!disposed && current === folderGeneration) folder.value = data.runDefaults || {}; }
    catch (exception) { if (!disposed && current === folderGeneration) inheritanceError.value=message(exception); }
    finally { if (!disposed && current === folderGeneration) inheritanceLoading.value=false; }
  }
  async function load(id = '', folderId = '', initial = null) {
    if (saving.value) return false;
    const current = ++generation; loading.value=true;loaded.value=false;error.value='';
    try {
      const [detail, env, opts, dirs] = await Promise.all([id ? api.cronJob(id) : null,api.cronEnvironment(),api.rathOptions(),api.cronFolders()]);
      if (disposed || current !== generation) return false;
      environment.value=env;models.value=opts.models || [];folders.value=dirs.items || [];adopt(detail?.job || null,folderId);
      if (!id && initial?.draft) {
        Object.assign(draft,clone(initial.draft));
        if (!initial.dirty) baseline.value=JSON.stringify(draft);
      }
      await loadInheritance(); return !disposed && current === generation;
    } catch (exception) { if (!disposed && current === generation) error.value=message(exception);return false; }
    finally { if (!disposed && current === generation) loading.value=false; }
  }
  async function previewNext() {
    const issues = scheduleErrors(draft.config.schedule);
    errors.value=issues;
    if (issues.length) return false;
    return preview.load(clone(draft.config.schedule));
  }
  function validateField(path) {
    const issues = validateDraft(draft,{models:models.value,folder:folder.value,environment:environment.value});
    errors.value = [...errors.value.filter(item => item.path !== path), ...issues.filter(item => item.path === path)];
  }
  async function save({confirm} = {}) {
    if (!loaded.value || saving.value || loading.value || inheritanceLoading.value || inheritanceError.value) return false;
    errors.value=validateDraft(draft,{models:models.value,folder:folder.value,environment:environment.value});
    if (errors.value.length || (conflict.value && !conflict.value.mergeReady)) return false;
    const id=job.value?.id, current=generation, submitted=JSON.stringify(draft);
    const config=clone(draft.config);config.schedule=schedulePayload(config.schedule);config.totalTokensBudget=parseTokenBudget(config.totalTokensBudget);
    if (config.allowServerChanges === false) delete config.allowServerChanges;
    // An unset retention is omitted so the payload stays valid for a backend without the field.
    if (config.conversationRetention.keepLast == null && config.conversationRetention.keepDays == null) delete config.conversationRetention;
    const body=intents.body('save',{name:draft.name.trim(),description:draft.description,enabled:draft.enabled,config,...(id ? {expectedRevision:job.value.revision} : {folderId:draft.folderId})});
    saving.value=true;error.value='';
    try {
      if (confirm && !await confirm({id,config:clone(config)})) return false;
      if (disposed || current !== generation) return false;
      const data = id ? await api.updateCronJob(id,body) : await api.createCronJob(body);
      if (disposed || current !== generation) return false;
      const later=JSON.stringify(draft) !== submitted ? clone(draft) : null;
      adopt(data.job);if (later) Object.assign(draft,later);return true;
    } catch (exception) {
      if (!disposed && current === generation) { error.value=message(exception);if (exception?.response?.status === 409) conflict.value=exception.response.data; }
      return false;
    } finally { if (!disposed && current === generation) saving.value=false; }
  }
  async function prepareMerge() {
    if (!job.value || saving.value || loading.value) return false;
    const current=generation;saving.value=true;error.value='';
    try {
      const data=await api.cronJob(job.value.id);
      if (disposed || current !== generation) return false;
      const local=clone(draft);adopt(data.job);Object.assign(draft,local);
      conflict.value={mergeReady:true,currentRevision:data.job.revision,current:clone(data.job)};
      return true;
    } catch (exception) { if (!disposed && current === generation) error.value=message(exception);return false; }
    finally { if (!disposed && current === generation) saving.value=false; }
  }
  function dispose() { disposed=true;generation++;folderGeneration++;preview.dispose(); }
  return {draft,job,loaded,loading,saving,environment,models,folder,folders,inheritanceLoading,inheritanceError,errors,error,conflict,dirty,preview,load,loadInheritance,validateField,save,prepareMerge,previewNext,dispose};
}
export function createCronActions(api, {requestId} = {}) {
  const busy=ref(false), error=ref(''), preparation=ref(null), intents=createIntents(requestId);let disposed=false;
  async function perform(action, job, extra = {}) {
    if (busy.value) return null;
    busy.value=true;error.value='';const key=`${action}:${job.id}`;
    try {
      const payload=action === 'delete' ? {confirmationToken:extra.confirmationToken} : {expectedRevision:job.revision,...extra};
      const method={control:'controlCronJob',run:'runCronJob',delete:'deleteCronJob'}[action];
      const data=await api[method](job.id,intents.body(key,payload));
      if (disposed) return null;
      intents.clear(key);if(action === 'delete')preparation.value=null;return data;
    } catch (exception) { if (!disposed) error.value=message(exception);return null; }
    finally { if (!disposed) busy.value=false; }
  }
  async function prepareDelete(job) {
    if(busy.value)return false;
    busy.value=true;error.value='';preparation.value=null;
    try { const data=await api.cronDeleteImpact(job.id,{expectedRevision:job.revision});if(disposed)return false;preparation.value={...data,job:clone(job)};return true; }
    catch(exception){if(!disposed)error.value=message(exception);return false;}
    finally{if(!disposed)busy.value=false;}
  }
  async function deletePrepared() {
    const prepared=preparation.value;
    if(!prepared?.confirmationToken)return null;
    return perform('delete',prepared.job,{confirmationToken:prepared.confirmationToken});
  }
  return {busy,error,preparation,perform,prepareDelete,deletePrepared,dispose(){disposed=true;}};
}

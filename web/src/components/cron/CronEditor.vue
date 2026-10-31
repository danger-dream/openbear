<script setup>
import {computed,nextTick,onBeforeUnmount,ref,watch} from 'vue';
import {ElMessage,ElMessageBox} from 'element-plus';
import {Api} from '../../api.js';
import ModelSettings from '../ModelSettings.vue';
import AdaptiveMdEditor from '../AdaptiveMdEditor.vue';
import Field from '../webhooks/WebhookField.vue';
import CronScriptEditor from './CronScriptEditor.vue';
import CronDateTime from './CronDateTime.vue';
import {createCronEditor} from './useCron.js';
import {TABS,INTERVAL_UNITS,timezoneOptions,intervalUnitFor,overrides,effectiveModel,inheritedModel,changeModel,tabKey,time} from './cronConfig.js';
import './cron.css';
const props=defineProps({jobId:{type:String,default:''},folderId:{type:String,default:''},initial:{type:Object,default:null}});
const emit=defineEmits(['close','saved']);
const editor=createCronEditor(Api);
const {draft,job,loaded,loading,saving,dirty,error,errors,conflict,environment,models,folder,folders,inheritanceLoading,inheritanceError}=editor;
const {data:previewData,loading:previewLoading,error:previewError}=editor.preview;
const tab=ref('basic'),phase=ref('pre'),root=ref(null);
const effective=computed(()=>effectiveModel(draft.config.runConfig,folder.value,models.value));
const inherited=computed(()=>inheritedModel(draft.config.runConfig,folder.value,models.value));
const errorFor=path=>errors.value.find(item=>item.path===path)?.message;
const disabled=computed(()=>saving.value || loading.value);
const localTimezone=Intl.DateTimeFormat().resolvedOptions().timeZone;
const timezones=computed(()=>timezoneOptions(draft.config.schedule.timezone));
const intervalUnit=ref(1);
const intervalAmount=computed({
  get:()=>draft.config.schedule.everySeconds == null ? null : draft.config.schedule.everySeconds / intervalUnit.value,
  set:value=>{draft.config.schedule.everySeconds=value == null ? null : Number((value * intervalUnit.value).toPrecision(15));},
});
function setIntervalUnit(value){const amount=intervalAmount.value;intervalUnit.value=value;intervalAmount.value=amount;}
watch(loaded,ready=>{if(ready)intervalUnit.value=intervalUnitFor(draft.config.schedule.everySeconds);},{flush:'sync'});
function setModel(field,selection){draft.config.runConfig=changeModel(draft.config.runConfig,field,selection);}
async function navigateTabs(event){tab.value=tabKey(event,tab.value);await nextTick();if(['ArrowLeft','ArrowRight','Home','End'].includes(event.key))root.value?.querySelector(`[data-tab="${tab.value}"]`)?.focus();}
function retentionNeedsPreview(id,next){
  const saved=job.value?.config?.conversationRetention || {};
  const values=[next.keepLast,next.keepDays];
  if(!id || values.every(value=>value==null) || values.some(value=>value!=null && !(Number.isInteger(value) && value>0)))return false;
  return next.keepLast!==(saved.keepLast ?? null) || next.keepDays!==(saved.keepDays ?? null);
}
async function confirmRetention({id,config}){
  const next=config.conversationRetention || {};
  if(!retentionNeedsPreview(id,next))return true;
  const {count}=await Api.previewConversationRetention({kind:'cron',id,retention:{keepLast:next.keepLast ?? null,keepDays:next.keepDays ?? null}});
  if(!count)return true;
  try{await ElMessageBox.confirm(`保存后会立即删除这个任务创建的 ${count} 个旧会话，删除后不可恢复。`,'确认会话保留',{type:'warning',confirmButtonText:'保存并清理',cancelButtonText:'取消',closeOnClickModal:false});return true;}
  catch{return false;}
}
async function save(){
  if(await editor.save({confirm:confirmRetention})){ElMessage.success('定时任务已保存');emit('saved',job.value);return true;}
  if(errors.value.length){
    const first=errors.value[0];tab.value=first.tab;if(tab.value==='scripts')phase.value=first.path.split('.')[0];
    await nextTick();root.value?.querySelector(`[data-field="${first.path}"] input`)?.focus();
  }
  return false;
}
let leaveCheck=null;
async function canLeave(){
  if(saving.value)return false;
  if(!dirty.value)return true;
  if(!leaveCheck)leaveCheck=ElMessageBox.confirm('尚有未保存的定时任务配置，确认放弃？','离开编辑器',{type:'warning',confirmButtonText:'放弃修改',cancelButtonText:'继续编辑',closeOnClickModal:false})
    .then(()=>!saving.value,()=>false).finally(()=>{leaveCheck=null;});
  return leaveCheck;
}
async function close(done){if(!await canLeave())return;if(typeof done==='function')done();emit('close');}
function beforeUnload(event){if(dirty.value || saving.value){event.preventDefault();event.returnValue='';}}
watch(()=>JSON.stringify(draft.config.schedule),()=>editor.preview.clear());
editor.load(props.jobId,props.folderId,props.initial);
window.addEventListener('beforeunload',beforeUnload);
onBeforeUnmount(()=>{editor.dispose();window.removeEventListener('beforeunload',beforeUnload);});
defineExpose({canLeave,saving});
</script>
<template>
  <el-dialog :model-value="true" :title="job ? `编辑 · ${job.name}` : '新建定时任务'" width="min(900px, calc(100vw - 24px))" append-to-body class="cron-dialog cron-root mobile-viewport-dialog" :before-close="close" :close-on-click-modal="false" :close-on-press-escape="!saving" :show-close="!saving">
    <div ref="root" class="cron-editor">
      <nav class="cron-tabs" role="tablist" aria-label="定时任务配置" @keydown="navigateTabs"><button v-for="[key,title] in TABS" :id="`cron-tab-${key}`" :key="key" type="button" role="tab" :data-tab="key" :aria-selected="tab===key" :aria-controls="`cron-panel-${key}`" :tabindex="tab===key?0:-1" @click="tab=key">{{ title }}</button></nav>
      <div class="cron-editor-scroll" :class="{'is-instructions':tab==='instructions'}">
        <el-skeleton v-if="loading" :rows="7" animated />
        <div v-if="error" class="cron-alert" role="alert">{{ error }}<el-button v-if="!loaded" link @click="editor.load(jobId,folderId,initial)">重新加载</el-button></div>
        <div v-if="conflict" class="cron-alert" role="alert"><strong>版本冲突，草稿已保留。</strong><p>{{ conflict.mergeReady ? `已读取最新版本 v${conflict.currentRevision}，下次保存将以本地完整草稿覆盖该版本。请对照下方最新内容，再决定保存。` : '不会自动覆盖他人修改。请先读取最新版本，对比后再保存。' }}</p><el-button :disabled="saving" @click="editor.prepareMerge">读取最新版本，保留本地草稿</el-button><details v-if="conflict.current"><summary>查看服务器最新配置</summary><pre>{{ JSON.stringify(conflict.current,null,2) }}</pre></details></div>
        <div v-if="errors.length" class="cron-alert" role="alert"><ul><li v-for="item in errors" :key="item.path">{{ item.message }}</li></ul></div>
        <template v-if="loaded && !loading">
          <section v-show="tab==='basic'" id="cron-panel-basic" role="tabpanel" aria-labelledby="cron-tab-basic" class="cron-section cron-basic">
            <div class="cron-toolbar cron-plan-status"><p class="cron-note">每次在所属目录新建会话；启停仅影响未来计划。</p><el-switch v-model="draft.enabled" :disabled="disabled" active-text="启用计划" /></div>
            <div class="cron-grid">
              <Field v-model="draft.name" label="任务名称" :disabled="disabled" :error="errorFor('name')" />
              <Field v-model="draft.folderId" path="folderId" label="所属目录" required filterable placeholder="请选择所属目录" :disabled="disabled || Boolean(job)" :options="folders.map(item=>({value:item.id,label:item.path || item.name}))" :error="errorFor('folderId')" @blur="editor.validateField('folderId')" @update:model-value="editor.loadInheritance($event);editor.validateField('folderId')" />
            </div>
            <Field v-model="draft.description" label="说明" :disabled="disabled" />
            <p v-if="inheritanceError" class="cron-alert" role="alert">目录继承加载失败：{{ inheritanceError }} <el-button link @click="editor.loadInheritance()">重试</el-button></p>
            <ModelSettings class="cron-model-settings" :model-value="overrides(draft.config.runConfig)" :effective="effective" :inherited="inherited" :models="models" inheritable repair-unsupported :show-agent="false" :show-strategy="false" :disabled="disabled || inheritanceLoading || Boolean(inheritanceError)" @change="setModel" />
            <div class="cron-toolbar cron-model-caption"><p class="cron-note">模型设置逐项继承目录及系统。</p><el-button link :disabled="disabled" @click="draft.config.runConfig={mainModel:null,mainThinkingLevel:null,mainFastMode:null}">恢复继承</el-button></div>
            <div class="cron-grid cron-three">
              <Field v-model="draft.config.timeoutSeconds" path="timeoutSeconds" label="单次执行超时" type="number" placeholder="例如 600" unit="秒" :disabled="disabled" :error="errorFor('timeoutSeconds')" @blur="editor.validateField('timeoutSeconds')" @update:model-value="editor.validateField('timeoutSeconds')" />
              <Field v-model="draft.config.totalTokensBudget" path="totalTokensBudget" label="总 Tokens 预算" placeholder="例如 10m、100m、1024k" hint="k = 千，m = 百万，b = 十亿；不区分大小写。" :disabled="disabled" :error="errorFor('totalTokensBudget')" @blur="editor.validateField('totalTokensBudget')" @update:model-value="editor.validateField('totalTokensBudget')" />
              <Field v-model="draft.config.costBudgetUsd" label="金额预算" type="number" placeholder="例如 5" unit="USD" :disabled="disabled" :error="errorFor('costBudgetUsd')" />
            </div>
            <p class="cron-note">留空不限制。超时覆盖前置和模型，后置独立限时；预算包含子 Agent，缺失费用忽略，已发出调用可能超额。</p>
            <div class="cron-grid">
              <Field v-model="draft.config.notifications.mode" label="完成通知" :disabled="disabled" :options="[{value:'inherit',label:'继承系统会话通知'},{value:'silent',label:'静默'},{value:'telegram',label:'Telegram'},{value:'push',label:'Push 推送'},{value:'both',label:'Telegram + Push'}]" />
              <Field label="通知范围"><el-checkbox v-model="draft.config.notifications.errorsOnly" :disabled="disabled || draft.config.notifications.mode==='silent'">仅异常通知</el-checkbox></Field>
            </div>
            <p class="cron-note">后置结束后通知；所选渠道须已在系统中配置。</p>
            <div class="cron-grid">
              <Field v-model="draft.config.conversationRetention.keepLast" path="conversationRetention.keepLast" label="会话保留最近" type="number" nullable :step="1" :min="1" unit="个" :disabled="disabled" :error="errorFor('conversationRetention.keepLast')" />
              <Field v-model="draft.config.conversationRetention.keepDays" path="conversationRetention.keepDays" label="会话保留天数" type="number" nullable :step="1" :min="1" unit="天" :disabled="disabled" :error="errorFor('conversationRetention.keepDays')" />
            </div>
            <p class="cron-note">超过任一条件的旧会话会自动删除，不可恢复；留空不清理。置顶、归档、移出本目录或你发过消息的会话会保留。</p>
          </section>
          <section v-show="tab==='schedule'" id="cron-panel-schedule" role="tabpanel" aria-labelledby="cron-tab-schedule" class="cron-section">
            <div class="cron-grid"><Field v-model="draft.config.schedule.kind" label="时间规则" :disabled="disabled" :options="[{value:'at',label:'一次性'},{value:'every',label:'固定间隔'},{value:'cron',label:'Cron 表达式'}]" /><Field v-model="draft.config.schedule.timezone" path="schedule.timezone" label="规则时区" filterable :options="timezones" :disabled="disabled" placeholder="搜索并选择时区" :error="errorFor('schedule.timezone')" @blur="editor.validateField('schedule.timezone')" @update:model-value="editor.validateField('schedule.timezone')" /></div>
            <CronDateTime v-if="draft.config.schedule.kind==='at'" v-model="draft.config.schedule.at" label="执行时间" :hint="`按本地时区 ${localTimezone} 选择，自动保存时区信息。`" :disabled="disabled" :error="errorFor('schedule.at')" />
            <div v-if="draft.config.schedule.kind==='every'" class="cron-grid">
              <div class="cron-interval-field">
                <Field v-model="intervalAmount" label="固定间隔" type="number" :min="1/intervalUnit" :disabled="disabled" :error="errorFor('schedule.everySeconds')" />
                <Field :model-value="intervalUnit" label="单位" :options="INTERVAL_UNITS" :disabled="disabled" @update:model-value="setIntervalUnit" />
              </div>
              <CronDateTime v-model="draft.config.schedule.anchorAt" label="间隔起点" placeholder="留空，以保存时间为起点" :hint="`本地时区 ${localTimezone}；可留空。`" :disabled="disabled" :error="errorFor('schedule.anchorAt')" />
            </div>
            <template v-if="draft.config.schedule.kind==='cron'">
              <Field v-model="draft.config.schedule.expression" label="五段 Cron 表达式" placeholder="0 9 * * *" :disabled="disabled" :error="errorFor('schedule.expression')" />
              <div class="cron-expression-help">
                <p>依次为 <code>分 时 日 月 星期</code>，按所选时区执行；星期 0 / 7 为周日。</p>
                <p><code>*</code> 任意值，<code>,</code> 多个值，<code>-</code> 范围，<code>/</code> 间隔。</p>
                <p><code>0 9 * * *</code> 每天 09:00 · <code>0 9 * * 1-5</code> 周一至周五 09:00</p>
                <p><code>*/30 * * * *</code> 每 30 分钟；下方可预览实际执行时间。</p>
              </div>
              <Field v-model="draft.config.schedule.second" label="执行秒（0–59，留空为 0）" type="number" :min="0" :max="59" :disabled="disabled" :error="errorFor('schedule.second')" />
            </template>
              <div v-if="draft.config.schedule.kind!=='at'" class="cron-grid">
                <CronDateTime v-model="draft.config.schedule.startAt" label="开始时间（含，可选）" placeholder="不限开始时间" :hint="`本地时区 ${localTimezone}；只在此时之后按规则执行。`" :disabled="disabled" :error="errorFor('schedule.startAt')" />
                <CronDateTime v-model="draft.config.schedule.endAt" label="结束时间（不含，可选）" placeholder="不限结束时间" hint="到此时间后计划结束。例如执行至 7 日，结束时间设为 8 日 00:00。" :disabled="disabled" :error="errorFor('schedule.endAt')" />
              </div>
            <Field label="跳过节假日" hint="中国大陆：周末及法定假日跳过，调休工作日照常执行。日历每天自动更新；无需结束日期，缺年时等待数据，不补跑。"><el-switch v-model="draft.config.schedule.skipHolidays" :disabled="disabled" aria-label="跳过节假日" /></Field>
            <div class="cron-preview"><div class="cron-toolbar"><h3>接下来 5 次</h3><el-button :loading="previewLoading" :disabled="disabled" @click="editor.previewNext">预览时间</el-button></div><p class="cron-note">按本地时区 {{ localTimezone }} 显示；间隔起点留空时，预览以当前时间计算。</p><p v-if="previewError" class="cron-error" role="alert">{{ previewError }}</p><p v-if="previewData?.message" class="cron-note" role="status">{{ previewData.message }}</p><ol v-if="previewData?.times?.length"><li v-for="value in previewData.times" :key="value"><time :datetime="value" :title="value">{{ time(value) }}</time></li></ol><p v-else class="cron-note">{{ previewData?.scheduleState==='waiting_calendar' ? '可以保存，日历数据补齐后自动继续排程。' : previewData ? '没有未来执行时点。' : '修改规则后请重新预览。' }}</p></div>
            <p class="cron-note">到点开始，不排队；停机错过直接跳过，不补跑。</p>
          </section>
          <section v-show="tab==='instructions'" id="cron-panel-instructions" role="tabpanel" aria-labelledby="cron-tab-instructions" class="cron-section cron-section-instructions">
            <p class="cron-note">填写本次执行的目标、范围与授权；会话同时继承目录上下文。</p>
            <div class="cron-code cron-instructions"><AdaptiveMdEditor v-model="draft.config.instructions" :read-only="disabled" language="markdown" completion-mode="none" /></div>
            <div class="cron-server-changes" data-field="allowServerChanges"><el-checkbox v-model="draft.config.allowServerChanges" :disabled="disabled">允许改动服务器和实例</el-checkbox><p :class="draft.config.allowServerChanges ? 'cron-alert' : 'cron-note'">{{ draft.config.allowServerChanges ? '定时任务无人值守，可能被服务器返回的内容误导而执行改动（运行命令、写文件、操作节点和实例）。只在任务确实需要时开启。' : '未勾选时，执行中只能查看服务器和实例。' }}</p></div>
          </section>
          <section v-show="tab==='scripts'" id="cron-panel-scripts" role="tabpanel" aria-labelledby="cron-tab-scripts" class="cron-section"><el-radio-group v-model="phase" aria-label="脚本阶段"><el-radio-button value="pre">前置脚本</el-radio-button><el-radio-button value="post">后置脚本</el-radio-button></el-radio-group><CronScriptEditor :key="phase" :script="draft.config[phase]" :phase="phase" :environment="environment" :errors="errors" :disabled="disabled" /></section>
        </template>
      </div>
    </div>
    <template #footer><div class="cron-footer"><span class="cron-note">{{ dirty ? '有未保存的修改' : job ? `版本 ${job.revision}` : '新任务默认停用' }}</span><div><el-button :disabled="saving" @click="close">取消</el-button><el-button type="primary" :loading="saving" :disabled="!loaded || loading || inheritanceLoading || Boolean(inheritanceError) || Boolean(conflict && !conflict.mergeReady)" @click="save">保存</el-button></div></div></template>
  </el-dialog>
</template>

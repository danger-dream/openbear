<script setup>
import {computed} from 'vue';
import {modelThinkingLevels, thinkingLabel} from '../views/consoleView/display.js';
import {RUN_DEFAULT_INHERIT, runDefaultOption, runDefaultSelection} from './folderRunDefaults.js';
import {normalizeImageOneTurn} from '../views/consoleView/imageOneTurn.js';
const props = defineProps({modelValue:{type:Object,default:()=>({})},effective:{type:Object,default:()=>({})},inherited:{type:Object,default:null},models:{type:Array,default:()=>[]},inheritable:Boolean,repairUnsupported:Boolean,inheritLabel:{type:String,default:'继承目录'},disabled:Boolean,showAgent:{type:Boolean,default:true},showStrategy:{type:Boolean,default:true}});
const emit = defineEmits(['change']);
const agentThinkingFollowLabel = '跟随主会话（不支持时用模型默认）';
const effective = computed(() => ({...props.modelValue,...props.effective}));
const main = computed(() => props.models.find(item => item.key === effective.value.mainModel));
const agent = computed(() => props.models.find(item => item.key === (effective.value.agentModel || effective.value.mainModel)));
const columns = computed(() => props.showAgent ? ['main','agent'] : ['main']);
const rows = [['Model','模型'],['Thinking','思考强度'],['Fast','Fast 模式']];
const field = (owner,kind) => ({main:{Model:'mainModel',Thinking:'mainThinkingLevel',Fast:'mainFastMode'},agent:{Model:'agentModel',Thinking:'agentThinkLevel',Fast:'agentFastMode'}})[owner][kind];
const info = owner => owner === 'main' ? main.value : agent.value;
const supported = (owner,kind) => kind === 'Model' || (kind === 'Thinking' ? modelThinkingLevels(info(owner)).length > 0 : Boolean(info(owner)?.supportsFast));
function options(owner,kind) {
  if (kind === 'Model') return [...(owner === 'agent' ? [{value:'',label:'跟随主模型'}] : []),...props.models.map(item=>({value:item.key,label:item.label || item.name || item.key}))];
  if (kind === 'Thinking') return [...(owner === 'agent' ? [{value:'',label:agentThinkingFollowLabel}] : []),...modelThinkingLevels(info(owner)).map(value=>({value,label:thinkingLabel(value)}))];
  if (props.repairUnsupported && !supported(owner,kind)) return [{value:false,label:'关闭'}];
  return [...(owner === 'agent' ? [{value:null,label:'跟随主会话'}] : []),{value:true,label:'开启'},{value:false,label:'关闭'}];
}
function label(key,value) {
  if (key.endsWith('Model')) return value === '' ? '跟随主模型' : props.models.find(item=>item.key===value)?.label || value || '未配置';
  if (key.endsWith('FastMode')) return value == null ? '跟随主会话' : value ? '开启' : '关闭';
  if (key === 'contextStrategy') return value === 'model_summary' ? '模型摘要' : '滑动窗口';
  if (key === 'imageOneTurn') {
    const image = normalizeImageOneTurn(value);
    return image.enabled ? `开启 · ${image.includeUser ? '含我发送的图片' : '只处理 AI 读取的图片'} · Agent ${image.agents ? '跟随' : '不跟随'}` : '关闭';
  }
  return value === '' ? (key === 'agentThinkLevel' ? agentThinkingFollowLabel : '模型默认') : thinkingLabel(value) || '关闭';
}
function current(key) { return runDefaultSelection(props.modelValue,key); }
// Off is one choice whatever the two follow-up switches hold.
const imageChoices = [normalizeImageOneTurn(), ...[[true,true],[true,false],[false,true],[false,false]].map(([includeUser,agents]) => ({enabled:true,includeUser,agents}))];
function currentImage() {
  if (!Object.hasOwn(props.modelValue,'imageOneTurn')) return props.inheritable ? RUN_DEFAULT_INHERIT : runDefaultOption(normalizeImageOneTurn());
  const image = normalizeImageOneTurn(props.modelValue.imageOneTurn);
  return runDefaultOption(image.enabled ? image : normalizeImageOneTurn());
}
function inheritText(key) { return `${props.inheritLabel} · ${label(key,(props.inherited || props.effective)[key])}`; }
function changed(key,selection) { emit('change',key,selection); }
</script>
<template>
  <div class="model-settings run-default-groups" :class="{'main-only':!showAgent}">
    <div class="run-grid-head"><span>设置项</span><h3>主会话</h3><h3 v-if="showAgent">Agent</h3></div>
    <div v-for="[kind,title] in rows" :key="kind" class="run-setting-row">
      <h4>{{ title }}</h4>
      <div v-for="owner in columns" :key="owner" class="run-default-cell" :data-owner="owner === 'main' ? '主会话' : 'Agent'">
        <el-select v-if="supported(owner,kind) || (repairUnsupported && inheritable && Object.hasOwn(modelValue,field(owner,kind)))" :model-value="current(field(owner,kind))" :disabled="disabled" :aria-label="`${owner === 'main' ? '主会话' : 'Agent'}${title}`" :data-run-default-field="field(owner,kind)" popper-class="folder-properties-popover" :show-arrow="false" @change="changed(field(owner,kind),$event)">
          <el-option v-if="inheritable" :value="RUN_DEFAULT_INHERIT" :label="inheritText(field(owner,kind))" />
          <el-option v-if="Object.hasOwn(modelValue,field(owner,kind)) && !options(owner,kind).some(item=>Object.is(item.value,modelValue[field(owner,kind)]))" :value="current(field(owner,kind))" :label="`当前不可用 · ${label(field(owner,kind),modelValue[field(owner,kind)])}`" disabled />
          <el-option v-for="item in options(owner,kind)" :key="String(item.value)" :value="runDefaultOption(item.value)" :label="item.label" />
        </el-select>
        <span v-else class="run-field-hint">当前模型不支持{{ kind === 'Thinking' ? '思考强度' : ' Fast' }}</span>
      </div>
    </div>
    <div v-if="showStrategy" class="run-setting-row model-strategy-row"><h4>上下文压缩</h4><div class="run-default-cell"><el-select :model-value="current('contextStrategy')" :disabled="disabled" data-run-default-field="contextStrategy" aria-label="上下文压缩策略" popper-class="folder-properties-popover" :show-arrow="false" @change="changed('contextStrategy',$event)"><el-option v-if="inheritable" :value="RUN_DEFAULT_INHERIT" :label="inheritText('contextStrategy')" /><el-option :value="runDefaultOption('sliding_window')" label="滑动窗口 · 保留近期原文" /><el-option :value="runDefaultOption('model_summary')" label="模型摘要 · 整理历史后保留摘要" /></el-select></div></div>
    <div v-if="showStrategy" class="run-setting-row model-strategy-row"><h4>图片只看一轮</h4><div class="run-default-cell"><el-select :model-value="currentImage()" :disabled="disabled" data-run-default-field="imageOneTurn" aria-label="图片只看一轮" popper-class="folder-properties-popover" :show-arrow="false" @change="changed('imageOneTurn',$event)"><el-option v-if="inheritable" :value="RUN_DEFAULT_INHERIT" :label="inheritText('imageOneTurn')" /><el-option v-for="item in imageChoices" :key="runDefaultOption(item)" :value="runDefaultOption(item)" :label="label('imageOneTurn',item)" /></el-select></div></div>
  </div>
</template>
<style scoped>
.model-settings.main-only .run-grid-head,.model-settings.main-only .run-setting-row{grid-template-columns:80px minmax(0,1fr)}
.model-strategy-row .run-default-cell{grid-column:2/-1}
@media(max-width:620px){.model-settings.main-only .run-setting-row{grid-template-columns:minmax(0,1fr)}.model-strategy-row .run-default-cell{grid-column:auto;display:block}}
</style>

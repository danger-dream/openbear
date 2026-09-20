<script setup>
import {nextTick,onBeforeUnmount,ref,watch} from 'vue';
import {computePosition,flip,offset,shift} from '@floating-ui/dom';
const props=defineProps({open:Boolean,anchor:{default:null},placement:{type:String,default:'right-start'},width:{type:Number,default:340},height:{type:Number,default:0},label:{type:String,default:'引用资源'}});
const emit=defineEmits(['close','enter','leave','keydown']);
const panel=ref(null),style=ref({visibility:'hidden'});
let frame=0,resizeObserver=null,positionGeneration=0;
async function position(){
 if(!props.open||!panel.value||!props.anchor||typeof window==='undefined')return;
 const generation=++positionGeneration;
 const viewport=window.visualViewport;
 const width=Math.min(props.width,(viewport?.width||window.innerWidth)-24);
 const availableHeight=Math.max(120,(viewport?.height||window.innerHeight)-32);
 const height=props.height?`${Math.min(props.height,430,availableHeight)}px`:'';
 panel.value.style.width=`${width}px`;panel.value.style.maxHeight=`${Math.min(430,availableHeight)}px`;panel.value.style.height=height;
 const anchor=typeof props.anchor.getBoundingClientRect==='function'?props.anchor:{getBoundingClientRect:()=>props.anchor};
 const pos=await computePosition(anchor,panel.value,{strategy:'fixed',placement:props.placement,middleware:[offset(8),flip(),shift({padding:12})]});
 if(!props.open||generation!==positionGeneration||!panel.value)return;
 const top=Math.min(Math.max((viewport?.offsetTop||0)+8,pos.y),(viewport?.offsetTop||0)+(viewport?.height||innerHeight)-panel.value.offsetHeight-8);
 style.value={position:'fixed',left:`${pos.x}px`,top:`${Math.max(8,top)}px`,width:`${width}px`,height,maxHeight:`${Math.min(430,availableHeight)}px`,visibility:'visible'};
}
function schedule(){cancelAnimationFrame(frame);frame=requestAnimationFrame(position);}
function outside(event){if(!panel.value?.contains(event.target)&&!props.anchor?.contains?.(event.target))emit('close');}
function key(event){if(event.key==='Escape'&&!event.defaultPrevented&&!event.isComposing&&event.keyCode!==229)emit('close');}
watch(()=>[props.open,props.anchor,props.height],async()=>{resizeObserver?.disconnect();positionGeneration++;if(props.open&&typeof window!=='undefined'){await nextTick();schedule();if(typeof ResizeObserver!=='undefined'&&panel.value){resizeObserver=new ResizeObserver(schedule);resizeObserver.observe(panel.value);}}},{immediate:true});
watch(()=>props.open,open=>{
 if(typeof window==='undefined')return;
 const method=open?'addEventListener':'removeEventListener';
 window[method]('resize',schedule);window[method]('scroll',schedule,true);document[method]('pointerdown',outside,true);document[method]('keydown',key);
 window.visualViewport?.[method]('resize',schedule);window.visualViewport?.[method]('scroll',schedule);
},{immediate:true});
onBeforeUnmount(()=>{resizeObserver?.disconnect();positionGeneration++;if(typeof window==='undefined')return;cancelAnimationFrame(frame);window.removeEventListener('resize',schedule);window.removeEventListener('scroll',schedule,true);document.removeEventListener('pointerdown',outside,true);document.removeEventListener('keydown',key);window.visualViewport?.removeEventListener('resize',schedule);window.visualViewport?.removeEventListener('scroll',schedule);});
defineExpose({position});
</script>
<template><Teleport to="body"><section v-if="open" ref="panel" class="reference-floating-panel" :style="style" :aria-label="label" @pointerenter="emit('enter')" @pointerleave="emit('leave')" @click.stop @keydown="emit('keydown',$event)"><slot/></section></Teleport></template>
<style>
.reference-floating-panel{z-index:3100;display:flex;flex-direction:column;overflow:hidden;border:1px solid rgba(80,89,106,.18);border-radius:11px;background:rgba(252,252,253,.98);box-shadow:0 12px 35px rgba(25,37,55,.14),0 2px 6px rgba(25,37,55,.08);color:#303846;font-family:inherit;font-size:12px;line-height:1.5;backdrop-filter:blur(16px)}
html.dark .reference-floating-panel{background:rgba(35,38,45,.98);border-color:rgba(190,202,221,.2);color:#dde2e9;box-shadow:0 12px 35px rgba(0,0,0,.4),0 2px 6px rgba(0,0,0,.2)}
</style>

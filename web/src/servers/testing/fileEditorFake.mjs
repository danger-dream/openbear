import {h} from 'vue';
export default {props: ['modelValue', 'label'], emits: ['update:modelValue'], setup(props, {emit}) {
  return () => h('textarea', {'aria-label': props.label, value: props.modelValue, onInput: e => emit('update:modelValue', e.target.value)});
}};

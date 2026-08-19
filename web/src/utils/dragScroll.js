// SortableJS 在“外层滚动容器 + 内层嵌套 draggable”中无法稳定识别滚动父级。
// 强制使用 fallback 拖拽并由页面显式传入 scroll DOM，保证靠近上下边缘时持续滚动。
export const dragAutoScrollOptions = Object.freeze({
  forceFallback: true,
  // Chromium 的 PointerEvent + fallback 组合偶发收不到 pointerup，改走稳定的 mouse/touch 事件链。
  supportPointer: false,
  fallbackOnBody: true,
  fallbackTolerance: 3,
  bubbleScroll: true,
  scrollSensitivity: 90,
  scrollSpeed: 14,
});

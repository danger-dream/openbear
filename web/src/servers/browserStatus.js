// RPC success means the status was read, not that Chrome finished starting.
export function browserRestartFeedback(browser) {
  switch (browser?.state) {
    case 'ready': return {type: 'success', message: '浏览器已重启'};
    case 'starting': return {type: 'info', message: '浏览器正在启动'};
    case 'installing': return {type: 'info', message: '正在下载浏览器，下载完成后首次使用时启动'};
    case 'failed': return {type: 'error', message: browser.error || '浏览器启动失败'};
    default: return {type: 'warning', message: browser?.error || '浏览器尚未启动'};
  }
}

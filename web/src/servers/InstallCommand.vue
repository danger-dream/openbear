<script setup>
import { computed, ref, watch } from 'vue';
import { ElMessage } from 'element-plus';
import { CopyDocument } from '@element-plus/icons-vue';

// The one-time install command for Linux and macOS (sh) or Windows (PowerShell).
const props = defineProps({command: {type: String, default: ''}, windowsCommand: {type: String, default: ''}, platform: {type: String, default: ''},
  note: Boolean});
const windows = ref(props.platform === 'windows');
watch(() => props.platform, v => { if (v) windows.value = v === 'windows'; });
const shown = computed(() => (windows.value && props.windowsCommand) || props.command);
async function copy() {
  try { await navigator.clipboard.writeText(shown.value); ElMessage.success('已复制安装命令'); }
  catch { ElMessage.warning('浏览器不允许复制，请手动选中命令'); }
}
</script>
<template>
  <div class="sv-install-command">
    <div v-if="windowsCommand" class="pb-chips sv-os" role="group" aria-label="服务器系统">
      <button type="button" :aria-pressed="!windows" @click="windows = false">Linux / macOS</button>
      <button type="button" :aria-pressed="windows" @click="windows = true">Windows</button>
    </div>
    <div class="sv-command">
      <code>{{ shown }}</code>
      <button type="button" class="ix-icon-button" aria-label="复制安装命令" title="复制" @click="copy"><el-icon><CopyDocument /></el-icon></button>
    </div>
    <p v-if="note && windows && windowsCommand" class="ix-card-note">在“以管理员身份运行”的 PowerShell 中粘贴执行，需要 Windows 10 / Server 2016 或更新的系统。命令 30 分钟内有效，只能使用一次。加 <code>-NoExec</code> 可禁止命令执行、文件读写和在线更新，加 <code>-NoTerminal</code> 可禁止网页终端。机器上已有其他节点时命令会停下，确认要覆盖就在末尾加 <code>-Force</code>。</p>
    <p v-else-if="note" class="ix-card-note">在服务器上以 root 执行（其他用户在 sh 前加 sudo，macOS 也一样）。命令 30 分钟内有效，只能使用一次。加 <code>--no-exec</code> 可禁止命令执行、文件读写和在线更新，加 <code>--no-terminal</code> 可禁止网页终端。机器上已有其他节点时命令会停下，确认要覆盖就在末尾加 <code>--force</code>（Windows 加 <code>-Force</code>）。</p>
  </div>
</template>

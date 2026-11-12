import { ref, watch } from 'vue';

// The detail page's tabs, and the one shown: shared by every server's page, so another server opens on the
// tab being looked at, and kept for the next visit.
export const DETAIL_TABS = [['overview', '概览'], ['net', '网络'], ['system', '系统'], ['tools', '工具']];
const KEY = 'openbear.servers.detailTab';
const stored = (() => { try { return localStorage.getItem(KEY); } catch { return null; } })();
export const detailTab = ref(DETAIL_TABS.some(([key]) => key === stored) ? stored : 'overview');
watch(detailTab, value => { try { localStorage.setItem(KEY, value); } catch { /* private mode */ } });

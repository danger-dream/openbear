import {computed, ref, watch} from 'vue';

// Shared by the desktop activity section and the mobile tree's virtual branch.
// These are shortcuts; none of the rows' real folder IDs or ordering is changed.
export function useRecentConversationRows(props) {
  const selectedRow = ref(null);
  watch(() => [props.items, props.activeConversationUuid, props.readVersions], () => {
    const current = props.items.find(item => item.conversationUuid === props.activeConversationUuid);
    if (current) selectedRow.value = current;
    else if (selectedRow.value?.conversationUuid !== props.activeConversationUuid
      || !selectedRow.value?.activityVersion
      || Number(props.readVersions.get(props.activeConversationUuid) || 0) < selectedRow.value.activityVersion) selectedRow.value = null;
  }, {immediate: true});
  const visibleItems = computed(() => {
    const items = [...props.items];
    if (selectedRow.value && !items.some(item => item.conversationUuid === selectedRow.value.conversationUuid)) {
      items.push({...selectedRow.value, running: false, activityUnread: false, readWhileSelected: true});
    }
    return items;
  });
  const recent = computed(() => [...new Map((props.recentItems || [])
    .filter(item => item?.conversationUuid && !item.archived && !item.local && Number(item.lastInteractionAtMs) > 0)
    .map(item => [item.conversationUuid, item])).values()]
    .sort((a, b) => Number(b.lastInteractionAtMs) - Number(a.lastInteractionAtMs) || a.conversationUuid.localeCompare(b.conversationUuid))
    .slice(0, 15));
  const rows = computed(() => {
    const unique = new Map(recent.value.map(item => [item.conversationUuid, item]));
    for (const item of visibleItems.value) {
      if (!item?.conversationUuid || item.archived || item.local || item.hiddenFromRecent || props.hiddenConversationIds?.includes(item.conversationUuid)) continue;
      const current = unique.get(item.conversationUuid);
      // Live status beats a retention snapshot; do not duplicate pending work.
      if (!current || !item.readWhileSelected) unique.set(item.conversationUuid, {...current, ...item});
    }
    const interactionAt = item => Number(item.lastInteractionAtMs || item.activityAtMs || 0);
    return [...unique.values()].sort((a, b) => interactionAt(b) - interactionAt(a)
      || a.conversationUuid.localeCompare(b.conversationUuid));
  });
  return {selectedRow, visibleItems, recent, rows};
}

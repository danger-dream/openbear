import {onBeforeUnmount, onMounted, ref} from 'vue';

const pad = value => String(value).padStart(2, '0');
function localDate(date) {
  return `${String(date.getFullYear()).padStart(4, '0')}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

const day = ref(localDate(new Date()));
const labels = new Map();
let subscribers = 0;
let dayTimer = null;

function refreshDay() {
  const now = new Date();
  day.value = localDate(now);
  clearTimeout(dayTimer);
  const next = new Date(now);
  next.setHours(24, 0, 0, 20);
  dayTimer = setTimeout(refreshDay, Math.max(1, next.getTime() - now.getTime()));
}

function refreshVisibleDay() {
  if (document.visibilityState === 'visible') refreshDay();
}

export function useConversationDay() {
  let active = false;
  onMounted(() => {
    if (typeof window === 'undefined' || typeof document === 'undefined') return;
    active = true;
    if (++subscribers !== 1) return;
    refreshDay();
    document.addEventListener('visibilitychange', refreshVisibleDay);
    window.addEventListener('focus', refreshDay);
  });
  onBeforeUnmount(() => {
    if (!active || --subscribers !== 0) return;
    clearTimeout(dayTimer);
    dayTimer = null;
    document.removeEventListener('visibilitychange', refreshVisibleDay);
    window.removeEventListener('focus', refreshDay);
  });
  return day;
}

export function conversationTimeParts(value, today = localDate(new Date())) {
  const ms = Number(value);
  if (!Number.isFinite(ms) || ms <= 0) return {date: '', time: '', full: ''};
  let result = labels.get(ms);
  if (!result) {
    const date = new Date(ms);
    if (!Number.isFinite(date.getTime())) return {date: '', time: '', full: ''};
    const dateLabel = localDate(date);
    const time = `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
    result = {date: dateLabel, time, full: `${dateLabel} ${time}`};
    labels.set(ms, result);
    if (labels.size > 2048) labels.delete(labels.keys().next().value);
  }
  return {date: result.date === today ? '' : result.date, time: result.time, full: result.full};
}

export function groupConversationTimes(lines, today) {
  let previousDate = '';
  return lines.map(line => {
    const timestamp = Number(line.ts || 0);
    const ms = timestamp > 0 ? timestamp * (timestamp < 10_000_000_000 ? 1000 : 1) : Number(line.timeMs || 0);
    const parts = conversationTimeParts(ms, today);
    const date = parts.full.slice(0, 10);
    const dateHeading = parts.date && date !== previousDate ? date : '';
    previousDate = date;
    return {...line, dateHeading, timestampTitle: parts.full, displayTime: parts.time || line.timeLabel};
  });
}

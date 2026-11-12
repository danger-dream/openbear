import { regionName } from '../serverHelpers.js';
import { serviceLogo } from '../serviceLogos.js';

// 服务解锁 on the detail page: the full check's services (unlock_full.py) in its order, the cards' nine
// with the cards' own verdicts in their place (measured per address family, and what the cards show)
// unless those could not tell; without a full result, the nine alone. card: unlockServices().
export const UNLOCK_GROUPS = [['media', '流媒体'], ['ai', 'AI'], ['web', '网站与游戏']];
const CARD_GROUPS = {youtube: 'media', netflix: 'media', spotify: 'media', tiktok: 'media', openai: 'ai', anthropic: 'ai', gemini: 'ai', google: 'web', reddit: 'web'};
const STATES = {ok: '可用', limited: '受限', blocked: '不可用', error: '未知'};

export function unlockList(full, card) {
  const cards = new Map((card?.items || []).map(c => [c.id, c]));
  const base = full?.items?.length ? [...full.items] : [];
  for (const c of cards.values()) {
    if (!base.some(item => item.id === c.id)) base.push({id: c.id, name: c.name, group: CARD_GROUPS[c.id] || 'web', state: 'error', region: '', notes: []});
  }
  const items = base.map(item => {
    const c = cards.get(item.id);
    const mine = c && (c.state !== 'error' || item.state === 'error') ? c : null;
    const state = mine ? c.state : item.state, region = mine ? c.region : item.region;
    const notes = [...new Set([...(mine?.limit ? [mine.limit] : []), ...(item.notes || [])])];
    const said = mine ? c.label.split('\n').slice(1) : [`${STATES[state] || '未知'}${region ? ` · ${regionName(region)}（${region}）` : ''}`];
    return {id: item.id, name: c?.name || item.name, group: item.group, state, region, notes, mark: mine?.mark || null,
      text: state === 'ok' ? region || '可用' : STATES[state] || '未知', label: [c?.name || item.name, ...said, ...notes].join('\n'), logo: serviceLogo(item.id)};
  });
  const counts = {ok: 0, limited: 0, blocked: 0, error: 0};
  for (const item of items) counts[item.state in counts ? item.state : 'error'] += 1;
  return {items, counts, groups: UNLOCK_GROUPS.map(([key, name]) => ({key, name, items: items.filter(i => i.group === key)})).filter(g => g.items.length)};
}

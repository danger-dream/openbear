"""Automatic link building (design 2.3): probe candidate generation, the greedy
shortest-path optimizer over relays and non-transit members, flap/disappointment
damping, 7-day suppression of auto links deleted by the user, and merging auto
changes with a dirty draft."""
from __future__ import annotations

import copy
import heapq
import itertools
import statistics

from app.servers import mesh_auto, mesh_limits
from app.servers.mesh_config import (
    TRANSPORTS,
    can_dial,
    endpoint_host,
    front_exits,
    is_site,
    lan_endpoint,
    public_port,
    punch_host,
    reflectors,
    relay_pairs,
)

# A candidate is built once it has made some node pair buildMs faster, or buildRatio faster and keepMs
# at least, for holdS; an auto link goes once no pair has been keepMs faster for dropAfterS. Times
# the mesh spends settling (SETTLE_S) do not count towards either.
DEFAULT_SETTINGS = {
    'enabled': True,
    'perNode': 4,
    'buildMs': 15,
    'buildRatio': 0.10,
    'keepMs': 10,
    'holdS': 1800,
    'dropAfterS': 6 * 3600,
}
# The keys of the rules before buildMs and keepMs: accepted and dropped. A document that has them and
# the one-hour drop of those rules gets the default drop.
LEGACY_SETTINGS = frozenset({'gainRatio', 'gainMs', 'holdRounds'})
LEGACY_DROP_AFTER_S = 3600
SETTINGS_KEYS = frozenset(DEFAULT_SETTINGS) | {'dropAfter'} | LEGACY_SETTINGS
# Costs are the median of what was measured over SMOOTH_S; a line goes in the graph while it was up
# for half of it. A node that restarted, or had a link change state, within SETTLE_S, and the whole
# mesh within SETTLE_S of an applied revision, are not judged.
SMOOTH_S = 1800
SETTLE_S = 600
MAX_PROBE_TARGETS = 64
PROBE_INTERVAL_S = 300
MIN_COST = 0.1
DISAPPOINT_TTL_MS = 24 * 3600_000
# A pair let down again within STRIKE_MEMORY_MS of the last time waits the longer TTL.
REPEAT_DISAPPOINT_TTL_MS = 72 * 3600_000
STRIKE_MEMORY_MS = 7 * 24 * 3600_000
SUPPRESS_TTL_MS = 7 * 24 * 3600_000
# The share of rounds an auto link, or a relay's links, are up, smoothed by HEALTH_WEIGHT a round:
# after HEALTH_ROUNDS rounds an auto link below UNSTABLE_SHARE goes, and a relay below it is no end
# of a new one until it has stayed at or above it for NODE_STABLE_MS.
HEALTH_WEIGHT = 0.1
HEALTH_ROUNDS = 10
UNSTABLE_SHARE = 0.5
NODE_STABLE_MS = 3600_000
AUTO_REVISION_INTERVAL_MS = 10 * 60_000
AUTO_LOOP_INTERVAL_S = 60


def default_settings():
    return dict(DEFAULT_SETTINGS)


def _migrated(raw):
    out = {k: v for k, v in (raw or {}).items() if k != 'dropAfter'}
    if LEGACY_SETTINGS & set(out) and out.get('dropAfterS') == LEGACY_DROP_AFTER_S:
        out.pop('dropAfterS')
    return {k: v for k, v in out.items() if k not in LEGACY_SETTINGS}


def settings_of(doc):
    """The document's autoLinks settings over the defaults, those of the older rules carried over."""
    return dict(DEFAULT_SETTINGS, **_migrated((doc.get('settings') or {}).get('autoLinks')))


def enabled(doc):
    """Whether the mesh adjusts its links itself: no rounds, no probes, no revisions while off."""
    return bool(((doc.get('settings') or {}).get('autoLinks') or {}).get('enabled', DEFAULT_SETTINGS['enabled']))


def _int(value, low, high):
    return type(value) is int and low <= value <= high


def _num(value, low, high):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and low <= float(value) <= high


def clean_settings(raw, prior, errors):
    out = dict(DEFAULT_SETTINGS, **_migrated(prior))
    if not isinstance(raw, dict) or set(raw) - SETTINGS_KEYS:
        errors.add('settings.autoLinks', 'invalid_auto_links', '自动调整线路设置格式不正确')
        return out
    if LEGACY_SETTINGS & set(raw) and raw.get('dropAfterS', raw.get('dropAfter')) == LEGACY_DROP_AFTER_S:
        raw = {k: v for k, v in raw.items() if k not in ('dropAfterS', 'dropAfter')}
    if 'enabled' in raw:
        if type(raw['enabled']) is not bool:
            errors.add('settings.autoLinks.enabled', 'invalid_auto_links', '自动调整线路开关须是开或关')
        else:
            out['enabled'] = raw['enabled']
    if 'perNode' in raw:
        if not _int(raw['perNode'], 0, 32):
            errors.add('settings.autoLinks.perNode', 'invalid_auto_links', '每节点新增自动线上限须在 0～32 之间')
        else:
            out['perNode'] = raw['perNode']
    for key, label in (('buildMs', '新建门槛'), ('keepMs', '保留门槛')):
        if key in raw:
            if not _num(raw[key], 1, 10000):
                errors.add(f'settings.autoLinks.{key}', 'invalid_auto_links', f'{label}须在 1～10000 毫秒之间')
            else:
                out[key] = int(raw[key]) if float(raw[key]).is_integer() else round(float(raw[key]), 1)
    if 'buildRatio' in raw:
        if not _num(raw['buildRatio'], 0.01, 0.99):
            errors.add('settings.autoLinks.buildRatio', 'invalid_auto_links', '新建的改善比例须在 0.01～0.99 之间')
        else:
            out['buildRatio'] = round(float(raw['buildRatio']), 4)
    if 'holdS' in raw:
        if not _int(raw['holdS'], 0, 86400):
            errors.add('settings.autoLinks.holdS', 'invalid_auto_links', '新建前持续达标的秒数须在 0～86400 之间')
        else:
            out['holdS'] = raw['holdS']
    drop_key = 'dropAfterS' if 'dropAfterS' in raw else 'dropAfter' if 'dropAfter' in raw else None
    if drop_key is not None:
        if not _int(raw[drop_key], 0, 7 * 86400):
            errors.add(f'settings.autoLinks.{drop_key}', 'invalid_auto_links', '自动拆线等待秒数须在 0～604800 之间')
        else:
            out['dropAfterS'] = raw[drop_key]
    if out['keepMs'] > out['buildMs']:
        errors.add('settings.autoLinks.keepMs', 'invalid_auto_links', '保留门槛不能高于新建门槛')
    return out


def clean_state(raw, now_ms=0):
    """Cleaned optimizer persistence state stored in settings_json (_autoLinksState)."""
    raw = raw if isinstance(raw, dict) else {}
    suppressed = {}
    for k, exp in (raw.get('suppressed') if isinstance(raw.get('suppressed'), dict) else {}).items():
        if isinstance(k, str) and '|' in k and isinstance(exp, (int, float)) and not isinstance(exp, bool):
            if int(exp) > now_ms:
                suppressed[k] = int(exp)
    disappointed = {}
    for k, exp in (raw.get('disappointed') if isinstance(raw.get('disappointed'), dict) else {}).items():
        if isinstance(k, str) and '|' in k and isinstance(exp, (int, float)) and not isinstance(exp, bool):
            if int(exp) > now_ms:
                disappointed[k] = int(exp)
    watch_since = {k: _ms(ts) for k, ts in (raw.get('watchSince') if isinstance(raw.get('watchSince'), dict) else {}).items()
                   if isinstance(k, str) and '|' in k and _ms(ts)}
    idle_since = {}
    for k, ts in (raw.get('idleSince') if isinstance(raw.get('idleSince'), dict) else {}).items():
        if isinstance(k, str) and '|' in k and isinstance(ts, (int, float)) and not isinstance(ts, bool) and int(ts) > 0:
            idle_since[k] = int(ts)
    idle_best = {k: round(float(v), 1) for k, v in (raw.get('idleBest') if isinstance(raw.get('idleBest'), dict) else {}).items()
                 if k in idle_since and _num(v, 0, 1e6)}
    strikes = {}
    for k, s in (raw.get('strikes') if isinstance(raw.get('strikes'), dict) else {}).items():
        if isinstance(k, str) and '|' in k and isinstance(s, dict) and type(s.get('count')) is int and s['count'] > 0 \
                and _ms(s.get('at')) and _ms(s['at']) + STRIKE_MEMORY_MS > now_ms:
            strikes[k] = {'count': s['count'], 'at': _ms(s['at'])}
    link_health = {k: _health(h) for k, h in (raw.get('linkHealth') if isinstance(raw.get('linkHealth'), dict) else {}).items()
                   if isinstance(k, str) and '|' in k and _health(h)}
    node_health = {sid: _health(h) for sid, h in (raw.get('nodeHealth') if isinstance(raw.get('nodeHealth'), dict) else {}).items()
                   if isinstance(sid, str) and _health(h)}
    last_auto = raw.get('lastAutoRevisionMs')
    last_auto_ms = int(last_auto) if isinstance(last_auto, (int, float)) and not isinstance(last_auto, bool) and int(last_auto) > 0 else 0
    last_change = raw.get('lastChange') if isinstance(raw.get('lastChange'), dict) else None
    # What the last round measured, for the lines page: each auto link's gains and the candidates watched.
    benefits = {k: {'kind': str(b.get('kind') or ''), 'count': int(b.get('count') or 0), 'pairs': [g for g in b['pairs'][:PAIRS_SHOWN] if isinstance(g, dict)]}
                for k, b in (raw.get('benefits') if isinstance(raw.get('benefits'), dict) else {}).items()
                if isinstance(k, str) and '|' in k and isinstance(b, dict) and isinstance(b.get('pairs'), list)}
    watching = [w for w in raw.get('watching') if isinstance(w, dict) and isinstance(w.get('key'), str)] if isinstance(raw.get('watching'), list) else []
    measured = raw.get('measuredAt')
    return {
        'suppressed': suppressed,
        'disappointed': disappointed,
        'watchSince': watch_since,
        'idleSince': idle_since,
        'idleBest': idle_best,
        'strikes': strikes,
        'linkHealth': link_health,
        'nodeHealth': node_health,
        'lastAutoRevisionMs': last_auto_ms,
        'lastChange': last_change,
        'benefits': benefits,
        'watching': watching,
        'measuredAt': int(measured) if isinstance(measured, (int, float)) and not isinstance(measured, bool) and measured > 0 else 0,
        'paused': raw.get('paused') is True,
    }


def _ms(value):
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else 0


def _health(raw):
    """A cleaned {share, rounds, up?, until?} health record, or None."""
    if not isinstance(raw, dict) or not _num(raw.get('share'), 0, 1) or type(raw.get('rounds')) is not int or raw['rounds'] < 0:
        return None
    out = {'share': float(raw['share']), 'rounds': raw['rounds']}
    if raw.get('up') is True:
        out['up'] = True
    if _ms(raw.get('until')):
        out['until'] = _ms(raw['until'])
    return out


def _smoothed(prior, up):
    """prior after one more round that found the share up (0-1) up; a first round starts from all up."""
    prior = prior or {}
    share = prior.get('share', 1.0)
    out = {**prior, 'share': round(share + (up - share) * HEALTH_WEIGHT, 4), 'rounds': prior.get('rounds', 0) + 1}
    if up > 0:
        out['up'] = True
    return out


def _disappoint(st, k, now_ms):
    """Keep pair k from being built again: DISAPPOINT_TTL_MS, or REPEAT_DISAPPOINT_TTL_MS when it
    already let the mesh down within STRIKE_MEMORY_MS."""
    count = (st['strikes'].get(k) or {}).get('count', 0) + 1
    st['strikes'][k] = {'count': count, 'at': now_ms}
    st['disappointed'][k] = now_ms + (DISAPPOINT_TTL_MS if count == 1 else REPEAT_DISAPPOINT_TTL_MS)


def track_nodes(st, live, statuses, now_ms):
    """Follow the share of each relay's links that are up, by its own report; returns the relays that
    are no end of a new auto link now: below UNSTABLE_SHARE lately, or not yet NODE_STABLE_MS back at it."""
    health = {}
    for sid, m in live.items():
        if m['role'] != 'relay':
            continue
        prior = st['nodeHealth'].get(sid)
        links = [l for l in (((_status_of(statuses, sid) or {}).get('routing') or {}).get('links') or []) if l.get('peer') in live]
        if not links:
            if prior:
                health[sid] = prior
            continue
        h = _smoothed(prior, sum(l.get('state') == 'up' for l in links) / len(links))
        h.pop('up', None)
        if h['rounds'] >= HEALTH_ROUNDS and h['share'] < UNSTABLE_SHARE:
            h['until'] = now_ms + NODE_STABLE_MS
        health[sid] = h
    st['nodeHealth'] = health
    return {sid for sid, h in health.items() if h.get('until', 0) > now_ms}


class History:
    """What earlier rounds saw, kept by the caller in memory from round to round: each line's costs
    over SMOOTH_S (None for a round a configured line was down), and when each node last had a link
    or entry change state."""

    def __init__(self):
        self.costs = {}
        self.states = {}
        self.changed = {}

    def record(self, now_ms, real_costs, probe_costs, expected, statuses, live):
        for kind, costs, keys in (('real', real_costs, set(expected) | set(real_costs)), ('probe', probe_costs, set(probe_costs))):
            for k in keys:
                self.costs.setdefault((kind, k), []).append((now_ms, costs.get(k)))
        horizon = now_ms - SMOOTH_S * 1000
        for key, samples in list(self.costs.items()):
            kept = [s for s in samples if s[0] > horizon]
            if kept:
                self.costs[key] = kept
            else:
                del self.costs[key]
        for sid in live:
            st = _status_of(statuses, sid) or {}
            seen = [(l.get('peer'), l.get('state')) for l in (st.get('routing') or {}).get('links') or []]
            seen += [(c.get('relay'), c.get('state')) for c in (st.get('entry') or {}).get('candidates') or []]
            for peer, state in seen:
                prior = self.states.get((sid, peer))
                if prior is not None and prior != state:
                    self.changed[sid] = now_ms
                    if peer in live:
                        self.changed[peer] = now_ms
                self.states[(sid, peer)] = state

    def smoothed(self, kind):
        """{line: median cost} of the lines of kind up for half the rounds kept at least."""
        out = {}
        for (k_kind, k), samples in self.costs.items():
            vals = [c for _, c in samples if c is not None]
            if k_kind == kind and vals and 2 * len(vals) >= len(samples):
                out[k] = statistics.median(vals)
        return out


def settling(statuses, live, now_ms, history=None):
    """The nodes not judged now: their data plane started, or a link or entry of theirs changed
    state, within SETTLE_S."""
    out = set()
    for sid in live:
        started = _ms((_status_of(statuses, sid) or {}).get('startedAt'))
        if started and now_ms - started < SETTLE_S * 1000:
            out.add(sid)
    if history:
        out |= {sid for sid, at in history.changed.items() if sid in live and now_ms - at < SETTLE_S * 1000}
    return out


def pair_key(a, b):
    x, y = sorted((a, b))
    return f'{x}|{y}'


def _split_key(key):
    a, _, b = key.partition('|')
    return a, b


def _live(doc):
    return {m['serverId']: m for m in doc.get('members', []) if not m.get('removing')}


def _name(sid, facts):
    return ((facts or {}).get(sid) or {}).get('name') or sid


def _exits_of(sid, facts):
    return {e['serverId'] for e in ((facts or {}).get(sid) or {}).get('front') or [] if isinstance(e, dict) and e.get('serverId')}


def _is_front(sid, facts):
    return bool(_exits_of(sid, facts))


def _front_allows(a, b, facts):
    if _is_front(a, facts) and b not in _exits_of(a, facts):
        return False
    if _is_front(b, facts) and a not in _exits_of(b, facts):
        return False
    return True


def _tls_ok(sid, facts):
    tls = ((facts or {}).get(sid) or {}).get('tls') or {}
    return bool((tls.get('active') or {}).get('fingerprint') or (tls.get('pending') or {}).get('fingerprint'))


def _can_dial_transport(target, transport, facts):
    if not can_dial(target, transport, facts):
        return False
    if transport == 'tls':
        # When facts specify tls=None or omit tls in unit tests without TLS setup, allow unless tls is explicitly empty dict;
        # wait: in validate(applying=True), TLS requires _pins(facts, target). If pins are missing and any other transport works, prefer that.
        return True
    return True


def _pick_relay_link(a, b, doc, live, facts):
    """Return a valid relayLink dict {a, b, transport, cost, dialer, bias} for relays a and b, or None."""
    ma, mb = live[a], live[b]
    if is_site(ma) and is_site(mb):
        return None
    if not _front_allows(a, b, facts):
        return None
    limits = doc.get('limits') or []
    allowed = mesh_limits.transports_for(limits, ma, mb, facts, automatic=True) if limits else list(TRANSPORTS)
    if not allowed:
        return None
    rules = doc.get('lineRules') or []
    bias = mesh_auto.bias_default(ma, mb, facts, rules)[0]
    if is_site(ma) or is_site(mb):
        site_m, pub_m = (ma, mb) if is_site(ma) else (mb, ma)
        dialer = 'a' if site_m is ma else 'b'
        if _is_front(pub_m['serverId'], facts):
            for t in ('tls', 'tcp', 'udp'):
                if t in allowed and pub_m['listen'].get(t) and (t != 'tls' or _tls_ok(pub_m['serverId'], facts) or not (pub_m['listen'].get('tcp') or pub_m['listen'].get('udp'))):
                    return {'a': a, 'b': b, 'transport': t, 'cost': 1, 'dialer': dialer, 'bias': bias}
            return None
        order = mesh_auto._order_for(site_m, pub_m, 'lines', facts, rules)[0]
        for t in [*order, *TRANSPORTS]:
            if t in allowed and can_dial(pub_m, t, facts) and (t != 'tls' or _tls_ok(pub_m['serverId'], facts)):
                return {'a': a, 'b': b, 'transport': t, 'cost': 1, 'dialer': dialer, 'bias': bias}
        for t in [*order, *TRANSPORTS]:
            if t in allowed and can_dial(pub_m, t, facts):
                return {'a': a, 'b': b, 'transport': t, 'cost': 1, 'dialer': dialer, 'bias': bias}
        return None

    for front_m, exit_m, side in ((ma, mb, 'a'), (mb, ma, 'b')):
        if exit_m['serverId'] in _exits_of(front_m['serverId'], facts):
            for t in ('tls', 'tcp', 'udp'):
                if t in allowed and exit_m['listen'].get(t) and (t != 'udp' or front_m['listen'].get('udp')):
                    return {'a': a, 'b': b, 'transport': t, 'cost': 1, 'dialer': side, 'bias': bias}
            return None

    pref_side = mesh_auto.dialer_default(ma, mb, facts, rules)[0]
    sides = ([pref_side, 'b' if pref_side == 'a' else 'a'] if pref_side else ['a', 'b'])
    order = mesh_auto._order_for(ma, mb, 'lines', facts, rules)[0]
    transports = list(dict.fromkeys([*order, *TRANSPORTS]))
    for require_pins in (True, False):
        for side in sides:
            dialer_m, target_m = (ma, mb) if side == 'a' else (mb, ma)
            for t in transports:
                if t in allowed and can_dial(target_m, t, facts) and (not require_pins or t != 'tls' or _tls_ok(target_m['serverId'], facts)):
                    return {'a': a, 'b': b, 'transport': t, 'cost': 1, 'dialer': side, 'bias': bias}
    return None


def _pick_member_upstream(member_id, relay_id, doc, live, facts):
    """Return a valid entry candidate dict {relay, transport} for member_id -> relay_id, or None."""
    m, r = live[member_id], live[relay_id]
    if m['role'] != 'member' or r['role'] != 'relay':
        return None
    # A fixed or failover entry uses its first relay: another would not carry anything.
    if (m.get('entry') or {}).get('mode') != 'upstreams' or not m.get('direct', True):
        return None
    if not _front_allows(member_id, relay_id, facts):
        return None
    candidates = (m.get('entry') or {}).get('candidates') or []
    if len(candidates) >= 8:
        return None
    if any(c['relay'] == relay_id for c in candidates):
        return None
    limits = doc.get('limits') or []
    allowed = mesh_limits.transports_for(limits, m, r, facts, automatic=True) if limits else list(TRANSPORTS)
    if not allowed:
        return None
    if is_site(r):
        if 'udp' not in allowed or not r['listen'].get('udp') or not m['listen'].get('udp'):
            return None
        entry_relays = [live[c['relay']] for c in candidates if live.get(c['relay'], {}).get('role') == 'relay']
        has_bootstrap = any(not is_site(er) or lan_endpoint(m, er, facts) for er in [*entry_relays, r])
        if not has_bootstrap:
            return None
        return {'relay': relay_id, 'transport': 'udp'}
    rules = doc.get('lineRules') or []
    order = mesh_auto._order_for(m, r, 'entries', facts, rules)[0]
    transports = list(dict.fromkeys([*order, *TRANSPORTS]))
    for require_pins in (True, False):
        for t in transports:
            if t in allowed and can_dial(r, t, facts) and (not require_pins or t != 'tls' or _tls_ok(relay_id, facts)):
                return {'relay': relay_id, 'transport': t}
    return None


def candidate_spec(a, b, doc, live, facts):
    """If (a, b) is an allowed candidate for auto-linking, return {'kind': 'relay'|'entry', ...}, else None."""
    ma, mb = live.get(a), live.get(b)
    if not ma or not mb or a == b:
        return None
    if ma['role'] == 'relay' and mb['role'] == 'relay':
        x, y = sorted((a, b))
        pair = _pick_relay_link(x, y, doc, live, facts)
        return {'kind': 'relay', 'a': x, 'b': y, 'pair': pair} if pair else None
    if ma['role'] == 'member' and mb['role'] == 'relay':
        cand = _pick_member_upstream(a, b, doc, live, facts)
        return {'kind': 'entry', 'member': a, 'relay': b, 'candidate': cand} if cand else None
    if ma['role'] == 'relay' and mb['role'] == 'member':
        cand = _pick_member_upstream(b, a, doc, live, facts)
        return {'kind': 'entry', 'member': b, 'relay': a, 'candidate': cand} if cand else None
    return None


def probe_targets(doc, facts, statuses=None):
    """{serverId: {'targets': [{'peer', 'host', 'udp', 'tcp', 'tls'}], 'intervalS': 300}}
    for every live member/relay in doc."""
    doc = mesh_auto.resolve(doc, facts)
    live = _live(doc)
    existing = existing_links(doc, facts)
    out = {}
    for sid, m in sorted(live.items()):
        if m['role'] == 'member' and not m.get('direct', True):
            out[sid] = {'targets': [], 'intervalS': PROBE_INTERVAL_S}
            continue
        targets = []
        for other_id, other in sorted(live.items()):
            if other_id == sid or frozenset((sid, other_id)) in existing:
                continue
            spec = candidate_spec(sid, other_id, doc, live, facts)
            if not spec:
                continue
            if is_site(other):
                continue
            host = front_exits(facts, sid).get(other_id, '') or endpoint_host(other, facts)
            if not host:
                continue
            udp = other['listen'].get('udp', 0) if front_exits(facts, sid).get(other_id) else public_port(other, 'udp') if other['listen'].get('udp') else 0
            tcp = other['listen'].get('tcp', 0) if front_exits(facts, sid).get(other_id) else public_port(other, 'tcp') if other['listen'].get('tcp') else 0
            tls = other['listen'].get('tls', 0) if front_exits(facts, sid).get(other_id) else public_port(other, 'tls') if other['listen'].get('tls') else 0
            if not (udp or tcp or tls):
                continue
            targets.append({'peer': other_id, 'host': host, **({'udp': udp} if udp else {}),
                            **({'tcp': tcp} if tcp else {}), **({'tls': tls} if tls else {})})
            if len(targets) >= MAX_PROBE_TARGETS:
                break
        out[sid] = {'targets': targets, 'intervalS': PROBE_INTERVAL_S}
    return out


def existing_links(doc, facts):
    """Set of frozenset((a, b)) currently connected in doc."""
    live = _live(doc)
    links = set()
    for p in relay_pairs(doc, facts):
        if p['a'] in live and p['b'] in live and p['a'] != p['b']:
            links.add(frozenset((p['a'], p['b'])))
    for m in live.values():
        if m['role'] == 'member':
            for c in (m.get('entry') or {}).get('candidates') or []:
                if c['relay'] in live and live[c['relay']]['role'] == 'relay':
                    links.add(frozenset((m['serverId'], c['relay'])))
    for d in doc.get('directLinks') or []:
        if d['a'] in live and d['b'] in live and d['a'] != d['b']:
            links.add(frozenset((d['a'], d['b'])))
    return links


def auto_keys_in_doc(doc):
    """{pair_key(a, b): info} for every auto link currently present in doc."""
    live = _live(doc)
    out = {}
    for p in doc['relayLinks'].get('pairs') or []:
        if p.get('origin') == 'auto' and p['a'] in live and p['b'] in live:
            out[pair_key(p['a'], p['b'])] = {'kind': 'relay', 'a': p['a'], 'b': p['b'], 'since': p.get('since', 0), 'reason': p.get('reason', '')}
    for m in live.values():
        if m['role'] != 'member':
            continue
        for c in (m.get('entry') or {}).get('candidates') or []:
            if c.get('origin') == 'auto' and c['relay'] in live:
                out[pair_key(m['serverId'], c['relay'])] = {'kind': 'entry', 'member': m['serverId'], 'relay': c['relay'],
                                                             'since': c.get('since', 0), 'reason': c.get('reason', '')}
    return out


def _status_of(statuses, sid):
    if not statuses:
        return None
    rec = statuses.get(sid)
    if not isinstance(rec, dict):
        return None
    return rec.get('status') if isinstance(rec.get('status'), dict) else rec


def _cost_formula(rtt, jitter=0.0, loss=0.0, bias=0.0):
    if rtt is None or rtt <= 0:
        return None
    jitter = float(jitter or 0.0)
    loss = float(loss or 0.0)
    return max(MIN_COST, float(rtt) + 2.0 * jitter + 1000.0 * max(0.0, loss) + float(bias or 0.0))


def collect_measurements(doc, facts, statuses=None, probes=None, unreachable=None):
    """Return (real_costs, probe_costs) where each maps frozenset((a, b)) -> raw cost (without link bias).
    statuses may be {sid: {'status': ...}} or {sid: status_dict}.
    probes may be passed explicitly as {(a, b)|'a|b'|frozenset((a,b)): {'rtt'|'rttMs', 'jitter'|'jitterMs', 'loss'} or float}
    in addition to status['probes']. unreachable, a set when given, gets the pairs a probe found nothing on."""
    live = _live(doc)
    real_costs = {}
    probe_costs = {}
    # A pair one end probes and gets nothing back from: the other end's probe does not make up for it.
    unreachable = set() if unreachable is None else unreachable

    for sid in live:
        st = _status_of(statuses, sid)
        if not st:
            continue
        for l in ((st.get('routing') or {}).get('links') or []):
            peer = l.get('peer')
            if peer not in live or l.get('state') != 'up':
                continue
            k = frozenset((sid, peer))
            # From what was measured: the cost the node reports has its bias in already.
            val = _cost_formula(l.get('rttMs'), l.get('jitterMs', 0.0), l.get('loss', 0.0))
            if val is not None:
                real_costs[k] = max(real_costs.get(k, 0.0), val)
        entry = st.get('entry') or {}
        for c in entry.get('candidates') or []:
            relay = c.get('relay')
            if relay in live and c.get('state') in ('active', 'ready') and c.get('rttMs'):
                val = _cost_formula(c.get('rttMs'), 0.0, c.get('timeoutRate', c.get('lossRate', 0.0)))
                if val is not None:
                    k = frozenset((sid, relay))
                    real_costs[k] = min(real_costs.get(k, val), val)
        for p in st.get('peers') or []:
            peer = p.get('id')
            if peer not in live or not p.get('active') or not p.get('rttMs'):
                continue
            k = frozenset((sid, peer))
            if k not in real_costs:
                val = _cost_formula(p.get('rttMs'), 0.0, p.get('timeoutRate', 0.0))
                if val is not None:
                    real_costs[k] = val
        for pr in st.get('probes') or []:
            peer = pr.get('peer')
            if peer not in live:
                continue
            k = frozenset((sid, peer))
            if pr.get('rttMs') is None:
                if float(pr.get('loss') or 0.0) >= 1.0:
                    unreachable.add(k)
                continue
            val = _cost_formula(pr.get('rttMs'), pr.get('jitterMs', 0.0), pr.get('loss', 0.0))
            if val is not None:
                probe_costs[k] = max(probe_costs.get(k, val), val)

    if isinstance(probes, dict):
        for raw_k, raw_v in probes.items():
            if isinstance(raw_k, frozenset) and len(raw_k) == 2:
                a, b = tuple(raw_k)
            elif isinstance(raw_k, tuple) and len(raw_k) == 2:
                a, b = raw_k
            elif isinstance(raw_k, str) and '|' in raw_k:
                a, b = _split_key(raw_k)
            else:
                continue
            if a not in live or b not in live or a == b:
                continue
            k = frozenset((a, b))
            if isinstance(raw_v, (int, float)) and not isinstance(raw_v, bool):
                val = float(raw_v) if raw_v > 0 else None
            elif isinstance(raw_v, dict):
                rtt = raw_v.get('rttMs', raw_v.get('rtt'))
                jitter = raw_v.get('jitterMs', raw_v.get('jitter', 0.0))
                loss = raw_v.get('loss', 0.0)
                val = _cost_formula(rtt, jitter, loss)
            else:
                val = None
            if val is not None:
                probe_costs[k] = max(probe_costs.get(k, val), val)

    for k in unreachable:
        probe_costs.pop(k, None)
    return real_costs, probe_costs


def _pair_bias_map(pairs):
    return {frozenset((p['a'], p['b'])): float(p.get('bias', 0) or 0) for p in pairs}


def _link_bias_fast(live, pair_bias, rules, facts, a, b, bias_cache):
    k = frozenset((a, b))
    if k in pair_bias:
        return pair_bias[k]
    if k in bias_cache:
        return bias_cache[k]
    if a in live and b in live and live[a]['role'] == 'relay' and live[b]['role'] == 'relay':
        val = float(mesh_auto.bias_default(live[a], live[b], facts, rules)[0] or 0)
    else:
        val = 0.0
    bias_cache[k] = val
    return val


def link_bias(doc, facts, a, b):
    live = _live(doc)
    for p in relay_pairs(doc, facts):
        if frozenset((p['a'], p['b'])) == frozenset((a, b)):
            return float(p.get('bias', 0) or 0)
    if a in live and b in live and live[a]['role'] == 'relay' and live[b]['role'] == 'relay':
        rules = doc.get('lineRules') or []
        return float(mesh_auto.bias_default(live[a], live[b], facts, rules)[0] or 0)
    return 0.0


def _edge_cost_fast(k, a, b, live, pair_bias, rules, facts, real_costs, probe_costs, bias_cache, *, use_real=True):
    if use_real and k in real_costs:
        bias = _link_bias_fast(live, pair_bias, rules, facts, a, b, bias_cache)
        return max(MIN_COST, real_costs[k] + bias), 'real'
    if k in probe_costs:
        bias = _link_bias_fast(live, pair_bias, rules, facts, a, b, bias_cache)
        return max(MIN_COST, probe_costs[k] + bias), 'probe'
    return None, ''


def edge_cost(a, b, doc, facts, real_costs, probe_costs, *, use_real=True):
    """Return (cost, source) where source is 'real' or 'probe', or (None, '')."""
    k = frozenset((a, b))
    bias = link_bias(doc, facts, a, b)
    if use_real and k in real_costs:
        return max(MIN_COST, real_costs[k] + bias), 'real'
    if k in probe_costs:
        return max(MIN_COST, probe_costs[k] + bias), 'probe'
    return None, ''


def dijkstra(g, src, transit):
    dist, prev, q = {src: 0.0}, {}, [(0.0, src)]
    while q:
        d, u = heapq.heappop(q)
        if d > dist.get(u, 1e18):
            continue
        if u != src and u not in transit:
            continue
        for v, w in g[u].items():
            nd = d + w
            if nd < dist.get(v, 1e18):
                dist[v], prev[v] = nd, u
                heapq.heappush(q, (nd, v))
    return dist, prev


def _dijkstra_dist(g, src, transit):
    dist, q = {src: 0.0}, [(0.0, src)]
    while q:
        d, u = heapq.heappop(q)
        if d > dist.get(u, 1e18):
            continue
        if u != src and u not in transit:
            continue
        for v, w in g[u].items():
            nd = d + w
            if nd < dist.get(v, 1e18):
                dist[v] = nd
                heapq.heappush(q, (nd, v))
    return dist


def _shortest_dists(g, relays):
    return {s: _dijkstra_dist(g, s, relays) for s in g}


def meets(cfg, building):
    """(meets(cut, before), floor): whether a node pair cut ms off a cost of before counts, and the
    least cut that can. Building: buildMs, or buildRatio of before and keepMs; keeping: keepMs."""
    keep, build, ratio = float(cfg['keepMs']), float(cfg['buildMs']), float(cfg['buildRatio'])
    if building:
        return (lambda cut, before: cut >= build or (cut >= keep and cut >= ratio * before)), min(keep, build)
    return (lambda cut, before: cut >= keep), keep


def _evaluate_dist_gains(before, after, meet):
    """(total, hits, best): the cuts that meet meet[0], summed and listed the most first, and the
    largest cut of any pair whether it meets it or not."""
    total, hits, best = 0.0, [], 0.0
    for s, b_row in before.items():
        a_row = after.get(s)
        if not a_row:
            continue
        for t, b_cost in b_row.items():
            if t == s or t not in a_row:
                continue
            diff = b_cost - a_row[t]
            if diff > best:
                best = diff
            if diff > 0 and meet[0](diff, b_cost):
                total += diff
                hits.append((diff, (s, t), b_cost, a_row[t]))
    hits.sort(key=lambda x: (-x[0], x[1]))
    return total, hits, best


def _added_edge_score(current_dists, relays, a, b, w, meet):
    """Total gain of adding undirected edge (a, b) with cost w>0 under the relay-only transit rule."""
    ok, floor = meet
    a_relay, b_relay = a in relays, b in relays
    if not (a_relay or b_relay):
        return 0.0
    # Triangle inequality: no path through (a, b) can improve by more than D[a][b] - w.
    dab = current_dists[a].get(b)
    if dab is not None and dab - w < floor - 1e-9:
        return 0.0
    total = 0.0
    if a_relay and b_relay:
        da_out = current_dists[a]
        db_out = current_dists[b]
        for s, row in current_dists.items():
            sa = row.get(a)
            sb = row.get(b)
            if sa is None and sb is None:
                continue
            sa_w = sa + w if sa is not None else None
            sb_w = sb + w if sb is not None else None
            for t, b_cost in row.items():
                if t == s:
                    continue
                best = b_cost
                if sa_w is not None:
                    bt = db_out.get(t)
                    if bt is not None:
                        v = sa_w + bt
                        if v < best:
                            best = v
                if sb_w is not None:
                    at = da_out.get(t)
                    if at is not None:
                        v = sb_w + at
                        if v < best:
                            best = v
                if best < b_cost and ok(b_cost - best, b_cost):
                    total += b_cost - best
        return total
    # A non-transit member can only be the path's source or destination.
    m, r = (b, a) if a_relay else (a, b)
    dm = current_dists[m]
    dr = current_dists[r]
    for t, b_cost in dm.items():
        if t == m:
            continue
        rt = dr.get(t)
        if rt is not None:
            v = w + rt
            if v < b_cost and ok(b_cost - v, b_cost):
                total += b_cost - v
    for s, row in current_dists.items():
        if s == m:
            continue
        b_cost = row.get(m)
        if b_cost is None:
            continue
        sr = row.get(r)
        if sr is not None:
            v = sr + w
            if v < b_cost and ok(b_cost - v, b_cost):
                total += b_cost - v
    return total


def _collect_base_edges(doc, live, pairs):
    edges = set()
    for p in pairs:
        if p['a'] in live and p['b'] in live and p['a'] != p['b']:
            edges.add(frozenset((p['a'], p['b'])))
    for m in live.values():
        if m['role'] == 'member':
            for c in (m.get('entry') or {}).get('candidates') or []:
                if c['relay'] in live and live[c['relay']]['role'] == 'relay':
                    edges.add(frozenset((m['serverId'], c['relay'])))
    for d in doc.get('directLinks') or []:
        if d['a'] in live and d['b'] in live and d['a'] != d['b']:
            edges.add(frozenset((d['a'], d['b'])))
    return edges


def _graph_from_costs(live, edges, costs, *, exclude=frozenset()):
    g = {sid: {} for sid in live}
    for k in edges:
        if k in exclude:
            continue
        cst = costs.get(k)
        if cst is not None:
            a, b = tuple(k)
            g[a][b] = g[b][a] = cst
    return g


def _path(prev, src, dst):
    out = [dst]
    while out[-1] != src:
        out.append(prev[out[-1]])
    return out[::-1]


def shortest_table(g, relays):
    """{(s, t): (cost, path)} for every ordered pair of distinct nodes reachable in g where only relays transit."""
    out = {}
    for s in g:
        dist, prev = dijkstra(g, s, relays)
        for t, d in dist.items():
            if t != s:
                out[(s, t)] = (d, _path(prev, s, t))
    return out


def build_graph(doc, facts, real_costs, probe_costs, *, exclude=frozenset(), extra=(), use_real=True):
    """Build adjacency graph {sid: {peer: cost}} for active nodes in doc."""
    live = _live(doc)
    pairs = relay_pairs(doc, facts)
    pair_bias = _pair_bias_map(pairs)
    rules = doc.get('lineRules') or []
    bias_cache = {}
    g = {sid: {} for sid in live}
    edges = _collect_base_edges(doc, live, pairs)
    edges -= set(exclude)
    for a, b in extra:
        if a in live and b in live and a != b:
            edges.add(frozenset((a, b)))
    for k in edges:
        a, b = tuple(k)
        cst, _ = _edge_cost_fast(k, a, b, live, pair_bias, rules, facts, real_costs, probe_costs, bias_cache, use_real=use_real)
        if cst is not None:
            g[a][b] = g[b][a] = cst
    return g


def evaluate_gains(before, after, meet=None):
    """Return (total_gain, hits) where hits is sorted [(gain, (s, t), before_cost, after_cost), ...] descending;
    meet is meets(...) of a candidate under the default settings unless given."""
    ok = (meet or meets(DEFAULT_SETTINGS, True))[0]
    total, hits = 0.0, []
    for k, (b_cost, _) in before.items():
        if k not in after:
            continue
        a_cost = after[k][0]
        if a_cost < b_cost and ok(b_cost - a_cost, b_cost):
            diff = b_cost - a_cost
            total += diff
            hits.append((diff, k, b_cost, a_cost))
    hits.sort(key=lambda x: (-x[0], x[1]))
    return total, hits


# How many of the node pairs a link makes faster are listed with it.
PAIRS_SHOWN = 30


def gain_pairs(hits):
    """(count, pairs): the node pairs a link makes faster, each once whichever way, the most first,
    as [{src, dst, before, after}] in ms; count is all of them, pairs the first PAIRS_SHOWN."""
    seen, out = set(), []
    for _, (s, t), b_cost, a_cost in hits:
        uk = frozenset((s, t))
        if uk in seen:
            continue
        seen.add(uk)
        out.append({'src': s, 'dst': t, 'before': round(b_cost, 1), 'after': round(a_cost, 1)})
    return len(out), out[:PAIRS_SHOWN]


def format_reason(hits, facts, cfg=None):
    """Format user-readable Chinese reason (≤500 chars), e.g. '让 App-SG → App-EU 从 276ms 降到 172ms 等 12 对';
    with cfg, how much faster and the threshold it met: '让 … 降到 172ms（快 104ms，新建门槛 15ms）等 12 对'."""
    if not hits:
        return ''
    count, pairs = gain_pairs(hits)
    top = pairs[0]
    s, t, b_cost, a_cost = top['src'], top['dst'], top['before'], top['after']
    ns, nt = _name(s, facts), _name(t, facts)
    text = f'让 {ns} → {nt} 从 {b_cost:.0f}ms 降到 {a_cost:.0f}ms'
    if cfg:
        cut = b_cost - a_cost
        if cut >= float(cfg['buildMs']):
            text += f'（快 {cut:.0f}ms，新建门槛 {_ms_text(cfg["buildMs"])}）'
        else:
            text += f'（快 {cut:.0f}ms、{cut / b_cost * 100:.0f}%，新建门槛 {float(cfg["buildRatio"]) * 100:.0f}% 且 {_ms_text(cfg["keepMs"])}）'
    if count > 1:
        text += f' 等 {count} 对'
    return text[:500]


def _ms_text(v):
    return f'{float(v):g}ms'


def _duration_text(ms):
    minutes = max(1, round(ms / 60000))
    if minutes < 60:
        return f'{minutes} 分钟'
    return f'{minutes // 60} 小时' + (f' {minutes % 60} 分钟' if minutes % 60 else '')


def format_revision_reason(added, dropped, facts):
    """Format revision reason containing '自动' and the change summary."""
    parts = []
    for item in added:
        na, nb = _name(item['a'], facts), _name(item['b'], facts)
        detail = item.get('shortReason') or item.get('reason') or ''
        parts.append(f'新建 {na} ↔ {nb}' + (f'（{detail}）' if detail else ''))
    for item in dropped:
        na, nb = _name(item['a'], facts), _name(item['b'], facts)
        why = item.get('why') or '不再改善选路'
        parts.append(f'拆除 {na} ↔ {nb}（{why}）')
    body = '；'.join(parts) if parts else '调整自动线路'
    return f'自动：{body}'[:500]


def record_user_deletions(before_doc, after_doc, state, now_ms):
    """Detect auto links present in before_doc that the user removed in after_doc, and suppress them for 7 days."""
    state = clean_state(state, now_ms)
    before_auto = auto_keys_in_doc(before_doc)
    after_all = existing_links(after_doc, {})
    for k, info in before_auto.items():
        a, b = _split_key(k)
        if frozenset((a, b)) not in after_all:
            state['suppressed'][k] = now_ms + SUPPRESS_TTL_MS
            state['watchSince'].pop(k, None)
            state['idleSince'].pop(k, None)
            state['idleBest'].pop(k, None)
    return state


def optimize(doc, facts, statuses=None, probes=None, *, state=None, now_ms=0, history=None, applied_ms=0):
    """Run one optimization round on the applied document. history, a History the caller keeps from
    round to round, smooths the costs and tells which nodes are settling; without it a round judges
    what it measures. applied_ms is when the applied revision went out: the mesh settles after it.
    Returns {
      'doc': updated_doc (same as input when enabled=False or no change),
      'changed': bool,
      'added': [{'key', 'a', 'b', 'kind', 'transport', 'cost', 'gain', 'reason', 'shortReason'}],
      'dropped': [{'key', 'a', 'b', 'kind', 'why', 'code'}], code deleted|unstable|disappointing|idle,
      'suggestions': [{'key', 'a', 'b', 'kind', 'transport', 'cost', 'gain', 'reason', 'since', 'heldMs', 'ready', 'paused'?}],
      'paused': bool, the whole mesh settling,
      'revisionReason': str,
      'state': updated_state,
      'lastChange': dict | None,
    }"""
    now_ms = int(now_ms or 0)
    # In 'auto' mode, if pairs are not yet populated, generate the baseline relay pairs first.
    if doc.get('relayLinks', {}).get('mode') == 'auto' and not (doc.get('relayLinks', {}).get('pairs')):
        doc = copy.deepcopy(doc)
        doc['relayLinks']['pairs'] = mesh_auto.auto_pairs(doc, facts)
    resolved = mesh_auto.resolve(doc, facts)
    cfg = settings_of(doc)
    enabled = bool(cfg['enabled'])
    per_node = int(cfg['perNode'])
    keep, build = meets(cfg, False), meets(cfg, True)
    hold_ms = int(cfg['holdS']) * 1000
    drop_after_ms = int(cfg['dropAfterS']) * 1000

    st = clean_state(state if state is not None else doc.get('_autoLinksState'), now_ms)
    # A clock that runs while the mesh settles stops: it gets this round's time back.
    elapsed = now_ms - st['measuredAt'] if 0 < st['measuredAt'] < now_ms else 0
    live = _live(resolved)
    relays = {sid for sid, m in live.items() if m['role'] == 'relay'}
    pairs = relay_pairs(resolved, facts)
    pair_bias = _pair_bias_map(pairs)
    rules = resolved.get('lineRules') or []
    bias_cache = {}
    base_edges = _collect_base_edges(resolved, live, pairs)
    unreachable = set()
    real_costs, probe_costs = collect_measurements(resolved, facts, statuses, probes, unreachable)
    # Up or down as measured now, for each auto link's health; the costs the round judges are smoothed.
    up_now = set(real_costs)
    if history is not None:
        history.record(now_ms, real_costs, probe_costs, base_edges, statuses, live)
        real_costs = history.smoothed('real')
        probe_costs = {k: v for k, v in history.smoothed('probe').items() if k not in unreachable}
    paused = bool(applied_ms and 0 <= now_ms - applied_ms < SETTLE_S * 1000)
    settling_nodes = settling(statuses, live, now_ms, history)

    def held(a, b):
        return paused or a in settling_nodes or b in settling_nodes

    real_edge_costs = {}
    probe_only_edge_costs = {}
    for k in base_edges:
        a, b = tuple(k)
        cst, _ = _edge_cost_fast(k, a, b, live, pair_bias, rules, facts, real_costs, probe_costs, bias_cache, use_real=True)
        if cst is not None:
            real_edge_costs[k] = cst
        pcst, _ = _edge_cost_fast(k, a, b, live, pair_bias, rules, facts, {}, probe_costs, bias_cache, use_real=False)
        if pcst is not None:
            probe_only_edge_costs[k] = pcst
    unstable = track_nodes(st, live, statuses, now_ms)

    # Step 1: check existing auto links (down most rounds, measured worse than probed, or idle for dropAfterS).
    current_auto = auto_keys_in_doc(resolved)
    dropped = []
    to_remove = set()
    from app.servers import mesh_paths  # it imports mesh_config, as this module does
    on_paths = mesh_paths.in_use(resolved, facts)
    # What each auto link gives now: the node pairs it keeps keepMs faster, as the lines page shows them.
    benefits = {}
    cached_gone = None
    cached_with_dists = None
    cached_probe_dists = None

    def forget(k):
        for part in ('idleSince', 'idleBest', 'watchSince'):
            st[part].pop(k, None)

    for k, info in sorted(current_auto.items()):
        a, b = _split_key(k)
        edge = frozenset((a, b))
        if _status_of(statuses, a) or _status_of(statuses, b):
            st['linkHealth'][k] = _smoothed(st['linkHealth'].get(k), 1.0 if edge in up_now else 0.0)
        if k in st['suppressed']:
            to_remove.add(k)
            dropped.append({'key': k, 'a': a, 'b': b, 'kind': info['kind'], 'why': '已手动删除', 'code': 'deleted'})
            continue
        gone = frozenset(frozenset(_split_key(x)) for x in to_remove)
        if gone != cached_gone:
            cached_gone = gone
            g_with = _graph_from_costs(live, base_edges, real_edge_costs, exclude=gone)
            cached_with_dists = _shortest_dists(g_with, relays)
            cached_probe_dists = None
        g_without = _graph_from_costs(live, base_edges, real_edge_costs, exclude=gone | {edge})
        t_without = _shortest_dists(g_without, relays)
        gain_real, hits, best = _evaluate_dist_gains(t_without, cached_with_dists, keep)
        count, pairs_shown = gain_pairs(hits)
        benefits[k] = {'kind': info['kind'], 'count': count, 'pairs': pairs_shown}
        # A fixed path goes over it: kept whatever it gains.
        if edge in on_paths:
            forget(k)
            continue

        health = st['linkHealth'].get(k) or {}
        if health.get('rounds', 0) >= HEALTH_ROUNDS and health['share'] < UNSTABLE_SHARE:
            # Down most rounds: what it gains when up is not worth waiting for, nor building again.
            _disappoint(st, k, now_ms)
            forget(k)
            to_remove.add(k)
            dropped.append({'key': k, 'a': a, 'b': b, 'kind': info['kind'], 'why': '实测不稳定' if health.get('up') else '实测连不上',
                            'code': 'unstable'})
            continue

        if gain_real > 0:
            forget(k)
            continue
        if held(a, b):
            if k in st['idleSince']:
                st['idleSince'][k] += elapsed
            continue
        since = st['idleSince'].setdefault(k, now_ms)
        st['idleBest'][k] = top = round(max(st['idleBest'].get(k, 0.0), best), 1)
        idle_ms = now_ms - since
        keep_text = f'不到保留门槛 {_ms_text(cfg["keepMs"])}'
        if edge in real_costs and idle_ms >= min(hold_ms, drop_after_ms):
            # Built on a probe that measured better than the link does: gone, and kept out a while.
            if cached_probe_dists is None:
                g_with_probe = _graph_from_costs(live, base_edges, probe_only_edge_costs, exclude=gone)
                cached_probe_dists = _shortest_dists(g_with_probe, relays)
            gain_probe, probe_hits, _ = _evaluate_dist_gains(t_without, cached_probe_dists, build)
            if gain_probe > 0:
                _disappoint(st, k, now_ms)
                forget(k)
                to_remove.add(k)
                dropped.append({'key': k, 'a': a, 'b': b, 'kind': info['kind'], 'code': 'disappointing',
                                'why': f'实测最好只快 {top:g}ms，{keep_text}；探测时预计快 {probe_hits[0][0]:.0f}ms'})
                continue
        if idle_ms >= drop_after_ms:
            forget(k)
            to_remove.add(k)
            dropped.append({'key': k, 'a': a, 'b': b, 'kind': info['kind'], 'code': 'idle',
                            'why': f'连续 {_duration_text(idle_ms)}最好只快 {top:g}ms，{keep_text}'})

    for part in ('idleSince', 'idleBest', 'linkHealth'):
        for k in list(st[part]):
            if k not in current_auto:
                st[part].pop(k, None)

    # Count how many auto links each node currently uses (after removals).
    used = {sid: 0 for sid in live}
    for k in current_auto:
        if k not in to_remove:
            a, b = _split_key(k)
            if a in used:
                used[a] += 1
            if b in used:
                used[b] += 1

    # Step 2: greedy selection of new candidate links, none with a settling end.
    removed_edges = frozenset(frozenset(_split_key(k)) for k in to_remove)
    present_edges = set(base_edges) - set(removed_edges)
    base_graph = _graph_from_costs(live, base_edges, real_edge_costs, exclude=removed_edges)
    current_dists = cached_with_dists if cached_gone == removed_edges and cached_with_dists is not None else _shortest_dists(base_graph, relays)

    chosen = []
    nodes_sorted = sorted(live)
    candidates = []
    if per_node > 0:
        for a, b in itertools.combinations(nodes_sorted, 2):
            edge = frozenset((a, b))
            k = pair_key(a, b)
            if edge in present_edges or k in st['suppressed'] or k in st['disappointed'] or a in unstable or b in unstable or held(a, b):
                continue
            spec = candidate_spec(a, b, resolved, live, facts)
            if not spec:
                continue
            cst, _ = _edge_cost_fast(edge, a, b, live, pair_bias, rules, facts, real_costs, probe_costs, bias_cache, use_real=True)
            if cst is None:
                continue
            candidates.append((a, b, edge, k, spec, cst))

    skipped = set()
    while candidates:
        best = None
        for a, b, edge, k, spec, cst in candidates:
            if edge in present_edges or edge in skipped:
                continue
            if used.get(a, 0) >= per_node or used.get(b, 0) >= per_node:
                continue
            score = _added_edge_score(current_dists, relays, a, b, cst, build)
            if score > 0 and (best is None or score > best[0]):
                best = (score, a, b, edge, k, spec, cst)
        if not best:
            break
        _, a, b, edge, k, spec, cst = best
        base_graph[a][b] = base_graph[b][a] = cst
        next_dists = _shortest_dists(base_graph, relays)
        score, hits, _ = _evaluate_dist_gains(current_dists, next_dists, build)
        if score <= 0 or not hits:
            base_graph[a].pop(b, None)
            base_graph[b].pop(a, None)
            skipped.add(edge)
            continue
        current_dists = next_dists
        skipped.clear()
        present_edges.add(edge)
        used[a] += 1
        used[b] += 1
        reason = format_reason(hits, facts, cfg)
        count, pairs_shown = gain_pairs(hits)
        transport = spec['pair']['transport'] if spec['kind'] == 'relay' else spec['candidate']['transport']
        chosen.append({
            'count': count,
            'pairs': pairs_shown,
            'key': k,
            'a': a,
            'b': b,
            'kind': spec['kind'],
            'spec': spec,
            'transport': transport,
            'cost': round(cst, 1),
            'gain': round(score, 1),
            'reason': reason,
            'shortReason': reason[2:] if reason.startswith('让 ') else reason,
        })

    # A candidate holds from the round it was first chosen in a row; one left out while an end of it
    # settles keeps its time, its clock stopped.
    chosen_keys = {item['key'] for item in chosen}
    watching_before = {w['key']: w for w in st['watching']}
    watch = {item['key']: st['watchSince'].get(item['key'], now_ms) for item in chosen}
    for k, since in st['watchSince'].items():
        if k not in watch and held(*_split_key(k)) and frozenset(_split_key(k)) not in present_edges:
            watch[k] = since + elapsed
    st['watchSince'] = watch

    suggestions = []
    ready_to_add = []
    for item in chosen:
        since = watch[item['key']]
        ready = now_ms - since >= hold_ms
        suggestions.append({k: item[k] for k in ('key', 'a', 'b', 'kind', 'transport', 'cost', 'gain', 'reason', 'count', 'pairs')}
                           | {'since': since, 'heldMs': now_ms - since, 'ready': ready})
        if ready:
            ready_to_add.append(item)
    for k, since in watch.items():
        if k not in chosen_keys and k in watching_before:
            suggestions.append({**watching_before[k], 'since': since, 'heldMs': now_ms - since, 'ready': False, 'paused': True})
    st['benefits'], st['watching'], st['measuredAt'] = benefits, suggestions, now_ms
    st['paused'] = paused

    if not enabled or (not ready_to_add and not to_remove):
        out_doc = copy.deepcopy(doc)
        out_doc['_autoLinksState'] = st
        return {
            'doc': out_doc,
            'changed': False,
            'added': [],
            'dropped': [],
            'suggestions': suggestions,
            'benefits': benefits,
            'paused': paused,
            'revisionReason': '',
            'state': st,
            'lastChange': st.get('lastChange'),
        }

    out_doc = apply_auto_changes(doc, ready_to_add, to_remove, now_ms)
    rev_reason = format_revision_reason(ready_to_add, dropped, facts)
    last_change = {
        'at': now_ms,
        'reason': rev_reason,
        'added': [{'a': x['a'], 'b': x['b'], 'kind': x['kind'], 'transport': x['transport'], 'reason': x['reason'], 'count': x['count'], 'pairs': x['pairs']}
                  for x in ready_to_add],
        'dropped': [{'a': x['a'], 'b': x['b'], 'kind': x['kind'], 'why': x['why']} for x in dropped],
    }
    st['lastChange'] = last_change
    out_doc['_autoLinksState'] = st
    return {
        'doc': out_doc,
        'changed': True,
        'added': ready_to_add,
        'dropped': dropped,
        'suggestions': suggestions,
        'benefits': benefits,
        'paused': paused,
        'revisionReason': rev_reason,
        'state': st,
        'lastChange': last_change,
    }


def apply_auto_changes(doc, to_add, to_remove_keys, now_ms):
    """Return a copy of doc with to_remove_keys auto links removed and to_add auto links added."""
    out = copy.deepcopy(doc)
    remove_set = set(to_remove_keys or ())
    if remove_set:
        out['relayLinks']['pairs'] = [
            p for p in out['relayLinks']['pairs']
            if not (p.get('origin') == 'auto' and pair_key(p['a'], p['b']) in remove_set)
        ]
        for m in out['members']:
            if m['role'] == 'member':
                m['entry']['candidates'] = [
                    c for c in m['entry']['candidates']
                    if not (c.get('origin') == 'auto' and pair_key(m['serverId'], c['relay']) in remove_set)
                ]

    by_id = {m['serverId']: m for m in out['members'] if not m.get('removing')}
    existing_relay_pairs = {frozenset((p['a'], p['b'])) for p in out['relayLinks']['pairs']}
    for item in to_add or ():
        spec = item['spec']
        if spec['kind'] == 'relay':
            pair = dict(spec['pair'])
            if frozenset((pair['a'], pair['b'])) in existing_relay_pairs:
                continue
            pair.update(origin='auto', reason=item['reason'], since=now_ms)
            out['relayLinks']['pairs'].append(pair)
            existing_relay_pairs.add(frozenset((pair['a'], pair['b'])))
        elif spec['kind'] == 'entry':
            m = by_id.get(spec['member'])
            if not m or m['role'] != 'member':
                continue
            cands = m['entry']['candidates']
            if any(c['relay'] == spec['relay'] for c in cands) or len(cands) >= 8:
                continue
            used_ids = {c['id'] for c in cands if c.get('id')}
            n = 1
            while f'c{n}' in used_ids:
                n += 1
            cands.append({
                'id': f'c{n}',
                'relay': spec['relay'],
                'transport': spec['candidate']['transport'],
                'origin': 'auto',
                'reason': item['reason'],
                'since': now_ms,
            })
    return out


def _strip_internal(doc):
    if not isinstance(doc, dict) or not ({'_autoLinksState', '_autoBase'} & set(doc)):
        return doc
    return {k: v for k, v in doc.items() if k not in ('_autoLinksState', '_autoBase')}


def align_draft(draft, before_applied, after_applied):
    """Align draft onto after_applied when an automatic revision only added/removed auto links on before_applied,
    preserving all user edits in draft."""
    if _strip_internal(draft) == _strip_internal(before_applied):
        return copy.deepcopy(after_applied)
    before_auto = auto_keys_in_doc(before_applied)
    after_auto = auto_keys_in_doc(after_applied)
    removed_keys = set(before_auto) - set(after_auto)
    added_keys = set(after_auto) - set(before_auto)

    out = copy.deepcopy(draft)
    if removed_keys:
        out['relayLinks']['pairs'] = [
            p for p in out['relayLinks']['pairs']
            if not (p.get('origin') == 'auto' and pair_key(p['a'], p['b']) in removed_keys)
        ]
        for m in out['members']:
            if m['role'] == 'member':
                m['entry']['candidates'] = [
                    c for c in m['entry']['candidates']
                    if not (c.get('origin') == 'auto' and pair_key(m['serverId'], c['relay']) in removed_keys)
                ]

    draft_live = _live(out)
    draft_relay_pairs = {frozenset((p['a'], p['b'])): p for p in out['relayLinks']['pairs']}
    for p in after_applied['relayLinks']['pairs']:
        if p.get('origin') != 'auto':
            continue
        k = frozenset((p['a'], p['b']))
        if p['a'] not in draft_live or p['b'] not in draft_live:
            continue
        if draft_live[p['a']]['role'] != 'relay' or draft_live[p['b']]['role'] != 'relay':
            continue
        if k in draft_relay_pairs:
            if draft_relay_pairs[k].get('origin') == 'auto':
                draft_relay_pairs[k].update(reason=p.get('reason', ''), since=p.get('since', 0))
            continue
        if pair_key(p['a'], p['b']) not in added_keys:
            continue
        out['relayLinks']['pairs'].append(copy.deepcopy(p))
        draft_relay_pairs[k] = out['relayLinks']['pairs'][-1]

    after_members = {m['serverId']: m for m in after_applied['members'] if not m.get('removing')}
    for sid, m in draft_live.items():
        if m['role'] != 'member' or sid not in after_members:
            continue
        cands = m['entry']['candidates']
        by_relay = {c['relay']: c for c in cands}
        used_ids = {c['id'] for c in cands if c.get('id')}
        for ac in (after_members[sid].get('entry') or {}).get('candidates') or []:
            if ac.get('origin') != 'auto' or ac['relay'] not in draft_live or draft_live[ac['relay']]['role'] != 'relay':
                continue
            if ac['relay'] in by_relay:
                if by_relay[ac['relay']].get('origin') == 'auto':
                    by_relay[ac['relay']].update(reason=ac.get('reason', ''), since=ac.get('since', 0))
                continue
            if pair_key(sid, ac['relay']) not in added_keys or len(cands) >= 8:
                continue
            cid = ac.get('id') or ''
            if not cid or cid in used_ids:
                n = 1
                while f'c{n}' in used_ids:
                    n += 1
                cid = f'c{n}'
            used_ids.add(cid)
            cands.append({**copy.deepcopy(ac), 'id': cid})

    if '_autoLinksState' in after_applied:
        out['_autoLinksState'] = copy.deepcopy(after_applied['_autoLinksState'])
    return out


def view_of(doc, facts=None, statuses=None, probes=None, now_ms=0):
    """The autoLinks section of GET /api/servers/mesh, as the last optimizer round left it (it runs every
    AUTO_LOOP_INTERVAL_S), so the page reads the same between rounds: suggestions (candidates watched,
    with since when they have held), lastChange, benefits {pair key: {kind, count, pairs}} of the auto
    links in use, idleSince {pair key: ms} of those that keep no pair keepMs faster (dropped after
    dropAfterS of it), idleBest {pair key: ms} the most they did meanwhile, measuredAt, when that round
    ran, paused, whether the mesh was settling then, and the thresholds."""
    st = clean_state(doc.get('_autoLinksState'), now_ms)
    cfg = settings_of(doc)
    return {
        'suggestions': st['watching'],
        'lastChange': st.get('lastChange'),
        'benefits': st['benefits'],
        'idleSince': st['idleSince'],
        'idleBest': st['idleBest'],
        'measuredAt': st['measuredAt'],
        'paused': st['paused'],
        **{k: cfg[k] for k in ('buildMs', 'buildRatio', 'keepMs', 'holdS', 'dropAfterS')},
    }

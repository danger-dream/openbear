"""Automatic link building (design 2.3): probe candidate generation, the greedy
shortest-path optimizer over relays and non-transit members, flap/disappointment
damping, 7-day suppression of auto links deleted by the user, and merging auto
changes with a dirty draft."""
from __future__ import annotations

import copy
import heapq
import itertools

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

DEFAULT_SETTINGS = {
    'enabled': True,
    'perNode': 4,
    'gainRatio': 0.85,
    'gainMs': 20,
    'holdRounds': 3,
    'dropAfterS': 3600,
}
SETTINGS_KEYS = frozenset(DEFAULT_SETTINGS) | {'dropAfter'}
MAX_PROBE_TARGETS = 64
PROBE_INTERVAL_S = 300
LOSS_CAP = 0.05
MIN_COST = 0.1
DISAPPOINT_TTL_MS = 24 * 3600_000
SUPPRESS_TTL_MS = 7 * 24 * 3600_000
AUTO_REVISION_INTERVAL_MS = 10 * 60_000
AUTO_LOOP_INTERVAL_S = 60


def default_settings():
    return dict(DEFAULT_SETTINGS)


def enabled(doc):
    """Whether the mesh adjusts its links itself: no rounds, no probes, no revisions while off."""
    return bool(((doc.get('settings') or {}).get('autoLinks') or {}).get('enabled', DEFAULT_SETTINGS['enabled']))


def _int(value, low, high):
    return type(value) is int and low <= value <= high


def _num(value, low, high):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and low <= float(value) <= high


def clean_settings(raw, prior, errors):
    out = dict(DEFAULT_SETTINGS, **(prior or {}))
    out.pop('dropAfter', None)
    if not isinstance(raw, dict) or set(raw) - SETTINGS_KEYS:
        errors.add('settings.autoLinks', 'invalid_auto_links', '自动调整线路设置格式不正确')
        return out
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
    if 'gainRatio' in raw:
        if not _num(raw['gainRatio'], 0.1, 0.99):
            errors.add('settings.autoLinks.gainRatio', 'invalid_auto_links', '改善比例须在 0.1～0.99 之间')
        else:
            out['gainRatio'] = round(float(raw['gainRatio']), 4)
    if 'gainMs' in raw:
        if not _num(raw['gainMs'], 1, 10000):
            errors.add('settings.autoLinks.gainMs', 'invalid_auto_links', '最小改善毫秒数须在 1～10000 之间')
        else:
            out['gainMs'] = int(raw['gainMs']) if float(raw['gainMs']).is_integer() else round(float(raw['gainMs']), 1)
    if 'holdRounds' in raw:
        if not _int(raw['holdRounds'], 1, 20):
            errors.add('settings.autoLinks.holdRounds', 'invalid_auto_links', '连续确认轮数须在 1～20 之间')
        else:
            out['holdRounds'] = raw['holdRounds']
    drop_key = 'dropAfterS' if 'dropAfterS' in raw else 'dropAfter' if 'dropAfter' in raw else None
    if drop_key is not None:
        if not _int(raw[drop_key], 0, 7 * 86400):
            errors.add(f'settings.autoLinks.{drop_key}', 'invalid_auto_links', '自动拆线等待秒数须在 0～604800 之间')
        else:
            out['dropAfterS'] = raw[drop_key]
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
    streaks = {}
    for k, n in (raw.get('streaks') if isinstance(raw.get('streaks'), dict) else {}).items():
        if isinstance(k, str) and '|' in k and type(n) is int and n > 0:
            streaks[k] = n
    idle_since = {}
    for k, ts in (raw.get('idleSince') if isinstance(raw.get('idleSince'), dict) else {}).items():
        if isinstance(k, str) and '|' in k and isinstance(ts, (int, float)) and not isinstance(ts, bool) and int(ts) > 0:
            idle_since[k] = int(ts)
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
        'streaks': streaks,
        'idleSince': idle_since,
        'lastAutoRevisionMs': last_auto_ms,
        'lastChange': last_change,
        'benefits': benefits,
        'watching': watching,
        'measuredAt': int(measured) if isinstance(measured, (int, float)) and not isinstance(measured, bool) and measured > 0 else 0,
    }


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


def _cost_formula(rtt, jitter=0.0, loss=0.0, bias=0.0, cap_loss=True):
    if rtt is None or rtt <= 0:
        return None
    jitter = float(jitter or 0.0)
    loss = float(loss or 0.0)
    if cap_loss:
        loss = min(loss, LOSS_CAP)
    return max(MIN_COST, float(rtt) + 2.0 * jitter + 1000.0 * max(0.0, loss) + float(bias or 0.0))


def collect_measurements(doc, facts, statuses=None, probes=None):
    """Return (real_costs, probe_costs) where each maps frozenset((a, b)) -> raw cost (without link bias).
    statuses may be {sid: {'status': ...}} or {sid: status_dict}.
    probes may be passed explicitly as {(a, b)|'a|b'|frozenset((a,b)): {'rtt'|'rttMs', 'jitter'|'jitterMs', 'loss'} or float}
    in addition to status['probes']."""
    live = _live(doc)
    real_costs = {}
    probe_costs = {}

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
            val = _cost_formula(l.get('rttMs'), l.get('jitterMs', 0.0), l.get('loss', 0.0), cap_loss=False)
            if val is not None:
                real_costs[k] = max(real_costs.get(k, 0.0), val)
        entry = st.get('entry') or {}
        for c in entry.get('candidates') or []:
            relay = c.get('relay')
            if relay in live and c.get('state') in ('active', 'ready') and c.get('rttMs'):
                val = _cost_formula(c.get('rttMs'), 0.0, c.get('timeoutRate', c.get('lossRate', 0.0)), cap_loss=False)
                if val is not None:
                    k = frozenset((sid, relay))
                    real_costs[k] = min(real_costs.get(k, val), val)
        for p in st.get('peers') or []:
            peer = p.get('id')
            if peer not in live or not p.get('active') or not p.get('rttMs'):
                continue
            k = frozenset((sid, peer))
            if k not in real_costs:
                val = _cost_formula(p.get('rttMs'), 0.0, p.get('timeoutRate', 0.0), cap_loss=False)
                if val is not None:
                    real_costs[k] = val
        for pr in st.get('probes') or []:
            peer = pr.get('peer')
            if peer not in live or pr.get('rttMs') is None:
                continue
            val = _cost_formula(pr.get('rttMs'), pr.get('jitterMs', 0.0), pr.get('loss', 0.0), cap_loss=True)
            if val is not None:
                k = frozenset((sid, peer))
                probe_costs[k] = min(probe_costs.get(k, val), val)

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
                val = _cost_formula(rtt, jitter, loss, cap_loss=True)
            else:
                val = None
            if val is not None:
                probe_costs[k] = min(probe_costs.get(k, val), val)

    return real_costs, probe_costs


def link_bias(doc, facts, a, b):
    live = _live(doc)
    for p in relay_pairs(doc, facts):
        if frozenset((p['a'], p['b'])) == frozenset((a, b)):
            return float(p.get('bias', 0) or 0)
    if a in live and b in live and live[a]['role'] == 'relay' and live[b]['role'] == 'relay':
        rules = doc.get('lineRules') or []
        return float(mesh_auto.bias_default(live[a], live[b], facts, rules)[0] or 0)
    return 0.0


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
    g = {sid: {} for sid in live}
    edges = set()
    for p in relay_pairs(doc, facts):
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
    edges -= set(exclude)
    for a, b in extra:
        if a in live and b in live and a != b:
            edges.add(frozenset((a, b)))
    for k in edges:
        a, b = tuple(k)
        cst, _ = edge_cost(a, b, doc, facts, real_costs, probe_costs, use_real=use_real)
        if cst is not None:
            g[a][b] = g[b][a] = cst
    return g


def evaluate_gains(before, after, gain_ratio=0.85, gain_ms=20.0):
    """Return (total_gain, hits) where hits is sorted [(gain, (s, t), before_cost, after_cost), ...] descending."""
    total, hits = 0.0, []
    for k, (b_cost, _) in before.items():
        if k not in after:
            continue
        a_cost = after[k][0]
        if a_cost < b_cost * gain_ratio and b_cost - a_cost >= gain_ms:
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


def format_reason(hits, facts):
    """Format user-readable Chinese reason (≤500 chars), e.g. '让 App-SG → App-EU 从 276ms 降到 172ms 等 12 对'."""
    if not hits:
        return ''
    count, pairs = gain_pairs(hits)
    top = pairs[0]
    s, t, b_cost, a_cost = top['src'], top['dst'], top['before'], top['after']
    ns, nt = _name(s, facts), _name(t, facts)
    if count <= 1:
        text = f'让 {ns} → {nt} 从 {b_cost:.0f}ms 降到 {a_cost:.0f}ms'
    else:
        text = f'让 {ns} → {nt} 从 {b_cost:.0f}ms 降到 {a_cost:.0f}ms 等 {count} 对'
    return text[:500]


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
            state['streaks'].pop(k, None)
            state['idleSince'].pop(k, None)
    return state


def optimize(doc, facts, statuses=None, probes=None, *, state=None, now_ms=0):
    """Run one optimization round on the applied document.
    Returns {
      'doc': updated_doc (same as input when enabled=False or no change),
      'changed': bool,
      'added': [{'key', 'a', 'b', 'kind', 'transport', 'cost', 'gain', 'reason', 'shortReason'}],
      'dropped': [{'key', 'a', 'b', 'kind', 'why'}],
      'suggestions': [{'key', 'a', 'b', 'kind', 'transport', 'cost', 'gain', 'reason', 'rounds', 'ready'}],
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
    cfg = dict(DEFAULT_SETTINGS, **(doc.get('settings', {}).get('autoLinks') or {}))
    enabled = bool(cfg.get('enabled', True))
    per_node = int(cfg.get('perNode', DEFAULT_SETTINGS['perNode']))
    gain_ratio = float(cfg.get('gainRatio', DEFAULT_SETTINGS['gainRatio']))
    gain_ms = float(cfg.get('gainMs', DEFAULT_SETTINGS['gainMs']))
    hold_rounds = int(cfg.get('holdRounds', DEFAULT_SETTINGS['holdRounds']))
    drop_after_ms = int(cfg.get('dropAfterS', DEFAULT_SETTINGS['dropAfterS'])) * 1000

    st = clean_state(state if state is not None else doc.get('_autoLinksState'), now_ms)
    live = _live(resolved)
    relays = {sid for sid, m in live.items() if m['role'] == 'relay'}
    real_costs, probe_costs = collect_measurements(resolved, facts, statuses, probes)

    # Step 1: check existing auto links (disappointment after real measurement, or idle for dropAfterS).
    current_auto = auto_keys_in_doc(resolved)
    dropped = []
    to_remove = set()
    from app.servers import mesh_paths  # it imports mesh_config, as this module does
    on_paths = mesh_paths.in_use(resolved, facts)
    # What each auto link gives now: the node pairs it makes faster, as the lines page shows them.
    benefits = {}
    for k, info in sorted(current_auto.items()):
        a, b = _split_key(k)
        edge = frozenset((a, b))
        if k in st['suppressed']:
            to_remove.add(k)
            dropped.append({'key': k, 'a': a, 'b': b, 'kind': info['kind'], 'why': '已手动删除'})
            continue
        gone = {frozenset(_split_key(x)) for x in to_remove}
        g_with = build_graph(resolved, facts, real_costs, probe_costs, exclude=gone, use_real=True)
        g_without = build_graph(resolved, facts, real_costs, probe_costs, exclude=gone | {edge}, use_real=True)
        t_with = shortest_table(g_with, relays)
        t_without = shortest_table(g_without, relays)
        gain_real, hits = evaluate_gains(t_without, t_with, gain_ratio=gain_ratio, gain_ms=gain_ms)
        count, pairs = gain_pairs(hits)
        benefits[k] = {'kind': info['kind'], 'count': count, 'pairs': pairs}
        # A fixed path goes over it: kept whatever it gains.
        if edge in on_paths:
            st['idleSince'].pop(k, None)
            continue

        if edge in real_costs and gain_real <= 0:
            # Check if probe had predicted a gain while real measurement says it does not pay off.
            g_with_probe = build_graph(resolved, facts, {}, probe_costs, exclude=gone, use_real=False)
            t_with_probe = shortest_table(g_with_probe, relays)
            gain_probe, _ = evaluate_gains(t_without, t_with_probe, gain_ratio=gain_ratio, gain_ms=gain_ms)
            if gain_probe > 0:
                st['disappointed'][k] = now_ms + DISAPPOINT_TTL_MS
                st['idleSince'].pop(k, None)
                st['streaks'].pop(k, None)
                to_remove.add(k)
                dropped.append({'key': k, 'a': a, 'b': b, 'kind': info['kind'], 'why': '实测不划算'})
                continue

        if gain_real > 0:
            st['idleSince'].pop(k, None)
        else:
            since = st['idleSince'].setdefault(k, now_ms)
            if now_ms - since >= drop_after_ms:
                st['idleSince'].pop(k, None)
                st['streaks'].pop(k, None)
                to_remove.add(k)
                dropped.append({'key': k, 'a': a, 'b': b, 'kind': info['kind'], 'why': '连续无改善'})

    for k in list(st['idleSince']):
        if k not in current_auto:
            st['idleSince'].pop(k, None)

    # Count how many auto links each node currently uses (after removals).
    used = {sid: 0 for sid in live}
    for k in current_auto:
        if k not in to_remove:
            a, b = _split_key(k)
            if a in used:
                used[a] += 1
            if b in used:
                used[b] += 1

    # Step 2: greedy selection of new candidate links.
    removed_edges = {frozenset(_split_key(k)) for k in to_remove}
    present_edges = existing_links(resolved, facts) - removed_edges
    base_graph = build_graph(resolved, facts, real_costs, probe_costs, exclude=removed_edges, use_real=True)
    current_table = shortest_table(base_graph, relays)

    chosen = []
    extra_edges = []
    nodes_sorted = sorted(live)
    while True:
        best = None
        for a, b in itertools.combinations(nodes_sorted, 2):
            edge = frozenset((a, b))
            k = pair_key(a, b)
            if edge in present_edges or k in st['suppressed'] or k in st['disappointed']:
                continue
            if used.get(a, 0) >= per_node or used.get(b, 0) >= per_node:
                continue
            spec = candidate_spec(a, b, resolved, live, facts)
            if not spec:
                continue
            cst, _ = edge_cost(a, b, resolved, facts, real_costs, probe_costs, use_real=True)
            if cst is None:
                continue
            trial_graph = build_graph(resolved, facts, real_costs, probe_costs, exclude=removed_edges, extra=[*extra_edges, (a, b)], use_real=True)
            trial_table = shortest_table(trial_graph, relays)
            score, hits = evaluate_gains(current_table, trial_table, gain_ratio=gain_ratio, gain_ms=gain_ms)
            if score > 0 and (best is None or score > best[0]):
                best = (score, (a, b), k, spec, hits, trial_table, cst)
        if not best:
            break
        score, (a, b), k, spec, hits, current_table, cst = best
        present_edges.add(frozenset((a, b)))
        extra_edges.append((a, b))
        used[a] += 1
        used[b] += 1
        reason = format_reason(hits, facts)
        count, pairs = gain_pairs(hits)
        transport = spec['pair']['transport'] if spec['kind'] == 'relay' else spec['candidate']['transport']
        chosen.append({
            'count': count,
            'pairs': pairs,
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

    # Update consecutive round streaks for chosen candidates; reset others.
    chosen_keys = {item['key'] for item in chosen}
    new_streaks = {}
    for item in chosen:
        k = item['key']
        new_streaks[k] = st['streaks'].get(k, 0) + 1
    st['streaks'] = new_streaks

    suggestions = []
    ready_to_add = []
    for item in chosen:
        rounds = st['streaks'][item['key']]
        ready = rounds >= hold_rounds
        suggestions.append({
            'key': item['key'],
            'a': item['a'],
            'b': item['b'],
            'kind': item['kind'],
            'transport': item['transport'],
            'cost': item['cost'],
            'gain': item['gain'],
            'reason': item['reason'],
            'count': item['count'],
            'pairs': item['pairs'],
            'rounds': rounds,
            'ready': ready,
        })
        if ready:
            ready_to_add.append(item)
    st['benefits'], st['watching'], st['measuredAt'] = benefits, suggestions, now_ms

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


def align_draft(draft, before_applied, after_applied):
    """Align draft onto after_applied when an automatic revision only added/removed auto links on before_applied,
    preserving all user edits in draft."""
    if draft == before_applied:
        return copy.deepcopy(after_applied)
    before_auto = auto_keys_in_doc(before_applied)
    after_auto = auto_keys_in_doc(after_applied)
    removed_keys = set(before_auto) - set(after_auto)

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
            if len(cands) >= 8:
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
    with the rounds they have held), lastChange, benefits {pair key: {kind, count, pairs}} of the auto
    links in use, idleSince {pair key: ms} of those that gain nothing (dropped after dropAfterS) and
    measuredAt, when that round ran."""
    st = clean_state(doc.get('_autoLinksState'), now_ms)
    cfg = dict(DEFAULT_SETTINGS, **((doc.get('settings') or {}).get('autoLinks') or {}))
    return {
        'suggestions': st['watching'],
        'lastChange': st.get('lastChange'),
        'benefits': st['benefits'],
        'idleSince': st['idleSince'],
        'measuredAt': st['measuredAt'],
        'holdRounds': int(cfg.get('holdRounds', DEFAULT_SETTINGS['holdRounds'])),
        'dropAfterS': int(cfg.get('dropAfterS', DEFAULT_SETTINGS['dropAfterS'])),
    }

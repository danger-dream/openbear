"""What the mesh works out by itself: the values a draft leaves to it ("auto") and the line
rules users write to steer them.

A relay link, an entry candidate or a member lists in its `auto` the fields that follow the
mesh instead of holding what was entered: resolve fills them from what each end listens on, can
be reached at and the line rules, every time a draft is saved and a config is built, so they
change with the machines. Fields not listed keep their value. A field leaves auto only when a
draft leaves it out of the list: a value sent for a field still listed is worked out again, so a
stale copy of the draft never pins what the mesh had set. For listen, auto decides which
transports are open; the port numbers stay as entered. explain gives every such field's
value and the reason, listed or not, for the page to show beside what was entered.

Built in, nothing but reachability and listen ports decide; a line rule is the user's own: which
links or entries (lines, entries or both), between which two sides (any machine, regions, tags or
machines), and what it sets: the protocol order, a bias in milliseconds, the side that dials. The
first enabled rule covering a pair and setting a field decides it."""
from __future__ import annotations

import copy
import itertools
import re

TRANSPORTS = ('udp', 'tcp', 'tls')
PAIR_AUTO = ('transport', 'dialer', 'bias')
CANDIDATE_AUTO = ('transport',)
MEMBER_AUTO = ('listen', 'relayMode')
RULE_APPLIES = ('lines', 'entries', 'both')
SELECTOR_KINDS = ('any', 'region', 'tag', 'machines')
MAX_LINE_RULES = 50
BIAS_SPAN = 10000
AUTO_LINKS = 3  # relays each relay links to in auto mode
_ID = re.compile(r'[A-Za-z0-9_-]{1,32}')
_REGION = re.compile(r'[A-Z]{2}')
NAMES = {'udp': 'UDP', 'tcp': 'TCP', 'tls': 'TLS'}


# ----- line rules -----
def clean_auto(raw, allowed, p, errors):
    """An item's auto list: some of allowed, each once, in allowed's order."""
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(x, str) and x in allowed for x in raw):
        errors.add(f'{p}.auto', 'invalid_auto', f'自动的字段只能是 {"、".join(allowed)}')
        return []
    return [x for x in allowed if x in raw]


def _selector(raw, p, errors):
    if not isinstance(raw, dict) or raw.get('kind') not in SELECTOR_KINDS or set(raw) - {'kind', 'regions', 'not', 'tags', 'ids'}:
        errors.add(p, 'invalid_line_rule_side', '范围只能是任意节点、地区、标签或节点')
        return {'kind': 'any'}
    kind = raw['kind']
    if kind == 'any':
        return {'kind': 'any'}
    if kind == 'region':
        regions = raw.get('regions', [])
        if not isinstance(regions, list) or len(regions) > 60 or not all(isinstance(r, str) and _REGION.fullmatch(r.upper()) for r in regions):
            errors.add(f'{p}.regions', 'invalid_line_rule_side', '地区须是两个字母的国家或地区代码，例如 CN、HK')
            regions = []
        if type(raw.get('not', False)) is not bool:
            errors.add(f'{p}.not', 'invalid_line_rule_side', '格式不正确')
        return {'kind': 'region', 'regions': list(dict.fromkeys(r.upper() for r in regions)), 'not': raw.get('not') is True}
    key = 'tags' if kind == 'tag' else 'ids'
    values = raw.get(key, [])
    if not isinstance(values, list) or len(values) > 100 or not all(isinstance(v, str) and 0 < len(v.strip()) <= 128 for v in values):
        errors.add(f'{p}.{key}', 'invalid_line_rule_side', '格式不正确')
        values = []
    return {'kind': kind, key: list(dict.fromkeys(v.strip() for v in values))}


def clean_line_rules(raw, errors):
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_LINE_RULES:
        errors.add('lineRules', 'invalid_line_rules', f'线路规则最多 {MAX_LINE_RULES} 条')
        return []
    out, used = [], set()
    for i, r in enumerate(raw):
        q = f'lineRules[{i}]'
        if not isinstance(r, dict) or set(r) - {'id', 'name', 'on', 'applies', 'a', 'b', 'order', 'bias', 'dialer'}:
            errors.add(q, 'invalid_line_rule', '线路规则格式不正确')
            continue
        rule = {'id': r.get('id', ''), 'name': r.get('name', ''), 'on': r.get('on', True), 'applies': r.get('applies', 'both'),
                'a': _selector(r.get('a', {'kind': 'any'}), f'{q}.a', errors), 'b': _selector(r.get('b', {'kind': 'any'}), f'{q}.b', errors),
                'order': None, 'bias': None, 'dialer': None}
        if not isinstance(rule['id'], str) or rule['id'] and not _ID.fullmatch(rule['id']) or rule['id'] and rule['id'] in used:
            errors.add(f'{q}.id', 'invalid_line_rule', '规则 id 格式不正确或重复')
        if not isinstance(rule['name'], str) or not 1 <= len(rule['name'].strip()) <= 60:
            errors.add(f'{q}.name', 'invalid_line_rule_name', '规则名称 1～60 字')
        else:
            rule['name'] = rule['name'].strip()
        if type(rule['on']) is not bool:
            errors.add(f'{q}.on', 'invalid_line_rule', '启用须是开或关')
        if rule['applies'] not in RULE_APPLIES:
            errors.add(f'{q}.applies', 'invalid_line_rule', '用于只能是 lines、entries 或 both')
        order = r.get('order')
        if order is not None:
            if not isinstance(order, list) or not 1 <= len(order) <= 3 or len(set(order)) != len(order) or not all(t in TRANSPORTS for t in order):
                errors.add(f'{q}.order', 'invalid_line_rule_order', '协议顺序须是 udp、tcp、tls 中的一个或几个，不重复')
            else:
                rule['order'] = list(order)
        bias = r.get('bias')
        if bias is not None:
            if type(bias) is not int or not -BIAS_SPAN <= bias <= BIAS_SPAN:
                errors.add(f'{q}.bias', 'invalid_bias', f'偏好须是 -{BIAS_SPAN}～{BIAS_SPAN} 的整数（毫秒）')
            else:
                rule['bias'] = bias
        dialer = r.get('dialer')
        if dialer is not None:
            if dialer not in ('a', 'b'):
                errors.add(f'{q}.dialer', 'invalid_line_rule', '主动方只能是 a（一端）或 b（另一端）')
            else:
                rule['dialer'] = dialer
        if rule['id']:
            used.add(rule['id'])
        out.append(rule)
    n = 1
    for rule in out:
        if not rule['id']:
            while f'l{n}' in used:
                n += 1
            rule['id'] = f'l{n}'
            used.add(rule['id'])
    return out


def matches(sel, m, facts):
    kind = sel.get('kind', 'any')
    if kind == 'any':
        return True
    if kind == 'region':
        region = (facts.get(m['serverId']) or {}).get('region', '')
        inside = bool(region) and region in sel.get('regions', [])
        # Not these regions: a machine whose region is unknown is not known to be outside them.
        return (bool(region) and not inside) if sel.get('not') else inside
    if kind == 'tag':
        return bool(set(m.get('tags') or []) & set(sel.get('tags', [])))
    return m['serverId'] in sel.get('ids', [])


def rule_for(rules, x, y, kind, field, facts):
    """(rule, a, b): the first enabled rule for kind ('lines' or 'entries') covering members x and
    y and setting field, with a and b the members on its two sides; None without one."""
    for rule in rules or []:
        if not rule.get('on', True) or rule['applies'] not in (kind, 'both') or rule.get(field) is None:
            continue
        if matches(rule['a'], x, facts) and matches(rule['b'], y, facts):
            return rule, x, y
        if matches(rule['a'], y, facts) and matches(rule['b'], x, facts):
            return rule, y, x
    return None


# ----- facts -----
def _site(m):
    return m['role'] == 'relay' and m.get('relayMode') == 'site'


def _front(m, facts):
    return bool((facts.get(m['serverId']) or {}).get('front'))


def _exits_of(m, facts):
    return {e['serverId'] for e in (facts.get(m['serverId']) or {}).get('front') or []}


def reachable(m, facts):
    """Whether others can dial the member: not behind NAT as a site relay, announced, and either
    given an address with a port mapping or found reachable from outside by its own check."""
    if _site(m) or m.get('announce') is False:
        return False
    return bool(m.get('publicEndpoint')) or (facts.get(m['serverId']) or {}).get('verdict') == 'public'


def _name(m, facts):
    return (facts.get(m['serverId']) or {}).get('name') or m['serverId']


# ----- defaults, each with its reason -----
def relay_mode_default(m, facts):
    verdict = (facts.get(m['serverId']) or {}).get('verdict', '')
    if _front(m, facts):
        return 'public', '前置机'
    if m.get('publicEndpoint'):
        return 'public', '填了对外地址'
    if verdict == 'public':
        return 'public', '可达性检测：公网可达'
    if verdict in ('punchable', 'symmetric', 'no_udp'):
        return 'site', '可达性检测：在 NAT 后面'
    return m.get('relayMode') or 'public', '还没有可达性检测结果，保持现在的'


def dialer_default(a, b, facts, rules):
    """(side, reason): 'a' or 'b' for the end that dials, None when either may."""
    if _site(a) != _site(b):
        return ('a' if _site(a) else 'b'), '站点中转在 NAT 后，只能由它主动连'
    if b['serverId'] in _exits_of(a, facts):
        return 'a', '前置机主动连它的出口'
    if a['serverId'] in _exits_of(b, facts):
        return 'b', '前置机主动连它的出口'
    hit = rule_for(rules, a, b, 'lines', 'dialer', facts)
    if hit:
        rule, x, y = hit
        dialer = x if rule['dialer'] == 'a' else y
        other = y if dialer is x else x
        return ('a' if dialer is a else 'b'), f'按规则「{rule["name"]}」：{_name(other, facts)} 不接受连入'
    ra, rb = reachable(a, facts), reachable(b, facts)
    if ra != rb:
        return ('b' if ra else 'a'), f'只有 {_name(a if ra else b, facts)} 能被直接连到（可达性检测）'
    return None, '两端都能被直接连到，谁主动都可以' if ra else '两端都还不确定能不能被直接连到'


def _order_for(x, y, kind, facts, rules):
    hit = rule_for(rules, x, y, kind, 'order', facts)
    return (hit[0]['order'], hit[0]) if hit else (list(TRANSPORTS), None)


def transport_default(dialer, passive, facts, rules, kind='lines'):
    """(transport, reason) for dialer dialing passive."""
    if _site(passive):
        return 'udp', f'{_name(passive, facts)} 是站点中转，在 NAT 后只能 UDP 打洞'
    if _front(dialer, facts) or _front(passive, facts):
        for t in ('tls', 'tcp', 'udp'):
            if passive['listen'].get(t):
                return t, '前置机的线路先用 TLS'
    order, rule = _order_for(dialer, passive, kind, facts, rules)
    open_ = [t for t in TRANSPORTS if passive['listen'].get(t)]
    for t in order:
        if t in open_:
            skipped = [NAMES[x] for x in order[:order.index(t)]]
            if rule:
                return t, f'按规则「{rule["name"]}」' + (f'（{_name(passive, facts)} 没开 {"、".join(skipped)}）' if skipped else '')
            return t, f'{_name(passive, facts)} 开着 UDP，UDP 延迟最低' if t == 'udp' else f'{_name(passive, facts)} 没开 {"、".join(skipped)}，用 {NAMES[t]}'
    return (order[0] if order else 'udp'), f'{_name(passive, facts)} 没有监听可用的协议'


def bias_default(a, b, facts, rules):
    hit = rule_for(rules, a, b, 'lines', 'bias', facts)
    return (hit[0]['bias'], f'按规则「{hit[0]["name"]}」') if hit else (0, '没有规则改它')


def candidate_default(member, relay, facts, rules):
    return transport_default(member, relay, facts, rules, kind='entries')


def listen_default(m, doc, live, facts, rules):
    """({udp, tcp, tls} ports, reason): what those that dial the member need. Members and site
    relays only UDP (hole punching); fronts only TLS; public relays UDP and TCP for newcomers and
    whatever else a link or entry dialing them asks for first. Open ports keep their number."""
    ports = doc['settings']['ports']
    keep = {t: m['listen'].get(t) or ports[t] for t in TRANSPORTS}

    def of(protos):
        return {t: keep[t] if t in protos else 0 for t in TRANSPORTS}
    if _front(m, facts):
        return of({'tls'}), '前置机只开 TLS，只主动连它的出口'
    if m['role'] != 'relay':
        return of({'udp'}), '成员只开 UDP：别人一般不主动连成员，UDP 用来打洞直连'
    if _site(m):
        return of({'udp'}), '站点中转在 NAT 后，只能靠 UDP 打洞'
    need = {}
    for pair in doc['relayLinks']['pairs']:
        if m['serverId'] not in (pair['a'], pair['b']) or pair['a'] == pair['b']:
            continue
        other = live.get(pair['b'] if pair['a'] == m['serverId'] else pair['a'])
        if not other:
            continue
        dialer = pair['a'] if pair['dialer'] == 'a' else pair['b']
        if _site(other):
            dialer = other['serverId']
        if dialer == m['serverId']:
            continue
        first = _order_for(other, m, 'lines', facts, rules)[0][0] if 'transport' in (pair.get('auto') or []) else pair['transport']
        need[first] = need.get(first, 0) + 1
    if rules and doc.get('_fullLineRules', True) and doc['relayLinks']['mode'] == 'full':
        # The links no one listed dial by the rules' order too (full_pair); a front's take what its exit has.
        listed = {frozenset((p['a'], p['b'])) for p in doc['relayLinks']['pairs']}
        for other in live.values():
            if other is m or other['role'] != 'relay' or _front(other, facts) or frozenset((m['serverId'], other['serverId'])) in listed:
                continue
            a, b = sorted((other, m), key=lambda x: x['serverId'])
            side = dialer_default(a, b, facts, rules)[0]
            if side and (a if side == 'a' else b) is m:
                continue
            first = _order_for(other, m, 'lines', facts, rules)[0][0]
            need[first] = need.get(first, 0) + 1
    for x in live.values():
        for c in (x.get('entry') or {}).get('candidates', []):
            if x['role'] == 'member' and c['relay'] == m['serverId']:
                first = _order_for(x, m, 'entries', facts, rules)[0][0] if 'transport' in (c.get('auto') or []) else c['transport']
                need[first] = need.get(first, 0) + 1
    extra = [t for t in TRANSPORTS if t not in ('udp', 'tcp') and need.get(t)]
    reason = '公网中转常开 UDP 和 TCP，新节点接入时用'
    if extra:
        reason += '；' + '、'.join(f'{need[t]} 处要用 {NAMES[t]}' for t in extra)
    return of({'udp', 'tcp', *extra}), reason


def _full(pair, a, b, facts, rules):
    """The same field defaults as listed auto links, keeping the legacy full default where no rule acts."""
    out, why = dict(pair), {}
    if rule_for(rules, a, b, 'lines', 'bias', facts):
        out['bias'], why['bias'] = bias_default(a, b, facts, rules)
    if _front(a, facts) or _front(b, facts) or _site(a) and _site(b):
        return out, why
    if not any(rule_for(rules, a, b, 'lines', f, facts) for f in ('order', 'dialer')):
        return out, why
    side, why['dialer'] = dialer_default(a, b, facts, rules)
    out['dialer'] = side or pair['dialer']
    dialer, passive = (a, b) if out['dialer'] == 'a' else (b, a)
    out['transport'], why['transport'] = transport_default(dialer, passive, facts, rules)
    return out, why


def _explain_full(doc, live, facts, rules):
    """explain's pairs for full mode's links no one listed, as mesh_config builds them."""
    from app.servers import mesh_config  # it imports this module
    rules = rules if doc.get('_fullLineRules', True) else []
    listed = {frozenset((p['a'], p['b'])) for p in doc['relayLinks']['pairs']}
    out = {}
    for p in mesh_config.relay_pairs(doc, facts):
        if frozenset((p['a'], p['b'])) in listed:
            continue
        a, b = live[p['a']], live[p['b']]
        why = _full(mesh_config.plain_pair(a, b, facts), a, b, facts, rules)[1]

        def said(field, plain):
            return why.get(field, plain)
        if _site(a) and _site(b):
            transport, dialer = '两台站点中转之间只能 UDP 打洞', '两台站点中转互相打洞'
        elif _front(a, facts) or _front(b, facts):
            transport, dialer = '前置机的线路先用 TLS', '前置机主动连它的出口，站点中转主动连前置机'
        else:
            transport = said('transport', '全部互连：用对方开着、能连上的第一个协议')
            dialer = '站点中转在 NAT 后，只能由它主动连' if _site(a) != _site(b) else said('dialer', '全部互连：能连上对方的一端主动连')
        ruled = [r['id'] for r in rules if any(rule_for([r], a, b, 'lines', f, facts) for f in ('order', 'bias', 'dialer'))]
        out[f'{p["a"]}|{p["b"]}'] = {'transport': {'value': p['transport'], 'reason': transport},
                                     'dialer': {'value': None if p.get('punch') else p['dialer'], 'reason': dialer},
                                     'bias': {'value': p['bias'], 'reason': said('bias', '没有规则改它')}, 'rules': ruled, 'implicit': True}
    return out


def full_pair(pair, a, b, facts, rules):
    """Rules on an implicit link; without a matching rule keep the old full-mode defaults exactly.
    Unusable rules fail the same draft checks as listed auto links, rather than silently being ignored."""
    return _full(pair, a, b, facts, rules)[0] if rules else pair


# ----- resolve and explain -----
def for_draft(doc):
    """Preview/edit opts into full-mode rules. A member's fixed or failover entry stays as it is
    until the user moves it to upstreams."""
    if doc.get('_fullLineRules', True) or doc['relayLinks']['mode'] != 'full' or not doc.get('lineRules'):
        return doc
    return {**doc, '_fullLineRules': True}


def _live(doc):
    return {m['serverId']: m for m in doc['members'] if not m['removing']}


def resolve(doc, facts, rtts=None, links=False):
    """doc with every auto field worked out (a copy). links: auto mode's links are chosen again
    (a draft being saved), with rtts {frozenset((a, b)): ms} measured between relays; otherwise
    (a config being built) the links it chose stay as they are."""
    doc = copy.deepcopy(doc)
    rules = doc.get('lineRules') or []
    live = _live(doc)
    for m in live.values():
        if 'relayMode' in (m.get('auto') or []) and m['role'] == 'relay':
            m['relayMode'] = relay_mode_default(m, facts)[0]
    if links and doc['relayLinks']['mode'] == 'auto':
        doc['relayLinks']['pairs'] = auto_pairs(doc, facts, rtts)
    pairs = [p for p in doc['relayLinks']['pairs'] if p['a'] in live and p['b'] in live]
    for p in pairs:
        if 'dialer' in (p.get('auto') or []):
            side = dialer_default(live[p['a']], live[p['b']], facts, rules)[0]
            if side:
                p['dialer'] = side
    for m in live.values():
        if 'listen' in (m.get('auto') or []):
            m['listen'] = listen_default(m, doc, live, facts, rules)[0]
    for p in pairs:
        auto = p.get('auto') or []
        if 'transport' in auto:
            dialer, passive = (live[p['a']], live[p['b']]) if p['dialer'] == 'a' else (live[p['b']], live[p['a']])
            if _site(dialer) and _site(passive):
                p['transport'] = 'udp'
            else:
                if _site(dialer) or _site(passive):
                    dialer, passive = (dialer, passive) if _site(dialer) else (passive, dialer)
                p['transport'] = within_limits(transport_default(dialer, passive, facts, rules)[0], dialer, passive, doc, facts, automatic_pair(p, doc))
        if 'bias' in auto:
            p['bias'] = bias_default(live[p['a']], live[p['b']], facts, rules)[0]
    for m in live.values():
        for c in (m.get('entry') or {}).get('candidates', []):
            relay = live.get(c['relay'])
            if relay and 'transport' in (c.get('auto') or []):
                c['transport'] = within_limits(candidate_default(m, relay, facts, rules)[0], m, relay, doc, facts, c.get('origin') == 'auto')
    return doc


def automatic_pair(pair, doc):
    """Whether a relay link is the mesh's own to the connection limits, as the draft check counts it."""
    return pair.get('origin') == 'auto' or doc['relayLinks']['mode'] == 'auto' and set(pair.get('auto') or []) >= set(PAIR_AUTO)


def within_limits(transport, dialer, passive, doc, facts, automatic=True):
    """An auto transport kept to the connection limits: the first one they allow that passive
    listens on when transport is not allowed; transport itself when none is, for the draft check
    to name the limit. automatic: the connection itself is the mesh's (manualOnly limits refuse it)."""
    from app.servers import mesh_limits  # it imports this module
    limits = doc.get('limits') or []
    if not limits:
        return transport
    allowed = mesh_limits.transports_for(limits, dialer, passive, facts, automatic)
    if transport in allowed:
        return transport
    return next((t for t in allowed if passive['listen'].get(t)), transport)


def auto_pairs(doc, facts, rtts=None):
    """Auto mode's links: each relay to the AUTO_LINKS relays nearest by measured round trip (in
    the same region first while there is no measurement, then by id), with any missing link added
    so every relay reaches every other; a listed pair the user changed keeps what it holds. New
    pairs leave every field to the mesh."""
    live = _live(doc)
    relays = sorted(sid for sid, m in live.items() if m['role'] == 'relay')
    # A link of a relay that has just become a member goes with its role, whatever was set on it.
    listed = {frozenset((p['a'], p['b'])): p for p in doc['relayLinks']['pairs'] if p['a'] in relays and p['b'] in relays and p['a'] != p['b']}
    kept = {k: p for k, p in listed.items() if set(PAIR_AUTO) - set(p.get('auto') or [])}
    rtts = rtts or {}

    def region(sid):
        return (facts.get(sid) or {}).get('region', '')

    def distance(a, b):
        ms = rtts.get(frozenset((a, b)))
        if ms is not None:
            return 0, ms, a + b
        return 1, 0 if region(a) and region(a) == region(b) else 1, min(a, b) + max(a, b)
    from app.servers import mesh_limits  # it imports this module
    limits = doc.get('limits') or []

    def permitted(a, b):
        return not limits or bool(mesh_limits.transports_for(limits, live[a], live[b], facts))
    chosen = set(kept)
    for a in relays:
        if _front(live[a], facts):
            continue
        near = sorted((b for b in relays if b != a and not _front(live[b], facts) and not (_site(live[a]) and _site(live[b])) and permitted(a, b)),
                      key=lambda b: distance(a, b))
        for b in near[:AUTO_LINKS]:
            chosen.add(frozenset((a, b)))
    # Fronts link to their exits only.
    for a in relays:
        for b in _exits_of(live[a], facts) & set(relays):
            chosen.add(frozenset((a, b)))
    # Joined up: the nearest pair between the parts until there is one.
    parts = {r: {r} for r in relays}
    for k in chosen:
        x, y = tuple(k)
        if parts[x] is not parts[y]:
            merged = parts[x] | parts[y]
            for r in merged:
                parts[r] = merged
    candidates = sorted((distance(a, b), a, b) for a, b in itertools.combinations(relays, 2)
                        if not (_site(live[a]) and _site(live[b])) and not _front(live[a], facts) and not _front(live[b], facts) and permitted(a, b))
    for _, a, b in candidates:
        if parts[a] is not parts[b]:
            chosen.add(frozenset((a, b)))
            merged = parts[a] | parts[b]
            for r in merged:
                parts[r] = merged
    out = []
    for k in sorted(chosen, key=lambda k: tuple(sorted(k))):
        if k in listed:
            out.append(listed[k])
        else:
            a, b = sorted(k)
            out.append({'a': a, 'b': b, 'transport': 'udp', 'cost': 1, 'dialer': 'a', 'bias': 0, 'auto': list(PAIR_AUTO)})
    return out


def explain(doc, facts):
    """What the mesh would set for every field it can work out, and why: relay links by 'a|b', entry
    candidates by 'member|relay', members' listen ports and relay types by serverId."""
    rules = doc.get('lineRules') or []
    live = _live(doc)
    out = {'pairs': {}, 'candidates': {}, 'listen': {}, 'relayMode': {}}
    for m in live.values():
        value, reason = listen_default(m, doc, live, facts, rules)
        out['listen'][m['serverId']] = {'value': value, 'reason': reason}
        if m['role'] == 'relay':
            mode, why = relay_mode_default(m, facts)
            out['relayMode'][m['serverId']] = {'value': mode, 'reason': why}
    for p in doc['relayLinks']['pairs']:
        a, b = live.get(p['a']), live.get(p['b'])
        if not a or not b or a is b:
            continue
        side, side_why = dialer_default(a, b, facts, rules)
        dialer_side = side or p['dialer']
        dialer, passive = (a, b) if dialer_side == 'a' else (b, a)
        if _site(dialer) and _site(passive):
            transport, why = 'udp', '两台站点中转之间只能 UDP 打洞'
        else:
            if _site(passive):
                dialer, passive = passive, dialer
            transport, why = _limited(*transport_default(dialer, passive, facts, rules), dialer, passive, doc, facts, automatic_pair(p, doc))
        bias, bias_why = bias_default(a, b, facts, rules)
        ruled = [r['id'] for r in rules if any(rule_for([r], a, b, 'lines', f, facts) for f in ('order', 'bias', 'dialer'))]
        out['pairs'][f'{p["a"]}|{p["b"]}'] = {'transport': {'value': transport, 'reason': why}, 'dialer': {'value': side, 'reason': side_why},
                                               'bias': {'value': bias, 'reason': bias_why}, 'rules': ruled}
    if doc['relayLinks']['mode'] == 'full':
        out['pairs'].update(_explain_full(doc, live, facts, rules))
    for m in live.values():
        for c in (m.get('entry') or {}).get('candidates', []):
            relay = live.get(c['relay'])
            if relay:
                t, why = _limited(*candidate_default(m, relay, facts, rules), m, relay, doc, facts, c.get('origin') == 'auto')
                out['candidates'][f'{m["serverId"]}|{c["relay"]}'] = {'transport': {'value': t, 'reason': why}}
    return out


def _limited(transport, why, dialer, passive, doc, facts, automatic):
    """explain's transport and reason as resolve sets the field: kept to the connection limits."""
    from app.servers import mesh_limits  # it imports this module
    kept = within_limits(transport, dialer, passive, doc, facts, automatic)
    if kept == transport:
        return transport, why
    names = '、'.join(f'「{k["name"]}」' for k in mesh_limits.crossing(doc.get('limits') or [], dialer, passive, facts))
    return kept, f'连接限制{names}不允许 {mesh_limits.NAMES[transport]}，用 {mesh_limits.NAMES[kept]}'
